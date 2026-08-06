import calendar as calendar_module
from datetime import date as date_cls
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Count, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from .models import GlobalScanCorrection, Item, ItemEnrichmentLog, ScanFieldCorrection, SiteConfiguration, Transaction

EXPIRING_SOON_DAYS = 7
OTHER_WALLET_COLOR = '#9ca3af'


def get_summary_stats(user):
    """
    Aggregate KPI stats for a user's items, for the analytics API. Values
    are grouped by currency (no lossy Fixer.io conversion here) and are the
    stored `value` per item — they do not include Transaction adjustments,
    unlike the dashboard view's "Total Value" card, which sums those in
    for a precise ledger. `redeemed_this_month` isn't included: the model
    has no redemption timestamp (only the `is_used` boolean), so "this
    month" can't be determined.

    Results are cached for 60 seconds per user to reduce repeated aggregations
    on dashboard reloads.
    """
    cache_key = f'summary_stats:{user.id}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    now = timezone.now()
    today = timezone.localtime(now).date()
    threshold_days = SiteConfiguration.load().expiry_threshold_days
    soon_cutoff = today + timedelta(days=threshold_days)
    week_cutoff = today + timedelta(days=EXPIRING_SOON_DAYS)

    active_items = Item.objects.filter(user=user, is_used=False)
    valued_items = active_items.exclude(type='loyaltycard')

    def currency_totals(qs):
        # Sum() on SQLite can return a Decimal with the scale collapsed
        # (e.g. "30" instead of "30.00") since it computes via its own
        # numeric type rather than preserving decimal_places — requantize
        # explicitly so the API always returns 2-decimal-place amounts.
        rows = qs.values('currency').annotate(total=Sum('value')).order_by('currency')
        return {
            row['currency']: str(row['total'].quantize(Decimal('0.01')))
            for row in rows if row['total'] is not None
        }

    result = {
        'total_items': active_items.count(),
        'used_items': Item.objects.filter(user=user, is_used=True).count(),
        'expired_items': active_items.filter(expiry_date__lt=today).count(),
        'expiring_7_days': active_items.filter(expiry_date__gte=today, expiry_date__lt=week_cutoff).count(),
        'expiring_30_days': active_items.filter(expiry_date__gte=today, expiry_date__lt=soon_cutoff).count(),
        'by_type': list(active_items.values('type').annotate(count=Count('id')).order_by('type')),
        'by_wallet': get_items_by_wallet(user),
        'value_by_currency': currency_totals(valued_items.filter(expiry_date__gte=today)),
        'at_risk_value_by_currency': currency_totals(
            valued_items.filter(expiry_date__gte=today, expiry_date__lt=soon_cutoff)
        ),
    }
    cache.set(cache_key, result, 60)
    return result


def get_expiry_timeline(user, months_ahead=None):
    """
    Items grouped by ISO expiry date (sparse — only dates with items),
    for the analytics API's calendar feed. `months_ahead=None` (the
    default) resolves to SiteConfiguration.calendar_months_ahead - the
    API endpoint that calls this always passes its own explicit
    `months` query param instead, so this default only matters for
    other callers.
    """
    if months_ahead is None:
        months_ahead = SiteConfiguration.load().calendar_months_ahead
    today = timezone.localtime().date()
    horizon_end = today + timedelta(days=31 * months_ahead)

    items = (
        Item.objects.filter(user=user, is_used=False, expiry_date__gte=today, expiry_date__lte=horizon_end)
        .order_by('expiry_date')
    )
    grouped = {}
    for item in items:
        key = item.expiry_date.isoformat()
        grouped.setdefault(key, []).append({
            'id': str(item.id),
            'name': item.name,
            'type': item.type,
            'value': str(item.value),
            'currency': item.currency,
        })
    return grouped


