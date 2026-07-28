from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch, MagicMock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from myapp.models import EnrichmentRun, EnrichmentRunItem, Item, SiteConfiguration
from myapp.tasks import (
    check_and_finalize_run, enrichment_run_watchdog_task,
    MAX_FINALIZE_ATTEMPTS, RETRY_MISSING_ITEMS_AT_ATTEMPT,
)


class CheckAndFinalizeRunTestCase(TestCase):
    """
    check_and_finalize_run used to reschedule itself every 60s forever
    with no cap - a single item that permanently failed to ever get an
    EnrichmentRunItem row left the run stuck at status='in_progress'
    indefinitely, retrying every minute for good with nothing surfacing
    it. It's now bounded to MAX_FINALIZE_ATTEMPTS, retries missing items
    once partway through, and fails the run explicitly if it never
    catches up.
    """

    def setUp(self):
        self.user = User.objects.create_user('watchdoguser', 'wd@example.com', 'password123')

    def _create_run(self, total_items=2, **kwargs):
        defaults = {'method': 'validation', 'status': 'in_progress', 'total_items': total_items}
        defaults.update(kwargs)
        return EnrichmentRun.objects.create(**defaults)

    def _create_item(self, **kwargs):
        defaults = {
            'user': self.user, 'name': 'Test Item', 'type': 'giftcard',
            'redeem_code': 'TEST123', 'issuer': 'Test Issuer',
            'expiry_date': timezone.now().date(), 'value': Decimal('10.00'),
        }
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    @patch('myapp.tasks.finalize_enrichment_run')
    def test_finalizes_when_all_items_complete(self, mock_finalize):
        run = self._create_run(total_items=1)
        item = self._create_item()
        EnrichmentRunItem.objects.create(run=run, item=item, success=True)

        check_and_finalize_run(str(run.id))

        mock_finalize.delay.assert_called_once_with(str(run.id))

    @patch('celery.current_app.send_task')
    def test_reschedules_with_incrementing_attempt_when_incomplete(self, mock_send_task):
        run = self._create_run(total_items=2)
        self._create_item()  # no EnrichmentRunItem yet

        check_and_finalize_run(str(run.id), attempt=3)

        mock_send_task.assert_called_once_with(
            'myapp.tasks.check_and_finalize_run', args=[str(run.id), 4], countdown=60,
        )

    @patch('celery.current_app.send_task')
    @patch('myapp.tasks.queue_item_enrichment')
    def test_requeues_missing_items_at_halfway_point(self, mock_queue, mock_send_task):
        run = self._create_run(total_items=2)
        done_item = self._create_item(name='Done')
        missing_item = self._create_item(name='Missing')
        EnrichmentRunItem.objects.create(run=run, item=done_item, success=True)

        check_and_finalize_run(str(run.id), attempt=RETRY_MISSING_ITEMS_AT_ATTEMPT)

        mock_queue.delay.assert_called_once_with(missing_item.id, str(run.id), 'validation', False)

    @patch('myapp.tasks._alert_stuck_enrichment_run')
    def test_marks_failed_after_max_attempts(self, mock_alert):
        run = self._create_run(total_items=5)
        self._create_item()  # only 1 of 5 will ever exist

        check_and_finalize_run(str(run.id), attempt=MAX_FINALIZE_ATTEMPTS)

        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertIsNotNone(run.completed_at)
        # total_items must NOT get silently overwritten to match whatever
        # completed - the whole point is that 5 were expected and 0 ever
        # produced a result.
        self.assertEqual(run.total_items, 5)
        self.assertIn('0/5', run.notes)
        mock_alert.assert_called_once()

    @patch('requests.post')
    def test_alert_sent_when_topic_configured(self, mock_post):
        config = SiteConfiguration.load()
        config.security_alert_ntfy_topic = 'watchdog-topic'
        config.save()
        run = self._create_run(total_items=3)

        check_and_finalize_run(str(run.id), attempt=MAX_FINALIZE_ATTEMPTS)

        mock_post.assert_called_once()
        args, _kwargs = mock_post.call_args
        self.assertIn('watchdog-topic', args[0])

    @patch('requests.post')
    def test_no_alert_when_topic_not_configured(self, mock_post):
        run = self._create_run(total_items=3)
        check_and_finalize_run(str(run.id), attempt=MAX_FINALIZE_ATTEMPTS)
        mock_post.assert_not_called()


class EnrichmentRunWatchdogTaskTestCase(TestCase):
    """
    Independent belt-and-braces sweep - catches a run whose own
    check_and_finalize_run countdown chain silently died entirely (e.g. a
    broker restart dropping the scheduled task), not just one that's
    still ticking through its own retry cap.
    """

    def setUp(self):
        self.user = User.objects.create_user('watchdog2', 'wd2@example.com', 'password123')

    def _create_run(self, status='in_progress', started_at=None, total_items=1):
        run = EnrichmentRun.objects.create(method='validation', status=status, total_items=total_items)
        if started_at is not None:
            EnrichmentRun.objects.filter(id=run.id).update(started_at=started_at)
            run.refresh_from_db()
        return run

    def test_fails_a_run_stuck_for_over_two_hours(self):
        run = self._create_run(started_at=timezone.now() - timedelta(hours=3))
        enrichment_run_watchdog_task()
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')

    def test_leaves_a_recent_in_progress_run_alone(self):
        run = self._create_run(started_at=timezone.now() - timedelta(minutes=10))
        enrichment_run_watchdog_task()
        run.refresh_from_db()
        self.assertEqual(run.status, 'in_progress')

    def test_leaves_completed_runs_alone(self):
        run = self._create_run(status='completed', started_at=timezone.now() - timedelta(hours=5))
        enrichment_run_watchdog_task()
        run.refresh_from_db()
        self.assertEqual(run.status, 'completed')
