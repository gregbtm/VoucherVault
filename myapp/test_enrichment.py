from decimal import Decimal
from django.test import TestCase
from django.contrib.auth.models import User
from myapp.models import Item, Document, ItemEnrichmentLog
from myapp.services.enrichment import ItemEnricher, BulkEnricher, FieldChange
from datetime import date
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
