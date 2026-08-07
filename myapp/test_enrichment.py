from decimal import Decimal
from unittest.mock import patch, MagicMock
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse
from myapp.models import (
    EnrichmentFieldPreference,
    EnrichmentRun,
    EnrichmentRunItem,
    Document,
    Item,
    ItemEnrichmentLog,
    MerchantProfile,
    ScanFieldCorrection,
)
from myapp.services.enrichment import (
    BulkEnricher,
    FieldChange,
    ItemEnricher,
    get_active_flags_for_items,
    get_at_risk_signals_for_items,
)
from datetime import date, timedelta
from django.utils import timezone
import uuid


class ItemEnricherTestCase(TestCase):
    """Test the ItemEnricher service."""

    def setUp(self):
        self.user = User.objects.create_user('testuser', 'test@example.com', 'password123')
        self.enricher = ItemEnricher(created_by=self.user)

    def _create_test_item(self, **kwargs):
        """Helper to create a test item with all required fields."""
        defaults = {
            'user': self.user,
            'name': 'Test Item',
            'type': 'giftcard',
            'redeem_code': 'TEST123',
            'issuer': 'Test Issuer',
            'expiry_date': date(2025, 12, 31),
            'value': Decimal('50.00'),
        }
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    def test_protected_fields_not_enriched(self):
        """Verify protected fields cannot be modified."""
        item = self._create_test_item()

        # Attempt to modify a protected field
        for protected_field in ['redeem_code', 'code_type', 'type']:
            self.assertIn(protected_field, ItemEnricher.PROTECTED_FIELDS)

    def test_enrichable_fields_defined(self):
        """Verify enrichable fields are properly defined."""
        self.assertIn('issuer', ItemEnricher.ENRICHABLE_FIELDS)
        self.assertIn('expiry_date', ItemEnricher.ENRICHABLE_FIELDS)
        self.assertIn('value', ItemEnricher.ENRICHABLE_FIELDS)

    def test_field_change_dataclass(self):
        """Test FieldChange dataclass creation."""
        change = FieldChange(
            field_name='issuer',
            old_value='Old Issuer',
            new_value='New Issuer',
            confidence_score=Decimal('0.85'),
            reason='Extracted from OCR'
        )

        self.assertEqual(change.field_name, 'issuer')
        self.assertEqual(change.old_value, 'Old Issuer')
        self.assertEqual(change.new_value, 'New Issuer')
        self.assertEqual(change.confidence_score, Decimal('0.85'))

    def test_enrich_from_ocr_no_documents(self):
        """Test OCR enrichment when item has no documents."""
        item = self._create_test_item()

        result = self.enricher.enrich_from_ocr(item)

        self.assertFalse(result.success)
        self.assertIsNotNone(result.error_message)
        self.assertEqual(len(result.changes), 0)

    @patch('ocr.backends.get_backend')
    @patch('ocr.backends.ocr_enabled', return_value=True)
    def test_enrich_from_ocr_reads_attached_document(self, mock_enabled, mock_get_backend):
        """
        Regression test: enrich_from_ocr used to go through a separate,
        broken OCR client (myapp.services.ocr_backend_integration) that
        read its API key from an unset env var and called the wrong SDK
        method, so it silently failed on every real document - it now
        reuses ocr.backends.get_backend(), the same SiteConfiguration-driven
        backend the rest of the app uses successfully.
        """
        item = self._create_test_item(issuer='')
        Document.objects.create(item=item, file=SimpleUploadedFile('receipt.png', b'fake-image-bytes', content_type='image/png'))

        mock_backend = MagicMock()
        mock_backend.extract.return_value = {
            'issuer': 'Costa Coffee',
            'expiry_date': None,
            'confidence': 0.88,
        }
        mock_get_backend.return_value = mock_backend

        result = self.enricher.enrich_from_ocr(item, confidence_threshold=Decimal('0.5'))

        mock_backend.extract.assert_called_once()
        called_bytes, called_mime = mock_backend.extract.call_args[0]
        self.assertEqual(called_bytes, b'fake-image-bytes')
        self.assertEqual(called_mime, 'image/png')

        self.assertTrue(result.success)
        self.assertEqual(len(result.changes), 1)
        self.assertEqual(result.changes[0].field_name, 'issuer')
        self.assertEqual(result.changes[0].new_value, 'Costa Coffee')
        self.assertEqual(result.changes[0].confidence_score, Decimal('0.88'))

    @patch('ocr.backends.ocr_enabled', return_value=False)
    def test_enrich_from_ocr_skips_gracefully_when_ocr_disabled(self, mock_enabled):
        """OCR-disabled deployments shouldn't error, just produce no changes."""
        item = self._create_test_item()
        Document.objects.create(item=item, file=SimpleUploadedFile('receipt.png', b'fake-image-bytes', content_type='image/png'))

        result = self.enricher.enrich_from_ocr(item)

        self.assertFalse(result.success)
        self.assertEqual(len(result.changes), 0)

    def test_validate_and_normalize(self):
        """Test validation and normalization enrichment."""
        item = self._create_test_item(issuer='  loose spaces  ')

        result = self.enricher.validate_and_normalize(item)

        # Should have at least the issuer normalization
        self.assertTrue(result.success)

    def test_apply_changes_respects_protected_fields(self):
        """Verify apply_changes rejects changes to protected fields."""
        item = self._create_test_item()

        # Try to apply a change to a protected field
        change = FieldChange(
            field_name='redeem_code',
            old_value='TEST123',
            new_value='NEW456',
            confidence_score=Decimal('0.9'),
            reason='Test'
        )

        success, error = self.enricher.apply_changes(item, [change])

        self.assertFalse(success)
        self.assertIn('protected', error.lower())

    def test_apply_changes_valid_field(self):
        """Test applying changes to valid enrichable fields."""
        item = self._create_test_item(issuer='Old Issuer')

        change = FieldChange(
            field_name='issuer',
            old_value='Old Issuer',
            new_value='New Issuer',
            confidence_score=Decimal('0.9'),
            reason='Test enrichment'
        )

        success, error = self.enricher.apply_changes(item, [change])

        self.assertTrue(success)
        self.assertIsNone(error)

        # Verify the change was applied
        item.refresh_from_db()
        self.assertEqual(item.issuer, 'New Issuer')

    def test_log_enrichment(self):
        """Test enrichment logging to audit trail."""
        item = self._create_test_item()

        enrichment_run_id = str(uuid.uuid4())
        changes = [
            FieldChange(
                field_name='issuer',
                old_value='Old',
                new_value='New',
                confidence_score=Decimal('0.8'),
                reason='Test'
            )
        ]

        self.enricher.log_enrichment(item, enrichment_run_id, changes, 'validation')

        # Verify log was created
        logs = ItemEnrichmentLog.objects.filter(enrichment_run_id=enrichment_run_id)
        self.assertEqual(logs.count(), 1)

        log = logs.first()
        self.assertEqual(log.item, item)
        self.assertEqual(log.field_name, 'issuer')
        self.assertEqual(log.old_value, 'Old')
        self.assertEqual(log.new_value, 'New')
        self.assertEqual(log.enrichment_type, 'validation')
        self.assertEqual(log.confidence_score, Decimal('0.8'))

    def test_enrichment_preview(self):
        """Test enrichment preview generation."""
        changes = [
            FieldChange(
                field_name='issuer',
                old_value='A',
                new_value='B',
                confidence_score=Decimal('0.9'),
                reason='Test'
            ),
            FieldChange(
                field_name='expiry_date',
                old_value='2025-12-31',
                new_value='2025-12-31',
                confidence_score=Decimal('0.8'),
                reason='Test'
            )
        ]

        preview = self.enricher.get_enrichment_preview(changes)

        self.assertEqual(preview['change_count'], 2)
        self.assertEqual(len(preview['changes']), 2)
        self.assertGreater(preview['total_confidence'], 0)


