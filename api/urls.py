from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.authtoken.views import obtain_auth_token
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register('items', views.ItemViewSet, basename='item')
router.register('transactions', views.TransactionViewSet, basename='transaction')
router.register('wallets', views.WalletViewSet, basename='wallet')
router.register('tags', views.TagViewSet, basename='tag')
router.register('notifications/rules', views.NotificationRuleViewSet, basename='notification-rule')
router.register('notifications/log', views.NotificationLogViewSet, basename='notification-log')
router.register('imports/jobs', views.ImportJobViewSet, basename='import-job')
router.register('merchants', views.MerchantProfileViewSet, basename='merchant')

urlpatterns = [
    path('auth/token/', obtain_auth_token, name='api-token-auth'),
    path('preferences/', views.UserPreferenceView.as_view(), name='api-preferences'),
    path('profile/', views.UserProfileView.as_view(), name='api-profile'),
    path('schema/', SpectacularAPIView.as_view(), name='api-schema'),
    path('docs/', SpectacularSwaggerView.as_view(url_name='api-schema'), name='api-docs'),
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
    path('', include(router.urls)),
]
