from myapp.models import SiteConfiguration

from .base import OCRBackend
from .claude_backend import ClaudeOCRBackend
from .openai_backend import OpenAIOCRBackend
from .tesseract import TesseractOCRBackend

BACKENDS = {
    'tesseract': TesseractOCRBackend,
    'claude': ClaudeOCRBackend,
    'openai': OpenAIOCRBackend,
}


def ocr_backend_name() -> str:
    return SiteConfiguration.load().ocr_backend


def ocr_enabled() -> bool:
    return ocr_backend_name() in BACKENDS


def get_backend() -> OCRBackend:
    try:
        backend_cls = BACKENDS[ocr_backend_name()]
    except KeyError:
        raise ValueError(f'OCR is disabled or the backend is unknown: {ocr_backend_name()!r}')
    return backend_cls()
