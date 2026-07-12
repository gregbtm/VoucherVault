from abc import ABC, abstractmethod

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
        expiry_date (ISO 8601 string or None), confidence (0.0-1.0).
        code_type is one of VALID_CODE_TYPES or None if the backend can't
        or didn't try to determine the barcode symbology.
        """
        ...
