import json
import logging
import re

import requests
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time
from django.utils.translation import gettext_lazy as _
from rest_framework import generics, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, inline_serializer

from imports.exporters.csv_export import export_items_csv
from imports.exporters.full_backup import export_full_backup
from imports.exporters.json_export import export_items_json
from imports.exporters.google_wallet import generate_google_wallet_save_url, google_wallet_enabled
from imports.exporters.pkpass import generate_pkpass, pkpass_enabled
from imports.full_backup_import import FullBackupImportError, import_full_backup
from imports.models import ImportJob
from imports.parsers import get_parser
from imports.pkpass_import import PkpassImportError, extract_pkpass_fields
from imports.tasks import process_import_job
from myapp.analytics import get_expiry_timeline, get_spend_stats, get_summary_stats
from myapp.merchant_logos import remember_balance_check_url
from myapp.models import (
    Document, Item, ItemShare, MerchantProfile, Tag, Transaction,
    UserPreference, UserProfile, UserWebhook, Wallet,
    WalletActivity, WalletMembership,
)
from dms.models import DMSProvider, DMSSyncLog
from myapp.pdf_ticket import decode_barcode_from_pdf, pdf_page_to_png_bytes
from myapp.scan_learning import apply_learned_corrections
from myapp.tasks import fetch_merchant_logo_task
from myapp.utils import generate_code_image_base64
from notify.models import NotificationLog, NotificationRule
from notify.tasks import (
    backfill_firefly_transactions,
    notify_balance_changed, notify_item_created, notify_item_shared, notify_item_used,
    send_test_notification,
    _find_firefly_rule,
)
from ocr.backends import get_backend, ocr_enabled
from ocr.backends.base import parse_float_or_none

from .filters import ItemFilter
from .permissions import IsItemOwnerOrWalletCollaborator, IsOwner, IsWalletOwnerOrReadOnlyCollaborator
from .serializers import (
    DMSProviderSerializer,
    DMSSyncLogSerializer,
    DocumentSerializer,
    ImportJobSerializer,
    ItemSerializer,
    ItemShareSerializer,
    MerchantProfileSerializer,
    NotificationLogSerializer,
    NotificationRuleSerializer,
    TagSerializer,
    TransactionSerializer,
    UserPreferenceSerializer,
    UserProfileSerializer,
    UserWebhookSerializer,
    WalletActivitySerializer,
    WalletMembershipSerializer,
    WalletSerializer,
)

logger = logging.getLogger(__name__)

PREVIEW_ROW_LIMIT = 50
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10MB
MAX_OCR_IMAGE_SIZE = 8 * 1024 * 1024  # 8MB
OCR_ALLOWED_CONTENT_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
MAX_PDF_UPLOAD_SIZE = 15 * 1024 * 1024  # 15MB

_RAIL_TICKET_TEXT_FIELDS = (
    'name', 'issuer', 'card_number', 'order_id', 'discount_applied',
    'journey_origin', 'journey_destination', 'travel_time', 'travel_date',
    'seat_number',
)

_RAIL_OPERATOR_LOGOS = {
    'avanti west coast': 'avantiwestcoast.co.uk',
    'c2c': 'c2crail.net',
    'caledonian sleeper': 'sleeper.scot',
    'chiltern railways': 'chilternrailways.co.uk',
    'crosscountry': 'crosscountrytrains.co.uk',
    'east midlands railway': 'eastmidlandsrailway.co.uk',
    'gatwick express': 'gatwickexpress.com',
    'grand central': 'grandcentralrail.com',
    'greater anglia': 'greateranglia.co.uk',
    'great western railway': 'gwr.com',
    'gwr': 'gwr.com',
    'heathrow express': 'heathrowexpress.com',
    'hull trains': 'hulltrains.co.uk',
    'lner': 'lner.co.uk',
    'merseyrail': 'merseyrail.org',
    'northern': 'northernrailway.co.uk',
    'scotrail': 'scotrail.co.uk',
    'southeastern': 'southeasternrailway.co.uk',
    'south western railway': 'southwesternrailway.com',
    'southern': 'southernrailway.com',
    'thameslink': 'thameslinkrailway.com',
    'transpennine express': 'tpexpress.co.uk',
    'west midlands trains': 'westmidlandsrailway.co.uk',
}


