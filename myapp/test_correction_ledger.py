import uuid
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from myapp.models import Item, ItemEnrichmentLog, ScanFieldCorrection
from myapp.scan_learning import record_enrichment_correction_feedback


def _make_item(user, **kwargs):
    defaults = {
        'type': 'voucher',
        'name': 'Test Voucher',
        'redeem_code': 'ABC123',
        'issuer': 'Acme',
        'expiry_date': date.today() + timedelta(days=30),
        'value': Decimal('10.00'),
        'user': user,
    }
    defaults.update(kwargs)
    return Item.objects.create(**defaults)


def _make_log(item, field_name, new_value, enrichment_type='merchant_lookup'):
    return ItemEnrichmentLog.objects.create(
        enrichment_run_id=uuid.uuid4(), item=item, field_name=field_name,
        old_value='irrelevant', new_value=new_value, enrichment_type=enrichment_type,
    )


class RecordEnrichmentCorrectionFeedbackTestCase(TestCase):
    """
    scan_learning.record_enrichment_correction_feedback() is the enrichment
    counterpart to record_scan_corrections() - both feed the same
    ScanFieldCorrection ledger (see myapp/scan_learning.py and the
    'unify correction ledgers' plan item), so a value the enrichment
    pipeline auto-applied and the user then reverted is just as learnable
    as a value an OCR scan got wrong.
    """

    def setUp(self):
        self.user = User.objects.create_user('ledgeruser', 'l@example.com', 'password123')

    def test_reverting_an_enrichment_fill_records_an_enrichment_sourced_correction(self):
        item = _make_item(self.user, issuer='Amazon')
        _make_log(item, 'issuer', 'Amazon', enrichment_type='merchant_lookup')

        item.issuer = 'Amzn Inc'
        record_enrichment_correction_feedback(self.user, item, {'issuer': 'Amazon'})

        correction = ScanFieldCorrection.objects.get(user=self.user, field='issuer', ai_value='Amazon')
        self.assertEqual(correction.corrected_value, 'Amzn Inc')
        self.assertEqual(correction.source, 'enrichment')
        self.assertEqual(correction.enrichment_method, 'merchant_lookup')
        self.assertEqual(correction.times_seen, 1)

    def test_repeated_reversal_increments_times_seen(self):
        item = _make_item(self.user, issuer='Amzn Inc')
        _make_log(item, 'issuer', 'Amazon', enrichment_type='merchant_lookup')
        ScanFieldCorrection.objects.create(
            user=self.user, item_type=item.type, field='issuer', ai_value='Amazon',
            corrected_value='Amzn Inc', source='enrichment', enrichment_method='merchant_lookup',
        )

        record_enrichment_correction_feedback(self.user, item, {'issuer': 'Amazon'})

        correction = ScanFieldCorrection.objects.get(user=self.user, field='issuer', ai_value='Amazon')
        self.assertEqual(correction.times_seen, 2)

    def test_no_enrichment_log_means_no_correction_recorded(self):
        item = _make_item(self.user, issuer='Amzn Inc')

        record_enrichment_correction_feedback(self.user, item, {'issuer': 'Amazon'})

        self.assertFalse(ScanFieldCorrection.objects.filter(user=self.user, field='issuer').exists())

    def test_field_already_edited_since_enrichment_is_not_treated_as_a_reversal(self):
        item = _make_item(self.user, issuer='Something Else Entirely')
        _make_log(item, 'issuer', 'Amazon', enrichment_type='merchant_lookup')

        # before_values shows the field was already 'Something Else
        # Entirely' before this save - not the log's 'Amazon' - so this
        # save isn't a reaction to the enrichment fill at all.
        record_enrichment_correction_feedback(self.user, item, {'issuer': 'Something Else Entirely'})

        self.assertFalse(ScanFieldCorrection.objects.filter(user=self.user, field='issuer').exists())

    def test_keeping_the_enrichment_value_retires_a_stale_enrichment_correction(self):
        item = _make_item(self.user, issuer='Amazon')
        _make_log(item, 'issuer', 'Amazon', enrichment_type='merchant_lookup')
        ScanFieldCorrection.objects.create(
            user=self.user, item_type=item.type, field='issuer', ai_value='Amazon',
            corrected_value='Amzn Inc', source='enrichment', enrichment_method='merchant_lookup',
        )

        # User kept the enrichment's value this time.
        record_enrichment_correction_feedback(self.user, item, {'issuer': 'Amazon'})

        self.assertFalse(ScanFieldCorrection.objects.filter(user=self.user, field='issuer').exists())

    def test_a_scan_sourced_correction_for_the_same_value_is_left_alone_when_kept(self):
        item = _make_item(self.user, issuer='Amazon')
        _make_log(item, 'issuer', 'Amazon', enrichment_type='merchant_lookup')
        scan_correction = ScanFieldCorrection.objects.create(
            user=self.user, item_type=item.type, field='issuer', ai_value='Amazon',
            corrected_value='Something Unrelated', source='scan',
        )

        record_enrichment_correction_feedback(self.user, item, {'issuer': 'Amazon'})

        scan_correction.refresh_from_db()  # untouched - only source='enrichment' rows are retired here
        self.assertEqual(scan_correction.corrected_value, 'Something Unrelated')

    def test_a_correction_for_a_different_item_type_is_left_alone_when_kept(self):
        # Same field/ai_value/source, but a different item_type - keeping
        # the enrichment value for a 'voucher' item must not retire a
        # correction that only applies to a different item type.
        item = _make_item(self.user, issuer='Amazon')
        _make_log(item, 'issuer', 'Amazon', enrichment_type='merchant_lookup')
        other_type_correction = ScanFieldCorrection.objects.create(
            user=self.user, item_type='giftcard', field='issuer', ai_value='Amazon',
            corrected_value='Amazon.co.uk', source='enrichment', enrichment_method='merchant_lookup',
        )

        record_enrichment_correction_feedback(self.user, item, {'issuer': 'Amazon'})

        other_type_correction.refresh_from_db()
        self.assertEqual(other_type_correction.corrected_value, 'Amazon.co.uk')

    def test_only_latest_enrichment_log_for_the_field_is_consulted(self):
        item = _make_item(self.user, issuer='Amazon UK')
        _make_log(item, 'issuer', 'Amzn', enrichment_type='merchant_lookup')
        _make_log(item, 'issuer', 'Amazon UK', enrichment_type='validation')

        record_enrichment_correction_feedback(self.user, item, {'issuer': 'Amazon UK'})
        item.issuer = 'Amazon UK Ltd'
        record_enrichment_correction_feedback(self.user, item, {'issuer': 'Amazon UK'})

        correction = ScanFieldCorrection.objects.get(user=self.user, field='issuer', ai_value='Amazon UK')
        self.assertEqual(correction.enrichment_method, 'validation')


