import io
import re
from datetime import datetime

import pytesseract
from PIL import Image

from .base import OCRBackend, sanitize_url

# Candidate redeem-code tokens: 5-20 chars, letters/digits/hyphens, at least
# one digit (filters out plain English words picked up from surrounding
# printed text).
_CODE_CANDIDATE_RE = re.compile(r'\b[A-Z0-9][A-Z0-9\-]{4,19}\b')
_HAS_DIGIT_RE = re.compile(r'\d')
# Kept in sync with the alphanumeric branch of scanner.js's
# guessCodeTypeFromValue() - matches the same Code 39-safe character set.
_CODE39_SAFE_RE = re.compile(r'^[A-Z0-9 \-.$/+%]+$')

_DATE_PATTERNS = [
    (re.compile(r'\b(\d{4})-(\d{1,2})-(\d{1,2})\b'), '%Y-%m-%d'),
    (re.compile(r'\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b'), '%d.%m.%Y'),
    (re.compile(r'\b(\d{1,2})[./](\d{1,2})[./](\d{2})\b'), '%d.%m.%y'),
]

# "PIN 1234", "PIN: 1234", "PIN CODE 1234" - a label immediately followed
# by a short digit run, same layout as the labeled boxes on most gift
# card screenshots (e.g. "PIN" / "GIFT CARD CODE" bounding boxes).
_PIN_RE = re.compile(r'\bPIN\b[: ]*(?:CODE[: ]*)?(\d{3,8})\b', re.IGNORECASE)
# A currency symbol/code next to a decimal amount, either order -
# "£50.00", "50.00 GBP", "USD 25.99".
_VALUE_RE = re.compile(
    r'(?:[£$€]|GBP|USD|EUR)\s*(\d+(?:[.,]\d{2})?)|(\d+(?:[.,]\d{2})?)\s*(?:[£$€]|GBP|USD|EUR)',
    re.IGNORECASE,
)
_CURRENCY_SYMBOL_MAP = {'£': 'GBP', '$': 'USD', '€': 'EUR'}
_CURRENCY_RE = re.compile(r'[£$€]|\b(?:GBP|USD|EUR)\b', re.IGNORECASE)
# A literal http(s) URL in the OCR'd text - some cards print their own
# balance-check link. Deliberately requires the scheme rather than
# guessing at a bare domain: OCR noise makes bare-domain guessing on
# plain text far less reliable than it is for a vision model that can
# actually see the card's layout.
_URL_RE = re.compile(r'https?://\S+', re.IGNORECASE)


class TesseractOCRBackend(OCRBackend):
    """
    Local, free OCR via the tesseract binary. Best-effort regex guesses
    against the raw recognized text for the redeem code, expiry date, PIN,
    value/currency (when a label or currency symbol sits right next to
    them), and a balance-check URL (when a literal http(s):// link is
    printed on the card) - no vision understanding, so "merchant name"
    and "issuer" always come back None, card_number is never guessed
    (nothing reliably distinguishes it from the redeem code in plain OCR
    text), and logo_slug is never guessed either (identifying a brand
    from its logo needs actual vision, not just text recognition).
    """

    def __init__(self):
        try:
            pytesseract.get_tesseract_version()
        except pytesseract.TesseractNotFoundError as exc:
            raise RuntimeError(
                'The tesseract binary is not installed. Install the '
                '"tesseract-ocr" system package or switch OCR_BACKEND to '
                '"claude".'
            ) from exc

    def extract(self, image_bytes: bytes, media_type: str) -> dict:
        image = Image.open(io.BytesIO(image_bytes))
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)

        words = []
        confidences = []
        for text, conf in zip(data['text'], data['conf']):
            text = text.strip()
            conf = float(conf)
            if text and conf >= 0:
                words.append(text)
                confidences.append(conf)

        full_text = ' '.join(words)
        code = self._guess_code(full_text)
        expiry_date = self._guess_expiry_date(full_text)
        pin = self._guess_pin(full_text, code)
        value, currency = self._guess_value_and_currency(full_text)
        balance_check_url = self._guess_balance_check_url(full_text)
        confidence = (sum(confidences) / len(confidences) / 100) if confidences else 0.0

        return {
            'code': code,
            'code_type': self._guess_code_type(code) if code else None,
            'name': None,
            'issuer': None,
            'expiry_date': expiry_date,
            'pin': pin,
            'value': value,
            'currency': currency,
            'card_number': None,
            'logo_slug': None,
            'balance_check_url': balance_check_url,
            'confidence': round(confidence, 2) if code else 0.0,
        }

    def _guess_code(self, text: str) -> str | None:
        candidates = _CODE_CANDIDATE_RE.findall(text.upper())
        candidates = [c for c in candidates if _HAS_DIGIT_RE.search(c)]
        if not candidates:
            return None
        # Prefer tokens closest to a typical 8-12 char voucher code length.
        return min(candidates, key=lambda c: abs(len(c) - 10))

    def _guess_code_type(self, code: str) -> str | None:
        """
        Tesseract has no vision understanding of what's on the card, so this
        is a best-effort guess from the shape of the extracted code alone -
        same heuristic used client-side in scanner.js for manually typed
        codes. Always worth less than an actual barcode scan.
        """
        if code.isdigit():
            length = len(code)
            if length == 8:
                return 'ean8'
            if length == 12:
                return 'upca'
            if length == 13:
                return 'ean13'
            if length in (6, 7):
                return 'upce'
            return 'interleaved2of5' if length % 2 == 0 else 'code128'
        if _CODE39_SAFE_RE.match(code):
            return 'code39'
        return 'code128'

    def _guess_expiry_date(self, text: str) -> str | None:
        for pattern, fmt in _DATE_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            try:
                return datetime.strptime(match.group(0), fmt).date().isoformat()
            except ValueError:
                continue
        return None

    def _guess_pin(self, text: str, code: str | None) -> str | None:
        """Looks for a 'PIN' label immediately followed by digits - cheap
        and only matches when the layout is that explicit, so it never
        fires on the redeem code itself (which _PIN_RE's \\bPIN\\b prefix
        won't match unless that literal word is printed on the card)."""
        match = _PIN_RE.search(text)
        if not match:
            return None
        pin = match.group(1)
        return pin if pin != code else None

    def _guess_value_and_currency(self, text: str) -> tuple[float | None, str | None]:
        """A currency symbol/code directly beside a decimal amount, in
        either order - e.g. '£50.00' or '50.00 GBP'. Returns (None, None)
        if no such pairing is found; a bare number or bare symbol alone
        isn't confident enough to guess from."""
        match = _VALUE_RE.search(text)
        if not match:
            return None, None
        amount_text = match.group(1) or match.group(2)
        try:
            value = float(amount_text.replace(',', '.'))
        except ValueError:
            return None, None
        symbol_match = _CURRENCY_RE.search(match.group(0))
        currency = None
        if symbol_match:
            token = symbol_match.group(0).upper()
            currency = _CURRENCY_SYMBOL_MAP.get(symbol_match.group(0), token if token in ('GBP', 'USD', 'EUR') else None)
        return value, currency

    def _guess_balance_check_url(self, text: str) -> str | None:
        """The first http(s) URL found in the OCR'd text, if any - tesseract
        sometimes splits a URL across word boundaries with stray spaces,
        so this is a best-effort match rather than a guarantee."""
        match = _URL_RE.search(text)
        return sanitize_url(match.group(0)) if match else None
