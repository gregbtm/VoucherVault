"""Smart cache invalidation for analytics and frequently accessed data."""

from django.core.cache import cache
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver


def invalidate_user_analytics_cache(user_id):
    """Invalidate all analytics caches for a user."""
    cache.delete(f'summary_stats:{user_id}')
    cache.delete(f'spend_stats:{user_id}')


def invalidate_wallet_budget_cache(wallet_id):
    """Invalidate budget-related caches for a wallet."""
    from myapp.models import Wallet
    try:
        wallet = Wallet.objects.get(id=wallet_id)
        invalidate_user_analytics_cache(wallet.user_id)
    except Wallet.DoesNotExist:
        pass


def invalidate_item_cache(item_id):
    """Invalidate item-related caches."""
    from myapp.models import Item
    try:
        item = Item.objects.get(id=item_id)
        invalidate_user_analytics_cache(item.user_id)
        if item.wallet:
            invalidate_wallet_budget_cache(item.wallet_id)
    except Item.DoesNotExist:
        pass


@receiver(post_save, sender='myapp.Item')
def invalidate_on_item_change(sender, instance, **kwargs):
    """Invalidate caches when an item is created/updated."""
    invalidate_item_cache(instance.id)


@receiver(post_delete, sender='myapp.Item')
def invalidate_on_item_delete(sender, instance, **kwargs):
    """Invalidate caches when an item is deleted."""
    invalidate_user_analytics_cache(instance.user_id)


@receiver(post_save, sender='myapp.Transaction')
def invalidate_on_transaction_change(sender, instance, **kwargs):
    """Invalidate caches when a transaction is created/updated."""
    if instance.item:
        invalidate_item_cache(instance.item_id)


@receiver(post_delete, sender='myapp.Transaction')
def invalidate_on_transaction_delete(sender, instance, **kwargs):
    """Invalidate caches when a transaction is deleted."""
    if instance.item:
        invalidate_item_cache(instance.item_id)
