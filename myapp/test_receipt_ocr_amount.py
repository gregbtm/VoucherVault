from decimal import Decimal
from unittest.mock import patch, MagicMock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from datetime import timedelta
from django.utils import timezone

from myapp.models import Document, Item, Transaction
from myapp.tasks import extract_document_text_task


def _base_ocr_result(**overrides):
    """A minimal structured-extraction dict, shaped like what any OCR
    backend's extract() returns - only the fields extract_document_text_task
    itself reads are populated by default."""
    result = {
        'name': None, 'issuer': None, 'description': None, 'notes': None,
        'code': None, 'value': None, 'currency': None,
    }
    result.update(overrides)
    return result


class ReceiptOCRAmountExtractionTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('receiptuser', 'receipt@example.com', 'password123')
        self.item = Item.objects.create(
            user=self.user, name='Test Card', type='giftcard', redeem_code='CODE1',
            issuer='Test Issuer', value=Decimal('50.00'), value_type='money',
            expiry_date=timezone.localtime().date() + timedelta(days=30),
        )
        self.document = Document.objects.create(
            item=self.item,
            file=SimpleUploadedFile('receipt.jpg', b'fake image bytes', content_type='image/jpeg'),
        )

    def _run_task_with_result(self, result):
        mock_backend = MagicMock()
        mock_backend.extract.return_value = result
        with patch('ocr.backends.ocr_enabled', return_value=True), \
             patch('ocr.backends.get_backend', return_value=mock_backend):
            extract_document_text_task(self.document.id)

    def test_logs_a_spend_transaction_when_a_value_is_extracted(self):
        self._run_task_with_result(_base_ocr_result(value=12.50, currency='GBP'))
        tx = Transaction.objects.get(item=self.item)
        self.assertEqual(tx.value, Decimal('-12.50'))
        self.assertIn(str(self.document.id), tx.description)

    def test_balance_reflects_the_logged_transaction(self):
        self._run_task_with_result(_base_ocr_result(value=12.50))
        self.item.refresh_from_db()
        self.assertEqual(self.item.get_current_balance(), Decimal('37.50'))

    def test_no_transaction_when_no_value_extracted(self):
        self._run_task_with_result(_base_ocr_result(value=None))
        self.assertFalse(Transaction.objects.filter(item=self.item).exists())

    def test_no_transaction_for_a_zero_or_negative_value(self):
        self._run_task_with_result(_base_ocr_result(value=0))
        self.assertFalse(Transaction.objects.filter(item=self.item).exists())

        self._run_task_with_result(_base_ocr_result(value=-5))
        self.assertFalse(Transaction.objects.filter(item=self.item).exists())

    def test_no_transaction_for_an_implausibly_large_value(self):
        """Guards against an OCR misread (e.g. a barcode digit string
        mistaken for an amount) being logged as a five/six-figure spend."""
        self._run_task_with_result(_base_ocr_result(value=250000))
        self.assertFalse(Transaction.objects.filter(item=self.item).exists())

    def test_no_transaction_for_a_non_numeric_value(self):
        self._run_task_with_result(_base_ocr_result(value='not-a-number'))
        self.assertFalse(Transaction.objects.filter(item=self.item).exists())

    def test_no_transaction_for_non_money_items(self):
        """Loyalty cards/travel passes have no balance concept."""
        loyalty_item = Item.objects.create(
            user=self.user, name='Loyalty Card', type='loyaltycard', redeem_code='LOY1',
            issuer='Test Issuer', value=Decimal('0'), value_type='points',
            expiry_date=timezone.localtime().date() + timedelta(days=30),
        )
        document = Document.objects.create(
            item=loyalty_item,
            file=SimpleUploadedFile('receipt2.jpg', b'fake image bytes', content_type='image/jpeg'),
        )
        mock_backend = MagicMock()
        mock_backend.extract.return_value = _base_ocr_result(value=12.50)
        with patch('ocr.backends.ocr_enabled', return_value=True), \
             patch('ocr.backends.get_backend', return_value=mock_backend):
            extract_document_text_task(document.id)
        self.assertFalse(Transaction.objects.filter(item=loyalty_item).exists())

    def test_does_not_log_the_same_document_twice(self):
        """Re-running OCR on the same document (e.g. a retry) must not
        double-log the same receipt as two separate spends."""
        self._run_task_with_result(_base_ocr_result(value=12.50))
        self._run_task_with_result(_base_ocr_result(value=12.50))
        self.assertEqual(Transaction.objects.filter(item=self.item).count(), 1)

    def test_extracted_text_still_populated_alongside_the_transaction(self):
        """The pre-existing extracted_text behavior is unaffected by the
        new amount-extraction logic added alongside it."""
        self._run_task_with_result(_base_ocr_result(value=12.50, name='Some Shop', code='ABC123'))
        self.document.refresh_from_db()
        self.assertIn('Some Shop', self.document.extracted_text)
        self.assertIn('ABC123', self.document.extracted_text)
        self.assertTrue(Transaction.objects.filter(item=self.item).exists())

    def test_ocr_disabled_does_nothing(self):
        mock_backend = MagicMock()
        mock_backend.extract.return_value = _base_ocr_result(value=12.50)
        with patch('ocr.backends.ocr_enabled', return_value=False), \
             patch('ocr.backends.get_backend', return_value=mock_backend):
            extract_document_text_task(self.document.id)
        self.assertFalse(Transaction.objects.filter(item=self.item).exists())
        mock_backend.extract.assert_not_called()

    @patch('notify.tasks.notify_balance_changed')
    def test_notify_balance_changed_fired_for_the_new_transaction(self, mock_notify):
        self._run_task_with_result(_base_ocr_result(value=12.50))
        mock_notify.assert_called_once()
        args, _ = mock_notify.call_args
        self.assertEqual(args[0], self.item)
        self.assertEqual(args[1].value, Decimal('-12.50'))
