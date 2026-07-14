import base64
import json
import logging

from openai import OpenAI

from myapp.models import SiteConfiguration

from .base import (
    OCRBackend, VALID_CODE_TYPES, VALID_CURRENCIES, parse_float_or_none,
    sanitize_domain_slug, sanitize_url, strip_json_fences,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = 'gpt-4o-mini'

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
    'symbology. Also try to identify the actual redeemable brand\'s '
    'website domain for logo lookup purposes - this is often the same as '
    'the issuer, but if the card was sold through a marketplace or '
    'reseller (e.g. a card usable at "Uber" or "Uber Eats" but issued/'
    'sold by "Every Wish" or a similar gift-card marketplace), use the '
    'actual redeemable brand\'s domain, not the reseller\'s. Also extract '
    'a balance or validity check URL if one is visibly printed on the '
    'card itself. Respond with ONLY a JSON object, no other text and no '
    'markdown code fences, in exactly this shape:\n'
    '{"code": "...", "code_type": "...", "name": "...", "issuer": "...", '
    '"expiry_date": "YYYY-MM-DD", "pin": "...", "value": 0.00, '
    '"currency": "...", "card_number": "...", "logo_slug": "...", '
    '"balance_check_url": "...", "confidence": 0.0}\n'
    f'"code_type" must be exactly one of: {_CODE_TYPE_OPTIONS}, "none" '
    '(if the code is a plain printed number with no separate scannable '
    'barcode at all), or null (if you cannot tell). "currency" must be a '
    'three-letter ISO 4217 code (e.g. "GBP", "USD", "EUR") or null. '
    '"logo_slug" must be a bare domain with no scheme or "www." (e.g. '
    '"uber.com"), or null if you cannot confidently tell. '
    '"balance_check_url" must be a full URL including "https://", or '
    'null - never a URL you are guessing at, only one actually printed '
    'on the card. Use null for any other field you cannot confidently '
    'determine, or that is genuinely blank on the card itself (e.g. an '
    'empty PIN box) - never invent a value. "confidence" is your own '
    'estimate (0.0-1.0) of how reliable the "code" extraction is.'
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
        empty = {
            'code': None, 'code_type': None, 'name': None, 'issuer': None, 'expiry_date': None,
            'pin': None, 'value': None, 'currency': None, 'card_number': None,
            'logo_slug': None, 'balance_check_url': None, 'confidence': 0.0,
        }

        image_b64 = base64.standard_b64encode(image_bytes).decode()
        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=400,
            timeout=20,
            # Guarantees the response is valid JSON - the prior prompt-only
            # instruction ("respond with ONLY a JSON object") wasn't
            # enforced by the API at all, and gpt-4o-mini would sometimes
            # wrap its answer in a ```json code fence anyway, silently
            # failing json.loads() below and looking identical to the model
            # genuinely finding nothing on the card.
            response_format={'type': 'json_object'},
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
            # Belt-and-suspenders: response_format should prevent this, but
            # if a fence slips through anyway, try once more before giving up.
            try:
                result = json.loads(strip_json_fences(text))
            except (json.JSONDecodeError, TypeError):
                logger.warning('OpenAI OCR response was not valid JSON: %r', text)
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
            'logo_slug': sanitize_domain_slug(result.get('logo_slug')),
            'balance_check_url': sanitize_url(result.get('balance_check_url')),
            'confidence': float(result.get('confidence') or 0.0),
        }
