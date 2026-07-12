import calendar as calendar_module
from datetime import date as date_cls
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone

from .models import Item, SiteConfiguration

CALENDAR_MONTHS_AHEAD = 3
EXPIRING_SOON_DAYS = 7
EXPIRING_SOON_LIMIT = 10
WALLET_CHART_LIMIT = 8
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
    """
    now = timezone.now()
    today = now.date()
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

    return {
        'total_items': active_items.count(),
        'used_items': Item.objects.filter(user=user, is_used=True).count(),
        'expired_items': active_items.filter(expiry_date__lt=today).count(),
        'expiring_7_days': active_items.filter(expiry_date__gte=today, expiry_date__lt=week_cutoff).count(),
        'expiring_30_days': active_items.filter(expiry_date__gte=today, expiry_date__lt=soon_cutoff).count(),
        'by_type': list(active_items.values('type').annotate(count=Count('id')).order_by('type')),
        'by_wallet': get_items_by_wallet(user, limit=WALLET_CHART_LIMIT),
        'value_by_currency': currency_totals(valued_items.filter(expiry_date__gte=today)),
        'at_risk_value_by_currency': currency_totals(
            valued_items.filter(expiry_date__gte=today, expiry_date__lt=soon_cutoff)
        ),
    }


def get_expiry_timeline(user, months_ahead=CALENDAR_MONTHS_AHEAD):
    """
    Items grouped by ISO expiry date (sparse — only dates with items),
    for the analytics API's calendar feed.
    """
    today = timezone.now().date()
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


def get_items_by_wallet(user, limit=WALLET_CHART_LIMIT):
    """
    Returns [{'name': str, 'color': str, 'count': int}, ...] for the user's
    active (not-used) items, sorted by count desc. Wallet-less items are
    grouped under "No Wallet"; beyond `limit` distinct wallets, the rest
    fold into "Other" rather than generating an unbounded colour cycle.
    """
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


def get_expiring_soon_items(user, days=EXPIRING_SOON_DAYS, limit=EXPIRING_SOON_LIMIT):
    """
    Active items expiring within the next `days` days, soonest first. Each
    item gets a `.days_left` attribute (int) attached for display.
    """
    now = timezone.now()
    today = now.date()
    cutoff = now + timedelta(days=days)
    items = list(
        Item.objects.filter(user=user, is_used=False, expiry_date__gte=now, expiry_date__lt=cutoff)
        .select_related('wallet')
        .order_by('expiry_date')[:limit]
    )
    for item in items:
        item.days_left = (item.expiry_date - today).days
    return items


def build_expiry_calendar(user, months_ahead=CALENDAR_MONTHS_AHEAD):
    """
    Returns a list of `months_ahead` month dicts (starting this month) for a
    day-grid calendar: {'label': 'July 2026', 'weeks': [[day_cell, ...], ...]}
    where each day_cell is None (padding outside the month) or a dict with
    day/date/is_today/is_past/count of items expiring that day.
    """
    today = timezone.now().date()
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
