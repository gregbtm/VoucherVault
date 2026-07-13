import base64
import json
import logging

import anthropic

from myapp.models import SiteConfiguration

from .base import OCRBackend, VALID_CODE_TYPES, VALID_CURRENCIES, parse_float_or_none, strip_json_fences

logger = logging.getLogger(__name__)

DEFAULT_MODEL = 'claude-sonnet-5'

_CODE_TYPE_OPTIONS = ', '.join(sorted(VALID_CODE_TYPES - {'none'}))

_PROMPT = (
    'This image shows a voucher, coupon, gift card, or loyalty card - it '
    'may be a photo of a physical card, or a screenshot (an emailed gift '
    'card, a retailer app screen, a digital wallet pass, a confirmation '
    'page). Extract whatever of the following you can confidently read: '
    'the redeem/reference code printed or barcoded on it, a separate PIN '
    'or security code if one is shown apart from the main code, a printed '
    'card/member/serial number if different from the redeem code, the '
    'merchant or brand name, the issuer (if different from the merchant), '
    'the monetary value and its currency, and the expiry date. If a '
    'barcode or QR code is visible next to the code, also identify its '
    'symbology. Respond with ONLY a JSON object, no other text and no '
    'markdown code fences, in exactly this shape:\n'
    '{"code": "...", "code_type": "...", "name": "...", "issuer": "...", '
    '"expiry_date": "YYYY-MM-DD", "pin": "...", "value": 0.00, '
    '"currency": "...", "card_number": "...", "confidence": 0.0}\n'
    f'"code_type" must be exactly one of: {_CODE_TYPE_OPTIONS}, "none" '
    '(if the code is a plain printed number with no separate scannable '
    'barcode at all), or null (if you cannot tell). "currency" must be a '
    'three-letter ISO 4217 code (e.g. "GBP", "USD", "EUR") or null. Use '
    'null for any other field you cannot confidently determine, or that '
    'is genuinely blank on the card itself (e.g. an empty PIN box) - '
    'never invent a value. "confidence" is your own estimate (0.0-1.0) of '
    'how reliable the "code" extraction is.'
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
        empty = {
            'code': None, 'code_type': None, 'name': None, 'issuer': None, 'expiry_date': None,
            'pin': None, 'value': None, 'currency': None, 'card_number': None, 'confidence': 0.0,
        }

        image_b64 = base64.standard_b64encode(image_bytes).decode()
        message = self.client.messages.create(
            model=self.model,
            max_tokens=400,
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
            # Claude generally respects "respond with ONLY JSON" more
            # reliably than smaller models, but strip a stray markdown
            # fence before giving up, same as the OpenAI backend does.
            try:
                result = json.loads(strip_json_fences(text))
            except (json.JSONDecodeError, TypeError):
                logger.warning('Claude OCR response was not valid JSON: %r', text)
                return empty

        code_type = result.get('code_type') or None
        if code_type not in VALID_CODE_TYPES:
            code_type = None

        currency = (result.get('currency') or '').upper() or None
        if currency not in VALID_CURRENCIES:
            currency = None

        return {
            'code': result.get('code') or None,
            'code_type': code_type,
            'name': result.get('name') or None,
            'issuer': result.get('issuer') or None,
            'expiry_date': result.get('expiry_date') or None,
            'pin': result.get('pin') or None,
            'value': parse_float_or_none(result.get('value')),
            'currency': currency,
            'card_number': result.get('card_number') or None,
            'confidence': float(result.get('confidence') or 0.0),
        }
