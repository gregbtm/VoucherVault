import io
import json
import zipfile

# Reverse of imports/exporters/pkpass.py's BARCODE_FORMAT_MAP. Any format we
# don't recognize falls back to 'qrcode' since that's always a safe default
# code_type for displaying the extracted redeem code.
BARCODE_FORMAT_TO_CODE_TYPE = {
    'PKBarcodeFormatQR': 'qrcode',
    'PKBarcodeFormatCode128': 'code128',
    'PKBarcodeFormatPDF417': 'pdf417',
    'PKBarcodeFormatAztec': 'azteccode',
}

# Apple's pass "style" keys — whichever one is present holds the field data.
PASS_STYLES = ('boardingPass', 'coupon', 'eventTicket', 'generic', 'storeCard')

MAX_PASS_JSON_SIZE = 1 * 1024 * 1024  # 1MB; a real pass.json is a few KB


class PkpassImportError(Exception):
    pass


def _find_field(style_fields, *keys_or_labels):
    """Search all field groups in a pass's style dict for a field whose key
    or label matches one of the given (case-insensitive) hints."""
    hints = {h.lower() for h in keys_or_labels}
    for group in ('primaryFields', 'secondaryFields', 'auxiliaryFields', 'backFields'):
        for field in style_fields.get(group, []):
            key = str(field.get('key', '')).lower()
            label = str(field.get('label', '')).lower()
            if key in hints or label in hints:
                return field.get('value')
    return None


def extract_pkpass_fields(file_bytes: bytes) -> dict:
    """
    Read a .pkpass file (just a zip containing pass.json + assets) and pull
    out the fields useful for pre-filling VoucherVault's create-item form.

    This is informational extraction only — a .pkpass's PKCS7 signature is
    never verified, since nothing here relies on it for a trust/authorization
    decision. We're reading text fields for convenience, the same way the
    OCR "Scan with AI" flow reads text out of a photo.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            try:
                info = zf.getinfo('pass.json')
            except KeyError:
                raise PkpassImportError('This does not look like a valid .pkpass file (no pass.json found).')
            if info.file_size > MAX_PASS_JSON_SIZE:
                raise PkpassImportError('pass.json inside this file is implausibly large.')
            raw = zf.read(info)
    except zipfile.BadZipFile:
        raise PkpassImportError('This does not look like a valid .pkpass file.')

    try:
        pass_dict = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise PkpassImportError('pass.json inside this file is not valid JSON.')

    style_key = next((s for s in PASS_STYLES if s in pass_dict), None)
    style_fields = pass_dict.get(style_key, {}) if style_key else {}

    barcodes = pass_dict.get('barcodes') or ([pass_dict['barcode']] if 'barcode' in pass_dict else [])
    barcode = barcodes[0] if barcodes else {}

    expiry_date = pass_dict.get('expirationDate')
    if not expiry_date:
        expiry_date = _find_field(style_fields, 'expiry', 'expires', 'expiration')
    if expiry_date and 'T' in str(expiry_date):
        expiry_date = str(expiry_date).split('T', 1)[0]

    return {
        'name': pass_dict.get('description') or '',
        'issuer': pass_dict.get('organizationName') or '',
        'redeem_code': barcode.get('message') or '',
        'code_type': BARCODE_FORMAT_TO_CODE_TYPE.get(barcode.get('format'), 'qrcode'),
        'expiry_date': expiry_date,
        'pin': _find_field(style_fields, 'pin'),
        'confidence': 1.0,
    }
