import io
import os
import shutil
import unittest
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from PIL import Image, ImageDraw, ImageFont

import pytesseract

from myapp.test_utils import set_site_config

from .backends import get_backend, ocr_enabled
from .backends.base import (
    parse_float_or_none, sanitize_domain_slug, sanitize_free_text,
    sanitize_tag_suggestions, sanitize_time_or_none, sanitize_url,
    strip_json_fences,
)
from .backends.claude_backend import ClaudeOCRBackend
from .backends.openai_backend import OpenAIOCRBackend
from .backends.tesseract import TesseractOCRBackend

DEJAVU_FONT_PATH = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'


def _tiny_png_bytes() -> bytes:
    image = Image.new('RGB', (10, 10), color='white')
    buf = io.BytesIO()
    image.save(buf, format='PNG')
    return buf.getvalue()


class BackendSelectionTests(TestCase):
    def test_disabled_by_default(self):
        set_site_config(ocr_backend='none')
        self.assertFalse(ocr_enabled())

    def test_enabled_for_known_backend(self):
        set_site_config(ocr_backend='tesseract')
        self.assertTrue(ocr_enabled())

    def test_disabled_for_unknown_backend(self):
        set_site_config(ocr_backend='bogus')
        self.assertFalse(ocr_enabled())

    def test_get_backend_raises_when_disabled(self):
        set_site_config(ocr_backend='none')
        with self.assertRaises(ValueError):
            get_backend()

    def test_get_backend_returns_tesseract(self):
        set_site_config(ocr_backend='tesseract')
        self.assertIsInstance(get_backend(), TesseractOCRBackend)

    def test_get_backend_returns_claude(self):
        set_site_config(ocr_backend='claude', anthropic_api_key='test-key')
        self.assertIsInstance(get_backend(), ClaudeOCRBackend)

    def test_get_backend_returns_openai(self):
        set_site_config(ocr_backend='openai', openai_api_key='test-key')
        self.assertIsInstance(get_backend(), OpenAIOCRBackend)


class TesseractBackendTests(TestCase):
    @patch('ocr.backends.tesseract.pytesseract.get_tesseract_version')
    def test_raises_when_binary_missing(self, mock_version):
        mock_version.side_effect = pytesseract.TesseractNotFoundError()
        with self.assertRaises(RuntimeError):
            TesseractOCRBackend()

    @patch('ocr.backends.tesseract.pytesseract.image_to_data')
    @patch('ocr.backends.tesseract.pytesseract.get_tesseract_version')
    def test_extract_guesses_code_and_expiry_from_recognized_words(self, mock_version, mock_data):
        mock_data.return_value = {
            'text': ['', 'CODE-83921X', 'Expires:', '31.12.2026', ''],
            'conf': ['-1', '95', '90', '92', '-1'],
        }
        backend = TesseractOCRBackend()
        result = backend.extract(_tiny_png_bytes(), 'image/png')

        self.assertEqual(result['code'], 'CODE-83921X')
        self.assertEqual(result['code_type'], 'code39')
        self.assertEqual(result['expiry_date'], '2026-12-31')
        self.assertIsNone(result['name'])
        self.assertIsNone(result['issuer'])
        self.assertIsNone(result['journey_origin'])
        self.assertIsNone(result['journey_destination'])
        self.assertGreater(result['confidence'], 0)

    @patch('ocr.backends.tesseract.pytesseract.image_to_data')
    @patch('ocr.backends.tesseract.pytesseract.get_tesseract_version')
    def test_extract_returns_none_code_when_nothing_recognized(self, mock_version, mock_data):
        mock_data.return_value = {'text': ['the', 'a', 'of'], 'conf': ['90', '88', '91']}
        backend = TesseractOCRBackend()
        result = backend.extract(_tiny_png_bytes(), 'image/png')

        self.assertIsNone(result['code'])
        self.assertIsNone(result['code_type'])
        self.assertEqual(result['confidence'], 0.0)

    @patch('ocr.backends.tesseract.pytesseract.image_to_data')
    @patch('ocr.backends.tesseract.pytesseract.get_tesseract_version')
    def test_extract_guesses_ean13_from_thirteen_digit_code(self, mock_version, mock_data):
        mock_data.return_value = {
            'text': ['', '4006381333931', ''],
            'conf': ['-1', '95', '-1'],
        }
        backend = TesseractOCRBackend()
        result = backend.extract(_tiny_png_bytes(), 'image/png')

        self.assertEqual(result['code'], '4006381333931')
        self.assertEqual(result['code_type'], 'ean13')

    @patch('ocr.backends.tesseract.pytesseract.get_tesseract_version')
    def test_guess_code_type_alphanumeric_matches_scanner_js_heuristic(self, mock_version):
        # scanner.js's guessCodeTypeFromValue() treats the same character
        # set (uppercase letters, digits, space, and -.$/+%) as Code 39-safe
        # and falls back to code128 for anything outside it. _guess_code()
        # always upper-cases extracted candidates, so 'code128' is only
        # reachable here for characters neither regex allows (e.g. '#').
        backend = TesseractOCRBackend()
        self.assertEqual(backend._guess_code_type('ABC-1234'), 'code39')
        self.assertEqual(backend._guess_code_type('ABC#1234'), 'code128')


