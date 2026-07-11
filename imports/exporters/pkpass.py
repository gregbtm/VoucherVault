import hashlib
import io
import json
import os
import zipfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import Encoding, pkcs7, pkcs12
from PIL import Image

# Only formats PassKit actually supports; everything else falls back to QR
# since it can always encode the raw redeem code string.
BARCODE_FORMAT_MAP = {
    'qrcode': 'PKBarcodeFormatQR',
    'code128': 'PKBarcodeFormatCode128',
    'pdf417': 'PKBarcodeFormatPDF417',
    'azteccode': 'PKBarcodeFormatAztec',
}
DEFAULT_BARCODE_FORMAT = 'PKBarcodeFormatQR'
DEFAULT_TILE_COLOR = '#4154f1'

# Loyalty and gift cards use Apple's "storeCard" style (balance/member id
# front and center); vouchers/coupons use "coupon".
STORE_CARD_TYPES = {'loyaltycard', 'giftcard'}


def pkpass_enabled() -> bool:
    path = os.environ.get('PKPASS_CERT_PATH')
    return bool(path) and os.path.isfile(path)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f'{name} is not set. Required for Apple Wallet export.')
    return value


def _load_signing_materials():
    cert_path = _require_env('PKPASS_CERT_PATH')
    wwdr_path = _require_env('PKPASS_WWDR_CERT_PATH')
    password = os.environ.get('PKPASS_CERT_PASSWORD') or None

    with open(cert_path, 'rb') as f:
        private_key, certificate, _extra_certs = pkcs12.load_key_and_certificates(
            f.read(), password.encode() if password else None
        )
    if private_key is None or certificate is None:
        raise RuntimeError('PKPASS_CERT_PATH does not contain both a certificate and a private key.')

    with open(wwdr_path, 'rb') as f:
        wwdr_data = f.read()
    try:
        wwdr_cert = x509.load_pem_x509_certificate(wwdr_data)
    except ValueError:
        wwdr_cert = x509.load_der_x509_certificate(wwdr_data)

    return private_key, certificate, wwdr_cert


def _hex_to_rgb_css(hex_color: str) -> str:
    hex_color = hex_color.lstrip('#')
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    return f'rgb({r}, {g}, {b})'


def _build_pass_json(item) -> dict:
    style = 'storeCard' if item.type in STORE_CARD_TYPES else 'coupon'
    fields = {'primaryFields': [], 'secondaryFields': [], 'auxiliaryFields': [], 'backFields': []}

    if item.type == 'giftcard':
        fields['primaryFields'].append({'key': 'balance', 'label': 'Balance', 'value': f'{item.value} {item.currency}'})
    elif item.type == 'loyaltycard':
        fields['primaryFields'].append({'key': 'member', 'label': item.issuer or 'Member', 'value': item.name})
    else:
        fields['primaryFields'].append({'key': 'offer', 'label': item.issuer or 'Offer', 'value': item.name})

    if item.issuer:
        fields['secondaryFields'].append({'key': 'issuer', 'label': 'Issuer', 'value': item.issuer})
    if item.expiry_date:
        fields['auxiliaryFields'].append({'key': 'expiry', 'label': 'Expires', 'value': item.expiry_date.isoformat()})
    if item.notes:
        fields['backFields'].append({'key': 'notes', 'label': 'Notes', 'value': item.notes})
    if item.pin:
        fields['backFields'].append({'key': 'pin', 'label': 'PIN', 'value': item.pin})

    pass_dict = {
        'formatVersion': 1,
        'passTypeIdentifier': _require_env('PKPASS_PASS_TYPE_ID'),
        'teamIdentifier': _require_env('PKPASS_TEAM_ID'),
        'organizationName': os.environ.get('PKPASS_ORGANIZATION_NAME', 'VoucherVault Plus+'),
        'serialNumber': str(item.id),
        'description': item.name,
        'barcodes': [{
            'format': BARCODE_FORMAT_MAP.get(item.code_type, DEFAULT_BARCODE_FORMAT),
            'message': item.redeem_code,
            'messageEncoding': 'iso-8859-1',
        }],
        style: fields,
    }
    pass_dict['backgroundColor'] = _hex_to_rgb_css(item.tile_color or DEFAULT_TILE_COLOR)
    return pass_dict


def _generate_icon_png(size: int, hex_color: str) -> bytes:
    image = Image.new('RGB', (size, size), color=hex_color)
    buf = io.BytesIO()
    image.save(buf, format='PNG')
    return buf.getvalue()


def generate_pkpass(item) -> bytes:
    """
    Builds a signed .pkpass bundle for a single item. Raises RuntimeError
    with a clear message if Apple Wallet export isn't fully configured —
    callers should turn that into a 503, not crash the request.
    """
    if not pkpass_enabled():
        raise RuntimeError('Apple Wallet export is not configured (PKPASS_CERT_PATH not set).')

    private_key, certificate, wwdr_cert = _load_signing_materials()
    tile_color = item.tile_color or DEFAULT_TILE_COLOR

    files = {
        'pass.json': json.dumps(_build_pass_json(item)).encode('utf-8'),
        'icon.png': _generate_icon_png(29, tile_color),
        'icon@2x.png': _generate_icon_png(58, tile_color),
    }
    # SHA-1 here is Apple's PassKit manifest format, not a security control.
    manifest = {name: hashlib.sha1(content, usedforsecurity=False).hexdigest() for name, content in files.items()}
    manifest_bytes = json.dumps(manifest).encode('utf-8')

    signature = (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(manifest_bytes)
        .add_signer(certificate, private_key, hashes.SHA256())
        .add_certificate(wwdr_cert)
        .sign(Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature, pkcs7.PKCS7Options.Binary])
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
        zf.writestr('manifest.json', manifest_bytes)
        zf.writestr('signature', signature)

    return buffer.getvalue()
