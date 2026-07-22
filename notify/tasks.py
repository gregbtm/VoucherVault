import itertools
import logging
from datetime import date, timedelta
from operator import attrgetter

import requests as requests_lib
from dateutil.relativedelta import relativedelta

from celery import shared_task

from django.db.models import Q
from django.utils import timezone

from myapp.models import Item, SiteConfiguration, Transaction, UserPreference
from myapp.utils import check_companies_house_status, _CH_BAD_STATUSES

from .backends import get_backend
from .models import DigestEntry, NotificationLog, NotificationRule

logger = logging.getLogger(__name__)


def default_threshold_days() -> int:
    return SiteConfiguration.load().expiry_threshold_days


def final_threshold_days() -> int:
    return SiteConfiguration.load().expiry_last_notification_days


def _already_notified(item, event_type, rule) -> bool:
    return NotificationLog.objects.filter(item=item, event_type=event_type, rule=rule, success=True).exists()


def fire_notifications(item, event_type: str, title: str, message: str, dedupe: bool = True, transaction=None):
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

    `transaction` is forwarded to backends that consume it (e.g. Firefly III
    balance-changed events); other backends ignore it.
    """
    if item.notifications_muted:
        return

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

        success, detail = send_via_rule(rule, title, message, item=item, transaction=transaction)
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
    _close_firefly_account_if_configured(item)


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
        transaction=transaction,
    )


def notify_item_shared(item, shared_with_username: str):
    """Fired every time an item is shared with another user."""
    fire_notifications(
        item, 'item_shared',
        title=f"🤝 {item.name} shared",
        message=f"Shared with {shared_with_username}",
        dedupe=False,
    )


def notify_wallet_invited(wallet, invited_user):
    """Notify a user when they are added as a collaborator to a wallet."""
    rules = NotificationRule.objects.filter(user=invited_user, enabled=True)
    matching_rules = [r for r in rules if 'wallet_invited' in (r.event_types or [])]
    for rule in matching_rules:
        title = f"📂 Added to '{wallet.name}'"
        message = f"Owner: {wallet.user.username}\nYou now have access to items in this wallet."
        success, detail = send_via_rule(rule, title, message)
        NotificationLog.objects.create(
            user=invited_user, rule=rule, item=None, event_type='wallet_invited',
            success=success, detail=detail,
        )


def notify_wallet_removed(wallet, removed_user, owner_username: str):
    """Notify a user when their access to a wallet is revoked."""
    rules = NotificationRule.objects.filter(user=removed_user, enabled=True)
    matching_rules = [r for r in rules if 'wallet_removed' in (r.event_types or [])]
    for rule in matching_rules:
        title = f"📂 Removed from '{wallet.name}'"
        message = f"You no longer have access to '{wallet.name}' (owner: {owner_username})."
        success, detail = send_via_rule(rule, title, message)
        NotificationLog.objects.create(
            user=removed_user, rule=rule, item=None, event_type='wallet_removed',
            success=success, detail=detail,
        )


def send_via_rule(rule, title: str, message: str, item=None, transaction=None) -> tuple[bool, str]:
    """Runs a rule's backend, translating any exception into a logged failure."""
    try:
        backend = get_backend(rule)
        success = backend.send(title, message, item=item, transaction=transaction)
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

    items = Item.objects.filter(is_used=False, is_archived=False, expiry_date__isnull=False).select_related('user')
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


def _find_firefly_rule(item):
    """
    Resolve the Firefly III rule for an item using the three-level cascade:
      1. item.firefly_rule (per-item override)
      2. item.wallet.firefly_rule (wallet-level override)
      3. user's first enabled Firefly rule (global fallback)
    Returns the first enabled rule found, or None.
    """
    if item.firefly_rule_id:
        rule = item.firefly_rule
        if rule and rule.enabled:
            return rule
    if item.wallet_id:
        # Reload wallet if needed to access firefly_rule
        from myapp.models import Wallet
        wallet = item.wallet if 'wallet' in item.__dict__ else Wallet.objects.filter(pk=item.wallet_id).first()
        if wallet and wallet.firefly_rule_id:
            rule = wallet.firefly_rule
            if rule and rule.enabled:
                return rule
    return NotificationRule.objects.filter(user=item.user, backend='firefly', enabled=True).first()