def _process_rail_ticket_pdf(pdf_bytes, user, preset_fields, create, serializer_context):
    """
    Shared processing logic for a single rail-ticket PDF. Returns (response_dict,
    http_status_code). Used by both the single-file and batch import endpoints.
    """
    try:
        redeem_code, code_type = decode_barcode_from_pdf(pdf_bytes)
    except Exception as exc:
        logger.warning('Rail ticket barcode decode failed: %s', exc, exc_info=True)
        redeem_code, code_type = None, None

    fields = {name: (preset_fields.get(name) or None) for name in _RAIL_TICKET_TEXT_FIELDS}
    fields['value'] = parse_float_or_none(preset_fields.get('value'))
    fields['currency'] = (preset_fields.get('currency') or '').upper() or None

    if ocr_enabled() and any(value is None for value in fields.values()):
        try:
            page_image_bytes = pdf_page_to_png_bytes(pdf_bytes)
            ocr_result = get_backend().extract(page_image_bytes, 'image/png')
        except Exception as exc:
            logger.warning('Rail ticket OCR fallback failed: %s', exc, exc_info=True)
            ocr_result = {}

        fields['name'] = fields['name'] or ocr_result.get('name')
        fields['issuer'] = fields['issuer'] or ocr_result.get('issuer')
        fields['card_number'] = fields['card_number'] or ocr_result.get('card_number')
        fields['journey_origin'] = fields['journey_origin'] or ocr_result.get('journey_origin')
        fields['journey_destination'] = fields['journey_destination'] or ocr_result.get('journey_destination')
        fields['travel_time'] = fields['travel_time'] or ocr_result.get('travel_time')
        fields['travel_date'] = (
            fields['travel_date'] or ocr_result.get('travel_date') or ocr_result.get('expiry_date')
        )
        fields['value'] = fields['value'] if fields['value'] is not None else ocr_result.get('value')
        fields['currency'] = fields['currency'] or ocr_result.get('currency')
        if redeem_code is None:
            redeem_code = ocr_result.get('code')
            code_type = code_type or ocr_result.get('code_type')

    barcode_decoded = bool(redeem_code) and code_type not in (None, 'none')
    if not redeem_code:
        redeem_code = fields['card_number'] or ''
        code_type = 'none'

    response_payload = {
        'created': False,
        'duplicate': False,
        'name': fields['name'],
        'issuer': fields['issuer'],
        'redeem_code': redeem_code or None,
        'code_type': code_type,
        'card_number': fields['card_number'],
        'order_id': fields['order_id'],
        'discount_applied': fields['discount_applied'],
        'journey_origin': fields['journey_origin'],
        'journey_destination': fields['journey_destination'],
        'travel_time': fields['travel_time'],
        'travel_date': fields['travel_date'],
        'value': fields['value'],
        'currency': fields['currency'],
        'barcode_decoded': barcode_decoded,
    }

    if not create:
        return response_payload, status.HTTP_200_OK

    if not fields['issuer'] or not redeem_code:
        return (
            {'detail': _(
                'Not enough information extracted to create an item '
                '(need at least an issuer and a code).'
            )},
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    # Primary dedup: decoded barcode redeem_code. The same physical ticket always
    # produces the same barcode regardless of how the PDF was re-encoded or
    # forwarded, so this is more reliable than order_id (which OCR can misread).
    if barcode_decoded and redeem_code:
        existing = Item.objects.filter(
            user=user, type='travelpass', redeem_code=redeem_code,
        ).first()
        if existing is not None:
            response_payload['duplicate'] = True
            response_payload['item'] = ItemSerializer(existing, context=serializer_context).data
            return response_payload, status.HTTP_409_CONFLICT

    # Fallback dedup: order_id catches tickets whose barcode could not be decoded.
    if fields['order_id']:
        existing = Item.objects.filter(
            user=user, order_id=fields['order_id'], type='travelpass',
        ).first()
        if existing is not None:
            response_payload['duplicate'] = True
            response_payload['item'] = ItemSerializer(existing, context=serializer_context).data
            return response_payload, status.HTTP_409_CONFLICT

    travel_date = parse_date(fields['travel_date']) if fields['travel_date'] else None
    travel_time = parse_time(fields['travel_time']) if fields['travel_time'] else None
    travel_day = travel_date or timezone.localdate()

    name = fields['name'] or ' to '.join(
        part for part in (fields['journey_origin'], fields['journey_destination']) if part
    ) or _('Train Ticket')

    # Build a human-readable description from journey and date fields.
    _desc_parts = []
    if fields['journey_origin'] and fields['journey_destination']:
        _desc_parts.append(f"{fields['journey_origin']} to {fields['journey_destination']}")
    if fields['travel_date']:
        _desc_parts.append(fields['travel_date'])
    description = ' | '.join(_desc_parts) if _desc_parts else None

    # Resolve a logo domain hint from the operator name for the logo fetch task.
    issuer_lower = (fields['issuer'] or '').lower().strip()
    logo_slug = _RAIL_OPERATOR_LOGOS.get(issuer_lower)

    item = Item.objects.create(
        user=user,
        type='travelpass',
        name=name,
        issuer=fields['issuer'],
        redeem_code=redeem_code,
        code_type=code_type or 'none',
        card_number=fields['card_number'] or '',
        order_id=fields['order_id'] or '',
        discount_applied=fields['discount_applied'] or '',
        journey_origin=fields['journey_origin'] or '',
        journey_destination=fields['journey_destination'] or '',
        travel_time=travel_time,
        issue_date=travel_day,
        expiry_date=travel_day,
        value=fields['value'] or 0,
        currency=fields['currency'] or 'GBP',
        source='api',
        description=description,
        logo_slug=logo_slug,
    )

    # Generate the barcode image. Binary ITSO payloads cannot be re-encoded
    # by treepoem, so fall back to code_type='none' if generation fails —
    # the rasterized PDF page saved below provides the visual barcode instead.
    try:
        item.qr_code_base64, item.code_type = generate_code_image_base64(item)
    except Exception:
        item.code_type = 'none'
        item.qr_code_base64 = None
    item.save(update_fields=['qr_code_base64', 'code_type'])

    # Save rasterized page 0 as item image so the barcode is visible in the UI.
    try:
        page_png = pdf_page_to_png_bytes(pdf_bytes)
        item.file.save(f'rail_{item.id}.png', ContentFile(page_png), save=True)
    except Exception as exc:
        logger.warning('Rail ticket page rasterize for file save failed: %s', exc)

    # Attach the original PDF as a Document so it can be downloaded.
    try:
        issuer_safe = re.sub(r'[^a-z0-9]', '_', issuer_lower) or 'ticket'
        Document.objects.create(
            item=item,
            file=ContentFile(pdf_bytes, name=f'{issuer_safe}_ticket.pdf'),
        )
    except Exception as exc:
        logger.warning('Rail ticket PDF document attach failed: %s', exc)

    # Queue logo fetch (best-effort — a broker outage must not block the response).
    if fields['issuer']:
        try:
            fetch_merchant_logo_task.delay(fields['issuer'], logo_slug)
        except Exception:
            logger.warning('Could not queue logo fetch for %r', fields['issuer'], exc_info=True)

    notify_item_created(item)

    response_payload['created'] = True
    response_payload['item'] = ItemSerializer(item, context=serializer_context).data
    return response_payload, status.HTTP_201_CREATED


class ItemViewSet(viewsets.ModelViewSet):
    """
    Full CRUD for the authenticated user's items, plus /redeem/, /transactions/
    and /shares/ sub-resources. Every queryset below is scoped to items
    `user=request.user` owns, plus items in wallets shared with them — no
    item outside either of those is ever reachable through this API, even
    by UUID guessing.
    """
    serializer_class = ItemSerializer
    permission_classes = [IsAuthenticated, IsItemOwnerOrWalletCollaborator]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_class = ItemFilter
    search_fields = ['name', 'redeem_code', 'issuer', 'description', 'notes', 'tags__name']
    ordering_fields = ['expiry_date', 'name', 'value', 'issue_date', 'last_used_at', 'is_pinned']
    ordering = ['expiry_date']

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Item.objects.none()
        return Item.objects.filter(
            Q(user=self.request.user) | Q(wallet__shared_with=self.request.user)
        ).distinct().select_related('wallet').prefetch_related('transactions', 'tags')

    def perform_create(self, serializer):
        item = serializer.save(user=self.request.user, source='api')
        remember_balance_check_url(item.issuer, item.balance_check_url)
        # Auto-assign wallet by issuer text match (same logic as the web form path)
        if not item.wallet_id and item.issuer:
            matched = Wallet.match_for_issuer(self.request.user, item.issuer)
            if matched:
                item.wallet = matched
                item.save(update_fields=['wallet'])
        notify_item_created(item)

    def perform_update(self, serializer):
        item = serializer.save()
        remember_balance_check_url(item.issuer, item.balance_check_url)

    @action(detail=True, methods=['post'])
    def redeem(self, request, pk=None):
        item = self.get_object()
        item.is_used = True
        item.save(update_fields=['is_used'])
        notify_item_used(item)
        return Response(self.get_serializer(item).data)

    @action(detail=True, methods=['post'], url_path='firefly-link')
    def firefly_link(self, request, pk=None):
        """
        Auto-create a Firefly III asset account for this item and store the
        account ID on the item.  Reads url/token from the user's first enabled
        Firefly notification rule so no extra config is needed here.
        """
        item = self.get_object()

        rule = _find_firefly_rule(item)
        if rule is None:
            return Response(
                {'detail': _('No enabled Firefly III notification rule found. Create one first.')},
                status=status.HTTP_400_BAD_REQUEST,
            )

        url = (rule.config.get('url') or '').rstrip('/')
        token = rule.config.get('token', '')
        if not url or not token:
            return Response(
                {'detail': _('Firefly III rule is missing url or token.')},
                status=status.HTTP_400_BAD_REQUEST,
            )

        account_name = f'{item.name} ({item.issuer})' if item.issuer else item.name
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

        # Search for an existing asset account with the same name before creating,
        # so re-clicking the button doesn't silently duplicate the account in Firefly.
        try:
            search_resp = requests.get(
                f'{url}/api/v1/search/accounts',
                params={'query': account_name, 'field': 'name', 'type': 'asset'},
                headers=headers,
                timeout=10,
            )
            search_resp.raise_for_status()
            results = search_resp.json().get('data', [])
            for acct in results:
                if acct.get('attributes', {}).get('name') == account_name:
                    account_id = str(acct['id'])
                    item.firefly_account_id = account_id
                    item.save(update_fields=['firefly_account_id'])
                    try:
                        backfill_firefly_transactions.delay(str(item.pk), rule.id)
                    except Exception:
                        pass
                    return Response({'firefly_account_id': account_id, 'existing': True})
        except requests.RequestException:
            pass  # If the search fails, fall through to create

        payload = {
            'name': account_name,
            'type': 'asset',
            'account_role': 'defaultAsset',
            'currency_code': item.currency,
            'opening_balance': str(item.value or 0),
            'opening_balance_date': str(item.issue_date or timezone.localtime().date()),
            'notes': f'Created by VoucherVault for item {item.pk}',
        }

        try:
            resp = requests.post(
                f'{url}/api/v1/accounts',
                json=payload,
                headers=headers,
                timeout=10,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            return Response(
                {'detail': _('Firefly III request failed: %(error)s') % {'error': str(exc)}},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        try:
            account_id = str(resp.json()['data']['id'])
        except (KeyError, ValueError):
            return Response(
                {'detail': _('Unexpected response from Firefly III.')},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        item.firefly_account_id = account_id
        item.save(update_fields=['firefly_account_id'])
        try:
            backfill_firefly_transactions.delay(str(item.pk), rule.id)
        except Exception:
            pass
        return Response({'firefly_account_id': account_id, 'existing': False})

    @action(detail=True, methods=['get', 'post'], url_path='transactions')
    def transactions(self, request, pk=None):
        item = self.get_object()
        if request.method == 'POST':
            serializer = TransactionSerializer(data=request.data, context={'item': item, 'request': request})
            serializer.is_valid(raise_exception=True)
            transaction = serializer.save(item=item)
            notify_balance_changed(item, transaction)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        serializer = TransactionSerializer(item.transactions.all(), many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get', 'post'], url_path='shares')
    def shares(self, request, pk=None):
        item = self.get_object()
        if request.method == 'POST':
            serializer = ItemShareSerializer(data=request.data, context={'item': item, 'request': request})
            serializer.is_valid(raise_exception=True)
            share = serializer.save()
            notify_item_shared(item, share.shared_with_user.username)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        serializer = ItemShareSerializer(item.shared_with.all(), many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['delete'], url_path=r'shares/(?P<share_id>\d+)')
    def delete_share(self, request, pk=None, share_id=None):
        item = self.get_object()
        share = get_object_or_404(ItemShare, item=item, pk=share_id)
        share.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['get'], url_path='pkpass')
    @extend_schema(responses={200: OpenApiTypes.BINARY})
    def pkpass(self, request, pk=None):
        """Download a signed Apple Wallet .pkpass for this item. 501 if not configured."""
        item = self.get_object()
        if not pkpass_enabled():
            return Response(
                {'detail': _('Apple Wallet export is not configured on this server.')},
                status=status.HTTP_501_NOT_IMPLEMENTED,
            )
        try:
            data = generate_pkpass(item)
        except Exception as exc:
            logger.warning('pkpass generation failed for item %s: %s', item.id, exc, exc_info=True)
            return Response(
                {'detail': _('Apple Wallet export failed: %(error)s') % {'error': str(exc)}},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        response = HttpResponse(data, content_type='application/vnd.apple.pkpass')
        response['Content-Disposition'] = f'attachment; filename="{item.id}.pkpass"'
        return response

    @action(detail=True, methods=['get'], url_path='google-wallet')
    @extend_schema(responses=inline_serializer(name='GoogleWalletSaveUrl', fields={'save_url': serializers.URLField()}))
    def google_wallet(self, request, pk=None):
        """Return a "Save to Google Wallet" link for this item. 501 if not configured."""
        item = self.get_object()
        if not google_wallet_enabled():
            return Response(
                {'detail': _('Google Wallet export is not configured on this server.')},
                status=status.HTTP_501_NOT_IMPLEMENTED,
            )
        try:
            save_url = generate_google_wallet_save_url(item)
        except Exception as exc:
            logger.warning('Google Wallet link generation failed for item %s: %s', item.id, exc, exc_info=True)
            return Response(
                {'detail': _('Google Wallet export failed: %(error)s') % {'error': str(exc)}},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({'save_url': save_url})


class WalletViewSet(viewsets.ModelViewSet):
    """
    Full CRUD for the authenticated user's wallets, plus /items/ and
    /share/ sub-resources. The queryset includes wallets shared with the
    user; IsWalletOwnerOrReadOnlyCollaborator restricts collaborators to
    read-only access on the wallet object itself (they get full read/write
    on the items inside it via ItemViewSet).
    """
    serializer_class = WalletSerializer
    permission_classes = [IsAuthenticated, IsWalletOwnerOrReadOnlyCollaborator]

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Wallet.objects.none()
        return Wallet.objects.filter(
            Q(user=self.request.user) | Q(shared_with=self.request.user)
        ).distinct().annotate(item_count=Count('items')).order_by('name')

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=True, methods=['get'])
    def items(self, request, pk=None):
        wallet = self.get_object()
        items = Item.objects.filter(wallet=wallet).select_related('wallet').prefetch_related('tags').order_by('expiry_date')
        page = self.paginate_queryset(items)
        serializer = ItemSerializer(page if page is not None else items, many=True, context={'request': request})
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @action(detail=True, methods=['get', 'post'], url_path='share')
    def share(self, request, pk=None):
        """Owner-only: list collaborators (GET) or invite one by username (POST)."""
        wallet = self.get_object()
        if wallet.user_id != request.user.id:
            return Response({'detail': _('Only the wallet owner can manage sharing.')}, status=status.HTTP_403_FORBIDDEN)

        if request.method == 'POST':
            username = request.data.get('username', '').strip()
            try:
                collaborator = User.objects.get(username=username)
            except User.DoesNotExist:
                return Response({'detail': _('No user with that username exists.')}, status=status.HTTP_400_BAD_REQUEST)
            if collaborator.id == wallet.user_id:
                return Response({'detail': _('You already own this wallet.')}, status=status.HTTP_400_BAD_REQUEST)
            role = request.data.get('role', WalletMembership.ROLE_EDITOR)
            if role not in (WalletMembership.ROLE_VIEWER, WalletMembership.ROLE_EDITOR):
                role = WalletMembership.ROLE_EDITOR
            wallet.shared_with.add(collaborator)
            WalletMembership.objects.update_or_create(
                wallet=wallet, user=collaborator,
                defaults={'role': role},
            )
            return Response({'username': collaborator.username, 'role': role}, status=status.HTTP_201_CREATED)

        memberships = {m.user_id: m.role for m in WalletMembership.objects.filter(wallet=wallet)}
        return Response([
            {'username': u.username, 'role': memberships.get(u.id, WalletMembership.ROLE_EDITOR)}
            for u in wallet.shared_with.all()
        ])

    @action(detail=True, methods=['delete'], url_path=r'share/(?P<user_id>\d+)')
    def unshare(self, request, pk=None, user_id=None):
        """Owner-only: revoke a collaborator's access."""
        wallet = self.get_object()
        if wallet.user_id != request.user.id:
            return Response({'detail': _('Only the wallet owner can manage sharing.')}, status=status.HTTP_403_FORBIDDEN)
        collaborator = get_object_or_404(User, id=user_id)
        wallet.shared_with.remove(collaborator)
        WalletMembership.objects.filter(wallet=wallet, user=collaborator).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TagViewSet(viewsets.ModelViewSet):
    """Full CRUD for the authenticated user's tags."""
    serializer_class = TagSerializer
    permission_classes = [IsAuthenticated, IsOwner]

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Tag.objects.none()
        return Tag.objects.filter(user=self.request.user).annotate(item_count=Count('items')).order_by('name')

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class TransactionViewSet(viewsets.ModelViewSet):
    """
    Direct access to individual transactions. Creation happens via
    /items/{id}/transactions/ since a transaction always belongs to an item.
    """
    serializer_class = TransactionSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ['get', 'put', 'patch', 'delete', 'head', 'options']

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Transaction.objects.none()
        return Transaction.objects.filter(item__user=self.request.user).order_by('-date')


class NotificationRuleViewSet(viewsets.ModelViewSet):
    """Full CRUD for the authenticated user's notification rules, plus /test/."""
    serializer_class = NotificationRuleSerializer
    permission_classes = [IsAuthenticated, IsOwner]

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return NotificationRule.objects.none()
        return NotificationRule.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=True, methods=['post'])
    def test(self, request, pk=None):
        rule = self.get_object()
        success, detail = send_test_notification(rule)
        status_code = status.HTTP_200_OK if success else status.HTTP_502_BAD_GATEWAY
        return Response({'success': success, 'detail': detail}, status=status_code)


class NotificationLogViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only notification history for the authenticated user."""
    serializer_class = NotificationLogSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return NotificationLog.objects.none()
        return NotificationLog.objects.filter(user=self.request.user).select_related('rule', 'item').order_by('-sent_at')


class UserPreferenceView(generics.RetrieveUpdateAPIView):
    """Singleton settings object for the authenticated user."""
    serializer_class = UserPreferenceSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        preference, _created = UserPreference.objects.get_or_create(user=self.request.user)
        return preference


class UserProfileView(generics.RetrieveUpdateAPIView):
    """Singleton profile object (Apprise notification URLs) for the authenticated user."""
    serializer_class = UserProfileSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        profile, _created = UserProfile.objects.get_or_create(user=self.request.user)
        return profile


def _validated_upload(request):
    """Shared validation for the import upload/preview endpoints. Returns
    (source_type, file) on success, or a Response describing the 400 error."""
    source_type = request.data.get('source_type')
    upload = request.FILES.get('file')

    if source_type not in dict(ImportJob.SOURCE_CHOICES):
        return None, None, Response({'source_type': _('Invalid or missing source_type.')}, status=status.HTTP_400_BAD_REQUEST)
    if not upload:
        return None, None, Response({'file': _('No file uploaded.')}, status=status.HTTP_400_BAD_REQUEST)
    if upload.size > MAX_UPLOAD_SIZE:
        return None, None, Response({'file': _('File is too large (max 10MB).')}, status=status.HTTP_400_BAD_REQUEST)

    return source_type, upload, None


_upload_request_schema = {
    'multipart/form-data': inline_serializer(
        name='ImportUploadRequest',
        fields={
            'source_type': serializers.ChoiceField(choices=ImportJob.SOURCE_CHOICES),
            'file': serializers.FileField(),
        },
    ),
}


class ImportUploadView(APIView):
    """POST a file + source_type to start an async import job."""
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(request=_upload_request_schema, responses=ImportJobSerializer)
    def post(self, request):
        source_type, upload, error_response = _validated_upload(request)
        if error_response:
            return error_response

        job = ImportJob.objects.create(user=request.user, source_type=source_type, file=upload)
        try:
            process_import_job.delay(str(job.id))
        except Exception as exc:
            job.status = 'failed'
            job.errors = [{'row': None, 'message': f'Could not queue the import task: {exc}'}]
            job.save(update_fields=['status', 'errors'])
            return Response(ImportJobSerializer(job).data, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response(ImportJobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class ImportPreviewView(APIView):
    """POST a file + source_type to parse it synchronously without saving anything."""
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        request=_upload_request_schema,
        responses=inline_serializer(
            name='ImportPreviewResponse',
            fields={
                'row_count': serializers.IntegerField(),
                'error_count': serializers.IntegerField(),
                'rows': serializers.ListField(child=serializers.DictField()),
                'errors': serializers.ListField(child=serializers.DictField()),
            },
        ),
    )
    def post(self, request):
        source_type, upload, error_response = _validated_upload(request)
        if error_response:
            return error_response

        parser = get_parser(source_type)
        try:
            rows, errors = parser(upload)
        except Exception as exc:
            return Response({'file': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        preview_rows = rows[:PREVIEW_ROW_LIMIT]
        for row in preview_rows:
            if row.get('value') is not None:
                row['value'] = str(row['value'])
            for key in ('issue_date', 'expiry_date'):
                if row.get(key) is not None:
                    row[key] = row[key].isoformat()

        return Response({
            'row_count': len(rows),
            'error_count': len(errors),
            'rows': preview_rows,
            'errors': errors,
        })


class ImportJobViewSet(viewsets.ReadOnlyModelViewSet):
    """Poll import job status/results. Jobs are created via ImportUploadView."""
    serializer_class = ImportJobSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return ImportJob.objects.none()
        return ImportJob.objects.filter(user=self.request.user)


class MerchantProfileViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only cached merchant logos, shared across all users."""
    queryset = MerchantProfile.objects.all()
    serializer_class = MerchantProfileSerializer
    permission_classes = [IsAuthenticated]


class OCRExtractView(APIView):
    """
    POST a photo of a physical voucher/coupon/loyalty card, get back a
    best-guess redeem code (and, with a vision backend, merchant name/
    issuer/expiry date/PIN/value/currency/card number/logo domain/balance
    check URL/item type/description/notes/suggested tags) to pre-fill the
    item form with. Processes the image synchronously — no ImportJob-style
    polling needed for a single image. Disabled (501) unless OCR_BACKEND
    is set.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        request={'multipart/form-data': inline_serializer(name='OCRExtractRequest', fields={'image': serializers.ImageField()})},
        responses=inline_serializer(
            name='OCRExtractResponse',
            fields={
                'code': serializers.CharField(allow_null=True),
                'code_type': serializers.CharField(allow_null=True),
                'name': serializers.CharField(allow_null=True),
                'issuer': serializers.CharField(allow_null=True),
                'expiry_date': serializers.CharField(allow_null=True),
                'pin': serializers.CharField(allow_null=True),
                'value': serializers.FloatField(allow_null=True),
                'currency': serializers.CharField(allow_null=True),
                'card_number': serializers.CharField(allow_null=True),
                'logo_slug': serializers.CharField(allow_null=True),
                'balance_check_url': serializers.CharField(allow_null=True),
                'type': serializers.CharField(allow_null=True),
                'description': serializers.CharField(allow_null=True),
                'notes': serializers.CharField(allow_null=True),
                'tags': serializers.ListField(child=serializers.CharField()),
                'journey_origin': serializers.CharField(allow_null=True),
                'journey_destination': serializers.CharField(allow_null=True),
                'travel_time': serializers.CharField(allow_null=True),
                'healed_fields': serializers.ListField(child=serializers.CharField()),
                'confidence': serializers.FloatField(),
            },
        ),
    )
    def post(self, request):
        if not ocr_enabled():
            return Response(
                {'detail': _('OCR scanning is disabled on this server.')},
                status=status.HTTP_501_NOT_IMPLEMENTED,
            )

        upload = request.FILES.get('image')
        if not upload:
            return Response({'image': _('No image uploaded.')}, status=status.HTTP_400_BAD_REQUEST)
        if upload.size > MAX_OCR_IMAGE_SIZE:
            return Response({'image': _('Image is too large (max 8MB).')}, status=status.HTTP_400_BAD_REQUEST)
        if upload.content_type not in OCR_ALLOWED_CONTENT_TYPES:
            return Response({'image': _('Unsupported image type. Use JPEG, PNG, or WebP.')}, status=status.HTTP_400_BAD_REQUEST)

        try:
            backend = get_backend()
            result = backend.extract(upload.read(), upload.content_type)
        except Exception as exc:
            logger.warning('OCR extraction failed: %s', exc, exc_info=True)
            return Response(
                {'detail': _('OCR scanning is temporarily unavailable: %(error)s') % {'error': str(exc)}},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # Self-healing pass: replay this user's past corrections against
        # the fresh extraction (see myapp/scan_learning.py) - e.g. an
        # operator name the model keeps misreading gets silently fixed
        # before the form ever sees it. healed_fields lets the UI say so.
        result['healed_fields'] = apply_learned_corrections(request.user, result)

        return Response(result)


class RailTicketImportView(APIView):
    """
    POST a PDF eTicket (e.g. an Uber/Omio-issued UK rail booking
    confirmation), get back extracted ticket fields with the barcode
    decoded server-side - or, with create=true, have the Item created
    directly. Two callers share this one endpoint:

    - The create-item "Scan from File" flow (create omitted/false): a
      human uploads a PDF, reviews the pre-filled form, and submits it
      themselves - the same "extract now, create later" shape as
      OCRExtractView/PkpassImportView above.
    - An unattended pipeline, e.g. n8n polling an inbox for ticket
      confirmation emails (see docs/N8N_SETUP.md): forwards the PDF with
      create=true and no human review step. n8n can also POST its own
      pre-extracted text fields (it's already parsing the email to know to
      call this endpoint at all) - anything it supplies is used as-is
      instead of re-derived via OCR, since text pulled directly from the
      PDF's own text layer is more reliable than a vision model reading a
      rasterized image of that same text.

    The barcode is always decoded here, server-side, via zxing-cpp
    (myapp.pdf_ticket) - asking a vision OCR model to *read* a barcode the
    way it reads other printed fields would be far less reliable than an
    actual barcode decoder, and it's the one piece of this pipeline no
    caller can reasonably do for us.

    Duplicate detection uses the decoded barcode redeem_code as the primary
    key (most reliable - same physical ticket always produces the same
    barcode), falling back to order_id for tickets whose barcode could not
    be decoded. A duplicate returns HTTP 409 with duplicate=true and the
    existing item; the n8n workflow treats 409 the same as 201.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        request={'multipart/form-data': inline_serializer(
            name='RailTicketImportRequest',
            fields={
                'file': serializers.FileField(),
                'create': serializers.BooleanField(required=False, default=False),
                'name': serializers.CharField(required=False),
                'issuer': serializers.CharField(required=False),
                'card_number': serializers.CharField(required=False),
                'order_id': serializers.CharField(required=False),
                'discount_applied': serializers.CharField(required=False),
                'journey_origin': serializers.CharField(required=False),
                'journey_destination': serializers.CharField(required=False),
                'travel_time': serializers.CharField(required=False, help_text='24-hour "HH:MM"'),
                'travel_date': serializers.CharField(required=False, help_text='"YYYY-MM-DD"'),
                'value': serializers.FloatField(required=False),
                'currency': serializers.CharField(required=False),
            },
        )},
        responses=inline_serializer(
            name='RailTicketImportResponse',
            fields={
                'created': serializers.BooleanField(),
                'duplicate': serializers.BooleanField(),
                'item': ItemSerializer(required=False),
                'name': serializers.CharField(allow_null=True),
                'issuer': serializers.CharField(allow_null=True),
                'redeem_code': serializers.CharField(allow_null=True),
                'code_type': serializers.CharField(allow_null=True),
                'card_number': serializers.CharField(allow_null=True),
                'order_id': serializers.CharField(allow_null=True),
                'discount_applied': serializers.CharField(allow_null=True),
                'journey_origin': serializers.CharField(allow_null=True),
                'journey_destination': serializers.CharField(allow_null=True),
                'travel_time': serializers.CharField(allow_null=True),
                'travel_date': serializers.CharField(allow_null=True),
                'value': serializers.FloatField(allow_null=True),
                'currency': serializers.CharField(allow_null=True),
                'barcode_decoded': serializers.BooleanField(),
            },
        ),
    )
    def post(self, request):
        upload = request.FILES.get('file')
        if not upload:
            return Response({'file': _('No file uploaded.')}, status=status.HTTP_400_BAD_REQUEST)
        if upload.size > MAX_PDF_UPLOAD_SIZE:
            return Response({'file': _('File is too large (max 15MB).')}, status=status.HTTP_400_BAD_REQUEST)
        is_pdf_content_type = upload.content_type in ('application/pdf', 'application/octet-stream')
        is_pdf_extension = upload.name.lower().endswith('.pdf')
        if not (is_pdf_content_type or is_pdf_extension):
            return Response({'file': _('Only PDF files are supported.')}, status=status.HTTP_400_BAD_REQUEST)

        pdf_bytes = upload.read()
        create = str(request.data.get('create', '')).lower() in ('1', 'true', 'yes')
        preset_fields = {name: (request.data.get(name) or None) for name in _RAIL_TICKET_TEXT_FIELDS}
        preset_fields['value'] = request.data.get('value')
        preset_fields['currency'] = request.data.get('currency')

        data, http_status = _process_rail_ticket_pdf(
            pdf_bytes, request.user, preset_fields, create, {'request': request}
        )
        return Response(data, status=http_status)


class RailTicketBatchImportView(APIView):
    """
    POST up to 10 PDF eTickets in a single multipart request and get back
    per-file results. Useful when an email contains multiple legs as separate
    PDFs, or when an n8n pipeline processes several queued emails at once.

    Files are supplied as repeated `files` fields (standard HTML multi-file
    upload). The `create=true` flag works identically to the single-file
    endpoint - each ticket is created if it does not already exist.

    Returns HTTP 207 Multi-Status with a `results` list (one entry per file).
    Each entry carries the same fields as a single-file response plus:
    - `file`: original filename
    - `status_code`: per-file HTTP status so callers can distinguish outcomes
      without inspecting every detail field.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    MAX_FILES = 10

    def post(self, request):
        files = request.FILES.getlist('files') or list(request.FILES.values())
        if not files:
            return Response({'files': _('No files uploaded.')}, status=status.HTTP_400_BAD_REQUEST)
        if len(files) > self.MAX_FILES:
            return Response(
                {'files': _(f'Too many files (max {self.MAX_FILES} per request).')},
                status=status.HTTP_400_BAD_REQUEST,
            )

        create = str(request.data.get('create', '')).lower() in ('1', 'true', 'yes')
        preset_fields = {name: (request.data.get(name) or None) for name in _RAIL_TICKET_TEXT_FIELDS}
        preset_fields['value'] = request.data.get('value')
        preset_fields['currency'] = request.data.get('currency')
        serializer_context = {'request': request}

        results = []
        for upload in files:
            entry = {'file': upload.name}

            if upload.size > MAX_PDF_UPLOAD_SIZE:
                entry['error'] = _('File is too large (max 15MB).')
                entry['status_code'] = status.HTTP_400_BAD_REQUEST
                results.append(entry)
                continue

            is_pdf = (
                upload.content_type in ('application/pdf', 'application/octet-stream')
                or upload.name.lower().endswith('.pdf')
            )
            if not is_pdf:
                entry['error'] = _('Only PDF files are supported.')
                entry['status_code'] = status.HTTP_400_BAD_REQUEST
                results.append(entry)
                continue

            pdf_bytes = upload.read()
            data, item_status = _process_rail_ticket_pdf(
                pdf_bytes, request.user, preset_fields, create, serializer_context
            )
            entry.update(data)
            entry['status_code'] = item_status
            results.append(entry)

        return Response({'results': results}, status=status.HTTP_207_MULTI_STATUS)


class PkpassImportView(APIView):
    """
    POST an existing Apple Wallet .pkpass file, get back the fields it
    contains (name, issuer, redeem code, code type, expiry, pin) to
    pre-fill the item form with. Informational extraction only — the
    pass's PKCS7 signature is never verified, since nothing here relies on
    it for a trust/authorization decision.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        request={'multipart/form-data': inline_serializer(name='PkpassImportRequest', fields={'file': serializers.FileField()})},
        responses=inline_serializer(
            name='PkpassImportResponse',
            fields={
                'name': serializers.CharField(allow_null=True),
                'issuer': serializers.CharField(allow_null=True),
                'redeem_code': serializers.CharField(allow_null=True),
                'code_type': serializers.CharField(allow_null=True),
                'expiry_date': serializers.CharField(allow_null=True),
                'pin': serializers.CharField(allow_null=True),
                'confidence': serializers.FloatField(),
            },
        ),
    )
    def post(self, request):
        upload = request.FILES.get('file')
        if not upload:
            return Response({'file': _('No file uploaded.')}, status=status.HTTP_400_BAD_REQUEST)
        if upload.size > MAX_UPLOAD_SIZE:
            return Response({'file': _('File is too large.')}, status=status.HTTP_400_BAD_REQUEST)

        try:
            result = extract_pkpass_fields(upload.read())
        except PkpassImportError as exc:
            return Response({'file': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(result)


def _parse_gift_card_email(subject, from_address, body_text, body_html):
    """
    Extract gift card fields from a raw email. Returns a dict with keys:
        name, issuer, redeem_code, value, currency, expiry_date, value_type
    Raises ValueError if neither a redeem_code nor a value can be found.
    """
    import datetime
    from html.parser import HTMLParser

    body = body_text or ''
    if body_html and not body_text:
        class _Strip(HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts = []
            def handle_data(self, d):
                self.parts.append(d)
        p = _Strip()
        p.feed(body_html)
        body = ' '.join(p.parts)

    # issuer from sender domain
    issuer = None
    m = re.search(r'@([\w.-]+)', from_address or '')
    if m:
        domain = m.group(1)
        parts = [p for p in domain.split('.') if p not in {'mail', 'noreply', 'no-reply', 'em', 'uk', 'us', 'co', 'com'}]
        if parts:
            issuer = parts[0].replace('-', ' ').title()

    # redeem code — try labelled patterns first, then structural heuristics
    redeem_code = None
    _CODE_PATTERNS = [
        r'(?i)(?:gift\s*card\s*(?:code|number)|card\s*code|voucher\s*code|promo\s*code|access\s*code|redeem\s*code|code)[:\s]+([A-Z0-9]{8,20})',
        r'(?i)pin[:\s]+([0-9]{4,8})',
        r'\b([A-Z]{2,4}[0-9]{4,16})\b',
        r'\b([A-Z0-9]{4}[-\s][A-Z0-9]{4}[-\s][A-Z0-9]{4}[-\s][A-Z0-9]{4})\b',
        r'\b([A-Z0-9]{16,20})\b',
    ]
    for pat in _CODE_PATTERNS:
        cm = re.search(pat, body)
        if cm:
            redeem_code = re.sub(r'[\s-]', '', cm.group(1))
            break

    # value + currency
    value = None
    currency = 'GBP'
    _CURRENCY_MAP = {'£': 'GBP', '€': 'EUR', '$': 'USD'}
    am = re.search(r'(?:£|€|\$|GBP|EUR|USD)\s*([\d,]+\.?\d*)|([\d,]+\.?\d*)\s*(?:£|€|\$|GBP|EUR|USD)', body)
    if am:
        raw = (am.group(1) or am.group(2) or '').replace(',', '')
        try:
            value = float(raw)
        except ValueError:
            pass
        sym = re.search(r'[£€$]|GBP|EUR|USD', am.group(0))
        if sym:
            currency = _CURRENCY_MAP.get(sym.group(0), sym.group(0))

    # expiry date
    expiry_date = None
    _EXP_PATTERNS = [
        r'(?i)(?:expir(?:y|es?|ation)|valid\s*until|use\s*by)[:\s]+(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})',
        r'(?i)(?:expir(?:y|es?|ation)|valid\s*until|use\s*by)[:\s]+(\d{1,2}\s+\w+\s+\d{4})',
        r'(?i)(?:expir(?:y|es?|ation)|valid\s*until)[:\s]+(\w+\s+\d{1,2},?\s+\d{4})',
    ]
    for pat in _EXP_PATTERNS:
        em = re.search(pat, body)
        if em:
            raw_date = em.group(1).strip()
            parsed = parse_date(raw_date)
            if parsed:
                expiry_date = parsed
                break
            for fmt in ('%d/%m/%Y', '%d-%m-%Y', '%m/%d/%Y', '%d %B %Y', '%B %d, %Y', '%B %d %Y', '%d/%m/%y'):
                try:
                    expiry_date = datetime.datetime.strptime(raw_date, fmt).date()
                    break
                except ValueError:
                    continue
            if expiry_date:
                break

    if not redeem_code and value is None:
        raise ValueError('Could not extract a gift card code or value from the email.')

    name = (subject or '').strip() or (f'{issuer} Gift Card' if issuer else 'Gift Card')
    result = {'name': name[:200], 'value_type': 'money', 'currency': currency}
    if issuer:
        result['issuer'] = issuer[:200]
    if redeem_code:
        result['redeem_code'] = redeem_code[:200]
    if value is not None:
        result['value'] = value
    if expiry_date:
        result['expiry_date'] = expiry_date
    return result


class GiftCardEmailImportView(APIView):
    """
    POST a parsed email (e.g. from an n8n Gmail trigger) and have the gift
    card automatically extracted and saved as an Item.

    Accepts JSON:
      {
        "subject":      "Your £50 Amazon Gift Card",
        "from_address": "noreply@amazon.co.uk",
        "body_text":    "... Your code: ABCD1234EFGH5678 ...",
        "body_html":    "...",   # optional, used if body_text is empty
        "attachments":  []       # reserved for future image-scan support
      }

    Returns 201 + item data on success, 409 + {duplicate: true} if a gift
    card with the same redeem_code already exists for this user, or 422 if
    neither a code nor a value could be parsed.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=inline_serializer(
            name='GiftCardEmailImportRequest',
            fields={
                'subject': serializers.CharField(allow_blank=True, default=''),
                'from_address': serializers.CharField(allow_blank=True, default=''),
                'body_text': serializers.CharField(allow_blank=True, default=''),
                'body_html': serializers.CharField(allow_blank=True, default=''),
            },
        ),
        responses={
            201: ItemSerializer,
            409: inline_serializer(
                name='GiftCardDuplicateResponse',
                fields={'duplicate': serializers.BooleanField(), 'id': serializers.UUIDField()},
            ),
            422: inline_serializer(
                name='GiftCardParseErrorResponse',
                fields={'error': serializers.CharField()},
            ),
        },
    )
    def post(self, request):
        subject = request.data.get('subject', '')
        from_address = request.data.get('from_address', '')
        body_text = request.data.get('body_text', '')
        body_html = request.data.get('body_html', '')

        try:
            fields = _parse_gift_card_email(subject, from_address, body_text, body_html)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        # Duplicate check: same redeem_code already exists for this user
        redeem_code = fields.get('redeem_code')
        if redeem_code:
            existing = Item.objects.filter(user=request.user, redeem_code=redeem_code).first()
            if existing:
                return Response(
                    {'duplicate': True, 'id': str(existing.id)},
                    status=status.HTTP_409_CONFLICT,
                )

        from datetime import date as _date, timedelta as _td
        today = _date.today()
        if 'expiry_date' not in fields:
            fields['expiry_date'] = today + _td(days=5 * 365)
        fields.setdefault('issue_date', today)
        item = Item.objects.create(user=request.user, type='giftcard', source='api', **fields)
        notify_item_created(item)
        fetch_merchant_logo_task.delay(str(item.id))

        return Response(ItemSerializer(item, context={'request': request}).data, status=status.HTTP_201_CREATED)


class ExportCsvView(APIView):
    """Download all of the authenticated user's items as a VoucherVault Plus+ CSV backup."""
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.BINARY})
    def get(self, request):
        items = Item.objects.filter(user=request.user).select_related('wallet').prefetch_related('tags')
        response = HttpResponse(export_items_csv(items), content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="vouchervault-export.csv"'
        return response


class ExportFullBackupView(APIView):
    """Download all of the authenticated user's items, plus their attached
    files and documents, as a single .zip backup bundle."""
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.BINARY})
    def get(self, request):
        items = Item.objects.filter(user=request.user).select_related('wallet').prefetch_related('tags', 'documents', 'transactions')
        response = HttpResponse(export_full_backup(items, user=request.user), content_type='application/zip')
        response['Content-Disposition'] = 'attachment; filename="vouchervault-full-backup.zip"'
        return response


class ImportFullBackupView(APIView):
    """
    Restore a Full Backup .zip bundle. Every item is created fresh with a
    new ID — this never overwrites or merges with existing items.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        request={'multipart/form-data': inline_serializer(name='FullBackupImportRequest', fields={'file': serializers.FileField()})},
        responses=inline_serializer(
            name='FullBackupImportResponse',
            fields={
                'imported_count': serializers.IntegerField(),
                'error_count': serializers.IntegerField(),
                'errors': serializers.ListField(child=serializers.DictField()),
                'settings_restored': serializers.BooleanField(),
            },
        ),
    )
    def post(self, request):
        upload = request.FILES.get('file')
        if not upload:
            return Response({'file': _('No file uploaded.')}, status=status.HTTP_400_BAD_REQUEST)

        try:
            result = import_full_backup(request.user, upload.read())
        except FullBackupImportError as exc:
            return Response({'file': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(result)


class ExportJsonView(APIView):
    """Download all of the authenticated user's items as a VoucherVault Plus+ JSON backup."""
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.BINARY})
    def get(self, request):
        items = Item.objects.filter(user=request.user).select_related('wallet').prefetch_related('tags')
        payload = json.dumps(export_items_json(items), indent=2)
        response = HttpResponse(payload, content_type='application/json')
        response['Content-Disposition'] = 'attachment; filename="vouchervault-export.json"'
        return response


