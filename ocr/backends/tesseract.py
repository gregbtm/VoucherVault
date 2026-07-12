import io
import re
from datetime import datetime

import pytesseract
from PIL import Image

from .base import OCRBackend

# Candidate redeem-code tokens: 5-20 chars, letters/digits/hyphens, at least
# one digit (filters out plain English words picked up from surrounding
# printed text).
_CODE_CANDIDATE_RE = re.compile(r'\b[A-Z0-9][A-Z0-9\-]{4,19}\b')
_HAS_DIGIT_RE = re.compile(r'\d')

_DATE_PATTERNS = [
    (re.compile(r'\b(\d{4})-(\d{1,2})-(\d{1,2})\b'), '%Y-%m-%d'),
    (re.compile(r'\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b'), '%d.%m.%Y'),
    (re.compile(r'\b(\d{1,2})[./](\d{1,2})[./](\d{2})\b'), '%d.%m.%y'),
]


class TesseractOCRBackend(OCRBackend):
    """
    Local, free OCR via the tesseract binary. Only extracts a best-guess
    redeem code from raw recognized text — it has no understanding of what
    a "merchant name" or "issuer" is, so those always come back None.
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
        confidence = (sum(confidences) / len(confidences) / 100) if confidences else 0.0

        return {
            'code': code,
            'code_type': self._guess_code_type(code) if code else None,
            'name': None,
            'issuer': None,
            'expiry_date': expiry_date,
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
