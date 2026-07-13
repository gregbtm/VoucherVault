from django.conf import settings
from django.db.models import Count, Q

from .models import SiteConfiguration, UpdateCheckStatus, UserPreference, Wallet
from .update_check import _is_newer


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
    status = UpdateCheckStatus.load()
    # Re-validate against the *currently running* version on every request,
    # rather than trusting status.update_available as stored - that flag is
    # only recomputed when a check actually runs (daily task or a manual
    # click), so a redeploy that ships the fix since the last check would
    # otherwise leave a stale "update available" banner showing forever
    # until someone happens to check again. Cheap (pure string parsing),
    # so safe to redo on every request instead of caching it too.
    update_available = _is_newer(status.latest_version, settings.VERSION)
    return {
        'update_check': status,
        'update_check_available': update_available,
        'portainer_redeploy_configured': bool(SiteConfiguration.load().portainer_webhook_url),
    }


def share_settings(request):
    """
    Expose the "Share via..." smart-share toggle to every page - one flag,
    read once per request here rather than adding a data attribute to
    every share button on pages that can render hundreds of them
    (Inventory). See myapp/static/assets/js/voucher-share.js.
    """
    if not request.user.is_authenticated:
        return {}
    return {'share_via_smart_enabled': SiteConfiguration.load().share_via_smart_enabled}
