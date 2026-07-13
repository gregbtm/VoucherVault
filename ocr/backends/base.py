import re
from abc import ABC, abstractmethod

from myapp.models import CURRENCY_CHOICES

# Kept in sync with the <select id="code_type"> options in
# create-item.html/edit-item.html. A vision backend's code_type guess is
# only ever useful to the frontend if it's one of these - anything else
# would leave the <select> with nothing selected, so callers must validate
# against this set before returning a code_type.
VALID_CODE_TYPES = {
    'qrcode', 'none', 'ean13', 'ean8', 'code128', 'code39', 'code93',
    'codabar', 'upca', 'upce', 'isbn13', 'issn', 'pdf417', 'datamatrix',
    'azteccode', 'interleaved2of5',
}

# Kept in sync with Item.currency's choices - a vision backend's currency
# guess is only useful to the frontend if it's a code the <select> actually
# offers.
VALID_CURRENCIES = {code for code, _ in CURRENCY_CHOICES}

_JSON_FENCE_RE = re.compile(r'^\s*```[a-zA-Z]*\s*\n?|\n?\s*```\s*$')


def strip_json_fences(text: str) -> str:
    """
    Vision models are told to respond with ONLY a JSON object, but
    sometimes wrap it in a markdown code fence anyway (a well-documented
    quirk, especially with smaller/cheaper models) - without this, a
    fenced-but-otherwise-perfectly-correct response fails json.loads() and
    gets silently treated identically to "nothing could be read", which is
    indistinguishable from an actual extraction failure to the end user.
    """
    return _JSON_FENCE_RE.sub('', text).strip()


def parse_float_or_none(value) -> float | None:
    """Best-effort numeric coercion for the "value" field - a vision model
    occasionally returns a currency-formatted string despite instructions
    (e.g. "50.00" as a JSON string, or with a stray currency symbol)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        cleaned = re.sub(r'[^0-9.\-]', '', str(value))
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


class OCRBackend(ABC):
    """
    Extracts a redeem code (and, where possible, other item fields) from a
    photo of a physical voucher/coupon/loyalty card. Implementations must
    never raise on a "nothing found" result — return an all-None payload
    with confidence 0.0 instead. Raising is reserved for backend
    unavailability (e.g. the tesseract binary isn't installed).
    """

    @abstractmethod
    def extract(self, image_bytes: bytes, media_type: str) -> dict:
        """
        Returns a dict with keys: code, code_type, name, issuer,
        expiry_date (ISO 8601 string or None), pin, value (float or None),
        currency, card_number, confidence (0.0-1.0). code_type is one of
        VALID_CODE_TYPES or None if the backend can't or didn't try to
        determine the barcode symbology. currency is one of
        VALID_CURRENCIES or None.
        """
        ...