def get_items_by_wallet(user, limit=None):
    """
    Returns [{'name': str, 'color': str, 'count': int}, ...] for the user's
    active (not-used) items, sorted by count desc. Wallet-less items are
    grouped under "No Wallet"; beyond `limit` distinct wallets, the rest
    fold into "Other" rather than generating an unbounded colour cycle.

    `limit=None` (the default) resolves to the admin-configured
    SiteConfiguration.wallet_chart_limit at call time.
    """
    if limit is None:
        limit = SiteConfiguration.load().wallet_chart_limit
    rows = (
        Item.objects.filter(user=user, is_used=False)
        .values('wallet__name', 'wallet__color')
        .annotate(count=Count('id'))
        .order_by('-count')
    )
    results = []
    other_count = 0
    for row in rows:
        entry = {
            'name': row['wallet__name'] or 'No Wallet',
            'color': row['wallet__color'] or OTHER_WALLET_COLOR,
            'count': row['count'],
        }
        if len(results) < limit:
            results.append(entry)
        else:
            other_count += entry['count']
    if other_count:
        results.append({'name': 'Other', 'color': OTHER_WALLET_COLOR, 'count': other_count})
    return results


def get_expiring_soon_items(user, days=None, limit=None):
    """
    Active items expiring within the next `days` days, soonest first. Each
    item gets a `.days_left` attribute (int) attached for display.

    `days=None` (the default) resolves to the admin-configured
    SiteConfiguration.expiry_threshold_days at call time rather than a
    fixed constant, so this list agrees with every other "soon expiring"
    count in the app (the Inventory filter chip, the notification
    default threshold) instead of silently using its own fixed window.
    `limit=None` similarly resolves to SiteConfiguration.expiring_soon_limit.
    """
    if days is None:
        days = SiteConfiguration.load().expiry_threshold_days
    if limit is None:
        limit = SiteConfiguration.load().expiring_soon_limit
    now = timezone.now()
    today = timezone.localtime(now).date()
    cutoff = now + timedelta(days=days)
    items = list(
        Item.objects.filter(user=user, is_used=False, expiry_date__gte=now, expiry_date__lt=cutoff)
        .select_related('wallet')
        .order_by('expiry_date')[:limit]
    )
    for item in items:
        item.days_left = (item.expiry_date - today).days
    return items


def get_next_up_items(wallets, limit=1):
    """
    Soonest-expiring active items across `wallets` (any iterable/queryset
    of Wallet), for the Inventory page's "Next Up" widget - up to `limit`
    items, soonest first, interleaved by date across every wallet rather
    than grouped per wallet. `wallets` empty means the feature is off, and
    an empty list is returned unchanged so callers don't need to check
    first. Each item gets a `.days_left` attribute attached for display.
    """
    wallet_ids = [w.id for w in wallets]
    if not wallet_ids:
        return []
    today = timezone.localtime().date()
    items = list(
        Item.objects.filter(wallet_id__in=wallet_ids, is_used=False, is_archived=False, expiry_date__gte=today)
        .select_related('wallet')
        .order_by('expiry_date', 'issue_date')[:limit]
    )
    for item in items:
        item.days_left = (item.expiry_date - today).days
    return items


def get_active_today_item(user, enabled, home_station, cutoff_time):
    """
    Picks out today's outward or return leg of a round-trip ticket (e.g. a
    daily commute) for the "Active Today" widget, purely a read - it never
    mutates anything, so it's safe to call on every page load regardless of
    whether the cutoff has passed. `home_station` is matched case-
    insensitively against Item.journey_origin/journey_destination: the
    "outward" leg is whichever candidate departs from home, the "return"
    leg is whichever arrives back at home.

    Before `cutoff_time`, the outward leg is shown (falling back to the
    return leg if only that was bought). From `cutoff_time` on, only the
    return leg is shown — the outward leg is assumed done for the day (see
    myapp.tasks.mark_expired_commute_outward_tickets, which flips its
    is_used flag around the same cutoff for bookkeeping; this function
    doesn't depend on that having run yet).

    Returns None if the widget is off, no home station is configured, or
    there's no unused, non-archived ticket valid today with both journey
    fields set.
    """
    if not enabled or not home_station:
        return None
    today = timezone.localtime().date()
    home = home_station.strip().lower()
    candidates = list(
        Item.objects.filter(user=user, is_archived=False, is_used=False, expiry_date=today)
        .exclude(journey_origin='').exclude(journey_destination='')
    )
    if not candidates:
        return None
    outward = next((i for i in candidates if i.journey_origin.strip().lower() == home), None)
    return_leg = next((i for i in candidates if i.journey_destination.strip().lower() == home), None)
    if timezone.localtime().time() < cutoff_time:
        return outward or return_leg
    return return_leg


