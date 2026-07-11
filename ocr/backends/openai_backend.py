import base64
import json
import logging

from django.conf import settings
from openai import OpenAI

from .base import OCRBackend

logger = logging.getLogger(__name__)

DEFAULT_MODEL = 'gpt-4o-mini'

_PROMPT = (
    'This is a photo of a physical voucher, coupon, gift card, or loyalty '
    'card. Extract whatever of the following you can confidently read: '
    'the redeem/reference code printed or barcoded on it, the merchant or '
    'brand name, the issuer (if different from the merchant), and the '
    'expiry date. Respond with ONLY a JSON object, no other text, in '
    'exactly this shape:\n'
    '{"code": "...", "name": "...", "issuer": "...", '
    '"expiry_date": "YYYY-MM-DD", "confidence": 0.0}\n'
    'Use null for any field you cannot confidently determine. '
    '"confidence" is your own estimate (0.0-1.0) of how reliable the '
    '"code" extraction is.'
)


class OpenAIOCRBackend(OCRBackend):
    """
    Vision-based extraction via the OpenAI API. Mirrors ClaudeOCRBackend -
    same prompt, same response shape - so either can be selected via
    OCR_BACKEND without any other code caring which one is active.
    """

    def __init__(self):
        api_key = settings.OPENAI_API_KEY
        if not api_key:
            raise RuntimeError(
                'OPENAI_API_KEY is not set. Required when '
                'OCR_BACKEND=openai.'
            )
        self.client = OpenAI(api_key=api_key)
        self.model = settings.OPENAI_OCR_MODEL or DEFAULT_MODEL

    def extract(self, image_bytes: bytes, media_type: str) -> dict:
        empty = {'code': None, 'name': None, 'issuer': None, 'expiry_date': None, 'confidence': 0.0}

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

        return {
            'code': result.get('code') or None,
            'name': result.get('name') or None,
            'issuer': result.get('issuer') or None,
            'expiry_date': result.get('expiry_date') or None,
            'confidence': float(result.get('confidence') or 0.0),
        }