class BulkEnricherTestCase(TestCase):
    """Test the BulkEnricher service."""

    def setUp(self):
        self.user = User.objects.create_user('testuser', 'test@example.com', 'password123')
        self.bulk_enricher = BulkEnricher(created_by=self.user)

    def _create_test_item(self, **kwargs):
        """Helper to create a test item with all required fields."""
        defaults = {
            'user': self.user,
            'name': 'Test Item',
            'type': 'giftcard',
            'redeem_code': 'TEST123',
            'issuer': 'Test Issuer',
            'expiry_date': date(2025, 12, 31),
            'value': Decimal('50.00'),
        }
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    def test_enrich_selected_items_empty_list(self):
        """Test bulk enrichment with empty item list."""
        results = self.bulk_enricher.enrich_selected_items([], 'ocr')

        self.assertEqual(len(results), 0)

    def test_enrich_selected_items_nonexistent(self):
        """Test bulk enrichment with nonexistent item IDs."""
        results = self.bulk_enricher.enrich_selected_items([999, 1000], 'ocr')

        self.assertEqual(len(results), 2)
        for result in results.values():
            self.assertFalse(result.success)
            self.assertIn('not found', result.error_message.lower())

    def test_enrich_selected_items_dry_run(self):
        """Test dry run mode doesn't apply changes."""
        item = self._create_test_item(issuer='Original Issuer')

        # Dry run should not modify the item
        results = self.bulk_enricher.enrich_selected_items(
            [item.id],
            'validation',
            dry_run=True
        )

        item.refresh_from_db()
        self.assertEqual(item.issuer, 'Original Issuer')

    def test_enrichment_summary(self):
        """Test enrichment summary generation."""
        from myapp.services.enrichment import EnrichmentResult

        results = {
            1: EnrichmentResult(
                item_id=1,
                enrichment_run_id='run-1',
                changes=[
                    FieldChange('issuer', 'A', 'B', Decimal('0.9'), 'Test'),
                    FieldChange('expiry_date', '2025-12-31', '2025-12-31', Decimal('0.8'), 'Test'),
                ],
                success=True
            ),
            2: EnrichmentResult(
                item_id=2,
                enrichment_run_id='run-2',
                changes=[
                    FieldChange('issuer', 'X', 'Y', Decimal('0.85'), 'Test'),
                ],
                success=True
            ),
        }

        summary = self.bulk_enricher.get_enrichment_summary(results)

        self.assertEqual(summary['total_items'], 2)
        self.assertEqual(summary['successful_enrichments'], 2)
        self.assertEqual(summary['total_changes'], 3)
        self.assertGreater(summary['average_confidence'], 0)
        self.assertEqual(len(summary['errors']), 0)

    def test_enrichment_summary_with_errors(self):
        """Test enrichment summary includes errors."""
        from myapp.services.enrichment import EnrichmentResult

        results = {
            1: EnrichmentResult(
                item_id=1,
                enrichment_run_id='run-1',
                changes=[],
                success=False,
                error_message='Item not found'
            ),
        }

        summary = self.bulk_enricher.get_enrichment_summary(results)

        self.assertEqual(summary['total_items'], 1)
        self.assertEqual(summary['successful_enrichments'], 0)
        self.assertEqual(len(summary['errors']), 1)
        self.assertIn('not found', summary['errors'][0].lower())

    def test_all_mode_runs_every_pass_not_just_ocr(self):
        """
        Regression test: 'all' mode used to short-circuit on the first
        'if enrichment_mode in (X, all): ... continue' block it hit (OCR),
        so validation and merchant_lookup never ran for 'all' - which is
        what the per-item on-demand Re-scan button always requests. An
        item with no documents (OCR produces nothing) but messy whitespace
        (validation produces something) must still come back with changes.
        """
        item = self._create_test_item(issuer='  Messy Issuer  ')

        results = self.bulk_enricher.enrich_selected_items([item.id], 'all', dry_run=True)
        result = results[item.id]

        self.assertTrue(any(c.field_name == 'issuer' for c in result.changes))

    def test_all_mode_applies_and_logs_each_pass_with_its_own_type(self):
        """Applied changes from a mixed 'all' run should each be logged
        under the enrichment_type of the pass that actually produced them."""
        item = self._create_test_item(issuer='  Messy Issuer  ')

        self.bulk_enricher.enrich_selected_items([item.id], 'all', dry_run=False)

        item.refresh_from_db()
        self.assertEqual(item.issuer, 'Messy Issuer')
        logs = ItemEnrichmentLog.objects.filter(item=item, field_name='issuer')
        self.assertTrue(logs.exists())
        self.assertEqual(logs.first().enrichment_type, 'validation')