def get_spend_stats(user):
    """
    Spending analytics for a user's transaction history (cached for 60s):

    - `total_spent`: absolute sum of all negative Transaction.value amounts
      across the user's items (i.e. money spent out of gift cards / vouchers),
      returned as a 2 d.p. string.
    - `monthly_spend`: list of {'month': 'YYYY-MM', 'amount': str} for the
      last 12 calendar months (current month included), ordered oldest-first.
      Months with no spend are included as '0.00' so the chart always has a
      complete 12-bar axis.
    - `redeemed_value`: sum of item.value (the stored face value) for all
      items owned by `user` where is_used=True and type is not 'loyaltycard',
      returned as a 2 d.p. string.  Loyalty cards are excluded because their
      `value` field typically holds points rather than a monetary amount.
    """
    cache_key = f'spend_stats:{user.id}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    now = timezone.now()

    # Total spent — sum of all negative transactions, returned as positive
    total_spent_agg = Transaction.objects.filter(
        item__user=user, value__lt=0
    ).aggregate(total=Sum('value'))
    total_spent_raw = total_spent_agg['total'] or Decimal('0')
    total_spent = abs(total_spent_raw).quantize(Decimal('0.01'))

    # Monthly spend for the last 12 calendar months
    twelve_months_ago = now - timedelta(days=365)
    monthly_rows = (
        Transaction.objects.filter(item__user=user, value__lt=0, date__gte=twelve_months_ago)
        .annotate(month=TruncMonth('date'))
        .values('month')
        .annotate(amount=Sum('value'))
        .order_by('month')
    )
    spend_by_month = {
        row['month'].strftime('%Y-%m'): abs(row['amount']).quantize(Decimal('0.01'))
        for row in monthly_rows
    }

    # Build a complete 12-month list (oldest → newest), filling gaps with 0
    current_year, current_month = now.year, now.month
    monthly_spend = []
    for i in range(11, -1, -1):
        m = current_month - i
        y = current_year
        while m <= 0:
            m += 12
            y -= 1
        label = f"{y:04d}-{m:02d}"
        monthly_spend.append({
            'month': label,
            'amount': str(spend_by_month.get(label, Decimal('0.00'))),
        })

    # Redeemed value — face value of used non-loyalty items
    redeemed_agg = Item.objects.filter(
        user=user, is_used=True
    ).exclude(type='loyaltycard').aggregate(total=Sum('value'))
    redeemed_raw = redeemed_agg['total'] or Decimal('0')
    redeemed_value = redeemed_raw.quantize(Decimal('0.01'))

    result = {
        'total_spent': str(total_spent),
        'monthly_spend': monthly_spend,
        'redeemed_value': str(redeemed_value),
    }
    cache.set(cache_key, result, 60)
    return result


