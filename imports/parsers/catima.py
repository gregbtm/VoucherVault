import csv
import io
from decimal import Decimal, InvalidOperation

from .utils import parse_bool, parse_date, parse_hex_color

CATIMA_CODE_TYPE_MAP = {
    'QR_CODE': 'qrcode',
    'AZTEC': 'azteccode',
    'CODABAR': 'codabar',
    'CODE_39': 'code39',
    'CODE_93': 'code93',
    'CODE_128': 'code128',
    'DATA_MATRIX': 'datamatrix',
    'EAN_8': 'ean8',
    'EAN_13': 'ean13',
    'ITF': 'interleaved2of5',
    'PDF_417': 'pdf417',
    'UPC_A': 'upca',
    'UPC_E': 'upce',
}


def _map_code_type(raw):
    if not raw:
        return 'qrcode'
    return CATIMA_CODE_TYPE_MAP.get(raw.strip().upper(), 'qrcode')


def parse(file_obj):
    """
    Parses a Catima CSV export. Expected columns:
    Group, Description, Note, Card Number, EAN Barcode ID, Card Type,
    Expiry, Balance, Balance Type, Colour, Star

    Returns (rows, errors) where rows are normalized dicts ready to become
    Items, and errors are [{"row": <line>, "message": <str>}].
    """
    text = file_obj.read()
    if isinstance(text, bytes):
        text = text.decode('utf-8-sig')

    reader = csv.DictReader(io.StringIO(text))
    rows = []
    errors = []

    for line_number, raw_row in enumerate(reader, start=2):  # header is line 1
        name = (raw_row.get('Description') or '').strip()
        redeem_code = (raw_row.get('Card Number') or raw_row.get('EAN Barcode ID') or '').strip()

        if not name or not redeem_code:
            errors.append({
                'row': line_number,
                'message': 'Missing required "Description" or "Card Number"/"EAN Barcode ID".',
            })
            continue

        balance_raw = (raw_row.get('Balance') or '').strip()
        try:
            balance = Decimal(balance_raw.replace(',', '.')) if balance_raw else Decimal('0')
        except InvalidOperation:
            errors.append({'row': line_number, 'message': f'Invalid balance "{balance_raw}", skipped.'})
            continue

        if balance > 0:
            item_type = 'giftcard'
            value = balance
        else:
            item_type = 'loyaltycard'
            value = Decimal('0')

        balance_type = (raw_row.get('Balance Type') or '').strip().upper()
        currency = balance_type if len(balance_type) == 3 and balance_type.isalpha() else 'GBP'

        rows.append({
            'type': item_type,
            'name': name,
            'issuer': name,
            'redeem_code': redeem_code,
            'code_type': _map_code_type(raw_row.get('Card Type')),
            'expiry_date': parse_date(raw_row.get('Expiry')),
            'value': value,
            'value_type': 'money',
            'currency': currency,
            'notes': (raw_row.get('Note') or '').strip(),
            'wallet_name': (raw_row.get('Group') or '').strip() or None,
            'tag_names': [],
            'tile_color': parse_hex_color(raw_row.get('Colour')),
            'is_pinned': parse_bool(raw_row.get('Star')),
        })

    return rows, errors
