from decimal import Decimal
from datetime import date
from unittest.mock import patch

from django.test import TestCase
from django.contrib.auth.models import User

from myapp.models import (
    EnrichmentConfig, EnrichmentFieldPreference, EnrichmentRun, EnrichmentRunItem,
    GlobalScanCorrection, Item, ItemEnrichmentLog, ScanFieldCorrection,
)
from myapp.scan_learning import find_retroactive_heal_candidates, RETROACTIVE_HEAL_FIELDS
from myapp.services.enrichment import ItemEnricher
from myapp.tasks import finalize_enrichment_run


class FindRetroactiveHealCandidatesTests(TestCase):
    """
    myapp.scan_learning.find_retroactive_heal_candidates - the backward-
    looking counterpart to apply_learned_corrections: checks an already-
    saved item's *current* values against the same correction tables,
    instead of a fresh scan result.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def _create_item(self, **kwargs):
        defaults = dict(
            user=self.user, name='Test Item', type='giftcard', redeem_code='TEST123',
            issuer='Merchant A', expiry_date=date(2030, 12, 31), value=Decimal('50.00'),
        )
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    def test_excludes_type_and_code_type(self):
        self.assertNotIn('type', RETROACTIVE_HEAL_FIELDS)
        self.assertNotIn('code_type', RETROACTIVE_HEAL_FIELDS)

    def test_heals_from_a_personal_correction(self):
        item = self._create_item(issuer='Nationl Rail', type='travelpass')
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='travelpass', field='issuer',
            ai_value='Nationl Rail', corrected_value='National Rail',
        )
        candidates = find_retroactive_heal_candidates(item)
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate['field'], 'issuer')
        self.assertEqual(candidate['old_value'], 'Nationl Rail')
        self.assertEqual(candidate['new_value'], 'National Rail')
        self.assertEqual(candidate['source_kind'], 'personal')

    def test_no_candidate_when_current_value_already_correct(self):
        item = self._create_item(issuer='National Rail', type='travelpass')
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='travelpass', field='issuer',
            ai_value='Nationl Rail', corrected_value='National Rail',
        )
        self.assertEqual(find_retroactive_heal_candidates(item), [])

    def test_scoped_to_matching_item_type(self):
        item = self._create_item(issuer='Nationl Rail', type='giftcard')
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='travelpass', field='issuer',
            ai_value='Nationl Rail', corrected_value='National Rail',
        )
        self.assertEqual(find_retroactive_heal_candidates(item), [])

    def test_scoped_to_the_owning_user(self):
        other_user = User.objects.create_user(username='bob', password='pw12345!')
        item = self._create_item(issuer='Nationl Rail', type='travelpass')
        ScanFieldCorrection.objects.create(
            user=other_user, item_type='travelpass', field='issuer',
            ai_value='Nationl Rail', corrected_value='National Rail',
        )
        self.assertEqual(find_retroactive_heal_candidates(item), [])

    def test_no_fuzzy_fallback(self):
        """
        Unlike apply_learned_corrections, retroactive healing is exact-match
        only - a near-miss of the taught ai_value must not heal.
        """
        item = self._create_item(issuer='Kwikfit', type='giftcard')
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='giftcard', field='issuer',
            ai_value='Kwikfip', corrected_value='Right Answer', times_seen=5,
        )
        self.assertEqual(find_retroactive_heal_candidates(item), [])

    def test_falls_back_to_a_global_pattern_when_no_personal_match(self):
        item = self._create_item(issuer='Merchant A', type='giftcard', currency='GBP')
        GlobalScanCorrection.objects.create(
            item_type='giftcard', issuer='Merchant A', field='currency',
            ai_value='GBP', corrected_value='EUR', confirmed_by_users=3,
        )
        candidates = find_retroactive_heal_candidates(item)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['source_kind'], 'global')
        self.assertEqual(candidates[0]['new_value'], 'EUR')

    def test_personal_correction_wins_over_global_pattern(self):
        item = self._create_item(issuer='Merchant A', type='giftcard', currency='GBP')
        GlobalScanCorrection.objects.create(
            item_type='giftcard', issuer='Merchant A', field='currency',
            ai_value='GBP', corrected_value='EUR', confirmed_by_users=3,
        )
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='giftcard', issuer='Merchant A', field='currency',
            ai_value='GBP', corrected_value='USD',
        )
        candidates = find_retroactive_heal_candidates(item)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['source_kind'], 'personal')
        self.assertEqual(candidates[0]['new_value'], 'USD')

    def test_global_pattern_does_not_bleed_into_a_different_merchant(self):
        item = self._create_item(issuer='Merchant B', type='giftcard', currency='GBP')
        GlobalScanCorrection.objects.create(
            item_type='giftcard', issuer='Merchant A', field='currency',
            ai_value='GBP', corrected_value='EUR', confirmed_by_users=3,
        )
        self.assertEqual(find_retroactive_heal_candidates(item), [])

    def test_blank_current_value_is_never_a_candidate(self):
        item = self._create_item(issuer='', type='giftcard')
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='giftcard', field='issuer',
            ai_value='', corrected_value='Merchant A',
        )
        self.assertEqual(find_retroactive_heal_candidates(item), [])


class EnrichFromLearnedCorrectionsTests(TestCase):
    """ItemEnricher.enrich_from_learned_corrections - turns retroactive-heal
    candidates into FieldChanges, tiering confidence by trust."""

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.enricher = ItemEnricher(created_by=None)

    def _create_item(self, **kwargs):
        defaults = dict(
            user=self.user, name='Test Item', type='travelpass', redeem_code='TEST123',
            issuer='Nationl Rail', expiry_date=date(2030, 12, 31), value=Decimal('50.00'),
        )
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    def test_personal_confidence_scales_with_times_seen(self):
        item = self._create_item()
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='travelpass', field='issuer',
            ai_value='Nationl Rail', corrected_value='National Rail', times_seen=3,
        )
        result = self.enricher.enrich_from_learned_corrections(item, confidence_threshold=Decimal('0.0'))
        change = next(c for c in result.changes if c.field_name == 'issuer')
        self.assertEqual(change.confidence_score, Decimal('0.5') + Decimal('0.1') * 3)

    def test_personal_confidence_caps_at_0_95(self):
        item = self._create_item()
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='travelpass', field='issuer',
            ai_value='Nationl Rail', corrected_value='National Rail', times_seen=50,
        )
        result = self.enricher.enrich_from_learned_corrections(item, confidence_threshold=Decimal('0.0'))
        change = next(c for c in result.changes if c.field_name == 'issuer')
        self.assertEqual(change.confidence_score, Decimal('0.95'))

    def test_global_confidence_is_flat_0_3(self):
        item = self._create_item(issuer='Merchant A', currency='GBP')
        GlobalScanCorrection.objects.create(
            item_type='travelpass', issuer='Merchant A', field='currency',
            ai_value='GBP', corrected_value='EUR', confirmed_by_users=5,
        )
        result = self.enricher.enrich_from_learned_corrections(item, confidence_threshold=Decimal('0.0'))
        change = next(c for c in result.changes if c.field_name == 'currency')
        self.assertEqual(change.confidence_score, Decimal('0.3'))

    def test_global_match_never_clears_the_default_auto_apply_bar(self):
        """A GlobalScanCorrection alone must never auto-apply at the
        EnrichmentConfig model's own default confidence_threshold."""
        item = self._create_item(issuer='Merchant A', currency='GBP')
        GlobalScanCorrection.objects.create(
            item_type='travelpass', issuer='Merchant A', field='currency',
            ai_value='GBP', corrected_value='EUR', confirmed_by_users=5,
        )
        default_threshold = EnrichmentConfig._meta.get_field('confidence_threshold').default
        result = self.enricher.enrich_from_learned_corrections(item, confidence_threshold=default_threshold)
        self.assertEqual(result.changes, [])

    def test_below_confidence_threshold_is_dropped(self):
        item = self._create_item()
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='travelpass', field='issuer',
            ai_value='Nationl Rail', corrected_value='National Rail', times_seen=1,
        )
        # times_seen=1 -> confidence 0.6, below a 0.9 bar.
        result = self.enricher.enrich_from_learned_corrections(item, confidence_threshold=Decimal('0.9'))
        self.assertEqual(result.changes, [])

    def test_respects_field_preference_opt_out(self):
        EnrichmentFieldPreference.objects.create(
            user=self.user, method='learned_correction', field_name='issuer',
        )
        item = self._create_item()
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='travelpass', field='issuer',
            ai_value='Nationl Rail', corrected_value='National Rail', times_seen=5,
        )
        result = self.enricher.enrich_from_learned_corrections(item, confidence_threshold=Decimal('0.0'))
        self.assertEqual(result.changes, [])

    def test_no_candidates_still_succeeds_with_no_changes(self):
        item = self._create_item(issuer='Already Correct')
        result = self.enricher.enrich_from_learned_corrections(item, confidence_threshold=Decimal('0.0'))
        self.assertTrue(result.success)
        self.assertEqual(result.changes, [])

    def test_apply_changes_accepts_a_currency_field_change(self):
        """
        Regression guard: logo_slug/currency/travel_time are only ever
        proposed by this method, but apply_changes() gates every method's
        changes through the same ENRICHABLE_FIELDS set - if those three
        aren't in it, a real, user-taught retroactive currency correction
        would be silently rejected as "not enrichable".
        """
        item = self._create_item(currency='GBP')
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='travelpass', issuer='Nationl Rail', field='currency',
            ai_value='GBP', corrected_value='EUR', times_seen=5,
        )
        result = self.enricher.enrich_from_learned_corrections(item, confidence_threshold=Decimal('0.0'))
        change = next(c for c in result.changes if c.field_name == 'currency')
        success, error = self.enricher.apply_changes(item, [change])
        self.assertTrue(success, error)
        item.refresh_from_db()
        self.assertEqual(item.currency, 'EUR')


