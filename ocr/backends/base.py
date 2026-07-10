from abc import ABC, abstractmethod


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
        """
        ...
