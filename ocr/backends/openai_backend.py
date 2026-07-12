import base64
import json
import logging

from openai import OpenAI

from myapp.models import SiteConfiguration

from .base import OCRBackend, VALID_CODE_TYPES

logger = logging.getLogger(__name__)

DEFAULT_MODEL = 'gpt-4o-mini'

_CODE_TYPE_OPTIONS = ', '.join(sorted(VALID_CODE_TYPES - {'none'}))

_PROMPT = (
    'This is a photo of a physical voucher, coupon, gift card, or loyalty '
    'card. Extract whatever of the following you can confidently read: '
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


class OpenAIOCRBackend(OCRBackend):
    """
    Vision-based extraction via the OpenAI API. Mirrors ClaudeOCRBackend -
    same prompt, same response shape - so either can be selected via
    OCR_BACKEND without any other code caring which one is active.
    """

    def __init__(self):
        config = SiteConfiguration.load()
        api_key = config.openai_api_key
        if not api_key:
            raise RuntimeError(
                'OPENAI_API_KEY is not set. Required when '
                'OCR_BACKEND=openai.'
            )
        self.client = OpenAI(api_key=api_key)
        self.model = config.openai_ocr_model or DEFAULT_MODEL

    def extract(self, image_bytes: bytes, media_type: str) -> dict:
        empty = {'code': None, 'code_type': None, 'name': None, 'issuer': None, 'expiry_date': None, 'confidence': 0.0}

        image_b64 = base64.standard_b64encode(image_bytes).decode()
        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=256,
            timeout=20,
            messages=[{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': _PROMPT},
                    {
                        'type': 'image_url',
                        'image_url': {'url': f'data:{media_type};base64,{image_b64}'},
                    },
                ],
            }],
        )

        text = response.choices[0].message.content or ''
        try:
            result = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            logger.warning('OpenAI OCR response was not valid JSON: %r', text)
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
