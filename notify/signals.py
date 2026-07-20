import logging

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

logger = logging.getLogger(__name__)


@receiver(post_save, sender='myapp.Item')
def handle_item_value_change(sender, instance, created, **kwargs):
    """
    When an existing item's `value` changes, create a compensating Transaction
    and notify balance-changed so Firefly III stays in sync with the new
    opening balance. The `_original_value` attribute is set by Item.__init__
    and reflects the value at the time the instance was last loaded from DB.

    Skipped for newly created items (created=True), for items with no
    Firefly account linked, and when the value hasn't actually changed.
    """
    if created:
        return
    original = getattr(instance, '_original_value', None)
    if original is None:
        return
    try:
        from decimal import Decimal
        original = Decimal(str(original))
        new_value = Decimal(str(instance.value))
    except Exception:
        return
    if original == new_value:
        return
    if not getattr(instance, 'firefly_account_id', ''):
        return
    if getattr(instance, 'is_archived', False):
        return

    from myapp.models import Transaction
    delta = new_value - original
    currency = getattr(instance, 'currency', '') or ''
    description = f'Value adjusted from {original:.2f} to {new_value:.2f}{" " + currency if currency else ""}'
    try:
        tx = Transaction.objects.create(
            item=instance,
            description=description,
            value=delta,
            date=timezone.now(),
        )
        from notify.tasks import notify_balance_changed
        notify_balance_changed(instance, tx)
        # Update cached value so a second save without a reload doesn't re-trigger.
        instance._original_value = instance.value
    except Exception as exc:
        logger.warning('handle_item_value_change: failed to create adjustment transaction for item %s: %s', instance.pk, exc)
