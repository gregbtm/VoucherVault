from django.utils import timezone

from .models import Item

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


def build_ics_calendar(user) -> bytes:
    """
    A minimal, read-only expiry calendar: one all-day VEVENT per active
    (not used, not archived) item with an expiry_date. This is intentionally
    basic - one-way, expiry-dates-only sync. Anything richer (other fields,
    real-time updates, two-way sync) should use the webhook system
    (see the notify app) instead of trying to extend this feed.
    """
    items = Item.objects.filter(
        user=user, is_used=False, is_archived=False, expiry_date__isnull=False,
    ).order_by('expiry_date')

    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        f'PRODID:{PRODID}',
        'CALSCALE:GREGORIAN',
        'METHOD:PUBLISH',
        'X-WR-CALNAME:VoucherVault Plus+ Expiry',
    ]

    now_stamp = timezone.now().strftime('%Y%m%dT%H%M%SZ')
    for item in items:
        description_parts = [item.get_type_display()]
        if item.issuer:
            description_parts.append(f'Issuer: {item.issuer}')
        if item.value is not None:
            description_parts.append(f'Value: {item.value} {item.currency}')

        lines.extend([
            'BEGIN:VEVENT',
            f'UID:{item.id}@vouchervault',
            f'DTSTAMP:{now_stamp}',
            f'DTSTART;VALUE=DATE:{item.expiry_date.strftime("%Y%m%d")}',
            f'SUMMARY:{_escape_text(item.name)} expires',
            f'DESCRIPTION:{_escape_text(" | ".join(description_parts))}',
            'END:VEVENT',
        ])

    lines.append('END:VCALENDAR')

    return ('\r\n'.join(_fold_line(line) for line in lines) + '\r\n').encode('utf-8')
