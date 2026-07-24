from django.urls import include, path
from django.utils.decorators import method_decorator
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.authtoken.views import obtain_auth_token
from rest_framework.routers import DefaultRouter
from csp.decorators import csp_replace

from . import views


# Swagger UI injects inline <script> blocks which the site-wide CSP blocks.
# Relax script-src to allow unsafe-inline only for this view.
class _SwaggerView(SpectacularSwaggerView):
    @method_decorator(csp_replace({"script-src": ["'self'", "'unsafe-inline'", "'unsafe-eval'"]}))
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

router = DefaultRouter()
router.register('items', views.ItemViewSet, basename='item')
router.register('transactions', views.TransactionViewSet, basename='transaction')
router.register('wallets', views.WalletViewSet, basename='wallet')
router.register('wallet-memberships', views.WalletMembershipViewSet, basename='wallet-membership')
router.register('wallet-activity', views.WalletActivityViewSet, basename='wallet-activity')
router.register('tags', views.TagViewSet, basename='tag')
router.register('notifications/rules', views.NotificationRuleViewSet, basename='notification-rule')
router.register('notifications/log', views.NotificationLogViewSet, basename='notification-log')
router.register('imports/jobs', views.ImportJobViewSet, basename='import-job')
router.register('merchants', views.MerchantProfileViewSet, basename='merchant')
router.register('webhooks', views.UserWebhookViewSet, basename='webhook')
router.register('dms/providers', views.DMSProviderViewSet, basename='dms-provider')
router.register('dms/sync-logs', views.DMSSyncLogViewSet, basename='dms-sync-log')

urlpatterns = [
    path('auth/token/', obtain_auth_token, name='api-token-auth'),
    path('preferences/', views.UserPreferenceView.as_view(), name='api-preferences'),
    path('profile/', views.UserProfileView.as_view(), name='api-profile'),
    path('schema/', SpectacularAPIView.as_view(), name='api-schema'),
    path('docs/', _SwaggerView.as_view(url_name='api-schema'), name='api-docs'),
    path('imports/upload/', views.ImportUploadView.as_view(), name='api-import-upload'),
    path('imports/preview/', views.ImportPreviewView.as_view(), name='api-import-preview'),
    path('exports/csv/', views.ExportCsvView.as_view(), name='api-export-csv'),
    path('exports/json/', views.ExportJsonView.as_view(), name='api-export-json'),
    path('exports/full-backup/', views.ExportFullBackupView.as_view(), name='api-export-full-backup'),
    path('imports/full-backup/', views.ImportFullBackupView.as_view(), name='api-import-full-backup'),
    path('analytics/summary/', views.AnalyticsSummaryView.as_view(), name='api-analytics-summary'),
    path('analytics/expiry-timeline/', views.AnalyticsExpiryTimelineView.as_view(), name='api-analytics-expiry-timeline'),
    path('ocr/extract/', views.OCRExtractView.as_view(), name='api-ocr-extract'),
    path('imports/pkpass/', views.PkpassImportView.as_view(), name='api-pkpass-import'),
    path('imports/rail-ticket/', views.RailTicketImportView.as_view(), name='api-rail-ticket-import'),
    path('imports/rail-ticket/batch/', views.RailTicketBatchImportView.as_view(), name='api-rail-ticket-batch-import'),
    path('imports/gift-card-email/', views.GiftCardEmailImportView.as_view(), name='api-gift-card-email-import'),
    # Nested: /api/v1/items/{item_pk}/documents/
    path('items/<uuid:item_pk>/documents/', views.ItemDocumentViewSet.as_view({'get': 'list', 'post': 'create'}), name='api-item-documents'),
    path('items/<uuid:item_pk>/documents/<int:pk>/', views.ItemDocumentViewSet.as_view({'delete': 'destroy'}), name='api-item-document-detail'),
    # Token-authenticated barcode image for ntfy notification attachments
    path('items/<uuid:item_pk>/notification-barcode/', views.NotificationBarcodeView.as_view(), name='api-notification-barcode'),
    path('', include(router.urls)),
]