class EditItemViewRecordsEnrichmentCorrectionsTestCase(TestCase):
    """End-to-end: editing an item through the real view wires the
    pre-save snapshot and post-save call correctly."""

    def setUp(self):
        self.user = User.objects.create_user('ledgerview', 'lv@example.com', 'password123')
        self.client.force_login(self.user)

    def test_editing_away_an_enriched_issuer_creates_a_correction(self):
        item = _make_item(self.user, issuer='Amazon')
        _make_log(item, 'issuer', 'Amazon', enrichment_type='merchant_lookup')

        response = self.client.post(reverse('edit_item', args=[item.id]), {
            'type': item.type, 'name': item.name, 'issuer': 'Amzn Inc',
            'redeem_code': item.redeem_code, 'value': item.value, 'currency': 'GBP',
            'code_type': 'none', 'value_type': 'money',
            'issue_date': date.today().isoformat(), 'expiry_date': item.expiry_date.isoformat(),
        })

        self.assertRedirects(response, reverse('view_item', args=[item.id]))
        correction = ScanFieldCorrection.objects.get(user=self.user, field='issuer', ai_value='Amazon')
        self.assertEqual(correction.corrected_value, 'Amzn Inc')
        self.assertEqual(correction.source, 'enrichment')
        self.assertEqual(correction.enrichment_method, 'merchant_lookup')
