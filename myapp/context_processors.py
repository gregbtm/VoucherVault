from django.db.models import Count, Q

from .models import Wallet


def sidebar_wallets(request):
    """Expose the current user's own and shared-with-them wallets to every template for sidebar navigation."""
    if not request.user.is_authenticated:
        return {}
    return {
        'sidebar_wallets': Wallet.objects.filter(
            Q(user=request.user) | Q(shared_with=request.user)
        ).distinct().annotate(item_count=Count('items')),
    }
