import base64
import json
import logging

import anthropic

from myapp.models import SiteConfiguration

from .base import OCRBackend, VALID_CODE_TYPES

logger = logging.getLogger(__name__)

DEFAULT_MODEL = 'claude-sonnet-5'

_CODE_TYPE_OPTIONS = ', '.join(sorted(VALID_CODE_TYPES - {'none'}))

_PROMPT = (
    'This image shows a voucher, coupon, gift card, or loyalty card - it '
    'may be a photo of a physical card, or a screenshot (an emailed gift '
    'card, a retailer app screen, a digital wallet pass, a confirmation '
    'page). Extract whatever of the following you can confidently read: '
    'the redeem/reference code printed or barcoded on it, the merchant or '
    'brand name, the issuer (if different from the merchant), and the '
    'expiry date. If a barcode or QR code is visible next to the code, '
    'also identify its symbology. Respond with ONLY a JSON object, no '
    'other text, in exactly this shape:\n'
    '{"code": "...", "code_type": "...", "name": "...", "issuer": "...", '
    '"expiry_date": "YYYY-MM-DD", "confidence": 0.0}\n'
    f'"code_type" must be exactly one of: {_CODE_TYPE_OPTIONS}, "none" '
    '(if the code is a plain printed number with no separate scannable '
    'barcode at all), or null (if you cannot tell). Use null for any '
    'other field you cannot confidently determine. "confidence" is your '
    'own estimate (0.0-1.0) of how reliable the "code" extraction is.'
)


class ClaudeOCRBackend(OCRBackend):
    """
    Vision-based extraction via the Claude API. Far more capable than local
    OCR — can identify the merchant name and issuer, not just raw text —
    at the cost of an API call per scan.
    """

    def __init__(self):
        config = SiteConfiguration.load()
        api_key = config.anthropic_api_key
        if not api_key:
            raise RuntimeError(
                'ANTHROPIC_API_KEY is not set. Required when '
                'OCR_BACKEND=claude.'
            )
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = config.anthropic_ocr_model or DEFAULT_MODEL

    def extract(self, image_bytes: bytes, media_type: str) -> dict:
        empty = {'code': None, 'code_type': None, 'name': None, 'issuer': None, 'expiry_date': None, 'confidence': 0.0}

        image_b64 = base64.standard_b64encode(image_bytes).decode()
        message = self.client.messages.create(
            model=self.model,
            max_tokens=256,
            timeout=20,
            messages=[{
                'role': 'user',
                'content': [
                    {
                        'type': 'image',
                        'source': {'type': 'base64', 'media_type': media_type, 'data': image_b64},
                    },
                    {'type': 'text', 'text': _PROMPT},
                ],
            }],
        )

        text = ''.join(block.text for block in message.content if block.type == 'text')
        try:
            result = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            logger.warning('Claude OCR response was not valid JSON: %r', text)
            return empty

        code_type = result.get('code_type') or None
        if code_type not in VALID_CODE_TYPES:
            code_type = None

        return {
            'code': result.get('code') or None,
            'code_type': code_type,
            'name': result.get('name') or None,
            'issuer': result.get('issuer') or None,
            'expiry_date': result.get('expiry_date') or None,
            'confidence': float(result.get('confidence') or 0.0),
        }
