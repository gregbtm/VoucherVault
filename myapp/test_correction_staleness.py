from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from myapp.models import GlobalScanCorrection, Item, ScanFieldCorrection
from myapp.scan_learning import (
    STALE_CORRECTION_THRESHOLD_DAYS,
    find_stale_corrections,
    find_stale_corrections_relevant_to_items,
)
from myapp.tasks import flag_stale_corrections_task
from myapp.test_utils import set_site_config


class FindStaleCorrectionsTests(TestCase):
    """
    myapp.scan_learning.find_stale_corrections - surfaces learned
    corrections that haven't healed a live scan in over a year, for
    flag_stale_corrections_task to alert an admin about.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.old = timezone.now() - timedelta(days=STALE_CORRECTION_THRESHOLD_DAYS + 1)
        self.recent = timezone.now() - timedelta(days=10)

    def _personal(self, **kwargs):
        defaults = dict(
            user=self.user, item_type='giftcard', issuer='Merchant A', field='issuer',
            ai_value='Merchan A', corrected_value='Merchant A',
        )
        defaults.update(kwargs)
        correction = ScanFieldCorrection.objects.create(**defaults)
        return correction

    def _global(self, **kwargs):
        defaults = dict(
            item_type='giftcard', issuer='Merchant B', field='code_type',
            ai_value='qrcode', corrected_value='azteccode', confirmed_by_users=3,
        )
        defaults.update(kwargs)
        return GlobalScanCorrection.objects.create(**defaults)

    def test_flags_a_personal_correction_that_fired_long_ago(self):
        correction = self._personal()
        ScanFieldCorrection.objects.filter(pk=correction.pk).update(last_applied_at=self.old)
        personal, global_rows = find_stale_corrections()
        self.assertEqual(personal, [correction])
        self.assertEqual(global_rows, [])

    def test_does_not_flag_a_personal_correction_that_fired_recently(self):
        correction = self._personal()
        ScanFieldCorrection.objects.filter(pk=correction.pk).update(last_applied_at=self.recent)
        personal, _ = find_stale_corrections()
        self.assertEqual(personal, [])

    def test_flags_a_never_applied_correction_that_is_old(self):
        correction = self._personal()
        ScanFieldCorrection.objects.filter(pk=correction.pk).update(updated_at=self.old)
        personal, _ = find_stale_corrections()
        self.assertEqual(personal, [correction])

    def test_does_not_flag_a_never_applied_correction_that_is_recent(self):
        """A correction taught last week hasn't had a chance to prove
        itself yet - not the same thing as being stale."""
        self._personal()
        personal, _ = find_stale_corrections()
        self.assertEqual(personal, [])

    def test_a_recent_re_teach_does_not_mask_a_long_ago_heal(self):
        """
        last_applied_at old + updated_at recent (re-taught since it last
        fired) must still count as stale - it hasn't proven useful since
        being re-confirmed, regardless of when that re-confirmation
        happened.
        """
        correction = self._personal()
        ScanFieldCorrection.objects.filter(pk=correction.pk).update(
            last_applied_at=self.old, updated_at=self.recent,
        )
        personal, _ = find_stale_corrections()
        self.assertEqual(personal, [correction])

    def test_flags_a_global_pattern_that_fired_long_ago(self):
        pattern = self._global()
        GlobalScanCorrection.objects.filter(pk=pattern.pk).update(last_applied_at=self.old)
        _, global_rows = find_stale_corrections()
        self.assertEqual(global_rows, [pattern])

    def test_flags_a_never_applied_global_pattern_that_is_old(self):
        pattern = self._global()
        GlobalScanCorrection.objects.filter(pk=pattern.pk).update(promoted_at=self.old)
        _, global_rows = find_stale_corrections()
        self.assertEqual(global_rows, [pattern])

    def test_already_alerted_rows_are_not_returned_again(self):
        correction = self._personal()
        ScanFieldCorrection.objects.filter(pk=correction.pk).update(
            last_applied_at=self.old, stale_alerted_at=timezone.now(),
        )
        personal, _ = find_stale_corrections()
        self.assertEqual(personal, [])

    def test_firing_again_clears_a_prior_stale_alert(self):
        """apply_learned_corrections' counter-bump clears stale_alerted_at
        whenever a heal actually fires - re-verified here at the query
        level, not just the bump itself (see ScanLearningTests)."""
        from myapp.scan_learning import apply_learned_corrections
        correction = self._personal(ai_value='Nationl Rail', corrected_value='National Rail', item_type='travelpass', issuer='')
        ScanFieldCorrection.objects.filter(pk=correction.pk).update(
            last_applied_at=self.old, stale_alerted_at=timezone.now(),
        )
        result = {'issuer': 'Nationl Rail', 'type': 'travelpass'}
        apply_learned_corrections(self.user, result)

        correction.refresh_from_db()
        self.assertIsNone(correction.stale_alerted_at)


def make_item(user, **kwargs):
    defaults = dict(
        user=user, type='giftcard', name='Test Item', redeem_code='TEST123',
        issuer='Merchant A', expiry_date=timezone.now().date() + timedelta(days=30),
        value='10.00',
    )
    defaults.update(kwargs)
    return Item.objects.create(**defaults)


class FindStaleCorrectionsRelevantToItemsTests(TestCase):
    """
    myapp.scan_learning.find_stale_corrections_relevant_to_items - the
    batched per-item counterpart to find_stale_corrections(), feeding the
    at-risk item signals (get_at_risk_signals_for_items).
    """

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.old = timezone.now() - timedelta(days=STALE_CORRECTION_THRESHOLD_DAYS + 1)
        self.recent = timezone.now() - timedelta(days=10)

    def _personal(self, **kwargs):
        defaults = dict(
            user=self.user, item_type='giftcard', issuer='Merchant A', field='issuer',
            ai_value='Merchan A', corrected_value='Merchant A',
        )
        defaults.update(kwargs)
        return ScanFieldCorrection.objects.create(**defaults)

    def _global(self, **kwargs):
        defaults = dict(
            item_type='giftcard', issuer='Merchant B', field='code_type',
            ai_value='qrcode', corrected_value='azteccode', confirmed_by_users=3,
        )
        defaults.update(kwargs)
        return GlobalScanCorrection.objects.create(**defaults)

    def test_empty_item_list_returns_empty_dict(self):
        self.assertEqual(find_stale_corrections_relevant_to_items([]), {})

    def test_matches_a_stale_personal_correction_for_the_same_user_type_and_issuer(self):
        correction = self._personal()
        ScanFieldCorrection.objects.filter(pk=correction.pk).update(last_applied_at=self.old)
        item = make_item(self.user, type='giftcard', issuer='Merchant A')

        result = find_stale_corrections_relevant_to_items([item])

        self.assertEqual(result, {item.id: correction})

    def test_does_not_match_a_correction_that_fired_recently(self):
        correction = self._personal()
        ScanFieldCorrection.objects.filter(pk=correction.pk).update(last_applied_at=self.recent)
        item = make_item(self.user, type='giftcard', issuer='Merchant A')

        result = find_stale_corrections_relevant_to_items([item])

        self.assertEqual(result, {})

    def test_does_not_match_another_users_personal_correction(self):
        other_user = User.objects.create_user(username='bob', password='pw12345!')
        correction = self._personal(user=other_user)
        ScanFieldCorrection.objects.filter(pk=correction.pk).update(last_applied_at=self.old)
        item = make_item(self.user, type='giftcard', issuer='Merchant A')

        result = find_stale_corrections_relevant_to_items([item])

        self.assertEqual(result, {})

    def test_does_not_match_a_different_item_type(self):
        correction = self._personal(item_type='giftcard')
        ScanFieldCorrection.objects.filter(pk=correction.pk).update(last_applied_at=self.old)
        item = make_item(self.user, type='voucher', issuer='Merchant A')

        result = find_stale_corrections_relevant_to_items([item])

        self.assertEqual(result, {})

    def test_blank_issuer_on_stored_correction_applies_unscoped(self):
        correction = self._personal(issuer='')
        ScanFieldCorrection.objects.filter(pk=correction.pk).update(last_applied_at=self.old)
        item = make_item(self.user, type='giftcard', issuer='Some Other Merchant')

        result = find_stale_corrections_relevant_to_items([item])

        self.assertEqual(result, {item.id: correction})

    def test_matches_a_stale_global_correction_when_no_personal_match(self):
        pattern = self._global(issuer='Merchant A')
        GlobalScanCorrection.objects.filter(pk=pattern.pk).update(last_applied_at=self.old)
        item = make_item(self.user, type='giftcard', issuer='Merchant A')

        result = find_stale_corrections_relevant_to_items([item])

        self.assertEqual(result, {item.id: pattern})

    def test_personal_match_takes_priority_over_global_match(self):
        personal = self._personal(issuer='Merchant A')
        ScanFieldCorrection.objects.filter(pk=personal.pk).update(last_applied_at=self.old)
        global_pattern = self._global(issuer='Merchant A')
        GlobalScanCorrection.objects.filter(pk=global_pattern.pk).update(last_applied_at=self.old)
        item = make_item(self.user, type='giftcard', issuer='Merchant A')

        result = find_stale_corrections_relevant_to_items([item])

        self.assertEqual(result, {item.id: personal})

    def test_ignores_stale_alerted_at_unlike_the_admin_sweep(self):
        correction = self._personal()
        ScanFieldCorrection.objects.filter(pk=correction.pk).update(
            last_applied_at=self.old, stale_alerted_at=timezone.now(),
        )
        item = make_item(self.user, type='giftcard', issuer='Merchant A')

        result = find_stale_corrections_relevant_to_items([item])

        self.assertEqual(result, {item.id: correction})

    def test_batches_across_multiple_items(self):
        correction = self._personal()
        ScanFieldCorrection.objects.filter(pk=correction.pk).update(last_applied_at=self.old)
        stale_item = make_item(self.user, type='giftcard', issuer='Merchant A', name='Stale')
        clean_item = make_item(self.user, type='giftcard', issuer='Merchant Z', name='Clean')

        result = find_stale_corrections_relevant_to_items([stale_item, clean_item])

        self.assertEqual(result, {stale_item.id: correction})


class FlagStaleCorrectionsTaskTests(TestCase):
    """
    myapp.tasks.flag_stale_corrections_task: the weekly Celery wrapper,
    following the same security_alert_ntfy_topic/ntfy_default_server
    pattern as detect_systemic_misreads_task.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.old = timezone.now() - timedelta(days=STALE_CORRECTION_THRESHOLD_DAYS + 1)

    def _stale_personal(self):
        correction = ScanFieldCorrection.objects.create(
            user=self.user, item_type='giftcard', issuer='Merchant A', field='issuer',
            ai_value='Merchan A', corrected_value='Merchant A', times_seen=4,
        )
        ScanFieldCorrection.objects.filter(pk=correction.pk).update(last_applied_at=self.old, times_applied=7)
        return correction

    @patch('requests.post')
    def test_no_op_when_nothing_is_stale(self, mock_post):
        set_site_config(security_alert_ntfy_topic='alerts')
        flag_stale_corrections_task()
        mock_post.assert_not_called()

    @patch('requests.post')
    def test_no_op_when_no_ntfy_topic_configured(self, mock_post):
        set_site_config(security_alert_ntfy_topic='')
        self._stale_personal()
        flag_stale_corrections_task()
        mock_post.assert_not_called()

    @patch('requests.post')
    def test_does_not_mark_alerted_when_no_topic_configured(self, mock_post):
        """Leaving the topic unset must not silently suppress this row
        forever - once an admin configures a topic, it should still be
        reported."""
        set_site_config(security_alert_ntfy_topic='')
        correction = self._stale_personal()
        flag_stale_corrections_task()

        set_site_config(security_alert_ntfy_topic='alerts')
        flag_stale_corrections_task()

        mock_post.assert_called_once()
        correction.refresh_from_db()
        self.assertIsNotNone(correction.stale_alerted_at)

    @patch('requests.post')
    def test_sends_an_alert_listing_the_stale_correction(self, mock_post):
        set_site_config(security_alert_ntfy_topic='alerts', ntfy_default_server='https://ntfy.sh')
        self._stale_personal()
        flag_stale_corrections_task()
        mock_post.assert_called_once()
        url = mock_post.call_args[0][0]
        body = mock_post.call_args[1]['data'].decode('utf-8')
        self.assertEqual(url, 'https://ntfy.sh/alerts')
        self.assertIn('Merchant A', body)
        self.assertIn('alice', body)

    @patch('requests.post')
    def test_does_not_re_alert_on_the_same_stale_row(self, mock_post):
        set_site_config(security_alert_ntfy_topic='alerts')
        self._stale_personal()
        flag_stale_corrections_task()
        mock_post.reset_mock()
        flag_stale_corrections_task()
        mock_post.assert_not_called()

    @patch('requests.post')
    def test_marks_rows_alerted_even_if_delivery_fails(self, mock_post):
        """Matches the rest of the app's admin-alert precedent: delivery
        is best-effort, state changes aren't rolled back on failure."""
        mock_post.side_effect = Exception('network down')
        set_site_config(security_alert_ntfy_topic='alerts')
        correction = self._stale_personal()
        flag_stale_corrections_task()
        correction.refresh_from_db()
        self.assertIsNotNone(correction.stale_alerted_at)

    @patch('requests.post')
    def test_does_not_flag_or_delete_the_row_itself(self, mock_post):
        set_site_config(security_alert_ntfy_topic='alerts')
        correction = self._stale_personal()
        flag_stale_corrections_task()
        self.assertTrue(ScanFieldCorrection.objects.filter(pk=correction.pk).exists())
