from django.db.models import Count, Q

from .models import SiteConfiguration, UpdateCheckStatus, UserPreference, Wallet


def sidebar_wallets(request):
    """Expose the current user's own and shared-with-them wallets to every template for sidebar navigation."""
    if not request.user.is_authenticated:
        return {}
    return {
        'sidebar_wallets': Wallet.objects.filter(
            Q(user=request.user) | Q(shared_with=request.user)
        ).distinct().annotate(item_count=Count('items')),
    }


def user_preferences(request):
    """Expose the current user's display preferences to every template (e.g. OLED dark mode)."""
    if not request.user.is_authenticated:
        return {}
    preferences, _ = UserPreference.objects.get_or_create(user=request.user)
    return {'global_preferences': preferences}


def update_check_status(request):
    """
    Expose the last GitHub Releases check result to superusers only - it's
    an operational/deployment concern, not something other users act on.
    """
    if not request.user.is_authenticated or not request.user.is_superuser:
        return {}
    return {
        'update_check': UpdateCheckStatus.load(),
        'portainer_redeploy_configured': bool(SiteConfiguration.load().portainer_webhook_url),
    }