def get_self_healing_stats(user):
    """
    Aggregate stats for the "Self-Healing Insights" analytics section
    (cached for 60s, same shape as get_summary_stats/get_spend_stats):

    - `corrections_taught`/`times_healed`: how many corrections this user
      has taught (myapp.scan_learning.ScanFieldCorrection rows) and how
      many times they've actually fired (Sum('times_applied') - see the
      counter added for retroactive healing; this counts every heal, live
      or retroactive, not just retroactive ones).
    - `fields_self_healing`: distinct fields with at least one correction
      that has actually fired at least once - "taught but never fired"
      doesn't count as currently self-healing anything.
    - `cross_user_patterns`: GlobalScanCorrection rows scoped to an issuer
      this user actually has items for - patterns for merchants the user
      has never dealt with don't benefit them, regardless of how many
      other users taught it.
    - `monthly_heals`: list of {'month': 'YYYY-MM', 'count': int} for the
      last 12 calendar months (current month included), sourced from
      ItemEnrichmentLog entries of enrichment_type='learned_correction' -
      the retroactive-healing side specifically, since live-scan-time
      heals aren't logged anywhere with a timestamp today. Months with no
      heals are included as 0 so the chart always has a complete axis.
    - `top_merchants`: up to 5 {'issuer': str, 'heals': int} entries,
      combining this user's own per-issuer heal counts with any relevant
      cross-user pattern's heal count for that same issuer, ordered by
      total heals descending. Issuers with zero combined heals are
      omitted.
    """
    cache_key = f'self_healing_stats:{user.id}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    personal = ScanFieldCorrection.objects.filter(user=user)
    totals = personal.aggregate(corrections_taught=Count('id'), times_healed=Sum('times_applied'))
    fields_self_healing = personal.filter(times_applied__gt=0).values('field').distinct().count()

    user_issuers = set(
        Item.objects.filter(user=user).exclude(issuer='').values_list('issuer', flat=True).distinct()
    )
    cross_user_patterns = (
        GlobalScanCorrection.objects.filter(issuer__in=user_issuers).count() if user_issuers else 0
    )

    now = timezone.now()
    twelve_months_ago = now - timedelta(days=365)
    monthly_rows = (
        ItemEnrichmentLog.objects
        .filter(item__user=user, enrichment_type='learned_correction', created_at__gte=twelve_months_ago)
        .annotate(month=TruncMonth('created_at'))
        .values('month')
        .annotate(count=Count('id'))
        .order_by('month')
    )
    heals_by_month = {row['month'].strftime('%Y-%m'): row['count'] for row in monthly_rows}
    current_year, current_month = now.year, now.month
    monthly_heals = []
    for i in range(11, -1, -1):
        m = current_month - i
        y = current_year
        while m <= 0:
            m += 12
            y -= 1
        label = f"{y:04d}-{m:02d}"
        monthly_heals.append({'month': label, 'count': heals_by_month.get(label, 0)})

    combined = {}
    for row in personal.exclude(issuer='').values('issuer').annotate(total=Sum('times_applied')).order_by():
        combined[row['issuer']] = combined.get(row['issuer'], 0) + (row['total'] or 0)
    if user_issuers:
        for row in (
            GlobalScanCorrection.objects.filter(issuer__in=user_issuers)
            .values('issuer').annotate(total=Sum('times_applied')).order_by()
        ):
            combined[row['issuer']] = combined.get(row['issuer'], 0) + (row['total'] or 0)
    top_merchants = [
        {'issuer': issuer, 'heals': total}
        for issuer, total in sorted(combined.items(), key=lambda kv: -kv[1])
        if total > 0
    ][:5]

    result = {
        'corrections_taught': totals['corrections_taught'] or 0,
        'times_healed': totals['times_healed'] or 0,
        'fields_self_healing': fields_self_healing,
        'cross_user_patterns': cross_user_patterns,
        'monthly_heals': monthly_heals,
        'top_merchants': top_merchants,
    }
    cache.set(cache_key, result, 60)
    return result


def build_expiry_calendar(user, months_ahead=None):
    """
    Returns a list of `months_ahead` month dicts (starting this month) for a
    day-grid calendar: {'label': 'July 2026', 'weeks': [[day_cell, ...], ...]}
    where each day_cell is None (padding outside the month) or a dict with
    day/date/is_today/is_past/count of items expiring that day.

    `months_ahead=None` (the default) resolves to the admin-configured
    SiteConfiguration.calendar_months_ahead at call time.
    """
    if months_ahead is None:
        months_ahead = SiteConfiguration.load().calendar_months_ahead
    today = timezone.localtime().date()
    horizon_end = today + timedelta(days=31 * months_ahead)

    rows = (
        Item.objects.filter(user=user, is_used=False, expiry_date__gte=today, expiry_date__lte=horizon_end)
        .values('expiry_date')
        .annotate(count=Count('id'))
    )
    counts_by_date = {row['expiry_date']: row['count'] for row in rows}

    cal = calendar_module.Calendar(firstweekday=0)  # Monday
    months = []
    year, month = today.year, today.month
    for _ in range(months_ahead):
        weeks = []
        for week in cal.monthdayscalendar(year, month):
            days = []
            for day_num in week:
                if day_num == 0:
                    days.append(None)
                    continue
                day_date = date_cls(year, month, day_num)
                days.append({
                    'day': day_num,
                    'date': day_date,
                    'is_today': day_date == today,
                    'is_past': day_date < today,
                    'count': counts_by_date.get(day_date, 0),
                })
            weeks.append(days)
        months.append({
            'label': date_cls(year, month, 1).strftime('%B %Y'),
            'weeks': weeks,
        })
        month += 1
        if month > 12:
            month = 1
            year += 1
    return months