class ValidateAndNormalizeTestCase(TestCase):
    """Test the expanded validate_and_normalize rules."""

    def setUp(self):
        self.user = User.objects.create_user('testuser2', 'test2@example.com', 'password123')
        self.enricher = ItemEnricher(created_by=self.user)

    def _create_test_item(self, **kwargs):
        defaults = {
            'user': self.user,
            'name': 'Test Item',
            'type': 'giftcard',
            'redeem_code': 'TEST123',
            'issuer': 'Test Issuer',
            'expiry_date': date(2025, 12, 31),
            'value': Decimal('50.00'),
        }
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    def test_whitespace_collapsed_on_text_fields(self):
        item = self._create_test_item(notes='line   with    extra   spaces  ')
        result = self.enricher.validate_and_normalize(item)
        change = next(c for c in result.changes if c.field_name == 'notes')
        self.assertEqual(change.new_value, 'line with extra spaces')

    def test_all_caps_issuer_gets_recased(self):
        item = self._create_test_item(issuer='COSTA COFFEE')
        result = self.enricher.validate_and_normalize(item)
        change = next(c for c in result.changes if c.field_name == 'issuer')
        self.assertEqual(change.new_value, 'Costa Coffee')

    def test_mixed_case_issuer_left_alone(self):
        """A deliberately mixed-case name like 'eBay' must not be
        clobbered by a naive title-case pass."""
        item = self._create_test_item(issuer='eBay')
        result = self.enricher.validate_and_normalize(item)
        self.assertFalse(any(c.field_name == 'issuer' for c in result.changes))

    def test_apostrophe_s_casing_preserved(self):
        item = self._create_test_item(issuer="mcdonald's")
        result = self.enricher.validate_and_normalize(item)
        change = next(c for c in result.changes if c.field_name == 'issuer')
        self.assertEqual(change.new_value, "McDonald's")

    def test_issuer_canonicalized_against_merchant_database(self):
        """A near-miss spelling of a known merchant should be corrected
        to the curated database's canonical name."""
        item = self._create_test_item(issuer='starbuks')
        result = self.enricher.validate_and_normalize(item)
        change = next(c for c in result.changes if c.field_name == 'issuer')
        self.assertEqual(change.new_value, 'Starbucks')

    def test_swapped_issue_expiry_dates_get_corrected(self):
        item = self._create_test_item(issue_date=date(2026, 1, 1), expiry_date=date(2020, 1, 1))
        result = self.enricher.validate_and_normalize(item)

        issue_change = next(c for c in result.changes if c.field_name == 'issue_date')
        expiry_change = next(c for c in result.changes if c.field_name == 'expiry_date')
        self.assertEqual(issue_change.new_value, '2020-01-01')
        self.assertEqual(expiry_change.new_value, '2026-01-01')

    def test_ordered_dates_left_alone(self):
        item = self._create_test_item(issue_date=date(2020, 1, 1), expiry_date=date(2026, 1, 1))
        result = self.enricher.validate_and_normalize(item)
        self.assertFalse(any(c.field_name in ('issue_date', 'expiry_date') for c in result.changes))

    def test_travel_pass_station_code_canonicalized(self):
        item = self._create_test_item(
            type='travelpass', journey_origin='KGX', journey_destination='PAD',
        )
        result = self.enricher.validate_and_normalize(item)
        origin_change = next(c for c in result.changes if c.field_name == 'journey_origin')
        self.assertEqual(origin_change.new_value, "London King's Cross")

    def test_redeem_code_with_ambiguous_characters_is_flagged_not_changed(self):
        item = self._create_test_item(redeem_code='ABC0O1I23')
        result = self.enricher.validate_and_normalize(item)

        self.assertFalse(any(c.field_name == 'redeem_code' for c in result.changes))
        flag = next(f for f in result.flags if f.field_name == 'redeem_code')
        self.assertEqual(flag.current_value, 'ABC0O1I23')

    def test_non_numeric_pin_is_flagged_not_changed(self):
        item = self._create_test_item(pin='12AB')
        result = self.enricher.validate_and_normalize(item)

        self.assertFalse(any(c.field_name == 'pin' for c in result.changes))
        self.assertTrue(any(f.field_name == 'pin' for f in result.flags))

    def test_clean_redeem_code_and_pin_produce_no_flags(self):
        item = self._create_test_item(redeem_code='4837562910', pin='4321')
        result = self.enricher.validate_and_normalize(item)
        self.assertEqual(result.flags, [])

    def test_log_flags_writes_audit_entries_without_changing_values(self):
        from myapp.services.enrichment import FieldFlag
        item = self._create_test_item()
        flags = [FieldFlag(field_name='pin', current_value='12AB', message='Non-numeric PIN')]

        self.enricher.log_flags(item, str(uuid.uuid4()), flags)

        log = ItemEnrichmentLog.objects.get(item=item, field_name='pin')
        self.assertEqual(log.enrichment_type, 'flagged')
        self.assertEqual(log.old_value, log.new_value)
        self.assertIsNone(log.confidence_score)


