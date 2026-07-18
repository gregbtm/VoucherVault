import json
import logging

from django.contrib.auth.models import User
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
from myapp.analytics import get_expiry_timeline, get_summary_stats
from myapp.merchant_logos import remember_balance_check_url
from myapp.models import Item, ItemShare, MerchantProfile, Tag, Transaction, UserPreference, UserProfile, Wallet
from myapp.pdf_ticket import decode_barcode_from_pdf, pdf_page_to_png_bytes
from myapp.scan_learning import apply_learned_corrections
from notify.models import NotificationLog, NotificationRule
from notify.tasks import notify_balance_changed, notify_item_created, notify_item_shared, notify_item_used, send_test_notification
from ocr.backends import get_backend, ocr_enabled
from ocr.backends.base import parse_float_or_none

from .filters import ItemFilter
from .permissions import IsItemOwnerOrWalletCollaborator, IsOwner, IsWalletOwnerOrReadOnlyCollaborator
from .serializers import (
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
    WalletSerializer,
)

logger = logging.getLogger(__name__)

PREVIEW_ROW_LIMIT = 50
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10MB
MAX_OCR_IMAGE_SIZE = 8 * 1024 * 1024  # 8MB
OCR_ALLOWED_CONTENT_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
MAX_PDF_UPLOAD_SIZE = 15 * 1024 * 1024  # 15MB


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
    search_fields = ['name', 'redeem_code', 'issuer', 'description']
    ordering_fields = ['expiry_date', 'name', 'value', 'issue_date']
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
            wallet.shared_with.add(collaborator)
            return Response({'username': collaborator.username}, status=status.HTTP_201_CREATED)

        return Response([u.username for u in wallet.shared_with.all()])

    @action(detail=True, methods=['delete'], url_path=r'share/(?P<user_id>\d+)')
    def unshare(self, request, pk=None, user_id=None):
        """Owner-only: revoke a collaborator's access."""
        wallet = self.get_object()
        if wallet.user_id != request.user.id:
            return Response({'detail': _('Only the wallet owner can manage sharing.')}, status=status.HTTP_403_FORBIDDEN)
        collaborator = get_object_or_404(User, id=user_id)
        wallet.shared_with.remove(collaborator)
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
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    _RAIL_TICKET_TEXT_FIELDS = (
        'name', 'issuer', 'card_number', 'order_id', 'discount_applied',
        'journey_origin', 'journey_destination', 'travel_time', 'travel_date',
    )

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
        if upload.content_type != 'application/pdf':
            return Response({'file': _('Only PDF files are supported.')}, status=status.HTTP_400_BAD_REQUEST)

        pdf_bytes = upload.read()

        try:
            redeem_code, code_type = decode_barcode_from_pdf(pdf_bytes)
        except Exception as exc:
            logger.warning('Rail ticket barcode decode failed: %s', exc, exc_info=True)
            redeem_code, code_type = None, None

        fields = {name: (request.data.get(name) or None) for name in self._RAIL_TICKET_TEXT_FIELDS}
        fields['value'] = parse_float_or_none(request.data.get('value'))
        fields['currency'] = (request.data.get('currency') or '').upper() or None

        # OCR fills in whatever the caller didn't already supply - the
        # common case for a manual upload with no pre-extraction step at
        # all, and a graceful fallback if n8n's own parsing missed a field.
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
            fields['travel_date'] = fields['travel_date'] or ocr_result.get('expiry_date')
            fields['value'] = fields['value'] if fields['value'] is not None else ocr_result.get('value')
            fields['currency'] = fields['currency'] or ocr_result.get('currency')
            if redeem_code is None:
                redeem_code = ocr_result.get('code')
                code_type = code_type or ocr_result.get('code_type')

        barcode_decoded = bool(redeem_code) and code_type not in (None, 'none')
        if not redeem_code:
            # Nothing decodable and OCR found nothing to read either - fall
            # back to the ticket number itself as the redeem code, same as
            # any other "No Barcode" item rather than failing outright.
            redeem_code = fields['card_number'] or ''
            code_type = 'none'

        create = str(request.data.get('create', '')).lower() in ('1', 'true', 'yes')

        response_payload = {
            'created': False,
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
            return Response(response_payload)

        if not fields['issuer'] or not redeem_code:
            return Response(
                {'detail': _(
                    'Not enough information extracted to create an item '
                    '(need at least an issuer and a code).'
                )},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        travel_date = parse_date(fields['travel_date']) if fields['travel_date'] else None
        travel_time = parse_time(fields['travel_time']) if fields['travel_time'] else None
        travel_day = travel_date or timezone.localdate()

        name = fields['name'] or ' to '.join(
            part for part in (fields['journey_origin'], fields['journey_destination']) if part
        ) or _('Train Ticket')

        item = Item.objects.create(
            user=request.user,
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
        )

        response_payload['created'] = True
        response_payload['item'] = ItemSerializer(item, context={'request': request}).data
        return Response(response_payload, status=status.HTTP_201_CREATED)


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
        return Response(get_summary_stats(request.user))


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