class QueueItemEnrichmentLearnedCorrectionTests(TestCase):
    """Full round-trip through the task dispatch (myapp.tasks.queue_item_enrichment)."""

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def _create_item(self, **kwargs):
        defaults = dict(
            user=self.user, name='Test Item', type='travelpass', redeem_code='TEST123',
            issuer='Nationl Rail', expiry_date=date(2030, 12, 31), value=Decimal('50.00'),
        )
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    def test_auto_apply_true_writes_the_field_and_logs_it(self):
        from myapp.tasks import queue_item_enrichment

        item = self._create_item()
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='travelpass', field='issuer',
            ai_value='Nationl Rail', corrected_value='National Rail', times_seen=5,
        )
        run = EnrichmentRun.objects.create(method='learned_correction', confidence_threshold=Decimal('0.5'))

        queue_item_enrichment(item.id, str(run.id), 'learned_correction', True)

        item.refresh_from_db()
        self.assertEqual(item.issuer, 'National Rail')
        log = ItemEnrichmentLog.objects.get(item=item, field_name='issuer')
        self.assertEqual(log.enrichment_type, 'learned_correction')
        self.assertEqual(log.new_value, 'National Rail')
        run_item = EnrichmentRunItem.objects.get(run=run, item=item)
        self.assertEqual(run_item.changes_applied, 1)

    def test_auto_apply_false_only_previews(self):
        from myapp.tasks import queue_item_enrichment

        item = self._create_item()
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='travelpass', field='issuer',
            ai_value='Nationl Rail', corrected_value='National Rail', times_seen=5,
        )
        run = EnrichmentRun.objects.create(method='learned_correction', confidence_threshold=Decimal('0.5'))

        queue_item_enrichment(item.id, str(run.id), 'learned_correction', False)

        item.refresh_from_db()
        self.assertEqual(item.issuer, 'Nationl Rail')
        self.assertFalse(ItemEnrichmentLog.objects.filter(item=item).exists())
        run_item = EnrichmentRunItem.objects.get(run=run, item=item)
        self.assertEqual(run_item.changes_proposed, 1)
        self.assertEqual(run_item.changes_applied, 0)
        self.assertEqual(run_item.preview_data['changes'][0]['field_name'], 'issuer')


