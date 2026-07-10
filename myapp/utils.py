import unicodedata
import urllib.request
import urllib.error
import json
import logging
import io
import base64
import qrcode
import treepoem

logger = logging.getLogger(__name__)

def generate_username(email):
    # Normalize the email
    normalized_email = unicodedata.normalize('NFKC', email)
    # Split the email to get the username part
    username_part = normalized_email.split('@')[0]
    # Slice the username to a maximum of 150 characters
    return username_part[:150]


def get_fixer_rates(api_key):
    """
    Fetch latest exchange rates from Fixer.io (EUR base, free plan).
    Returns a dict of {currency_code: rate} or None on failure.
    """
    url = f"https://data.fixer.io/api/latest?access_key={api_key}"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # nosec B310 — URL scheme is hardcoded to http://
            data = json.loads(response.read().decode())
        if not data.get('success'):
            logger.warning("Fixer.io API error: %s", data.get('error'))
            return None
        return data.get('rates', {})
    except (urllib.error.URLError, Exception) as exc:
        logger.warning("Fixer.io request failed: %s", exc)
        return None


def _calculate_ean13_check_digit(code):
    sum_odd = sum(int(code[i]) for i in range(0, 12, 2))
    sum_even = sum(int(code[i]) for i in range(1, 12, 2))
    checksum = (sum_odd + 3 * sum_even) % 10
    return (10 - checksum) % 10


def _is_valid_ean13(code):
    if len(code) != 13 or not code.isdigit():
        return False
    return int(code[-1]) == _calculate_ean13_check_digit(code)


def generate_code_image_base64(item):
    """
    Renders the QR code / barcode image for an Item's redeem_code and
    returns (base64_string, resolved_code_type). Mirrors the logic used
    by the create-item/edit-item web views so items created through any
    entry point (form or API) get a consistent code image.
    """
    if item.code_type != "qrcode" and _is_valid_ean13(item.redeem_code):
        code_type = "ean13"
    else:
        code_type = item.code_type

    buffer = io.BytesIO()
    if code_type == "qrcode":
        qr = qrcode.make(item.redeem_code)
        qr.save(buffer)
    else:
        barcode = treepoem.generate_barcode(
            barcode_type=code_type,
            data=item.redeem_code,
            scale=2
        )
        barcode.save(buffer, 'PNG')

    return base64.b64encode(buffer.getvalue()).decode(), code_type


def convert_currency(amount, from_currency, to_currency, rates):
    """
    Convert amount from from_currency to to_currency using EUR-based rates dict.
    Returns the converted amount as float, or None if a rate is missing.
    """
    if from_currency == to_currency:
        return float(amount)
    rate_from = rates.get(from_currency)
    rate_to = rates.get(to_currency)
    if not rate_from or not rate_to:
        return None
    # rates are relative to EUR: amount_in_eur = amount / rate_from
    # amount_in_target = amount_in_eur * rate_to
    return float(amount) / rate_from * rate_to
