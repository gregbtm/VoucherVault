import base64
import json
import logging

import anthropic

from myapp.models import SiteConfiguration

from .base import (
    OCRBackend, empty_vision_extraction, finalize_vision_extraction,
    strip_json_fences, VISION_PROMPT,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = 'claude-sonnet-5'


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

    def extract(self, image_bytes: bytes, media_type: str, user=None) -> dict:
        empty = empty_vision_extraction()

        from myapp.scan_learning import build_ocr_correction_hints
        prompt_text = VISION_PROMPT + build_ocr_correction_hints(user)

        image_b64 = base64.standard_b64encode(image_bytes).decode()
        message = self.client.messages.create(
            model=self.model,
            max_tokens=600,
            timeout=20,
            # Reading an exact code off a photo is an analytical task, not
            # a creative one - the API default (1.0) samples enough that
            # re-scanning the same image can misread a character
            # differently each time, which broke duplicate-code detection
            # for a "no barcode" card with no independent decode to check
            # the OCR read against. 0 asks for the single most likely
            # token at each step instead.
            temperature=0,
            messages=[{
                'role': 'user',
                'content': [
                    {
                        'type': 'image',
                        'source': {'type': 'base64', 'media_type': media_type, 'data': image_b64},
                    },
                    {'type': 'text', 'text': prompt_text},
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

        return finalize_vision_extraction(result)