class AnalyticsSummaryView(APIView):
    """Aggregate KPI stats for the authenticated user's items (counts, by type/wallet, value by currency)."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses=inline_serializer(
            name='AnalyticsSummaryResponse',
            fields={
                'total_items': serializers.IntegerField(),
                'used_items': serializers.IntegerField(),
                'expired_items': serializers.IntegerField(),
                'expiring_7_days': serializers.IntegerField(),
                'expiring_30_days': serializers.IntegerField(),
                'by_type': serializers.ListField(child=serializers.DictField()),
                'by_wallet': serializers.ListField(child=serializers.DictField()),
                'value_by_currency': serializers.DictField(),
                'at_risk_value_by_currency': serializers.DictField(),
            },
        )
    )
    def get(self, request):
        data = get_summary_stats(request.user)
        data['spend_stats'] = get_spend_stats(request.user)
        return Response(data)


class AnalyticsExpiryTimelineView(APIView):
    """Items grouped by ISO expiry date over the next `months` months (default 3) — a calendar feed."""
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        months = request.query_params.get('months', 3)
        try:
            months = max(1, min(int(months), 12))
        except ValueError:
            return Response({'months': 'Must be an integer.'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(get_expiry_timeline(request.user, months_ahead=months))


class UserWebhookViewSet(viewsets.ModelViewSet):
    """CRUD for outbound webhooks owned by the authenticated user."""
    serializer_class = UserWebhookSerializer
    permission_classes = [IsAuthenticated, IsOwner]

    def get_queryset(self):
        return UserWebhook.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=True, methods=['post'], url_path='test')
    def test_fire(self, request, pk=None):
        """Fire a synthetic test payload to the webhook URL."""
        from myapp.webhooks import fire_user_webhooks
        hook = self.get_object()
        from myapp.models import Item as _Item
        dummy = _Item(name='Test Item', issuer='VoucherVault', redeem_code='TEST123', value=10)
        fire_user_webhooks.__wrapped__ = getattr(fire_user_webhooks, '__wrapped__', None)
        import threading, json, hashlib, hmac, datetime, requests as _requests
        payload = {
            'event': 'test',
            'timestamp': datetime.datetime.utcnow().isoformat() + 'Z',
            'item': {'name': 'Test Item', 'issuer': 'VoucherVault', 'code': 'TEST123'},
        }
        body = json.dumps(payload, ensure_ascii=False).encode()
        sig = ''
        if hook.secret:
            sig = 'sha256=' + hmac.new(hook.secret.encode(), body, hashlib.sha256).hexdigest()
        headers = {
            'Content-Type': 'application/json',
            'X-VoucherVault-Event': 'test',
            'X-VoucherVault-Signature': sig,
        }
        try:
            resp = _requests.post(hook.url, data=body, headers=headers, timeout=10)
            return Response({'status_code': resp.status_code, 'ok': resp.ok})
        except Exception as exc:
            return Response({'error': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


class WalletMembershipViewSet(viewsets.ModelViewSet):
    """Manage collaborators on wallets you own. Filter by ?wallet=<id>."""
    serializer_class = WalletMembershipSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = WalletMembership.objects.filter(wallet__user=self.request.user).select_related('user', 'wallet')
        wallet_id = self.request.query_params.get('wallet')
        if wallet_id:
            qs = qs.filter(wallet_id=wallet_id)
        return qs

    def perform_create(self, serializer):
        wallet = get_object_or_404(Wallet, pk=self.request.data.get('wallet'), user=self.request.user)
        instance = serializer.save(wallet=wallet)
        wallet.shared_with.add(instance.user)

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.wallet.user != request.user:
            return Response({'detail': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.wallet.user != request.user:
            return Response({'detail': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
        instance.wallet.shared_with.remove(instance.user)
        return super().destroy(request, *args, **kwargs)


class WalletActivityViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only activity feed for wallets you own or are a member of. Filter by ?wallet=<id>."""
    serializer_class = WalletActivitySerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [OrderingFilter]
    ordering = ['-timestamp']

    def get_queryset(self):
        from django.db.models import Q as _Q
        qs = WalletActivity.objects.filter(
            _Q(wallet__user=self.request.user) |
            _Q(wallet__memberships__user=self.request.user)
        ).select_related('actor', 'wallet').distinct()
        wallet_id = self.request.query_params.get('wallet')
        if wallet_id:
            qs = qs.filter(wallet_id=wallet_id)
        return qs


