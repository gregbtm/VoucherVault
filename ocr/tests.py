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
        self.assertEqual(result['code_type'], 'code128')
        self.assertEqual(result['expiry_date'], '2026-12-31')
        self.assertIsNone(result['name'])
        self.assertIsNone(result['issuer'])
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
            text='{"code": "SAVE20", "code_type": "code128", "name": "Acme", "issuer": null, "expiry_date": "2026-12-31", "confidence": 0.9}',
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
        self.assertEqual(result['confidence'], 0.9)
        mock_client.messages.create.assert_called_once()

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
            content='{"code": "SAVE20", "code_type": "code128", "name": "Acme", "issuer": null, "expiry_date": "2026-12-31", "confidence": 0.9}',
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
        self.assertEqual(result['confidence'], 0.9)
        mock_client.chat.completions.create.assert_called_once()

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
