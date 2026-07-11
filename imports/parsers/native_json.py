import json
from decimal import Decimal, InvalidOperation

from .utils import parse_date

VALID_TYPES = {'voucher', 'giftcard', 'coupon', 'loyaltycard'}


def parse(file_obj):
    """
    Parses a VoucherVault JSON backup (see imports.exporters.json_export
    for the exact shape this expects: a JSON array of item objects, or
    {"items": [...]}) for full backup/restore round trips.
    """
    text = file_obj.read()
    if isinstance(text, bytes):
        text = text.decode('utf-8-sig')

    rows = []
    errors = []

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return rows, [{'row': None, 'message': f'Invalid JSON: {exc}'}]

    items = data.get('items', []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return rows, [{'row': None, 'message': 'Expected a JSON array of items (or {"items": [...]}).'}]

    for index, entry in enumerate(items, start=1):
        if not isinstance(entry, dict):
            errors.append({'row': index, 'message': 'Row is not a JSON object, skipped.'})
            continue

        name = (entry.get('name') or '').strip()
        redeem_code = (entry.get('redeem_code') or '').strip()
        item_type = (entry.get('type') or '').strip()

        if not name or not redeem_code:
            errors.append({'row': index, 'message': 'Missing required "name" or "redeem_code".'})
            continue
        if item_type not in VALID_TYPES:
            errors.append({'row': index, 'message': f'Invalid "type" value "{item_type}".'})
            continue

        value_raw = entry.get('value', 0)
        try:
            value = Decimal(str(value_raw).replace(',', '.')) if value_raw not in (None, '') else Decimal('0')
        except InvalidOperation:
            errors.append({'row': index, 'message': f'Invalid "value" "{value_raw}", skipped.'})
            continue

        tags = entry.get('tags') or []
        if not isinstance(tags, list):
            tags = [t.strip() for t in str(tags).split(',') if t.strip()]

        rows.append({
            'type': item_type,
            'name': name,
            'issuer': (entry.get('issuer') or '').strip() or name,
            'redeem_code': redeem_code,
            'pin': entry.get('pin') or None,
            'code_type': entry.get('code_type') or 'qrcode',
            'issue_date': parse_date(entry.get('issue_date')) if isinstance(entry.get('issue_date'), str) else None,
            'expiry_date': parse_date(entry.get('expiry_date')) if isinstance(entry.get('expiry_date'), str) else None,
            'value': value,
            'value_type': entry.get('value_type') or 'money',
            'currency': entry.get('currency') or 'GBP',
            'description': entry.get('description') or '',
            'notes': entry.get('notes') or '',
            'wallet_name': entry.get('wallet') or None,
            'tag_names': [str(t).strip() for t in tags if str(t).strip()],
            'is_used': bool(entry.get('is_used', False)),
            'is_pinned': bool(entry.get('is_pinned', False)),
            'tile_color': entry.get('tile_color') or None,
            'notify_days_before': entry.get('notify_days_before'),
            'logo_slug': entry.get('logo_slug') or None,
        })

    return rows, errors