class ItemEnrichmentHistoryViewTestCase(TestCase):
    """Test the merged on-demand + bulk-run enrichment history view."""

    def setUp(self):
        self.user = User.objects.create_user('historyuser', 'history@example.com', 'password123')
        self.client.force_login(self.user)

    def _create_test_item(self, **kwargs):
        defaults = {
            'user': self.user,
            'name': 'Test Item',
            'type': 'giftcard',
            'redeem_code': 'TEST123',
            'issuer': 'Test Issuer',
            'expiry_date': date(2025, 12, 31),
            'value': Decimal('50.00'),
        }
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    def test_on_demand_rescan_appears_in_item_history(self):
        """
        Regression test: item_enrich_now (the per-item Re-scan button)
        writes to ItemEnrichmentLog directly and never creates an
        EnrichmentRunItem, so the item's own history page - which used to
        only read EnrichmentRunItem - never showed it.
        """
        item = self._create_test_item(issuer='  Messy Issuer  ')

        from myapp.services.enrichment import BulkEnricher
        BulkEnricher(created_by=self.user).enrich_selected_items([item.id], 'all', dry_run=False)

        response = self.client.get(reverse('item_enrichment_history', args=[item.id]))
        self.assertEqual(response.status_code, 200)
        timeline = response.context['timeline']
        self.assertEqual(len(timeline), 1)
        self.assertTrue(any(c['field_name'] == 'issuer' for c in timeline[0]['changes']))

    def test_flags_appear_in_item_history_without_being_treated_as_changes(self):
        item = self._create_test_item(pin='12AB')

        from myapp.services.enrichment import BulkEnricher
        BulkEnricher(created_by=self.user).enrich_selected_items([item.id], 'validation', dry_run=False)

        response = self.client.get(reverse('item_enrichment_history', args=[item.id]))
        timeline = response.context['timeline']
        self.assertTrue(any(entry['flags'] for entry in timeline))


