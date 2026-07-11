import base64
import json
import os
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from django.conf import settings

SAVE_URL_PREFIX = 'https://pay.google.com/gp/v/save/'

# Google Wallet's Generic pass type covers loyalty cards, gift cards and
# vouchers alike with a single class, so the issuer only registers once
# instead of provisioning separate Loyalty/GiftCard/Offer classes per item
# type. See https://developers.google.com/wallet/generic
GENERIC_TYPE_MAP = {
    'loyaltycard': 'GENERIC_LOYALTY_CARD',
    'voucher': 'GENERIC_VOUCHER',
    'coupon': 'GENERIC_VOUCHER',
    'giftcard': 'GENERIC_OTHER',
}
DEFAULT_GENERIC_TYPE = 'GENERIC_TYPE_UNSPECIFIED'

# Only formats Google Wallet actually supports; everything else falls back
# to QR since it can always encode the raw redeem code string.
BARCODE_TYPE_MAP = {
    'qrcode': 'QR_CODE',
    'code128': 'CODE_128',
    'pdf417': 'PDF_417',
    'azteccode': 'AZTEC',
}
DEFAULT_BARCODE_TYPE = 'QR_CODE'
DEFAULT_TILE_COLOR = '#4154f1'


def google_wallet_enabled() -> bool:
    path = settings.GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH
    return bool(path) and bool(settings.GOOGLE_WALLET_ISSUER_ID) and os.path.isfile(path)


def _require_setting(name: str) -> str:
    value = getattr(settings, name, None)
    if not value:
        raise RuntimeError(f'{name} is not set. Required for Google Wallet export.')
    return value


def _load_service_account():
    key_path = _require_setting('GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH')
    with open(key_path, 'rb') as f:
        key_data = json.load(f)

    client_email = key_data.get('client_email')
    private_key_pem = key_data.get('private_key')
    if not client_email or not private_key_pem:
        raise RuntimeError(
            'GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH does not contain a valid service account key '
            '(missing client_email or private_key).'
        )

    private_key = serialization.load_pem_private_key(private_key_pem.encode('utf-8'), password=None)
    return client_email, private_key


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _sign_jwt(payload: dict, private_key) -> str:
    header = {'alg': 'RS256', 'typ': 'JWT'}
    signing_input = f'{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(payload).encode())}'
    signature = private_key.sign(signing_input.encode('ascii'), padding.PKCS1v15(), hashes.SHA256())
    return f'{signing_input}.{_b64url(signature)}'


def _localized(value: str) -> dict:
    return {'defaultValue': {'language': 'en-US', 'value': value}}


def _build_generic_object(item, issuer_id: str, class_id: str) -> dict:
    tile_color = item.tile_color or DEFAULT_TILE_COLOR
    if not tile_color.startswith('#'):
        tile_color = f'#{tile_color}'

    text_modules = []
    if item.type == 'giftcard':
        text_modules.append({'id': 'balance', 'header': 'Balance', 'body': f'{item.value} {item.currency}'})
    if item.expiry_date:
        text_modules.append({'id': 'expiry', 'header': 'Expires', 'body': item.expiry_date.isoformat()})

    return {
        'id': f'{issuer_id}.item-{item.id}',
        'classId': class_id,
        'genericType': GENERIC_TYPE_MAP.get(item.type, DEFAULT_GENERIC_TYPE),
        'state': 'ACTIVE',
        'header': _localized(item.name),
        'cardTitle': _localized(item.issuer or 'VoucherVault Plus+'),
        'subheader': _localized(item.get_type_display()),
        'hexBackgroundColor': tile_color,
        'barcode': {
            'type': BARCODE_TYPE_MAP.get(item.code_type, DEFAULT_BARCODE_TYPE),
            'value': item.redeem_code,
        },
        'textModulesData': text_modules,
    }


def generate_google_wallet_save_url(item) -> str:
    """
    Builds a "Save to Google Wallet" link for a single item. The pass class
    and object are embedded directly in the signed JWT, so Google creates
    (or updates) them the first time a user follows the link — no live API
    call needed to provision anything ahead of time. Raises RuntimeError if
    not configured; callers should turn that into a 503, not crash the
    request.
    """
    if not google_wallet_enabled():
        raise RuntimeError(
            'Google Wallet export is not configured (GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH not set).'
        )

    client_email, private_key = _load_service_account()
    issuer_id = _require_setting('GOOGLE_WALLET_ISSUER_ID')
    class_id = settings.GOOGLE_WALLET_CLASS_ID or f'{issuer_id}.vouchervault_generic'

    payload = {
        'iss': client_email,
        'aud': 'google',
        'typ': 'savetowallet',
        'iat': int(time.time()),
        'origins': [],
        'payload': {
            'genericClasses': [{'id': class_id}],
            'genericObjects': [_build_generic_object(item, issuer_id, class_id)],
        },
    }
    token = _sign_jwt(payload, private_key)
    return f'{SAVE_URL_PREFIX}{token}'
