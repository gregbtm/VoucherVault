import base64
import json
import logging
import os
import time

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from myapp.models import SiteConfiguration

logger = logging.getLogger(__name__)

SAVE_URL_PREFIX = 'https://pay.google.com/gp/v/save/'
OAUTH_TOKEN_URL = 'https://oauth2.googleapis.com/token'
OAUTH_SCOPE = 'https://www.googleapis.com/auth/wallet_object.issuer'
WALLET_OBJECTS_URL = 'https://walletobjects.googleapis.com/walletobjects/v1/genericObject'

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

# Only formats Google Wallet's BarcodeType enum actually supports
# (developers.google.com/wallet/reference/rest/v1/BarcodeType) - notably no
# Code 93 or UPC-E, so those still fall back to QR since it can always
# encode the raw redeem code string.
BARCODE_TYPE_MAP = {
    'qrcode': 'QR_CODE',
    'code128': 'CODE_128',
    'pdf417': 'PDF_417',
    'azteccode': 'AZTEC',
    'codabar': 'CODABAR',
    'code39': 'CODE_39',
    'ean8': 'EAN_8',
    'ean13': 'EAN_13',
    'datamatrix': 'DATA_MATRIX',
    'interleaved2of5': 'ITF_14',
    'upca': 'UPC_A',
}
DEFAULT_BARCODE_TYPE = 'QR_CODE'
DEFAULT_TILE_COLOR = '#4154f1'


def google_wallet_enabled() -> bool:
    config = SiteConfiguration.load()
    path = config.google_wallet_service_account_key_path
    return bool(path) and bool(config.google_wallet_issuer_id) and os.path.isfile(path)


def _require_setting(config, field_name: str, env_name: str) -> str:
    value = getattr(config, field_name, None)
    if not value:
        raise RuntimeError(f'{env_name} is not set. Required for Google Wallet export.')
    return value


def _load_service_account(config):
    key_path = _require_setting(config, 'google_wallet_service_account_key_path', 'GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH')
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
    if item.code_type == 'none':
        # No barcode to scan, so surface the redeem code as plain text instead.
        text_modules.append({'id': 'code', 'header': 'Code', 'body': item.redeem_code})

    generic_object = {
        'id': f'{issuer_id}.item-{item.id}',
        'classId': class_id,
        'genericType': GENERIC_TYPE_MAP.get(item.type, DEFAULT_GENERIC_TYPE),
        'state': 'COMPLETED' if (item.is_used or item.is_archived) else 'ACTIVE',
        'header': _localized(item.name),
        'cardTitle': _localized(item.issuer or 'VoucherVault Plus+'),
        'subheader': _localized(item.get_type_display()),
        'hexBackgroundColor': tile_color,
        'textModulesData': text_modules,
    }
    # code_type "none" means the item has no scannable barcode (e.g. a gift
    # card that's just a printed number) - omit 'barcode' entirely rather
    # than encoding the redeem code as a QR fallback the card doesn't have.
    if item.code_type != 'none':
        generic_object['barcode'] = {
            'type': BARCODE_TYPE_MAP.get(item.code_type, DEFAULT_BARCODE_TYPE),
            'value': item.redeem_code,
        }
    return generic_object


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

    config = SiteConfiguration.load()
    client_email, private_key = _load_service_account(config)
    issuer_id = _require_setting(config, 'google_wallet_issuer_id', 'GOOGLE_WALLET_ISSUER_ID')
    class_id = config.google_wallet_class_id or f'{issuer_id}.vouchervault_generic'

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


def _get_access_token(client_email: str, private_key) -> str:
    """
    Exchanges the service account's signed JWT assertion for a short-lived
    OAuth2 access token (the standard Google service-account "JWT bearer"
    flow), scoped to the Wallet Objects issuer API only.
    """
    now = int(time.time())
    assertion = _sign_jwt({
        'iss': client_email,
        'scope': OAUTH_SCOPE,
        'aud': OAUTH_TOKEN_URL,
        'iat': now,
        'exp': now + 3600,
    }, private_key)
    response = requests.post(OAUTH_TOKEN_URL, data={
        'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
        'assertion': assertion,
    }, timeout=10)
    response.raise_for_status()
    return response.json()['access_token']


def update_google_wallet_object(item) -> bool:
    """
    Pushes the item's current state to an already-issued Google Wallet
    object, so a balance/expiry/name change is reflected in a pass a user
    has already saved — without this, generate_google_wallet_save_url()
    only ever affects the *next* time someone follows the save link.

    Google gives no signal for whether a given item's pass was ever
    actually saved (the save link can be regenerated on every page view
    without the user clicking it), so this doesn't track that itself -
    it just attempts the update and treats "object doesn't exist yet"
    (404) as an expected no-op rather than an error. Returns True only on
    a confirmed update; callers should treat False as "nothing to do or
    it failed" and not surface it as a hard error.
    """
    if not google_wallet_enabled():
        return False

    config = SiteConfiguration.load()
    client_email, private_key = _load_service_account(config)
    issuer_id = _require_setting(config, 'google_wallet_issuer_id', 'GOOGLE_WALLET_ISSUER_ID')
    class_id = config.google_wallet_class_id or f'{issuer_id}.vouchervault_generic'
    object_id = f'{issuer_id}.item-{item.id}'

    access_token = _get_access_token(client_email, private_key)
    response = requests.patch(
        f'{WALLET_OBJECTS_URL}/{object_id}',
        headers={'Authorization': f'Bearer {access_token}'},
        json=_build_generic_object(item, issuer_id, class_id),
        timeout=10,
    )
    if response.status_code == 404:
        logger.info('Google Wallet object %s not found (never saved) - skipping update', object_id)
        return False
    response.raise_for_status()
    return True
