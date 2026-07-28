from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from myapp.models import Document, Item
from myapp.services.enrichment import ItemEnricher
from myapp.test_utils import set_site_config
from ocr.backends.base import sanitize_confidence
from ocr.backends.claude_backend import ClaudeOCRBackend
from ocr.validation import validate_and_score


class SanitizeConfidenceTestCase(TestCase):

    def test_clamps_above_one(self):
        self.assertEqual(sanitize_confidence(1.5, default=0.5), 1.0)

    def test_clamps_below_zero(self):
        self.assertEqual(sanitize_confidence(-0.2, default=0.5), 0.0)

    def test_falls_back_to_default_when_none(self):
        self.assertEqual(sanitize_confidence(None, default=0.42), 0.42)

    def test_falls_back_to_default_when_unparseable(self):
        self.assertEqual(sanitize_confidence('not-a-number', default=0.42), 0.42)

    def test_passes_through_a_valid_value(self):
        self.assertEqual(sanitize_confidence(0.73, default=0.5), 0.73)


class ValidateAndScorePerFieldConfidenceTestCase(TestCase):
    """
    expiry_date_confidence and value_confidence (from the OCR prompt's
    self-consistency re-read) must move independently of each other and
    of the overall 'confidence' - a bad date shouldn't drag down trust in
    a perfectly legible value, and vice versa.
    """

    def _base_extraction(self, **overrides):
        extraction = {
            'code': 'SAVE20', 'code_type': None, 'name': None, 'issuer': None,
            'expiry_date': '2027-01-01', 'pin': None, 'value': 25.0, 'currency': None,
            'card_number': None, 'logo_slug': None, 'balance_check_url': None,
            'type': None, 'description': None, 'notes': None, 'tags': [],
            'journey_origin': None, 'journey_destination': None, 'travel_time': None,
            'confidence': 0.9,
        }
        extraction.update(overrides)
        return extraction

    def test_valid_fields_keep_the_model_reported_confidence(self):
        result = validate_and_score(self._base_extraction(
            expiry_date_confidence=0.95, value_confidence=0.88,
        ))
        self.assertEqual(result['expiry_date_confidence'], 0.95)
        self.assertEqual(result['value_confidence'], 0.88)
        self.assertEqual(result['confidence'], 0.9)

    def test_missing_per_field_confidence_falls_back_to_overall(self):
        result = validate_and_score(self._base_extraction())
        self.assertEqual(result['expiry_date_confidence'], 0.9)
        self.assertEqual(result['value_confidence'], 0.9)

    def test_invalid_date_lowers_only_expiry_date_confidence(self):
        result = validate_and_score(self._base_extraction(
            expiry_date='not-a-date', expiry_date_confidence=0.9, value_confidence=0.9,
        ))
        self.assertLess(result['expiry_date_confidence'], 0.9)
        self.assertEqual(result['value_confidence'], 0.9)  # untouched

    def test_non_numeric_value_lowers_only_value_confidence(self):
        result = validate_and_score(self._base_extraction(
            value='not-a-number', expiry_date_confidence=0.9, value_confidence=0.9,
        ))
        self.assertEqual(result['expiry_date_confidence'], 0.9)  # untouched
        self.assertLess(result['value_confidence'], 0.9)

    def test_absent_fields_get_no_confidence_key(self):
        result = validate_and_score(self._base_extraction(expiry_date=None, value=None))
        self.assertNotIn('expiry_date_confidence', result)
        self.assertNotIn('value_confidence', result)


class ClaudeBackendPerFieldConfidenceTestCase(TestCase):

    def setUp(self):
        set_site_config(anthropic_api_key='test-key')

    @patch('ocr.backends.claude_backend.anthropic.Anthropic')
    def test_parses_per_field_confidence_from_response(self, mock_anthropic_cls):
        mock_client = MagicMock()
        mock_block = MagicMock(
            type='text',
            text=(
                '{"code": "SAVE20", "expiry_date": "2027-01-01", "value": 25.0, '
                '"confidence": 0.6, "expiry_date_confidence": 0.95, "value_confidence": 0.4}'
            ),
        )
        mock_client.messages.create.return_value = MagicMock(content=[mock_block])
        mock_anthropic_cls.return_value = mock_client

        result = ClaudeOCRBackend().extract(b'fake-bytes', 'image/jpeg')

        self.assertEqual(result['confidence'], 0.6)
        self.assertEqual(result['expiry_date_confidence'], 0.95)
        self.assertEqual(result['value_confidence'], 0.4)

    @patch('ocr.backends.claude_backend.anthropic.Anthropic')
    def test_falls_back_to_overall_confidence_when_omitted(self, mock_anthropic_cls):
        mock_client = MagicMock()
        mock_block = MagicMock(
            type='text',
            text='{"code": "SAVE20", "expiry_date": "2027-01-01", "value": 25.0, "confidence": 0.6}',
        )
        mock_client.messages.create.return_value = MagicMock(content=[mock_block])
        mock_anthropic_cls.return_value = mock_client

        result = ClaudeOCRBackend().extract(b'fake-bytes', 'image/jpeg')

        self.assertEqual(result['expiry_date_confidence'], 0.6)
        self.assertEqual(result['value_confidence'], 0.6)


class ExtractFromDocumentsUsesPerFieldConfidenceTestCase(TestCase):
    """
    ItemEnricher._extract_from_documents merges multiple documents'
    extractions field-by-field, preferring whichever document read that
    specific field more confidently - not whichever had the higher
    *overall* confidence, which could belong to an unrelated field.
    """

    def setUp(self):
        self.user = User.objects.create_user('perfieldconfuser', 'pfc@example.com', 'password123')
        self.enricher = ItemEnricher()

    def _item(self):
        from datetime import date
        return Item.objects.create(
            user=self.user, name='Test', type='giftcard', redeem_code='X',
            issuer='Issuer', expiry_date=date(2027, 1, 1), value=Decimal('10.00'),
        )

    @patch('ocr.backends.get_backend')
    @patch('ocr.backends.ocr_enabled', return_value=True)
    def test_higher_field_confidence_wins_even_with_lower_overall_confidence(self, mock_enabled, mock_get_backend):
        item = self._item()
        Document.objects.create(item=item, file=SimpleUploadedFile('a.png', b'a', content_type='image/png'))
        Document.objects.create(item=item, file=SimpleUploadedFile('b.png', b'b', content_type='image/png'))

        mock_backend = MagicMock()
        mock_backend.extract.side_effect = [
            # Document A: high overall confidence, but a poor value read.
            {'value': 19.99, 'confidence': 0.9, 'value_confidence': 0.3},
            # Document B: lower overall confidence, but a confident value read.
            {'value': 25.00, 'confidence': 0.5, 'value_confidence': 0.95},
        ]
        mock_get_backend.return_value = mock_backend

        merged = self.enricher._extract_from_documents(Document.objects.filter(item=item), user=self.user)

        self.assertEqual(merged['value'], 25.00)
        self.assertEqual(merged['value_confidence'], Decimal('0.95'))