class TesseractLiveIntegrationTests(TestCase):
    """Exercises the real tesseract binary end-to-end, no mocks."""

    @unittest.skipUnless(shutil.which('tesseract'), 'tesseract binary not installed')
    def test_real_ocr_extracts_code_and_expiry(self):
        image = Image.new('RGB', (500, 150), color='white')
        draw = ImageDraw.Draw(image)
        font = (
            ImageFont.truetype(DEJAVU_FONT_PATH, 28)
            if os.path.exists(DEJAVU_FONT_PATH)
            else ImageFont.load_default()
        )
        draw.text((10, 10), 'CODE-83921X', fill='black', font=font)
        draw.text((10, 70), 'Expires: 31.12.2026', fill='black', font=font)
        buf = io.BytesIO()
        image.save(buf, format='PNG')

        backend = TesseractOCRBackend()
        result = backend.extract(buf.getvalue(), 'image/png')

        self.assertEqual(result['code'], 'CODE-83921X')
        self.assertEqual(result['expiry_date'], '2026-12-31')
        self.assertGreater(result['confidence'], 0)


class ClaudeBackendTests(TestCase):
    def test_raises_without_api_key(self):
        set_site_config(anthropic_api_key='')
        with self.assertRaises(RuntimeError):
            ClaudeOCRBackend()

    @patch('ocr.backends.claude_backend.anthropic.Anthropic')
    def test_extract_parses_json_response(self, mock_anthropic_cls):
        set_site_config(anthropic_api_key='test-key')
        mock_client = MagicMock()
        mock_block = MagicMock(
            type='text',
            text=(
                '{"code": "SAVE20", "code_type": "code128", "name": "Acme", "issuer": null, '
                '"expiry_date": "2026-12-31", "pin": "4471", "value": 50.0, "currency": "gbp", '
                '"card_number": "4000123456789010", "confidence": 0.9}'
            ),
        )
        mock_client.messages.create.return_value = MagicMock(content=[mock_block])
        mock_anthropic_cls.return_value = mock_client

        backend = ClaudeOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertEqual(result['code'], 'SAVE20')
        self.assertEqual(result['code_type'], 'code128')
        self.assertEqual(result['name'], 'Acme')
        self.assertIsNone(result['issuer'])
        self.assertEqual(result['expiry_date'], '2026-12-31')
        self.assertEqual(result['pin'], '4471')
        self.assertEqual(result['value'], 50.0)
        self.assertEqual(result['currency'], 'GBP')
        self.assertEqual(result['card_number'], '4000123456789010')
        self.assertEqual(result['confidence'], 0.9)
        mock_client.messages.create.assert_called_once()

    @patch('ocr.backends.claude_backend.anthropic.Anthropic')
    def test_extract_parses_journey_fields_for_a_travel_ticket(self, mock_anthropic_cls):
        set_site_config(anthropic_api_key='test-key')
        mock_client = MagicMock()
        mock_block = MagicMock(
            type='text',
            text=(
                '{"code": "AABXF39DNGF", "code_type": "qrcode", "name": null, "issuer": "National Rail", '
                '"journey_origin": "Hatfield Peverel", "journey_destination": "London Terminals", '
                '"confidence": 0.9}'
            ),
        )
        mock_client.messages.create.return_value = MagicMock(content=[mock_block])
        mock_anthropic_cls.return_value = mock_client

        backend = ClaudeOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertEqual(result['journey_origin'], 'Hatfield Peverel')
        self.assertEqual(result['journey_destination'], 'London Terminals')

    @patch('ocr.backends.claude_backend.anthropic.Anthropic')
    def test_extract_journey_fields_none_for_a_non_ticket_card(self, mock_anthropic_cls):
        set_site_config(anthropic_api_key='test-key')
        mock_client = MagicMock()
        mock_block = MagicMock(
            type='text',
            text='{"code": "SAVE20", "code_type": "code128", "confidence": 0.9}',
        )
        mock_client.messages.create.return_value = MagicMock(content=[mock_block])
        mock_anthropic_cls.return_value = mock_client

        backend = ClaudeOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertIsNone(result['journey_origin'])
        self.assertIsNone(result['journey_destination'])

    @patch('ocr.backends.claude_backend.anthropic.Anthropic')
    def test_extract_parses_travel_time_and_type_for_a_travel_ticket(self, mock_anthropic_cls):
        set_site_config(anthropic_api_key='test-key')
        mock_client = MagicMock()
        mock_block = MagicMock(
            type='text',
            text=(
                '{"code": "AABXF39DNGF", "code_type": "qrcode", "issuer": "National Rail", '
                '"type": "travelpass", "journey_origin": "Hatfield Peverel", '
                '"journey_destination": "London Terminals", "travel_time": "09:14", '
                '"confidence": 0.9}'
            ),
        )
        mock_client.messages.create.return_value = MagicMock(content=[mock_block])
        mock_anthropic_cls.return_value = mock_client

        backend = ClaudeOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertEqual(result['type'], 'travelpass')
        self.assertEqual(result['travel_time'], '09:14')

    @patch('ocr.backends.claude_backend.anthropic.Anthropic')
    def test_extract_discards_malformed_travel_time(self, mock_anthropic_cls):
        set_site_config(anthropic_api_key='test-key')
        mock_client = MagicMock()
        mock_block = MagicMock(
            type='text',
            text='{"code": "AABXF39DNGF", "travel_time": "9:14 AM", "confidence": 0.9}',
        )
        mock_client.messages.create.return_value = MagicMock(content=[mock_block])
        mock_anthropic_cls.return_value = mock_client

        backend = ClaudeOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertIsNone(result['travel_time'])

    @patch('ocr.backends.claude_backend.anthropic.Anthropic')
    def test_extract_requests_zero_temperature(self, mock_anthropic_cls):
        """
        Regression test: a real-world report of the same physical card,
        scanned twice from the exact same photo, extracting a different
        redeem code each time - which broke duplicate-code detection since
        the two "identical" scans genuinely produced different strings.
        The API default (1.0) samples; 0 asks for the single most likely
        reading at each token instead.
        """
        set_site_config(anthropic_api_key='test-key')
        mock_client = MagicMock()
        mock_block = MagicMock(
            type='text',
            text='{"code": null, "code_type": null, "name": null, "issuer": null, '
                 '"expiry_date": null, "pin": null, "value": null, "currency": null, '
                 '"card_number": null, "confidence": 0.0}',
        )
        mock_client.messages.create.return_value = MagicMock(content=[mock_block])
        mock_anthropic_cls.return_value = mock_client

        backend = ClaudeOCRBackend()
        backend.extract(b'fake-bytes', 'image/jpeg')

        _, kwargs = mock_client.messages.create.call_args
        self.assertEqual(kwargs.get('temperature'), 0)

    @patch('ocr.backends.claude_backend.anthropic.Anthropic')
    def test_extract_strips_markdown_fence_before_parsing(self, mock_anthropic_cls):
        """
        Claude generally obeys "respond with ONLY JSON", but this backend
        has no API-level JSON-mode guarantee (unlike OpenAI's
        response_format) - a fenced response must still parse successfully
        rather than being silently treated as "nothing found".
        """
        set_site_config(anthropic_api_key='test-key')
        mock_client = MagicMock()
        mock_block = MagicMock(
            type='text',
            text='```json\n{"code": "SAVE20", "code_type": null, "name": null, "issuer": null, '
                 '"expiry_date": null, "pin": null, "value": null, "currency": null, '
                 '"card_number": null, "confidence": 0.8}\n```',
        )
        mock_client.messages.create.return_value = MagicMock(content=[mock_block])
        mock_anthropic_cls.return_value = mock_client

        backend = ClaudeOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertEqual(result['code'], 'SAVE20')
        self.assertEqual(result['confidence'], 0.8)

    @patch('ocr.backends.claude_backend.anthropic.Anthropic')
    def test_extract_discards_invalid_currency(self, mock_anthropic_cls):
        set_site_config(anthropic_api_key='test-key')
        mock_client = MagicMock()
        mock_block = MagicMock(
            type='text',
            text='{"code": "SAVE20", "code_type": null, "name": null, "issuer": null, '
                 '"expiry_date": null, "pin": null, "value": null, "currency": "XYZ", '
                 '"card_number": null, "confidence": 0.5}',
        )
        mock_client.messages.create.return_value = MagicMock(content=[mock_block])
        mock_anthropic_cls.return_value = mock_client

        backend = ClaudeOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertIsNone(result['currency'])

    @patch('ocr.backends.claude_backend.anthropic.Anthropic')
    def test_extract_discards_hallucinated_code_type(self, mock_anthropic_cls):
        set_site_config(anthropic_api_key='test-key')
        mock_client = MagicMock()
        mock_block = MagicMock(
            type='text',
            text='{"code": "SAVE20", "code_type": "not-a-real-type", "name": null, "issuer": null, "expiry_date": null, "confidence": 0.5}',
        )
        mock_client.messages.create.return_value = MagicMock(content=[mock_block])
        mock_anthropic_cls.return_value = mock_client

        backend = ClaudeOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertIsNone(result['code_type'])

    @patch('ocr.backends.claude_backend.anthropic.Anthropic')
    def test_extract_handles_malformed_response_gracefully(self, mock_anthropic_cls):
        set_site_config(anthropic_api_key='test-key')
        mock_client = MagicMock()
        mock_block = MagicMock(type='text', text='not valid json')
        mock_client.messages.create.return_value = MagicMock(content=[mock_block])
        mock_anthropic_cls.return_value = mock_client

        backend = ClaudeOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertIsNone(result['code'])
        self.assertEqual(result['confidence'], 0.0)

    @patch('ocr.backends.claude_backend.anthropic.Anthropic')
    def test_model_env_override(self, mock_anthropic_cls):
        set_site_config(anthropic_api_key='test-key', anthropic_ocr_model='claude-haiku-4-5-20251001')
        backend = ClaudeOCRBackend()
        self.assertEqual(backend.model, 'claude-haiku-4-5-20251001')

    @patch('ocr.backends.claude_backend.anthropic.Anthropic')
    def test_extract_parses_logo_slug_and_balance_check_url(self, mock_anthropic_cls):
        """
        The vision model can tell a card's actual redeemable brand apart
        from a marketplace/reseller printed as the issuer (e.g. an "Uber
        Eats" card issued by "Every Wish") - logo_slug should reflect the
        brand, not the reseller.
        """
        set_site_config(anthropic_api_key='test-key')
        mock_client = MagicMock()
        mock_block = MagicMock(
            type='text',
            text=(
                '{"code": "SAVE20", "code_type": null, "name": "Uber Eats", "issuer": "Every Wish", '
                '"expiry_date": null, "pin": null, "value": null, "currency": null, "card_number": null, '
                '"logo_slug": "uber.com", "balance_check_url": "https://example.com/balance", "confidence": 0.9}'
            ),
        )
        mock_client.messages.create.return_value = MagicMock(content=[mock_block])
        mock_anthropic_cls.return_value = mock_client

        backend = ClaudeOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertEqual(result['issuer'], 'Every Wish')
        self.assertEqual(result['logo_slug'], 'uber.com')
        self.assertEqual(result['balance_check_url'], 'https://example.com/balance')

    @patch('ocr.backends.claude_backend.anthropic.Anthropic')
    def test_extract_discards_malformed_logo_slug_and_balance_check_url(self, mock_anthropic_cls):
        set_site_config(anthropic_api_key='test-key')
        mock_client = MagicMock()
        mock_block = MagicMock(
            type='text',
            text=(
                '{"code": null, "code_type": null, "name": null, "issuer": null, '
                '"expiry_date": null, "pin": null, "value": null, "currency": null, "card_number": null, '
                '"logo_slug": "not a domain", "balance_check_url": "not a url", "confidence": 0.0}'
            ),
        )
        mock_client.messages.create.return_value = MagicMock(content=[mock_block])
        mock_anthropic_cls.return_value = mock_client

        backend = ClaudeOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertIsNone(result['logo_slug'])
        self.assertIsNone(result['balance_check_url'])

    @patch('ocr.backends.claude_backend.anthropic.Anthropic')
    def test_extract_parses_type_description_notes_and_tags(self, mock_anthropic_cls):
        set_site_config(anthropic_api_key='test-key')
        mock_client = MagicMock()
        mock_block = MagicMock(
            type='text',
            text=(
                '{"code": null, "code_type": null, "name": "Uber Eats", "issuer": "Every Wish", '
                '"expiry_date": null, "pin": null, "value": 50.0, "currency": "GBP", "card_number": null, '
                '"logo_slug": null, "balance_check_url": null, "type": "giftcard", '
                '"description": "£50 gift card for Uber and Uber Eats", '
                '"notes": "Valid in the UK only.", "tags": ["Restaurant", "Food Delivery"], "confidence": 0.9}'
            ),
        )
        mock_client.messages.create.return_value = MagicMock(content=[mock_block])
        mock_anthropic_cls.return_value = mock_client

        backend = ClaudeOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertEqual(result['type'], 'giftcard')
        self.assertEqual(result['description'], '£50 gift card for Uber and Uber Eats')
        self.assertEqual(result['notes'], 'Valid in the UK only.')
        self.assertEqual(result['tags'], ['Restaurant', 'Food Delivery'])

    @patch('ocr.backends.claude_backend.anthropic.Anthropic')
    def test_extract_discards_hallucinated_type(self, mock_anthropic_cls):
        set_site_config(anthropic_api_key='test-key')
        mock_client = MagicMock()
        mock_block = MagicMock(
            type='text',
            text='{"code": null, "code_type": null, "name": null, "issuer": null, "expiry_date": null, '
                 '"type": "not-a-real-type", "confidence": 0.0}',
        )
        mock_client.messages.create.return_value = MagicMock(content=[mock_block])
        mock_anthropic_cls.return_value = mock_client

        backend = ClaudeOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertIsNone(result['type'])

    @patch('ocr.backends.claude_backend.anthropic.Anthropic')
    def test_extract_missing_new_fields_default_sensibly(self, mock_anthropic_cls):
        """A response that predates these fields (or a model that omits
        them) must not error out - missing keys default the same as an
        explicit null/empty array would."""
        set_site_config(anthropic_api_key='test-key')
        mock_client = MagicMock()
        mock_block = MagicMock(
            type='text',
            text='{"code": "SAVE20", "confidence": 0.5}',
        )
        mock_client.messages.create.return_value = MagicMock(content=[mock_block])
        mock_anthropic_cls.return_value = mock_client

        backend = ClaudeOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertIsNone(result['type'])
        self.assertIsNone(result['description'])
        self.assertIsNone(result['notes'])
        self.assertEqual(result['tags'], [])


