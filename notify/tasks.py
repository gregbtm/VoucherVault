import itertools
from datetime import date
from operator import attrgetter

from celery import shared_task

from myapp.models import Item, SiteConfiguration, UserPreference

from .backends import get_backend
from .models import DigestEntry, NotificationLog, NotificationRule


def default_threshold_days() -> int:
    return SiteConfiguration.load().expiry_threshold_days


def final_threshold_days() -> int:
    return SiteConfiguration.load().expiry_last_notification_days


def _already_notified(item, event_type, rule) -> bool:
    return NotificationLog.objects.filter(item=item, event_type=event_type, rule=rule, success=True).exists()


def fire_notifications(item, event_type: str, title: str, message: str, dedupe: bool = True):
    """
    Sends `title`/`message` for `item` as `event_type` through every enabled
    rule the item's owner has subscribed to that event, logging each
    attempt.

    `dedupe=True` (the default) skips a rule that already has a successful
    log entry for this exact (item, event_type) — this is what makes
    re-running the periodic expiry-check task safe, since it re-evaluates
    every item on every run. Events fired directly from the action that
    caused them (an item created/used/archived/shared, a transaction
    added) should pass `dedupe=False`: each occurrence is a distinct,
    meaningful event rather than a periodic re-scan repeat, and the item
    may legitimately pass through the same event_type more than once
    (e.g. several transactions, or being marked used/available/used again).
    """
    rules = NotificationRule.objects.filter(user=item.user, enabled=True)
    matching_rules = [r for r in rules if event_type in (r.event_types or [])]

    if not matching_rules:
        return

    for rule in matching_rules:
        if dedupe and _already_notified(item, event_type, rule):
            continue

        if rule.digest_frequency == 'daily':
            DigestEntry.objects.create(rule=rule, item=item, event_type=event_type, title=title, message=message)
            # Logged immediately (not once the digest actually sends) so
            # dedupe=True callers - the periodic expiry re-scan - see this
            # as already handled and don't re-queue it every day between
            # now and the next digest send.
            NotificationLog.objects.create(
                user=item.user, rule=rule, item=item, event_type=event_type,
                success=True, detail='Queued for daily digest.',
            )
            continue

        success, detail = send_via_rule(rule, title, message, item=item)
        NotificationLog.objects.create(
            user=item.user, rule=rule, item=item, event_type=event_type,
            success=success, detail=detail,
        )


def _expiry_message(item, days_left: int) -> tuple[str, str]:
    if days_left >= 0:
        title = f"⏰ {item.name} expires in {days_left} day(s)"
    else:
        title = f"⏰ {item.name} has expired"
    message = f"Code: {item.redeem_code}\nValue: {item.value} {item.currency}\nExpiry: {item.expiry_date}"
    return title, message


def notify_item_created(item):
    """Fired once, right when an item is created (web UI or API)."""
    fire_notifications(
        item, 'item_created',
        title=f"➕ {item.name} added",
        message=f"Type: {item.get_type_display()}\nValue: {item.value} {item.currency}\nExpiry: {item.expiry_date}",
        dedupe=False,
    )


def notify_item_used(item):
    """Fired when an item transitions to is_used=True (not on the reverse toggle)."""
    fire_notifications(
        item, 'item_used',
        title=f"✅ {item.name} marked used",
        message=f"Code: {item.redeem_code}",
        dedupe=False,
    )


def notify_item_archived(item):
    """Fired when an item transitions to is_archived=True (not on unarchive)."""
    fire_notifications(
        item, 'item_archived',
        title=f"🗄️ {item.name} archived",
        message=f"Code: {item.redeem_code}",
        dedupe=False,
    )


def notify_balance_changed(item, transaction):
    """Fired every time a Transaction is recorded against an item."""
    fire_notifications(
        item, 'balance_changed',
        title=f"💷 {item.name} balance changed",
        message=(
            f"{transaction.description}: {transaction.value} {item.currency}\n"
            f"New balance: {item.get_current_balance()} {item.currency}"
        ),
        dedupe=False,
    )


def notify_item_shared(item, shared_with_username: str):
    """Fired every time an item is shared with another user."""
    fire_notifications(
        item, 'item_shared',
        title=f"🤝 {item.name} shared",
        message=f"Shared with {shared_with_username}",
        dedupe=False,
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
        title='VoucherVault Plus+ test notification',
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
        title, message = _expiry_message(item, days_left)

        if 0 <= days_left <= threshold:
            fire_notifications(item, 'expiry_warning', title, message)

        if 0 <= days_left <= final_threshold:
            fire_notifications(item, 'expiry_final', title, message)


@shared_task
def check_next_up_reminders():
    """
    Runs on the same daily schedule as check_and_notify_expiry (see
    create_default_periodic_tasks). Fires a next_up_reminder event for
    every active item that expires today in one of a user's configured
    "Next Up" wallets (UserPreference.next_up_wallets) - a no-op for any
    user who hasn't set one, and for a fresh install with none configured
    at all. Not limited by next_up_max_items: that field only caps how
    many items the Inventory widget displays, not which items are
    reminder-worthy - every item due today in a watched wallet gets one.
    """
    today = date.today()

    for preferences in UserPreference.objects.exclude(next_up_wallets=None).distinct():
        wallet_ids = list(preferences.next_up_wallets.values_list('id', flat=True))
        if not wallet_ids:
            continue
        items = Item.objects.filter(
            wallet_id__in=wallet_ids, user=preferences.user,
            is_used=False, is_archived=False, expiry_date=today,
        )
        for item in items:
            title = f"📌 {item.name} is today"
            message = f"Code: {item.redeem_code}\nWallet: {item.wallet.name}"
            fire_notifications(item, 'next_up_reminder', title, message)


@shared_task
def send_daily_digests():
    """
    Runs on the same daily schedule as check_and_notify_expiry. Groups
    every pending DigestEntry by rule and sends one combined message per
    rule - the entries themselves were queued at the moment each event
    happened (see fire_notifications), not built fresh here. Cleared
    after every attempt, successful or not: nothing in this app retries
    a failed send, so holding a failed digest's entries for tomorrow
    would just silently double it up rather than actually recover it.
    """
    all_entries = DigestEntry.objects.select_related('rule').order_by('rule_id')
    processed_ids = []
    for _rule_id, group in itertools.groupby(all_entries, key=attrgetter('rule_id')):
        entries = list(group)
        processed_ids.extend(e.id for e in entries)
        rule = entries[0].rule
        if rule.enabled:
            count = len(entries)
            title = f"📋 VoucherVault Plus+ daily digest ({count} update{'s' if count != 1 else ''})"
            message = '\n\n'.join(f'{e.title}\n{e.message}' for e in entries)
            success, detail = send_via_rule(rule, title, message)
            NotificationLog.objects.create(
                user=rule.user, rule=rule, item=None, event_type='daily_digest',
                success=success, detail=detail,
            )
    if processed_ids:
        DigestEntry.objects.filter(id__in=processed_ids).delete()