class ItemDocumentViewSet(viewsets.ModelViewSet):
    """
    Supporting documents (receipts, proof of purchase) attached to an item.
    Nested under items: GET/POST /api/v1/items/{item_pk}/documents/
    and DELETE /api/v1/items/{item_pk}/documents/{pk}/.
    """
    serializer_class = DocumentSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    http_method_names = ['get', 'post', 'delete', 'head', 'options']

    def _get_item(self):
        return get_object_or_404(
            Item,
            Q(user=self.request.user) | Q(wallet__shared_with=self.request.user),
            pk=self.kwargs['item_pk'],
        )

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Document.objects.none()
        item = self._get_item()
        return Document.objects.filter(item=item)

    def perform_create(self, serializer):
        item = self._get_item()
        serializer.save(item=item)


class DMSProviderViewSet(viewsets.ModelViewSet):
    """CRUD for the authenticated user's Document Management System providers."""
    serializer_class = DMSProviderSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return DMSProvider.objects.none()
        return DMSProvider.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=True, methods=['post'])
    def test(self, request, pk=None):
        """Test the connection to this DMS provider."""
        provider = self.get_object()
        from dms.clients import get_client as _get_dms_client
        try:
            client = _get_dms_client(provider)
            result = client.test_connection()
            ok = result.get('ok', False)
            message = result.get('message', '')
        except Exception as exc:
            ok, message = False, f'Connection failed: {exc}'

        from django.utils import timezone as _tz
        provider.last_checked = _tz.now()
        provider.status = DMSProvider.STATUS_OK if ok else DMSProvider.STATUS_ERROR
        provider.status_message = message
        provider.save(update_fields=['last_checked', 'status', 'status_message'])

        status_code = status.HTTP_200_OK if ok else status.HTTP_502_BAD_GATEWAY
        return Response({'ok': ok, 'message': message}, status=status_code)


