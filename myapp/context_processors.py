from django.db.models import Count, Q

from .models import UserPreference, Wallet


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