class GetActiveFlagsForItemsTestCase(TestCase):
    """Test the get_active_flags_for_items batch helper used by the review-flags UI."""

    def setUp(self):
        self.user = User.objects.create_user('flaguser', 'flag@example.com', 'password123')

    def _create_test_item(self, **kwargs):
        defaults = {
            'user': self.user,
            'name': 'Test Item',
            'type': 'giftcard',
            'redeem_code': 'TEST123',
            'issuer': 'Test Issuer',
            'expiry_date': date(2025, 12, 31),
            'value': Decimal('50.00'),
        }
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    def _flag(self, item, field_name, new_value, reason='Worth checking'):
        ItemEnrichmentLog.objects.create(
            item=item,
            enrichment_run_id=str(uuid.uuid4()),
            field_name=field_name,
            old_value=new_value,
            new_value=new_value,
            enrichment_type='flagged',
            reason=reason,
        )

    def test_returns_flag_when_current_value_still_matches(self):
        item = self._create_test_item(pin='12AB')
        self._flag(item, 'pin', '12AB', reason='Non-numeric PIN')

        result = get_active_flags_for_items([item])

        self.assertEqual(result, {item.id: [{'field_name': 'pin', 'message': 'Non-numeric PIN'}]})

    def test_drops_stale_flag_after_field_is_edited(self):
        item = self._create_test_item(pin='12AB')
        self._flag(item, 'pin', '12AB', reason='Non-numeric PIN')
        item.pin = '1234'
        item.save()

        result = get_active_flags_for_items([item])

        self.assertEqual(result, {})

    def test_only_most_recent_flag_per_field_is_kept(self):
        item = self._create_test_item(pin='12AB')
        self._flag(item, 'pin', '9999', reason='Old flag, no longer accurate')
        self._flag(item, 'pin', '12AB', reason='Latest flag')

        result = get_active_flags_for_items([item])

        self.assertEqual(result[item.id], [{'field_name': 'pin', 'message': 'Latest flag'}])

    def test_items_with_no_flags_are_excluded(self):
        flagged_item = self._create_test_item(pin='12AB')
        self._flag(flagged_item, 'pin', '12AB')
        clean_item = self._create_test_item(name='Clean Item')

        result = get_active_flags_for_items([flagged_item, clean_item])

        self.assertNotIn(clean_item.id, result)
        self.assertIn(flagged_item.id, result)

    def test_batches_across_multiple_items_in_one_query(self):
        item1 = self._create_test_item(pin='12AB')
        item2 = self._create_test_item(name='Item Two', redeem_code='ABCDEFGHIJKLMNOPQRST' * 5)
        self._flag(item1, 'pin', '12AB', reason='pin flag')
        self._flag(item2, 'redeem_code', item2.redeem_code, reason='redeem_code flag')

        with self.assertNumQueries(1):
            result = get_active_flags_for_items([item1, item2])

        self.assertEqual(result[item1.id], [{'field_name': 'pin', 'message': 'pin flag'}])
        self.assertEqual(result[item2.id], [{'field_name': 'redeem_code', 'message': 'redeem_code flag'}])

    def test_empty_item_list_returns_empty_dict(self):
        self.assertEqual(get_active_flags_for_items([]), {})


