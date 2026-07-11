import csv
import io
from decimal import Decimal, InvalidOperation

from .utils import parse_bool, parse_date, parse_hex_color

VALID_TYPES = {'voucher', 'giftcard', 'coupon', 'loyaltycard'}


def parse(file_obj):
    """
    Parses a VoucherVault CSV backup (see imports.exporters.csv_export for
    the exact column set this expects) for full backup/restore round trips.
    """
    text = file_obj.read()
    if isinstance(text, bytes):
        text = text.decode('utf-8-sig')

    reader = csv.DictReader(io.StringIO(text))
    rows = []
    errors = []

    for line_number, raw_row in enumerate(reader, start=2):
        name = (raw_row.get('name') or '').strip()
        redeem_code = (raw_row.get('redeem_code') or '').strip()
        item_type = (raw_row.get('type') or '').strip()

        if not name or not redeem_code:
            errors.append({'row': line_number, 'message': 'Missing required "name" or "redeem_code".'})
            continue
        if item_type not in VALID_TYPES:
            errors.append({'row': line_number, 'message': f'Invalid "type" value "{item_type}".'})
            continue

        value_raw = (raw_row.get('value') or '0').strip()
        try:
            value = Decimal(value_raw.replace(',', '.')) if value_raw else Decimal('0')
        except InvalidOperation:
            errors.append({'row': line_number, 'message': f'Invalid "value" "{value_raw}", skipped.'})
            continue

        notify_days_raw = (raw_row.get('notify_days_before') or '').strip()
        notify_days_before = int(notify_days_raw) if notify_days_raw.isdigit() else None

        tags_raw = (raw_row.get('tags') or '').strip()

        rows.append({
            'type': item_type,
            'name': name,
            'issuer': (raw_row.get('issuer') or '').strip() or name,
            'redeem_code': redeem_code,
            'pin': (raw_row.get('pin') or '').strip() or None,
            'code_type': (raw_row.get('code_type') or 'qrcode').strip() or 'qrcode',
            'issue_date': parse_date(raw_row.get('issue_date')),
            'expiry_date': parse_date(raw_row.get('expiry_date')),
            'value': value,
            'value_type': (raw_row.get('value_type') or 'money').strip() or 'money',
            'currency': (raw_row.get('currency') or 'GBP').strip() or 'GBP',
            'description': (raw_row.get('description') or '').strip(),
            'notes': (raw_row.get('notes') or '').strip(),
            'wallet_name': (raw_row.get('wallet') or '').strip() or None,
            'tag_names': [t.strip() for t in tags_raw.split(',') if t.strip()],
            'is_used': parse_bool(raw_row.get('is_used')),
            'is_pinned': parse_bool(raw_row.get('is_pinned')),
            'tile_color': parse_hex_color(raw_row.get('tile_color')),
            'notify_days_before': notify_days_before,
            'logo_slug': (raw_row.get('logo_slug') or '').strip() or None,
        })

    return rows, errors
