import base64
import json
import logging

from openai import OpenAI

from myapp.models import SiteConfiguration

from .base import (
    OCRBackend, empty_vision_extraction, finalize_vision_extraction,
    strip_json_fences, VISION_PROMPT,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = 'gpt-4o-mini'


class OpenAIOCRBackend(OCRBackend):
    """
    Vision-based extraction via the OpenAI API. Shares its prompt/response
    shape and post-processing with ClaudeOCRBackend (see
    ocr/backends/base.py::VISION_PROMPT/finalize_vision_extraction) so
    either can be selected via OCR_BACKEND without any other code caring
    which one is active - only the request/response plumbing below is
    genuinely SDK-specific.
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

    def extract(self, image_bytes: bytes, media_type: str, user=None) -> dict:
        empty = empty_vision_extraction()

        from myapp.scan_learning import build_ocr_correction_hints
        prompt_text = VISION_PROMPT + build_ocr_correction_hints(user)

        image_b64 = base64.standard_b64encode(image_bytes).decode()
        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=600,
            timeout=20,
            # Guarantees the response is valid JSON - the prior prompt-only
            # instruction ("respond with ONLY a JSON object") wasn't
            # enforced by the API at all, and gpt-4o-mini would sometimes
            # wrap its answer in a ```json code fence anyway, silently
            # failing json.loads() below and looking identical to the model
            # genuinely finding nothing on the card.
            response_format={'type': 'json_object'},
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
                    {'type': 'text', 'text': prompt_text},
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

        return finalize_vision_extraction(result)
