from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch, MagicMock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from myapp.models import Document, Item, ScanFieldCorrection
from myapp.services.enrichment import ItemEnricher


def _make_item(user, **kwargs):
    defaults = {
        'user': user, 'name': 'Test Item', 'type': 'giftcard',
        'redeem_code': 'TEST123', 'issuer': 'Test Issuer',
        'expiry_date': date.today() + timedelta(days=30),
        'value': Decimal('50.00'),
    }
    defaults.update(kwargs)
    return Item.objects.create(**defaults)


class EffectiveConfidenceThresholdTestCase(TestCase):
    """
    ItemEnricher._effective_confidence_threshold self-tunes a field+method's
    required confidence based on how many times this user has corrected
    away a value that method previously auto-applied for that field (see
    the unified ScanFieldCorrection ledger from the previous plan item).
    """

    def setUp(self):
        self.user = User.objects.create_user('tuninguser', 'tu@example.com', 'password123')
        self.enricher = ItemEnricher(created_by=self.user)

    def test_no_history_leaves_base_threshold_unchanged(self):
        effective = self.enricher._effective_confidence_threshold(
            self.user, 'merchant_lookup', 'issuer', Decimal('0.5'),
        )
        self.assertEqual(effective, Decimal('0.5'))

    def test_each_correction_raises_the_threshold(self):
        for ai_value in ('Amzn', 'Amazon UK', 'AMZ'):
            ScanFieldCorrection.objects.create(
                user=self.user, item_type='giftcard', field='issuer', ai_value=ai_value,
                corrected_value='Amazon', source='enrichment', enrichment_method='merchant_lookup',
            )
        effective = self.enricher._effective_confidence_threshold(
            self.user, 'merchant_lookup', 'issuer', Decimal('0.5'),
        )
        self.assertEqual(effective, Decimal('0.8'))  # 0.5 + 3 * 0.1

    def test_penalty_is_capped(self):
        for i in range(10):
            ScanFieldCorrection.objects.create(
                user=self.user, item_type='giftcard', field='issuer', ai_value=f'val{i}',
                corrected_value='Amazon', source='enrichment', enrichment_method='merchant_lookup',
            )
        effective = self.enricher._effective_confidence_threshold(
            self.user, 'merchant_lookup', 'issuer', Decimal('0.5'),
        )
        self.assertEqual(effective, Decimal('0.9'))  # 0.5 + capped 0.4

    def test_never_exceeds_max_effective_threshold(self):
        for i in range(10):
            ScanFieldCorrection.objects.create(
                user=self.user, item_type='giftcard', field='issuer', ai_value=f'val{i}',
                corrected_value='Amazon', source='enrichment', enrichment_method='merchant_lookup',
            )
        effective = self.enricher._effective_confidence_threshold(
            self.user, 'merchant_lookup', 'issuer', Decimal('0.9'),
        )
        self.assertEqual(effective, Decimal('0.95'))

    def test_scan_sourced_corrections_do_not_count_toward_the_penalty(self):
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='giftcard', field='issuer', ai_value='Amzn',
            corrected_value='Amazon', source='scan',
        )
        effective = self.enricher._effective_confidence_threshold(
            self.user, 'merchant_lookup', 'issuer', Decimal('0.5'),
        )
        self.assertEqual(effective, Decimal('0.5'))

    def test_a_different_method_on_the_same_field_is_tracked_separately(self):
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='giftcard', field='issuer', ai_value='Amzn',
            corrected_value='Amazon', source='enrichment', enrichment_method='ocr_rescan',
        )
        effective = self.enricher._effective_confidence_threshold(
            self.user, 'merchant_lookup', 'issuer', Decimal('0.5'),
        )
        self.assertEqual(effective, Decimal('0.5'))

    def test_result_is_cached_per_enricher_instance(self):
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='giftcard', field='issuer', ai_value='Amzn',
            corrected_value='Amazon', source='enrichment', enrichment_method='merchant_lookup',
        )
        first = self.enricher._effective_confidence_threshold(
            self.user, 'merchant_lookup', 'issuer', Decimal('0.5'),
        )
        # A correction recorded after the first lookup should not change the
        # cached result within the same enricher instance/run.
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='giftcard', field='issuer', ai_value='AMZ',
            corrected_value='Amazon', source='enrichment', enrichment_method='merchant_lookup',
        )
        second = self.enricher._effective_confidence_threshold(
            self.user, 'merchant_lookup', 'issuer', Decimal('0.5'),
        )
        self.assertEqual(first, second)


class OcrEnrichmentRespectsTunedThresholdTestCase(TestCase):
    """End-to-end: a field with enough recorded OCR-sourced reversals
    stops auto-applying a fill that would have passed the flat default."""

    def setUp(self):
        self.user = User.objects.create_user('ocrtuninguser', 'ot@example.com', 'password123')
        self.enricher = ItemEnricher(created_by=self.user)

    def _record_reversals(self, count, field='issuer', method='ocr_rescan'):
        for i in range(count):
            ScanFieldCorrection.objects.create(
                user=self.user, item_type='giftcard', field=field, ai_value=f'wrong{i}',
                corrected_value='Right Value', source='enrichment', enrichment_method=method,
            )

    @patch('ocr.backends.get_backend')
    @patch('ocr.backends.ocr_enabled', return_value=True)
    def test_fill_applies_with_no_correction_history(self, mock_enabled, mock_get_backend):
        item = _make_item(self.user, issuer='')
        Document.objects.create(item=item, file=SimpleUploadedFile('r.png', b'x', content_type='image/png'))
        mock_backend = MagicMock()
        mock_backend.extract.return_value = {'issuer': 'Costa Coffee', 'confidence': 0.6}
        mock_get_backend.return_value = mock_backend

        result = self.enricher.enrich_from_ocr(item, confidence_threshold=Decimal('0.5'))

        self.assertEqual(len(result.changes), 1)
        self.assertEqual(result.changes[0].field_name, 'issuer')

    @patch('ocr.backends.get_backend')
    @patch('ocr.backends.ocr_enabled', return_value=True)
    def test_same_fill_is_withheld_after_repeated_reversals(self, mock_enabled, mock_get_backend):
        item = _make_item(self.user, issuer='')
        self._record_reversals(4, field='issuer', method='ocr_rescan')  # -> +0.4 penalty
        Document.objects.create(item=item, file=SimpleUploadedFile('r.png', b'x', content_type='image/png'))
        mock_backend = MagicMock()
        mock_backend.extract.return_value = {'issuer': 'Costa Coffee', 'confidence': 0.6}
        mock_get_backend.return_value = mock_backend

        result = self.enricher.enrich_from_ocr(item, confidence_threshold=Decimal('0.5'))

        self.assertEqual(len(result.changes), 0)

    @patch('ocr.backends.get_backend')
    @patch('ocr.backends.ocr_enabled', return_value=True)
    def test_high_enough_confidence_still_applies_despite_history(self, mock_enabled, mock_get_backend):
        item = _make_item(self.user, issuer='')
        self._record_reversals(4, field='issuer', method='ocr_rescan')  # threshold -> 0.9
        Document.objects.create(item=item, file=SimpleUploadedFile('r.png', b'x', content_type='image/png'))
        mock_backend = MagicMock()
        mock_backend.extract.return_value = {'issuer': 'Costa Coffee', 'confidence': 0.95}
        mock_get_backend.return_value = mock_backend

        result = self.enricher.enrich_from_ocr(item, confidence_threshold=Decimal('0.5'))

        self.assertEqual(len(result.changes), 1)
