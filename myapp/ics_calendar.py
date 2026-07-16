from django.urls import reverse
from django.utils import timezone

from .models import Item, SiteConfiguration

PRODID = '-//VoucherVault Plus+//Expiry Calendar//EN'
LINE_MAX_OCTETS = 75


def _escape_text(value: str) -> str:
    """RFC5545 §3.3.11 TEXT escaping."""
    return (
        value.replace('\\', '\\\\')
        .replace(';', '\\;')
        .replace(',', '\\,')
        .replace('\n', '\\n')
    )


def _fold_line(line: str) -> str:
    """RFC5545 §3.1 line folding: continuation lines start with a single space, max 75 octets per physical line."""
    if len(line.encode('utf-8')) <= LINE_MAX_OCTETS:
        return line
    folded = []
    current = ''
    for char in line:
        candidate = current + char
        limit = LINE_MAX_OCTETS if not folded else LINE_MAX_OCTETS - 1
        if len(candidate.encode('utf-8')) > limit:
            folded.append(current)
            current = char
        else:
            current = candidate
    if current:
        folded.append(current)
    return '\r\n '.join(folded)


def build_ics_calendar(user, request=None) -> bytes:
    """
    A read-only expiry calendar: one all-day VEVENT per active (not used,
    not archived) item with an expiry_date. One-way, real-time updates
    only happen the next time a subscribed calendar app re-fetches the
    feed - two-way sync should use the webhook system (see the notify
    app) instead of trying to extend this feed.

    Deliberately never includes redeem_code, pin, or card_number: this
    feed is designed to be subscribed to from a phone's native calendar
    app, which typically means Google/Apple/Outlook silently sync every
    field of every event to their own cloud - putting an actual
    redeemable code there would leak it somewhere entirely outside
    VoucherVault's control. Everything else (issuer, value, wallet,
    tags, notes, a link back to the item) is fair game.

    `request` is optional (existing callers/tests that already have one
    should pass it) - only used to build an absolute URL back to the
    item; the feed is still fully valid without it, just without a URL
    property on each event.
    """
    items = Item.objects.filter(
        user=user, is_used=False, is_archived=False, expiry_date__isnull=False,
    ).select_related('wallet').prefetch_related('tags').order_by('expiry_date')

    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        f'PRODID:{PRODID}',
        'CALSCALE:GREGORIAN',
        'METHOD:PUBLISH',
        'X-WR-CALNAME:VoucherVault Plus+ Expiry',
    ]

    default_threshold = SiteConfiguration.load().expiry_threshold_days
    now_stamp = timezone.now().strftime('%Y%m%dT%H%M%SZ')
    for item in items:
        description_parts = [item.get_type_display()]
        if item.issuer:
            description_parts.append(f'Issuer: {item.issuer}')
        if item.value is not None:
            description_parts.append(f'Value: {item.value} {item.currency}')
        if item.wallet:
            description_parts.append(f'Wallet: {item.wallet.name}')
        if item.balance_check_url:
            description_parts.append(f'Balance check: {item.balance_check_url}')
        if item.notes:
            description_parts.append(f'Notes: {item.notes}')

        lines.extend([
            'BEGIN:VEVENT',
            f'UID:{item.id}@vouchervault',
            f'DTSTAMP:{now_stamp}',
            f'DTSTART;VALUE=DATE:{item.expiry_date.strftime("%Y%m%d")}',
            f'SUMMARY:{_escape_text(item.name)} expires',
            f'DESCRIPTION:{_escape_text(" | ".join(description_parts))}',
        ])

        if item.wallet:
            # Not a physical place, but the closest RFC5545 property for
            # "where this belongs" - most calendar apps surface LOCATION
            # prominently, giving the wallet a second, glanceable home.
            lines.append(f'LOCATION:{_escape_text(item.wallet.name)}')

        tag_names = [tag.name for tag in item.tags.all()]
        if tag_names:
            lines.append(f'CATEGORIES:{",".join(_escape_text(name) for name in tag_names)}')

        if request is not None:
            item_url = request.build_absolute_uri(reverse('view_item', args=[item.id]))
            lines.append(f'URL:{item_url}')

        threshold = item.notify_days_before if item.notify_days_before is not None else default_threshold
        lines.extend([
            'BEGIN:VALARM',
            'ACTION:DISPLAY',
            f'DESCRIPTION:{_escape_text(item.name)} expires soon',
            f'TRIGGER:-P{threshold}D',
            'END:VALARM',
        ])

        lines.append('END:VEVENT')

    lines.append('END:VCALENDAR')

    return ('\r\n'.join(_fold_line(line) for line in lines) + '\r\n').encode('utf-8')