class OpenAIBackendTests(TestCase):
    def test_raises_without_api_key(self):
        set_site_config(openai_api_key='')
        with self.assertRaises(RuntimeError):
            OpenAIOCRBackend()

    @patch('ocr.backends.openai_backend.OpenAI')
    def test_extract_parses_json_response(self, mock_openai_cls):
        set_site_config(openai_api_key='test-key')
        mock_client = MagicMock()
        mock_message = MagicMock(
            content=(
                '{"code": "SAVE20", "code_type": "code128", "name": "Acme", "issuer": null, '
                '"expiry_date": "2026-12-31", "pin": "4471", "value": 50.0, "currency": "gbp", '
                '"card_number": "4000123456789010", "confidence": 0.9}'
            ),
        )
        mock_client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=mock_message)])
        mock_openai_cls.return_value = mock_client

        backend = OpenAIOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertEqual(result['code'], 'SAVE20')
        self.assertEqual(result['code_type'], 'code128')
        self.assertEqual(result['name'], 'Acme')
        self.assertIsNone(result['issuer'])
        self.assertEqual(result['expiry_date'], '2026-12-31')
        self.assertEqual(result['pin'], '4471')
        self.assertEqual(result['value'], 50.0)
        self.assertEqual(result['currency'], 'GBP')
        self.assertEqual(result['card_number'], '4000123456789010')
        self.assertEqual(result['confidence'], 0.9)
        mock_client.chat.completions.create.assert_called_once()

    @patch('ocr.backends.openai_backend.OpenAI')
    def test_extract_parses_journey_fields_for_a_travel_ticket(self, mock_openai_cls):
        set_site_config(openai_api_key='test-key')
        mock_client = MagicMock()
        mock_message = MagicMock(
            content=(
                '{"code": "AABXF39DNGG", "code_type": "qrcode", "name": null, "issuer": "National Rail", '
                '"journey_origin": "London Terminals", "journey_destination": "Hatfield Peverel", '
                '"confidence": 0.9}'
            ),
        )
        mock_client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=mock_message)])
        mock_openai_cls.return_value = mock_client

        backend = OpenAIOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertEqual(result['journey_origin'], 'London Terminals')
        self.assertEqual(result['journey_destination'], 'Hatfield Peverel')

    @patch('ocr.backends.openai_backend.OpenAI')
    def test_extract_parses_travel_time_and_type_for_a_travel_ticket(self, mock_openai_cls):
        set_site_config(openai_api_key='test-key')
        mock_client = MagicMock()
        mock_message = MagicMock(
            content=(
                '{"code": "AABXF39DNGG", "code_type": "qrcode", "issuer": "National Rail", '
                '"type": "travelpass", "journey_origin": "London Terminals", '
                '"journey_destination": "Hatfield Peverel", "travel_time": "17:32", '
                '"confidence": 0.9}'
            ),
        )
        mock_client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=mock_message)])
        mock_openai_cls.return_value = mock_client

        backend = OpenAIOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertEqual(result['type'], 'travelpass')
        self.assertEqual(result['travel_time'], '17:32')

    @patch('ocr.backends.openai_backend.OpenAI')
    def test_extract_requests_json_mode(self, mock_openai_cls):
        """
        Regression test for the actual root cause of a real-world "nothing
        could be confidently read" report: gpt-4o-mini would occasionally
        wrap its answer in a markdown code fence despite being told not
        to, and json.loads() on the fenced text silently failed, returning
        the exact same response as a genuine "found nothing". response_format
        is the API-enforced fix - assert it's actually being sent.
        """
        set_site_config(openai_api_key='test-key')
        mock_client = MagicMock()
        mock_message = MagicMock(content='{"code": null, "code_type": null, "name": null, "issuer": null, '
                                          '"expiry_date": null, "pin": null, "value": null, "currency": null, '
                                          '"card_number": null, "confidence": 0.0}')
        mock_client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=mock_message)])
        mock_openai_cls.return_value = mock_client

        backend = OpenAIOCRBackend()
        backend.extract(b'fake-bytes', 'image/jpeg')

        _, kwargs = mock_client.chat.completions.create.call_args
        self.assertEqual(kwargs.get('response_format'), {'type': 'json_object'})

    @patch('ocr.backends.openai_backend.OpenAI')
    def test_extract_requests_zero_temperature(self, mock_openai_cls):
        """
        Regression test: a real-world report of the same physical card,
        scanned twice from the exact same photo, extracting a different
        redeem code each time - which broke duplicate-code detection since
        the two "identical" scans genuinely produced different strings.
        The API default (1.0) samples; 0 asks for the single most likely
        reading at each token instead.
        """
        set_site_config(openai_api_key='test-key')
        mock_client = MagicMock()
        mock_message = MagicMock(content='{"code": null, "code_type": null, "name": null, "issuer": null, '
                                          '"expiry_date": null, "pin": null, "value": null, "currency": null, '
                                          '"card_number": null, "confidence": 0.0}')
        mock_client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=mock_message)])
        mock_openai_cls.return_value = mock_client

        backend = OpenAIOCRBackend()
        backend.extract(b'fake-bytes', 'image/jpeg')

        _, kwargs = mock_client.chat.completions.create.call_args
        self.assertEqual(kwargs.get('temperature'), 0)

    @patch('ocr.backends.openai_backend.OpenAI')
    def test_extract_strips_markdown_fence_before_parsing(self, mock_openai_cls):
        set_site_config(openai_api_key='test-key')
        mock_client = MagicMock()
        mock_message = MagicMock(
            content='```json\n{"code": "SAVE20", "code_type": null, "name": null, "issuer": null, '
                    '"expiry_date": null, "pin": null, "value": null, "currency": null, '
                    '"card_number": null, "confidence": 0.8}\n```',
        )
        mock_client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=mock_message)])
        mock_openai_cls.return_value = mock_client

        backend = OpenAIOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertEqual(result['code'], 'SAVE20')
        self.assertEqual(result['confidence'], 0.8)

    @patch('ocr.backends.openai_backend.OpenAI')
    def test_extract_discards_invalid_currency(self, mock_openai_cls):
        set_site_config(openai_api_key='test-key')
        mock_client = MagicMock()
        mock_message = MagicMock(
            content='{"code": "SAVE20", "code_type": null, "name": null, "issuer": null, '
                    '"expiry_date": null, "pin": null, "value": null, "currency": "XYZ", '
                    '"card_number": null, "confidence": 0.5}',
        )
        mock_client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=mock_message)])
        mock_openai_cls.return_value = mock_client

        backend = OpenAIOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertIsNone(result['currency'])

    @patch('ocr.backends.openai_backend.OpenAI')
    def test_extract_parses_stringified_value(self, mock_openai_cls):
        """A model occasionally returns "value" as a currency-formatted
        string despite instructions - must still coerce to a float."""
        set_site_config(openai_api_key='test-key')
        mock_client = MagicMock()
        mock_message = MagicMock(
            content='{"code": null, "code_type": null, "name": null, "issuer": null, '
                    '"expiry_date": null, "pin": null, "value": "\\u00a350.00", "currency": null, '
                    '"card_number": null, "confidence": 0.5}',
        )
        mock_client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=mock_message)])
        mock_openai_cls.return_value = mock_client

        backend = OpenAIOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertEqual(result['value'], 50.0)

    @patch('ocr.backends.openai_backend.OpenAI')
    def test_extract_discards_hallucinated_code_type(self, mock_openai_cls):
        set_site_config(openai_api_key='test-key')
        mock_client = MagicMock()
        mock_message = MagicMock(
            content='{"code": "SAVE20", "code_type": "not-a-real-type", "name": null, "issuer": null, "expiry_date": null, "confidence": 0.5}',
        )
        mock_client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=mock_message)])
        mock_openai_cls.return_value = mock_client

        backend = OpenAIOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertIsNone(result['code_type'])

    @patch('ocr.backends.openai_backend.OpenAI')
    def test_extract_handles_malformed_response_gracefully(self, mock_openai_cls):
        set_site_config(openai_api_key='test-key')
        mock_client = MagicMock()
        mock_message = MagicMock(content='not valid json')
        mock_client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=mock_message)])
        mock_openai_cls.return_value = mock_client

        backend = OpenAIOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertIsNone(result['code'])
        self.assertEqual(result['confidence'], 0.0)

    @patch('ocr.backends.openai_backend.OpenAI')
    def test_model_env_override(self, mock_openai_cls):
        set_site_config(openai_api_key='test-key', openai_ocr_model='gpt-4o')
        backend = OpenAIOCRBackend()
        self.assertEqual(backend.model, 'gpt-4o')

    @patch('ocr.backends.openai_backend.OpenAI')
    def test_extract_parses_logo_slug_and_balance_check_url(self, mock_openai_cls):
        set_site_config(openai_api_key='test-key')
        mock_client = MagicMock()
        mock_message = MagicMock(
            content=(
                '{"code": "SAVE20", "code_type": null, "name": "Uber Eats", "issuer": "Every Wish", '
                '"expiry_date": null, "pin": null, "value": null, "currency": null, "card_number": null, '
                '"logo_slug": "uber.com", "balance_check_url": "https://example.com/balance", "confidence": 0.9}'
            ),
        )
        mock_client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=mock_message)])
        mock_openai_cls.return_value = mock_client

        backend = OpenAIOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertEqual(result['issuer'], 'Every Wish')
        self.assertEqual(result['logo_slug'], 'uber.com')
        self.assertEqual(result['balance_check_url'], 'https://example.com/balance')

    @patch('ocr.backends.openai_backend.OpenAI')
    def test_extract_discards_malformed_logo_slug_and_balance_check_url(self, mock_openai_cls):
        set_site_config(openai_api_key='test-key')
        mock_client = MagicMock()
        mock_message = MagicMock(
            content=(
                '{"code": null, "code_type": null, "name": null, "issuer": null, '
                '"expiry_date": null, "pin": null, "value": null, "currency": null, "card_number": null, '
                '"logo_slug": "not a domain", "balance_check_url": "not a url", "confidence": 0.0}'
            ),
        )
        mock_client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=mock_message)])
        mock_openai_cls.return_value = mock_client

        backend = OpenAIOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertIsNone(result['logo_slug'])
        self.assertIsNone(result['balance_check_url'])

    @patch('ocr.backends.openai_backend.OpenAI')
    def test_extract_strips_scheme_and_www_from_logo_slug(self, mock_openai_cls):
        """A model occasionally includes a scheme/www despite instructions
        to return a bare domain - must still be cleaned up rather than
        discarded outright."""
        set_site_config(openai_api_key='test-key')
        mock_client = MagicMock()
        mock_message = MagicMock(
            content=(
                '{"code": null, "code_type": null, "name": null, "issuer": null, '
                '"expiry_date": null, "pin": null, "value": null, "currency": null, "card_number": null, '
                '"logo_slug": "https://www.uber.com/", "balance_check_url": null, "confidence": 0.0}'
            ),
        )
        mock_client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=mock_message)])
        mock_openai_cls.return_value = mock_client

        backend = OpenAIOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertEqual(result['logo_slug'], 'uber.com')

    @patch('ocr.backends.openai_backend.OpenAI')
    def test_extract_parses_type_description_notes_and_tags(self, mock_openai_cls):
        set_site_config(openai_api_key='test-key')
        mock_client = MagicMock()
        mock_message = MagicMock(
            content=(
                '{"code": null, "code_type": null, "name": "Uber Eats", "issuer": "Every Wish", '
                '"expiry_date": null, "pin": null, "value": 50.0, "currency": "GBP", "card_number": null, '
                '"logo_slug": null, "balance_check_url": null, "type": "giftcard", '
                '"description": "£50 gift card for Uber and Uber Eats", '
                '"notes": "Valid in the UK only.", "tags": ["Restaurant", "Food Delivery"], "confidence": 0.9}'
            ),
        )
        mock_client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=mock_message)])
        mock_openai_cls.return_value = mock_client

        backend = OpenAIOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertEqual(result['type'], 'giftcard')
        self.assertEqual(result['description'], '£50 gift card for Uber and Uber Eats')
        self.assertEqual(result['notes'], 'Valid in the UK only.')
        self.assertEqual(result['tags'], ['Restaurant', 'Food Delivery'])

    @patch('ocr.backends.openai_backend.OpenAI')
    def test_extract_discards_hallucinated_type(self, mock_openai_cls):
        set_site_config(openai_api_key='test-key')
        mock_client = MagicMock()
        mock_message = MagicMock(
            content='{"code": null, "code_type": null, "name": null, "issuer": null, "expiry_date": null, '
                    '"type": "not-a-real-type", "confidence": 0.0}',
        )
        mock_client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=mock_message)])
        mock_openai_cls.return_value = mock_client

        backend = OpenAIOCRBackend()
        result = backend.extract(b'fake-bytes', 'image/jpeg')

        self.assertIsNone(result['type'])


