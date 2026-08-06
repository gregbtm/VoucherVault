from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase
from myapp.models import EnrichmentConfig


class EnrichmentScheduleToPeriodicTaskTestCase(TestCase):
    """
    EnrichmentConfig.schedule was admin-editable but had zero effect -
    nothing ever created the Celery Beat entry it implied. A post_save
    signal (myapp/signals.py:sync_enrichment_periodic_task) now keeps a
    matching django_celery_beat PeriodicTask in sync on every save.
    """

    def _task_name(self, method):
        from myapp.enrichment_scheduling import periodic_task_name
        return periodic_task_name(method)

    def test_enabling_daily_schedule_creates_periodic_task(self):
        from django_celery_beat.models import PeriodicTask
        config = EnrichmentConfig.objects.create(method='validation', enabled=True, schedule='daily')

        task = PeriodicTask.objects.get(name=self._task_name('validation'))
        self.assertEqual(task.task, 'myapp.tasks.run_scheduled_enrichment')
        self.assertTrue(task.enabled)
        self.assertEqual(task.crontab.hour, '2')
        self.assertEqual(task.crontab.day_of_week, '*')
        import json
        self.assertEqual(json.loads(task.args), ['validation'])

    def test_disabled_config_creates_no_periodic_task(self):
        from django_celery_beat.models import PeriodicTask
        EnrichmentConfig.objects.create(method='validation', enabled=False, schedule='daily')
        self.assertFalse(PeriodicTask.objects.filter(name=self._task_name('validation')).exists())

    def test_schedule_disabled_creates_no_periodic_task_even_if_enabled(self):
        from django_celery_beat.models import PeriodicTask
        EnrichmentConfig.objects.create(method='validation', enabled=True, schedule='disabled')
        self.assertFalse(PeriodicTask.objects.filter(name=self._task_name('validation')).exists())

    def test_turning_off_removes_existing_periodic_task(self):
        from django_celery_beat.models import PeriodicTask
        config = EnrichmentConfig.objects.create(method='ocr', enabled=True, schedule='daily')
        self.assertTrue(PeriodicTask.objects.filter(name=self._task_name('ocr')).exists())

        config.enabled = False
        config.save()

        self.assertFalse(PeriodicTask.objects.filter(name=self._task_name('ocr')).exists())

    def test_changing_schedule_updates_existing_task_in_place(self):
        from django_celery_beat.models import PeriodicTask
        config = EnrichmentConfig.objects.create(method='merchant_lookup', enabled=True, schedule='daily')
        config.schedule = 'monthly'
        config.save()

        tasks = PeriodicTask.objects.filter(name=self._task_name('merchant_lookup'))
        self.assertEqual(tasks.count(), 1)
        task = tasks.first()
        self.assertEqual(task.crontab.day_of_month, '1')

    def test_biweekly_schedule_uses_gated_wrapper_task(self):
        config = EnrichmentConfig.objects.create(method='validation', enabled=True, schedule='biweekly')
        from django_celery_beat.models import PeriodicTask
        task = PeriodicTask.objects.get(name=self._task_name('validation'))
        self.assertEqual(task.task, 'myapp.tasks.run_scheduled_enrichment_if_due')


class RunScheduledEnrichmentIfDueTestCase(TestCase):
    """Cron can't express "every 2 weeks" natively - the biweekly PeriodicTask
    fires every Monday, and this wrapper gates the actual run to even ISO
    weeks so it lands roughly every other week instead of weekly."""

    @patch('myapp.tasks.run_scheduled_enrichment')
    @patch('django.utils.timezone.now')
    def test_fires_on_even_iso_week(self, mock_now, mock_run):
        import datetime
        # 2026-01-05 is an ISO week-2 Monday (even week).
        mock_now.return_value = datetime.datetime(2026, 1, 5, 2, 0, tzinfo=datetime.timezone.utc)
        from myapp.tasks import run_scheduled_enrichment_if_due
        run_scheduled_enrichment_if_due('validation')
        mock_run.assert_called_once_with('validation')

    @patch('myapp.tasks.run_scheduled_enrichment')
    @patch('django.utils.timezone.now')
    def test_skips_on_odd_iso_week(self, mock_now, mock_run):
        import datetime
        # 2026-01-12 is an ISO week-3 Monday (odd week).
        mock_now.return_value = datetime.datetime(2026, 1, 12, 2, 0, tzinfo=datetime.timezone.utc)
        from myapp.tasks import run_scheduled_enrichment_if_due
        run_scheduled_enrichment_if_due('validation')
        mock_run.assert_not_called()


class SeedEnrichmentConfigTestCase(TestCase):
    """create_default_periodic_tasks runs on every container start
    (docker/entrypoint.sh) and now also seeds the EnrichmentConfig rows
    the config UI has nothing to show without - previously nothing ever
    created them, so a fresh install's Enrichment Configuration page was
    empty and unreachable per-method."""

    def test_seeds_a_config_row_for_every_enrichment_method(self):
        from django.core.management import call_command
        call_command('create_default_periodic_tasks')
        methods = set(EnrichmentConfig.objects.values_list('method', flat=True))
        self.assertEqual(methods, {method for method, _label in EnrichmentConfig.ENRICHMENT_METHODS})

    def test_seeded_configs_start_disabled(self):
        from django.core.management import call_command
        call_command('create_default_periodic_tasks')
        self.assertFalse(EnrichmentConfig.objects.filter(enabled=True).exists())

    def test_rerunning_does_not_duplicate_or_reset_admin_changes(self):
        from django.core.management import call_command
        call_command('create_default_periodic_tasks')
        config = EnrichmentConfig.objects.get(method='validation')
        config.enabled = True
        config.schedule = 'weekly'
        config.save()

        call_command('create_default_periodic_tasks')

        self.assertEqual(EnrichmentConfig.objects.filter(method='validation').count(), 1)
        config.refresh_from_db()
        self.assertTrue(config.enabled)
        self.assertEqual(config.schedule, 'weekly')
