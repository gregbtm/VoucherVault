import json
import logging

from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
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
from imports.exporters.json_export import export_items_json
from imports.models import ImportJob
from imports.parsers import get_parser
from imports.tasks import process_import_job
from myapp.analytics import get_expiry_timeline, get_summary_stats
from myapp.models import Item, ItemShare, MerchantProfile, Tag, Transaction, UserPreference, UserProfile, Wallet
from notify.models import NotificationLog, NotificationRule
from notify.tasks import send_test_notification
from ocr.backends import get_backend, ocr_enabled

from .filters import ItemFilter
from .permissions import IsOwner
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


class ItemViewSet(viewsets.ModelViewSet):
    """
    Full CRUD for the authenticated user's items, plus /redeem/, /transactions/
    and /shares/ sub-resources. Every queryset below is scoped to
    `user=request.user` — no item belonging to another user is ever
    reachable through this API, even by UUID guessing.
    """
    serializer_class = ItemSerializer
    permission_classes = [IsAuthenticated, IsOwner]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_class = ItemFilter
    search_fields = ['name', 'redeem_code', 'issuer', 'description']
    ordering_fields = ['expiry_date', 'name', 'value', 'issue_date']
    ordering = ['expiry_date']

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Item.objects.none()
        return Item.objects.filter(user=self.request.user).select_related('wallet').prefetch_related('transactions', 'tags')

    def perform_create(self, serializer):
        serializer.save(user=self.request.user, source='api')

    @action(detail=True, methods=['post'])
    def redeem(self, request, pk=None):
        item = self.get_object()
        item.is_used = True
        item.save(update_fields=['is_used'])
        return Response(self.get_serializer(item).data)

    @action(detail=True, methods=['get', 'post'], url_path='transactions')
    def transactions(self, request, pk=None):
        item = self.get_object()
        if request.method == 'POST':
            serializer = TransactionSerializer(data=request.data, context={'item': item, 'request': request})
            serializer.is_valid(raise_exception=True)
            serializer.save(item=item)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        serializer = TransactionSerializer(item.transactions.all(), many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get', 'post'], url_path='shares')
    def shares(self, request, pk=None):
        item = self.get_object()
        if request.method == 'POST':
            serializer = ItemShareSerializer(data=request.data, context={'item': item, 'request': request})
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        serializer = ItemShareSerializer(item.shared_with.all(), many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['delete'], url_path=r'shares/(?P<share_id>\d+)')
    def delete_share(self, request, pk=None, share_id=None):
        item = self.get_object()
        share = get_object_or_404(ItemShare, item=item, pk=share_id)
        share.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class WalletViewSet(viewsets.ModelViewSet):
    """Full CRUD for the authenticated user's wallets, plus /items/ sub-resource."""
    serializer_class = WalletSerializer
    permission_classes = [IsAuthenticated, IsOwner]

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Wallet.objects.none()
        return Wallet.objects.filter(user=self.request.user).annotate(item_count=Count('items')).order_by('name')

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=True, methods=['get'])
    def items(self, request, pk=None):
        wallet = self.get_object()
        items = Item.objects.filter(user=request.user, wallet=wallet).select_related('wallet').prefetch_related('tags').order_by('expiry_date')
        page = self.paginate_queryset(items)
        serializer = ItemSerializer(page if page is not None else items, many=True, context={'request': request})
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)


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
    best-guess redeem code (and, with the Claude backend, merchant name/
    issuer/expiry date) to pre-fill the item form with. Processes the
    image synchronously — no ImportJob-style polling needed for a single
    image. Disabled (501) unless OCR_BACKEND is set.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        request={'multipart/form-data': inline_serializer(name='OCRExtractRequest', fields={'image': serializers.ImageField()})},
        responses=inline_serializer(
            name='OCRExtractResponse',
            fields={
                'code': serializers.CharField(allow_null=True),
                'name': serializers.CharField(allow_null=True),
                'issuer': serializers.CharField(allow_null=True),
                'expiry_date': serializers.CharField(allow_null=True),
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

        return Response(result)


class ExportCsvView(APIView):
    """Download all of the authenticated user's items as a VoucherVault CSV backup."""
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.BINARY})
    def get(self, request):
        items = Item.objects.filter(user=request.user).select_related('wallet').prefetch_related('tags')
        response = HttpResponse(export_items_csv(items), content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="vouchervault-export.csv"'
        return response


class ExportJsonView(APIView):
    """Download all of the authenticated user's items as a VoucherVault JSON backup."""
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