class FinalizeEnrichmentRunNotifiesOnLearnedCorrectionTests(TestCase):
    """
    finalize_enrichment_run's call into notify.tasks.
    notify_retroactive_corrections_applied - only for a completed
    'learned_correction' run, never for pending_approval or any other
    method (see myapp/test_retroactive_healing.py's PR 2 tests and
    notify/tests.py's RetroactiveCorrectionsAppliedNotificationTests for
    the notifier's own behavior).
    """

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def _create_item(self, **kwargs):
        defaults = dict(
            user=self.user, name='Test Item', type='travelpass', redeem_code='TEST123',
            issuer='National Rail', expiry_date=date(2030, 12, 31), value=Decimal('50.00'),
        )
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    @patch('notify.tasks.notify_retroactive_corrections_applied')
    def test_notifies_when_a_learned_correction_run_completes(self, mock_notify):
        EnrichmentConfig.objects.update_or_create(method='learned_correction', defaults={'auto_apply': True})
        item = self._create_item()
        run = EnrichmentRun.objects.create(method='learned_correction')
        EnrichmentRunItem.objects.create(run=run, item=item, success=True, changes_applied=1)

        finalize_enrichment_run(str(run.id))

        mock_notify.assert_called_once()
        self.assertEqual(mock_notify.call_args[0][0].id, run.id)

    @patch('notify.tasks.notify_retroactive_corrections_applied')
    def test_does_not_notify_for_a_pending_approval_run(self, mock_notify):
        EnrichmentConfig.objects.update_or_create(method='learned_correction', defaults={'auto_apply': False})
        item = self._create_item()
        run = EnrichmentRun.objects.create(method='learned_correction')
        EnrichmentRunItem.objects.create(run=run, item=item, success=True, changes_proposed=1)

        finalize_enrichment_run(str(run.id))

        mock_notify.assert_not_called()

    @patch('notify.tasks.notify_retroactive_corrections_applied')
    def test_does_not_notify_for_a_different_method(self, mock_notify):
        EnrichmentConfig.objects.update_or_create(method='validation', defaults={'auto_apply': True})
        item = self._create_item()
        run = EnrichmentRun.objects.create(method='validation')
        EnrichmentRunItem.objects.create(run=run, item=item, success=True, changes_applied=1)

        finalize_enrichment_run(str(run.id))

        mock_notify.assert_not_called()