class GetAtRiskSignalsForItemsTestCase(TestCase):
    """
    Test the get_at_risk_signals_for_items batch helper - combines
    merchant health, stale corrections, pending enrichment approvals, and
    tripped circuit breakers into one read-only per-item signal set.
    """

    def setUp(self):
        self.user = User.objects.create_user('riskuser', 'risk@example.com', 'password123')

    def _create_test_item(self, **kwargs):
        defaults = {
            'user': self.user,
            'name': 'Test Item',
            'type': 'giftcard',
            'redeem_code': 'TEST123',
            'issuer': 'Test Issuer',
            'expiry_date': date(2025, 12, 31),
            'value': Decimal('50.00'),
        }
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    def test_empty_item_list_returns_empty_dict(self):
        self.assertEqual(get_at_risk_signals_for_items([]), {})

    def test_no_signals_for_a_clean_item(self):
        item = self._create_test_item()
        self.assertEqual(get_at_risk_signals_for_items([item]), {})

    def test_flags_item_with_unhealthy_merchant(self):
        item = self._create_test_item(issuer='Acme Retail')
        MerchantProfile.objects.create(name='Acme Retail', is_unhealthy=True, company_status='administration')

        result = get_at_risk_signals_for_items([item])

        self.assertEqual(len(result[item.id]), 1)
        self.assertEqual(result[item.id][0]['kind'], 'merchant_unhealthy')
        self.assertIn('Acme Retail', result[item.id][0]['message'])

    def test_merchant_health_match_is_case_insensitive(self):
        item = self._create_test_item(issuer='acme retail')
        MerchantProfile.objects.create(name='Acme Retail', is_unhealthy=True)

        result = get_at_risk_signals_for_items([item])

        self.assertIn(item.id, result)

    def test_healthy_merchant_does_not_flag(self):
        item = self._create_test_item(issuer='Acme Retail')
        MerchantProfile.objects.create(name='Acme Retail', is_unhealthy=False, company_status='active')

        self.assertEqual(get_at_risk_signals_for_items([item]), {})

    def test_flags_item_with_stale_correction(self):
        item = self._create_test_item(type='giftcard', issuer='Merchant A')
        correction = ScanFieldCorrection.objects.create(
            user=self.user, item_type='giftcard', issuer='Merchant A', field='issuer',
            ai_value='Merchan A', corrected_value='Merchant A',
        )
        old = timezone.now() - timedelta(days=400)
        ScanFieldCorrection.objects.filter(pk=correction.pk).update(last_applied_at=old)

        result = get_at_risk_signals_for_items([item])

        self.assertEqual(len(result[item.id]), 1)
        self.assertEqual(result[item.id][0]['kind'], 'stale_correction')
        self.assertIn('issuer', result[item.id][0]['message'])

    def test_flags_item_with_pending_enrichment_approval(self):
        item = self._create_test_item()
        run = EnrichmentRun.objects.create(method='ocr', status='pending_approval')
        EnrichmentRunItem.objects.create(run=run, item=item, changes_proposed=1)

        result = get_at_risk_signals_for_items([item])

        self.assertEqual(result[item.id], [{
            'kind': 'pending_enrichment',
            'message': 'A proposed change to this item is awaiting admin review',
        }])

    def test_does_not_flag_a_completed_enrichment_run(self):
        item = self._create_test_item()
        run = EnrichmentRun.objects.create(method='ocr', status='completed')
        EnrichmentRunItem.objects.create(run=run, item=item, changes_proposed=1)

        self.assertEqual(get_at_risk_signals_for_items([item]), {})

    def test_does_not_flag_a_pending_run_with_no_changes_proposed(self):
        item = self._create_test_item()
        run = EnrichmentRun.objects.create(method='ocr', status='pending_approval')
        EnrichmentRunItem.objects.create(run=run, item=item, changes_proposed=0)

        self.assertEqual(get_at_risk_signals_for_items([item]), {})

    def test_flags_item_with_tripped_circuit_breaker_on_a_populated_field(self):
        item = self._create_test_item(issuer='Test Issuer')
        EnrichmentFieldPreference.objects.create(
            user=self.user, method='ocr', field_name='issuer',
            reason='Auto-disabled after repeated corrections',
        )

        result = get_at_risk_signals_for_items([item])

        self.assertEqual(result[item.id], [{
            'kind': 'circuit_breaker',
            'message': "Automatic enrichment for 'issuer' has been disabled after repeated corrections",
        }])

    def test_does_not_flag_circuit_breaker_for_an_empty_field(self):
        item = self._create_test_item(description='')
        EnrichmentFieldPreference.objects.create(
            user=self.user, method='ocr', field_name='description',
            reason='Auto-disabled after repeated corrections',
        )

        self.assertEqual(get_at_risk_signals_for_items([item]), {})

    def test_does_not_flag_a_user_set_preference_with_blank_reason(self):
        item = self._create_test_item(issuer='Test Issuer')
        EnrichmentFieldPreference.objects.create(
            user=self.user, method='ocr', field_name='issuer', reason='',
        )

        self.assertEqual(get_at_risk_signals_for_items([item]), {})

    def test_combines_multiple_signals_on_the_same_item(self):
        item = self._create_test_item(issuer='Acme Retail')
        MerchantProfile.objects.create(name='Acme Retail', is_unhealthy=True)
        EnrichmentFieldPreference.objects.create(
            user=self.user, method='ocr', field_name='issuer',
            reason='Auto-disabled after repeated corrections',
        )

        result = get_at_risk_signals_for_items([item])

        kinds = {signal['kind'] for signal in result[item.id]}
        self.assertEqual(kinds, {'merchant_unhealthy', 'circuit_breaker'})

    def test_batches_across_multiple_items(self):
        flagged_item = self._create_test_item(issuer='Acme Retail', name='Flagged')
        clean_item = self._create_test_item(issuer='Clean Merchant', name='Clean')
        MerchantProfile.objects.create(name='Acme Retail', is_unhealthy=True)

        result = get_at_risk_signals_for_items([flagged_item, clean_item])

        self.assertIn(flagged_item.id, result)
        self.assertNotIn(clean_item.id, result)