class BaseHelperTests(TestCase):
    def test_strip_json_fences_removes_json_fence(self):
        text = '```json\n{"code": "X"}\n```'
        self.assertEqual(strip_json_fences(text), '{"code": "X"}')

    def test_strip_json_fences_removes_bare_fence(self):
        text = '```\n{"code": "X"}\n```'
        self.assertEqual(strip_json_fences(text), '{"code": "X"}')

    def test_strip_json_fences_leaves_unfenced_text_alone(self):
        text = '{"code": "X"}'
        self.assertEqual(strip_json_fences(text), '{"code": "X"}')

    def test_parse_float_or_none_handles_plain_number(self):
        self.assertEqual(parse_float_or_none(50), 50.0)
        self.assertEqual(parse_float_or_none(50.5), 50.5)

    def test_parse_float_or_none_handles_currency_formatted_string(self):
        self.assertEqual(parse_float_or_none('£50.00'), 50.0)
        self.assertEqual(parse_float_or_none('50.00 GBP'), 50.0)

    def test_parse_float_or_none_returns_none_for_junk(self):
        self.assertIsNone(parse_float_or_none(None))
        self.assertIsNone(parse_float_or_none(''))
        self.assertIsNone(parse_float_or_none('not a number'))

    def test_sanitize_domain_slug_accepts_bare_domain(self):
        self.assertEqual(sanitize_domain_slug('uber.com'), 'uber.com')
        self.assertEqual(sanitize_domain_slug('amazon.co.uk'), 'amazon.co.uk')

    def test_sanitize_domain_slug_strips_scheme_and_www(self):
        self.assertEqual(sanitize_domain_slug('https://www.uber.com'), 'uber.com')
        self.assertEqual(sanitize_domain_slug('http://uber.com/gift-cards'), 'uber.com')

    def test_sanitize_domain_slug_rejects_non_domains(self):
        self.assertIsNone(sanitize_domain_slug('Uber'))
        self.assertIsNone(sanitize_domain_slug('not a domain'))
        self.assertIsNone(sanitize_domain_slug(''))
        self.assertIsNone(sanitize_domain_slug(None))

    def test_sanitize_time_or_none_accepts_well_formed_24h_time(self):
        self.assertEqual(sanitize_time_or_none('09:14'), '09:14')
        self.assertEqual(sanitize_time_or_none('23:59'), '23:59')
        self.assertEqual(sanitize_time_or_none('00:00'), '00:00')

    def test_sanitize_time_or_none_rejects_junk(self):
        self.assertIsNone(sanitize_time_or_none('25:00'))
        self.assertIsNone(sanitize_time_or_none('9:14 AM'))
        self.assertIsNone(sanitize_time_or_none('not a time'))
        self.assertIsNone(sanitize_time_or_none(''))
        self.assertIsNone(sanitize_time_or_none(None))

    def test_sanitize_url_accepts_valid_http_url(self):
        self.assertEqual(sanitize_url('https://example.com/balance'), 'https://example.com/balance')
        self.assertEqual(sanitize_url('http://example.com'), 'http://example.com')

    def test_sanitize_url_rejects_non_urls(self):
        self.assertIsNone(sanitize_url('not a url'))
        self.assertIsNone(sanitize_url('ftp://example.com'))
        self.assertIsNone(sanitize_url(''))
        self.assertIsNone(sanitize_url(None))

    def test_sanitize_free_text_strips_and_passes_through(self):
        self.assertEqual(sanitize_free_text('  £50 gift card  ', 300), '£50 gift card')

    def test_sanitize_free_text_truncates_overlong_response(self):
        text = 'x' * 500
        self.assertEqual(sanitize_free_text(text, 300), 'x' * 300)

    def test_sanitize_free_text_rejects_junk(self):
        self.assertIsNone(sanitize_free_text('', 300))
        self.assertIsNone(sanitize_free_text(None, 300))
        self.assertIsNone(sanitize_free_text(123, 300))

    def test_sanitize_tag_suggestions_accepts_clean_list(self):
        self.assertEqual(sanitize_tag_suggestions(['Restaurant', 'Food Delivery']), ['Restaurant', 'Food Delivery'])

    def test_sanitize_tag_suggestions_dedupes_case_insensitively(self):
        self.assertEqual(sanitize_tag_suggestions(['Restaurant', 'restaurant', 'RESTAURANT']), ['Restaurant'])

    def test_sanitize_tag_suggestions_drops_non_strings_and_overlong_entries(self):
        self.assertEqual(sanitize_tag_suggestions(['Restaurant', 123, None, 'x' * 40]), ['Restaurant'])

    def test_sanitize_tag_suggestions_caps_at_four(self):
        self.assertEqual(
            sanitize_tag_suggestions(['A', 'B', 'C', 'D', 'E', 'F']),
            ['A', 'B', 'C', 'D'],
        )

    def test_sanitize_tag_suggestions_rejects_non_list(self):
        self.assertEqual(sanitize_tag_suggestions('Restaurant'), [])
        self.assertEqual(sanitize_tag_suggestions(None), [])


