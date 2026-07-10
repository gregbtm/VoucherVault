import re
from datetime import date, datetime

HEX_COLOR_RE = re.compile(r'^#(?:[0-9a-fA-F]{3}){1,2}$')


def parse_date(value):
    """Parses a handful of common date formats; returns a date or None."""
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y', '%Y/%m/%d'):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes', 'y')


def parse_hex_color(value):
    if not value:
        return None
    value = value.strip()
    return value if HEX_COLOR_RE.match(value) else None


def parse_decimal_or_none(value):
    if value in (None, ''):
        return None
    try:
        return str(value).strip().replace(',', '.')
    except (TypeError, ValueError):
        return None