def _close_firefly_account_if_configured(item):
    """
    Called after notify_item_archived. If the item is linked to a Firefly account
    and the rule config has close_account_on_archive: true, marks the account
    inactive in Firefly III by PATCHing it.
    """
    if not item.firefly_account_id:
        return
    rule = _find_firefly_rule(item)
    if rule is None:
        return
    if not rule.config.get('close_account_on_archive'):
        return

    url = (rule.config.get('url') or '').rstrip('/')
    token = rule.config.get('token', '')
    if not url or not token:
        return

    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }
    try:
        resp = requests_lib.patch(
            f'{url}/api/v1/accounts/{item.firefly_account_id}',
            json={'active': False},
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        logger.info('Firefly III: account %s marked inactive for item %s.', item.firefly_account_id, item.pk)
    except requests_lib.RequestException as exc:
        logger.warning('Firefly III: could not close account for item %s: %s', item.pk, exc)


@shared_task
def push_transaction_to_firefly(rule_id, item_id, transaction_id):
    """
    Async Celery task that pushes a single VoucherVault transaction to
    Firefly III and writes the returned Firefly transaction ID back to
    Transaction.firefly_transaction_id. Called from FireflyBackend.send()
    when a rule_id is available (i.e. the push came through the normal
    notification pipeline) and by backfill_firefly_transactions.
    """
    from notify.backends.firefly_backend import _do_firefly_push
    try:
        rule = NotificationRule.objects.get(pk=rule_id, backend='firefly', enabled=True)
    except NotificationRule.DoesNotExist:
        logger.warning('push_transaction_to_firefly: rule %s not found or disabled.', rule_id)
        return
    try:
        item = Item.objects.prefetch_related('tags').get(pk=item_id)
    except Item.DoesNotExist:
        logger.warning('push_transaction_to_firefly: item %s not found.', item_id)
        return
    try:
        tx = Transaction.objects.get(pk=transaction_id)
    except Transaction.DoesNotExist:
        logger.warning('push_transaction_to_firefly: transaction %s not found.', transaction_id)
        return
    _do_firefly_push(rule.config, item, tx)


@shared_task
def backfill_firefly_transactions(item_id, rule_id):
    """
    Celery task that pushes all unsynced transactions for an item to Firefly
    III, in date order. Transactions that already have a firefly_transaction_id
    are skipped. Called from the firefly-link API action immediately after an
    item is linked.
    """
    unsynced = Transaction.objects.filter(item_id=item_id, firefly_transaction_id='').order_by('date')
    for tx in unsynced:
        push_transaction_to_firefly.delay(rule_id, str(item_id), str(tx.pk))


@shared_task
def retry_failed_firefly_pushes():
    """
    Runs hourly. Finds every item linked to Firefly III that has transactions
    not yet synced (firefly_transaction_id is blank) and re-queues a push for
    each. Safe to re-run: once firefly_transaction_id is populated the
    transaction leaves the filter on the next cycle.
    """
    items = (
        Item.objects.exclude(firefly_account_id='')
        .filter(transactions__firefly_transaction_id='')
        .distinct()
        .select_related('user', 'wallet')
    )
    for item in items:
        rule = _find_firefly_rule(item)
        if rule is None:
            continue
        unsynced = item.transactions.filter(firefly_transaction_id='')
        for tx in unsynced:
            push_transaction_to_firefly.delay(rule.id, str(item.pk), str(tx.pk))


_RENEWAL_DELTA = {
    'weekly':    relativedelta(weeks=1),
    'monthly':   relativedelta(months=1),
    'quarterly': relativedelta(months=3),
    'biannual':  relativedelta(months=6),
    'annual':    relativedelta(years=1),
}


@shared_task
def advance_recurring_items():
    """
    Runs daily. For every recurring item whose renewal_date is today or in
    the past, advances renewal_date and expiry_date by one renewal_period,
    resets is_used=False so the item becomes active again, clears the
    expiry notification sent flags, and fires a renewal_advanced event.

    Safe to re-run: items are filtered to those where renewal_date <= today,
    so they advance exactly once per cycle (after advancing, the new date is
    in the future and they drop out of the filter).
    """
    today = date.today()
    items = Item.objects.filter(
        is_recurring=True,
        renewal_period__in=_RENEWAL_DELTA,
        renewal_date__isnull=False,
        renewal_date__lte=today,
    ).select_related('user')

    for item in items:
        delta = _RENEWAL_DELTA[item.renewal_period]
        item.renewal_date = item.renewal_date + delta
        if item.expiry_date:
            item.expiry_date = item.expiry_date + delta
        item.is_used = False
        item.default_expiry_notification_sent = False
        item.final_expiry_notification_sent = False
        item.save(update_fields=[
            'renewal_date', 'expiry_date', 'is_used',
            'default_expiry_notification_sent', 'final_expiry_notification_sent',
        ])
        title = f"🔄 {item.name} renewed"
        message = (
            f"Next renewal: {item.renewal_date}\n"
            f"Code: {item.redeem_code}\nValue: {item.value} {item.currency}"
        )
        fire_notifications(item, 'renewal_advanced', title, message, dedupe=False)


@shared_task
def check_and_notify_inactivity():
    """
    Runs weekly. Fires 'item_inactive' through each user's NotificationRules
    for every active money-type item (excluding loyalty cards and fully-spent
    ones) that has not been used/viewed for more than
    SiteConfiguration.inactivity_threshold_days.

    Uses its own dedup: fires at most once per threshold period per
    (item, rule) pair, so users get a periodic nudge rather than a one-off
    lifetime ping.  Falls back to last_used_at=epoch (item never opened)
    to treat never-used items the same as long-inactive ones.
    """
    cfg = SiteConfiguration.load()
    threshold_days = cfg.inactivity_threshold_days
    cutoff_dt = timezone.now() - timedelta(days=threshold_days)

    items = (
        Item.objects.filter(is_used=False, is_archived=False, value_type='money')
        .exclude(type='loyaltycard')
        .filter(
            Q(last_used_at__isnull=True) | Q(last_used_at__lt=cutoff_dt)
        )
        .select_related('user')
        .with_current_balance()
    )

    for item in items:
        if item.current_balance <= 0:
            continue

        rules = NotificationRule.objects.filter(user=item.user, enabled=True)
        matching_rules = [r for r in rules if 'item_inactive' in (r.event_types or [])]
        if not matching_rules:
            continue

        title = f"💤 {item.name} — unused for {threshold_days}+ days"
        message = (
            f"You have {item.current_balance:.2f} {item.currency} remaining.\n"
            f"Code: {item.redeem_code}"
        )

        for rule in matching_rules:
            if NotificationLog.objects.filter(
                item=item, event_type='item_inactive', rule=rule, success=True,
                sent_at__gte=cutoff_dt,
            ).exists():
                continue

            if rule.digest_frequency == 'daily':
                DigestEntry.objects.create(
                    rule=rule, item=item, event_type='item_inactive',
                    title=title, message=message,
                )
                NotificationLog.objects.create(
                    user=item.user, rule=rule, item=item, event_type='item_inactive',
                    success=True, detail='Queued for daily digest.',
                )
            else:
                success, detail = send_via_rule(rule, title, message, item=item)
                NotificationLog.objects.create(
                    user=item.user, rule=rule, item=item, event_type='item_inactive',
                    success=success, detail=detail,
                )


@shared_task
def check_merchant_health():
    """
    Runs weekly. For each unique issuer across all active items, queries the
    Companies House API (requires SiteConfiguration.companies_house_api_key)
    and fires 'merchant_health_alert' if the matched company is in a bad
    status (dissolved, liquidation, administration, etc.).

    Deduped per (item, rule): only fires once ever for the same issuer in bad
    standing — dedupe=True prevents repeat alerts while the situation persists.
    """
    cfg = SiteConfiguration.load()
    api_key = cfg.companies_house_api_key
    if not api_key:
        return

    issuers = (
        Item.objects.filter(is_used=False, is_archived=False)
        .exclude(issuer='')
        .values_list('issuer', flat=True)
        .distinct()
    )

    bad_issuers: dict[str, dict] = {}
    for issuer in issuers:
        if issuer in bad_issuers:
            continue
        result = check_companies_house_status(issuer, api_key)
        if result and result['company_status'] in _CH_BAD_STATUSES:
            bad_issuers[issuer] = result

    if not bad_issuers:
        return

    for issuer, ch_data in bad_issuers.items():
        items = Item.objects.filter(
            is_used=False, is_archived=False, issuer=issuer,
        ).select_related('user')

        for item in items:
            title = f"⚠️ {item.issuer} may be in {ch_data['company_status']}"
            message = (
                f"{ch_data['company_name']} (#{ch_data['company_number']}) is "
                f"listed as '{ch_data['company_status']}' on Companies House.\n"
                f"Consider spending your {item.name} balance soon."
            )
            fire_notifications(item, 'merchant_health_alert', title, message, dedupe=True)