class ReviewFlagsViewIntegrationTestCase(TestCase):
    """Test that view_item and show_items surface active flags in context."""

    def setUp(self):
        self.user = User.objects.create_user('flagviewuser', 'flagview@example.com', 'password123')
        self.client.force_login(self.user)

    def _create_test_item(self, **kwargs):
        defaults = {
            'user': self.user,
            'name': 'Test Item',
            'type': 'giftcard',
            'redeem_code': 'TEST123',
            'issuer': 'Test Issuer',
            'expiry_date': date(2025, 12, 31),
            'value': Decimal('50.00'),
        }
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    def _flag(self, item, field_name, new_value, reason='Worth checking'):
        ItemEnrichmentLog.objects.create(
            item=item,
            enrichment_run_id=str(uuid.uuid4()),
            field_name=field_name,
            old_value=new_value,
            new_value=new_value,
            enrichment_type='flagged',
            reason=reason,
        )

    def test_view_item_context_has_active_flags_for_flagged_item(self):
        item = self._create_test_item(pin='12AB')
        self._flag(item, 'pin', '12AB', reason='Non-numeric PIN')

        response = self.client.get(reverse('view_item', args=[item.id]))

        self.assertEqual(response.context['active_flags'], {'pin': 'Non-numeric PIN'})

    def test_view_item_context_active_flags_empty_for_unflagged_item(self):
        item = self._create_test_item()

        response = self.client.get(reverse('view_item', args=[item.id]))

        self.assertEqual(response.context['active_flags'], {})

    def test_show_items_marks_has_flags_true_only_for_flagged_items(self):
        flagged_item = self._create_test_item(pin='12AB')
        self._flag(flagged_item, 'pin', '12AB')
        clean_item = self._create_test_item(name='Clean Item')

        response = self.client.get(reverse('show_items'), {'status': 'all'})

        entries = {e['item'].id: e['has_flags'] for e in response.context['items_with_qr']}
        self.assertTrue(entries[flagged_item.id])
        self.assertFalse(entries[clean_item.id])

    def test_view_item_context_has_at_risk_signals_for_flagged_item(self):
        item = self._create_test_item(issuer='Acme Retail')
        MerchantProfile.objects.create(name='Acme Retail', is_unhealthy=True)

        response = self.client.get(reverse('view_item', args=[item.id]))

        signals = response.context['at_risk_signals']
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]['kind'], 'merchant_unhealthy')

    def test_view_item_context_at_risk_signals_empty_for_clean_item(self):
        item = self._create_test_item()

        response = self.client.get(reverse('view_item', args=[item.id]))

        self.assertEqual(response.context['at_risk_signals'], [])

    def test_show_items_marks_has_risk_true_only_for_at_risk_items(self):
        at_risk_item = self._create_test_item(issuer='Acme Retail')
        MerchantProfile.objects.create(name='Acme Retail', is_unhealthy=True)
        clean_item = self._create_test_item(name='Clean Item', issuer='Clean Merchant')

        response = self.client.get(reverse('show_items'), {'status': 'all'})

        entries = {e['item'].id: e['has_risk'] for e in response.context['items_with_qr']}
        self.assertTrue(entries[at_risk_item.id])
        self.assertFalse(entries[clean_item.id])