class TesseractPinAndValueTests(TestCase):
    @patch('ocr.backends.tesseract.pytesseract.get_tesseract_version')
    def test_guesses_pin_next_to_label(self, mock_version):
        backend = TesseractOCRBackend()
        self.assertEqual(backend._guess_pin('PIN: 4471', code=None), '4471')
        self.assertEqual(backend._guess_pin('PIN CODE 9910', code=None), '9910')
        self.assertIsNone(backend._guess_pin('no pin here', code=None))

    @patch('ocr.backends.tesseract.pytesseract.get_tesseract_version')
    def test_pin_never_matches_the_redeem_code_itself(self, mock_version):
        backend = TesseractOCRBackend()
        self.assertIsNone(backend._guess_pin('PIN: 4471', code='4471'))

    @patch('ocr.backends.tesseract.pytesseract.get_tesseract_version')
    def test_guesses_value_and_currency_from_symbol(self, mock_version):
        backend = TesseractOCRBackend()
        value, currency = backend._guess_value_and_currency('VOUCHER VALUE £50.00')
        self.assertEqual(value, 50.0)
        self.assertEqual(currency, 'GBP')

    @patch('ocr.backends.tesseract.pytesseract.get_tesseract_version')
    def test_guesses_value_and_currency_from_trailing_code(self, mock_version):
        backend = TesseractOCRBackend()
        value, currency = backend._guess_value_and_currency('Amount: 25.99 USD')
        self.assertEqual(value, 25.99)
        self.assertEqual(currency, 'USD')

    @patch('ocr.backends.tesseract.pytesseract.get_tesseract_version')
    def test_no_value_guess_without_a_currency_pairing(self, mock_version):
        backend = TesseractOCRBackend()
        value, currency = backend._guess_value_and_currency('Serial number 00103725714047298992')
        self.assertIsNone(value)
        self.assertIsNone(currency)

    @patch('ocr.backends.tesseract.pytesseract.image_to_data')
    @patch('ocr.backends.tesseract.pytesseract.get_tesseract_version')
    def test_extract_includes_pin_and_value_end_to_end(self, mock_version, mock_data):
        mock_data.return_value = {
            'text': ['CODE-83921X', 'PIN:', '4471', 'Value', '£50.00'],
            'conf': ['95', '90', '92', '88', '91'],
        }
        backend = TesseractOCRBackend()
        result = backend.extract(_tiny_png_bytes(), 'image/png')

        self.assertEqual(result['code'], 'CODE-83921X')
        self.assertEqual(result['pin'], '4471')
        self.assertEqual(result['value'], 50.0)
        self.assertEqual(result['currency'], 'GBP')
        self.assertIsNone(result['card_number'])
        self.assertIsNone(result['logo_slug'])
        self.assertIsNone(result['type'])
        self.assertIsNone(result['description'])
        self.assertIsNone(result['notes'])
        self.assertEqual(result['tags'], [])

    @patch('ocr.backends.tesseract.pytesseract.get_tesseract_version')
    def test_guesses_balance_check_url_from_printed_link(self, mock_version):
        backend = TesseractOCRBackend()
        self.assertEqual(
            backend._guess_balance_check_url('Check your balance at https://example.com/balance today'),
            'https://example.com/balance',
        )

    @patch('ocr.backends.tesseract.pytesseract.get_tesseract_version')
    def test_no_balance_check_url_without_a_scheme(self, mock_version):
        # Bare domains are deliberately not guessed from plain OCR text -
        # too noisy to be reliable without a vision model's layout sense.
        backend = TesseractOCRBackend()
        self.assertIsNone(backend._guess_balance_check_url('Check your balance at example.com/balance'))
