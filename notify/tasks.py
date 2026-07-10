import os
from datetime import date

from celery import shared_task

from myapp.models import Item

from .backends import get_backend
from .models import NotificationLog, NotificationRule


def default_threshold_days() -> int:
    return int(os.getenv('EXPIRY_THRESHOLD_DAYS', 30))


def final_threshold_days() -> int:
    return int(os.getenv('EXPIRY_THRESHOLD_DAYS_FINAL', os.getenv('EXPIRY_LAST_NOTIFICATION_DAYS', 7)))


def _already_notified(item, event_type, rule) -> bool:
    return NotificationLog.objects.filter(item=item, event_type=event_type, rule=rule, success=True).exists()


def fire_notifications(item, event_type: str, days_left: int):
    """
    Sends `event_type` for `item` through every enabled rule the item's
    owner has subscribed to that event, logging each attempt. A rule that
    already succeeded for this exact (item, event_type) is skipped, so
    re-running the periodic task is always safe.
    """
    rules = NotificationRule.objects.filter(user=item.user, enabled=True)
    matching_rules = [r for r in rules if event_type in (r.event_types or [])]

    if not matching_rules:
        return

    if days_left >= 0:
        title = f"⏰ {item.name} expires in {days_left} day(s)"
    else:
        title = f"⏰ {item.name} has expired"
    message = f"Code: {item.redeem_code}\nValue: {item.value} {item.currency}\nExpiry: {item.expiry_date}"

    for rule in matching_rules:
        if _already_notified(item, event_type, rule):
            continue
        success, detail = send_via_rule(rule, title, message, item=item)
        NotificationLog.objects.create(
            user=item.user, rule=rule, item=item, event_type=event_type,
            success=success, detail=detail,
        )


def send_via_rule(rule, title: str, message: str, item=None) -> tuple[bool, str]:
    """Runs a rule's backend, translating any exception into a logged failure."""
    try:
        backend = get_backend(rule)
        success = backend.send(title, message, item=item)
        return success, '' if success else 'Backend reported failure.'
    except Exception as exc:
        return False, str(exc)


def send_test_notification(rule) -> tuple[bool, str]:
    """Fires an immediate test notification for a rule and logs the attempt."""
    success, detail = send_via_rule(
        rule,
        title='VoucherVault test notification',
        message=f"This is a test notification for the rule '{rule.name}'.",
    )
    NotificationLog.objects.create(
        user=rule.user, rule=rule, item=None, event_type='test',
        success=success, detail=detail,
    )
    return success, detail


@shared_task
def check_and_notify_expiry():
    """
    Runs on a schedule (see create_default_periodic_tasks). Fires
    expiry_warning / expiry_final events through each user's configured
    NotificationRules. A per-item notify_days_before overrides the global
    EXPIRY_THRESHOLD_DAYS.

    This is independent of, and does not replace, the legacy
    myapp.tasks.run_expiration_check (Apprise-only) task — both can run
    side by side without double-notifying, since a fresh install has no
    NotificationRules and this task is then a no-op.
    """
    today = date.today()
    default_threshold = default_threshold_days()
    final_threshold = final_threshold_days()

    items = Item.objects.filter(is_used=False, expiry_date__isnull=False).select_related('user')
    for item in items:
        days_left = (item.expiry_date - today).days
        threshold = item.notify_days_before if item.notify_days_before is not None else default_threshold

        if 0 <= days_left <= threshold:
            fire_notifications(item, 'expiry_warning', days_left)

        if 0 <= days_left <= final_threshold:
            fire_notifications(item, 'expiry_final', days_left)