class EnrichmentFieldPreferenceTestCase(TestCase):
    """
    EnrichmentFieldPreference lets a user opt a specific field out of a
    specific enrichment method. The model and its migration have existed
    since Phase 97 but nothing ever read it - every pass ran against every
    enrichable field regardless of what a user had opted out of. These
    tests cover the fix: each of the three enrichment methods should skip
    any field the user has an EnrichmentFieldPreference row for.
    """

    def setUp(self):
        self.user = User.objects.create_user('prefuser', 'pref@example.com', 'password123')
        self.enricher = ItemEnricher(created_by=self.user)

    def _create_test_item(self, **kwargs):
        defaults = {
            'user': self.user,
            'name': 'Test Item',
            'type': 'giftcard',
            'redeem_code': 'TEST123',
            'issuer': 'Test Issuer',
            'expiry_date': date(2025, 12, 31),
            'value': Decimal('50.00'),
        }
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    def test_validation_skips_excluded_issuer(self):
        EnrichmentFieldPreference.objects.create(user=self.user, method='validation', field_name='issuer')
        item = self._create_test_item(issuer='COSTA COFFEE')

        result = self.enricher.validate_and_normalize(item)

        self.assertFalse(any(c.field_name == 'issuer' for c in result.changes))

    def test_validation_still_enriches_non_excluded_fields(self):
        """Excluding one field must not silently suppress every field."""
        EnrichmentFieldPreference.objects.create(user=self.user, method='validation', field_name='issuer')
        item = self._create_test_item(issuer='COSTA COFFEE', notes='line   with    extra   spaces  ')

        result = self.enricher.validate_and_normalize(item)

        self.assertTrue(any(c.field_name == 'notes' for c in result.changes))

    def test_validation_skips_excluded_value(self):
        EnrichmentFieldPreference.objects.create(user=self.user, method='validation', field_name='value')
        item = self._create_test_item(value=Decimal('50.005'))

        result = self.enricher.validate_and_normalize(item)

        self.assertFalse(any(c.field_name == 'value' for c in result.changes))

    def test_preference_is_scoped_to_its_own_method(self):
        """An opt-out for 'validation' must not bleed into 'ocr'."""
        EnrichmentFieldPreference.objects.create(user=self.user, method='validation', field_name='issuer')
        item = self._create_test_item(issuer='')
        Document.objects.create(item=item, file=SimpleUploadedFile('receipt.png', b'fake-image-bytes', content_type='image/png'))

        with patch('ocr.backends.ocr_enabled', return_value=True), \
             patch('ocr.backends.get_backend') as mock_get_backend:
            mock_backend = MagicMock()
            mock_backend.extract.return_value = {'issuer': 'Costa Coffee', 'confidence': 0.9}
            mock_get_backend.return_value = mock_backend
            result = self.enricher.enrich_from_ocr(item, confidence_threshold=Decimal('0.5'))

        self.assertTrue(any(c.field_name == 'issuer' for c in result.changes))

    def test_other_users_preference_does_not_apply(self):
        other_user = User.objects.create_user('otheruser', 'other@example.com', 'password123')
        EnrichmentFieldPreference.objects.create(user=other_user, method='validation', field_name='issuer')
        item = self._create_test_item(issuer='COSTA COFFEE')

        result = self.enricher.validate_and_normalize(item)

        self.assertTrue(any(c.field_name == 'issuer' for c in result.changes))

    def test_bulk_enricher_respects_preference_across_multiple_items(self):
        """Confidence-cache reuse in ItemEnricher must not leak between users
        or cause a stale/incorrect exclusion set on later items."""
        EnrichmentFieldPreference.objects.create(user=self.user, method='validation', field_name='issuer')
        item1 = self._create_test_item(issuer='COSTA COFFEE')
        item2 = self._create_test_item(issuer='COSTA COFFEE', notes='extra   spaces  ')

        bulk = BulkEnricher(created_by=self.user)
        results = bulk.enrich_selected_items([item1.id, item2.id], enrichment_mode='validation')

        self.assertFalse(any(c.field_name == 'issuer' for c in results[item1.id].changes))
        self.assertFalse(any(c.field_name == 'issuer' for c in results[item2.id].changes))
        self.assertTrue(any(c.field_name == 'notes' for c in results[item2.id].changes))