class NotificationBarcodeView(APIView):
    """
    GET /api/v1/items/<uuid>/notification-barcode/?s=<signed-token>

    Returns a PNG barcode image for the given item, authenticated via a
    time-limited signed token rather than the user's API key. This lets ntfy
    fetch the image as an attachment without the user embedding credentials
    in the notification URL.

    The token is generated by django.core.signing and is valid for 5 minutes
    (ntfy fetches attachments quickly after receiving the notification). No
    auth header is required.
    """
    permission_classes = []   # token in ?s= param acts as auth

    def get(self, request, item_pk):
        from django.core import signing
        from myapp.utils import generate_code_image_base64
        import base64

        token = request.query_params.get('s', '')
        try:
            item_id = signing.loads(token, salt='ntfy-barcode', max_age=300)
        except signing.BadSignature:
            return Response({'detail': 'Invalid or expired token.'}, status=status.HTTP_403_FORBIDDEN)

        if str(item_pk) != item_id:
            return Response({'detail': 'Token does not match item.'}, status=status.HTTP_403_FORBIDDEN)

        from myapp.models import Item
        try:
            item = Item.objects.get(pk=item_pk)
        except Item.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        b64, code_type = generate_code_image_base64(item)
        if b64 is None:
            return Response({'detail': 'No barcode for this item.'}, status=status.HTTP_404_NOT_FOUND)

        image_bytes = base64.b64decode(b64)
        return HttpResponse(image_bytes, content_type='image/png')


class DMSSyncLogViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only DMS sync history. Filter by ?provider=<uuid>."""
    serializer_class = DMSSyncLogSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [OrderingFilter]
    ordering = ['-created_at']

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return DMSSyncLog.objects.none()
        qs = DMSSyncLog.objects.filter(
            provider__user=self.request.user
        ).select_related('provider', 'item')
        provider_id = self.request.query_params.get('provider')
        if provider_id:
            qs = qs.filter(provider_id=provider_id)
        return qs
