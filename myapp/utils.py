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


def fetch_oidc_discovery(discovery_url):
    """
    Fetch and parse an OIDC provider's .well-known/openid-configuration
    document. Returns a dict (authorization_endpoint, token_endpoint,
    userinfo_endpoint, jwks_uri, ...) on success, or {} on any failure -
    callers fall back to manually configured OIDC_OP_* endpoints in that
    case. Extracted into its own function (rather than living inline in
    settings.py) purely so it's unit-testable without reloading the
    settings module.
    """
    try:
        with urllib.request.urlopen(discovery_url, timeout=5) as response:  # nosec B310 — operator-supplied provider URL, not user input
            return json.loads(response.read().decode())
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        logger.warning(
            "OIDC discovery failed for %s (%s) - falling back to any manually configured OIDC_OP_* endpoints.",
            discovery_url, exc,
        )
        return {}


def levenshtein_distance(a: str, b: str) -> int:
    """
    Classic edit-distance DP, no external dependency - redeem codes are
    short (tens of characters), so an O(len(a)*len(b)) table is trivial
    cost. Used to flag a "possible OCR misread of an existing code"
    duplicate warning that an exact-match comparison can't catch.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous_row = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current_row = [i]
        for j, char_b in enumerate(b, start=1):
            insert_cost = current_row[j - 1] + 1
            delete_cost = previous_row[j] + 1
            substitute_cost = previous_row[j - 1] + (char_a != char_b)
            current_row.append(min(insert_cost, delete_cost, substitute_cost))
        previous_row = current_row
    return previous_row[-1]


def _calculate_ean13_check_digit(code):
    sum_odd = sum(int(code[i]) for i in range(0, 12, 2))
    sum_even = sum(int(code[i]) for i in range(1, 12, 2))
    checksum = (sum_odd + 3 * sum_even) % 10
    return (10 - checksum) % 10


def _is_valid_ean13(code):
    if len(code) != 13 or not code.isdigit():
        return False
    return int(code[-1]) == _calculate_ean13_check_digit(code)


# Our code_type values are mostly treepoem/BWIPP barcode_type names verbatim,
# but a few need translating: "codabar" reads better in a dropdown than
# BWIPP's "rationalizedCodabar", and a retail ISBN-13 barcode is printed as a
# plain EAN-13 (no dashes) so it renders with the same symbology.
_TREEPOEM_TYPE_MAP = {
    'codabar': 'rationalizedCodabar',
    'isbn13': 'ean13',
}


def generate_code_image_base64(item):
    """
    Renders the QR code / barcode image for an Item's redeem_code and
    returns (base64_string, resolved_code_type). Mirrors the logic used
    by the create-item/edit-item web views so items created through any
    entry point (form or API) get a consistent code image.

    code_type == "none" is a deliberate opt-out for items that only have
    a plain number/code and no scannable barcode at all (some gift
    cards) - returns (None, "none") rather than generating anything, and
    every renderer/exporter (view-item.html, the .pkpass and Google
    Wallet exporters) treats that as "show the code as text, don't try
    to render a barcode".
    """
    if item.code_type == "none":
        return None, "none"

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
            barcode_type=_TREEPOEM_TYPE_MAP.get(code_type, code_type),
            data=item.redeem_code,
            scale=2
        )
        barcode.save(buffer, 'PNG')

    return base64.b64encode(buffer.getvalue()).decode(), code_type


_CH_BAD_STATUSES = frozenset({
    'dissolved', 'liquidation', 'administration', 'receivership',
    'converted-closed', 'voluntary-arrangement',
})


def check_companies_house_status(issuer_name: str, api_key: str) -> dict | None:
    """
    Look up issuer_name in Companies House and return a dict:
        {'company_name': ..., 'company_status': ..., 'company_number': ...}
    or None if no confident match is found or the request fails.

    A match is accepted when the issuer name appears (case-insensitively) in
    the top search result's company name, or vice versa — conservative to
    avoid false positives from common words.  Only the first result is
    checked; Companies House returns results ranked by relevance.
    """
    if not api_key or not issuer_name:
        return None

    import base64 as _b64
    query = issuer_name.strip()
    url = f"https://api.company-information.service.gov.uk/search/companies?q={urllib.request.quote(query)}&items_per_page=5"
    credentials = _b64.b64encode(f"{api_key}:".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {credentials}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:  # nosec B310
            data = json.loads(response.read().decode())
    except (urllib.error.URLError, Exception) as exc:
        logger.warning("Companies House request failed for %r: %s", issuer_name, exc)
        return None

    items = data.get('items') or []
    if not items:
        return None

    best = items[0]
    ch_name = (best.get('title') or best.get('company_name') or '').lower()
    query_lower = query.lower()

    # Accept if either name contains the other (simple containment check)
    if query_lower not in ch_name and ch_name not in query_lower:
        return None

    return {
        'company_name': best.get('title') or best.get('company_name'),
        'company_status': (best.get('company_status') or 'unknown').lower(),
        'company_number': best.get('company_number'),
    }


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
