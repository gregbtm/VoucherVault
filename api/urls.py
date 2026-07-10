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

urlpatterns = [
    path('auth/token/', obtain_auth_token, name='api-token-auth'),
    path('preferences/', views.UserPreferenceView.as_view(), name='api-preferences'),
    path('profile/', views.UserProfileView.as_view(), name='api-profile'),
    path('schema/', SpectacularAPIView.as_view(), name='api-schema'),
    path('docs/', SpectacularSwaggerView.as_view(url_name='api-schema'), name='api-docs'),
    path('', include(router.urls)),
]
