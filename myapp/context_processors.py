from django.db.models import Count

from .models import Wallet


def sidebar_wallets(request):
    """Expose the current user's wallets to every template for sidebar navigation."""
    if not request.user.is_authenticated:
        return {}
    return {
        'sidebar_wallets': Wallet.objects.filter(user=request.user).annotate(item_count=Count('items')),
    }
