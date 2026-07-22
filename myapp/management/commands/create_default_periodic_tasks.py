# yourapp/management/commands/create_default_periodic_tasks.py
from django.core.management.base import BaseCommand
from django_celery_beat.models import PeriodicTask, CrontabSchedule

class Command(BaseCommand):
    help = 'Create default Celery Beat periodic tasks'

    def handle(self, *args, **options):
        # Create a crontab schedule (run daily at 9 o'clock)
        crontab_schedule, created = CrontabSchedule.objects.get_or_create(
            minute='0',
            hour='9',
            day_of_week='*',
            day_of_month='*',
            month_of_year='*'
        )

        # Weekly schedule (Monday 10am UTC) for low-frequency health checks that
        # don't need daily cadence — inactivity nudges and merchant health alerts.
        weekly_schedule, created = CrontabSchedule.objects.get_or_create(
            minute='0',
            hour='10',
            day_of_week='1',
            day_of_month='*',
            month_of_year='*'
        )

        # A separate, quieter schedule for the nightly backup task, so it
        # doesn't compete with the 9am notification checks above.
        backup_schedule, created = CrontabSchedule.objects.get_or_create(
            minute='0',
            hour='3',
            day_of_week='*',
            day_of_month='*',
            month_of_year='*'
        )

        # This fork ships multiple releases a day during active work (see
        # the GitHub Releases page) - a once-a-day check made "installed
        # version ahead of the last known latest" the constant normal
        # state rather than a genuine signal, since the deployed VERSION
        # was almost always newer than whatever the last daily check had
        # seen. Hourly keeps the two in sync closely enough to be useful,
        # while the "Check for updates now" button in Site Settings still
        # covers the moment-you-just-deployed case immediately.
        hourly_schedule, created = CrontabSchedule.objects.get_or_create(
            minute='15',
            hour='*',
            day_of_week='*',
            day_of_month='*',
            month_of_year='*'
        )

        # Create default periodic tasks (disabled by default)
        tasks = [
            {'name': 'Periodic Expiry Check', 'task': 'myapp.tasks.run_expiration_check', 'crontab': crontab_schedule, 'enabled': True},
            # Per-item threshold + multi-backend (ntfy/webhook/apprise) notification rules.
            # A no-op until a user creates a NotificationRule, so it's safe to enable by default
            # alongside the legacy Apprise-only task above.
            {'name': 'Notification Rules Expiry Check', 'task': 'notify.tasks.check_and_notify_expiry', 'crontab': crontab_schedule, 'enabled': True},
            # Fires a 'next_up_reminder' event for items due today in a user's configured
            # Next Up wallet(s); a no-op until a user both sets one and has a matching
            # NotificationRule subscribed to that event.
            {'name': 'Next Up Reminder Check', 'task': 'notify.tasks.check_next_up_reminders', 'crontab': crontab_schedule, 'enabled': True},
            # Checks GitHub Releases for a newer version; a no-op if UPDATE_CHECK_ENABLED=False
            {'name': 'Update Check', 'task': 'myapp.tasks.check_for_update_task', 'crontab': hourly_schedule, 'enabled': True},
            # Checks l4rm4nd/VoucherVault's (upstream) latest release, purely informational
            {'name': 'Upstream Version Check', 'task': 'myapp.tasks.check_upstream_version_task', 'crontab': hourly_schedule, 'enabled': True},
            # Writes a rotating local Full Backup zip per user; a no-op if SCHEDULED_BACKUP_ENABLED=False
            {'name': 'Scheduled Backup', 'task': 'imports.tasks.run_scheduled_backups', 'crontab': backup_schedule, 'enabled': True},
            # Sends one combined message per rule set to "Daily Digest", batching whatever
            # fired that day; a no-op until a user opts a rule into digest delivery.
            {'name': 'Daily Notification Digest', 'task': 'notify.tasks.send_daily_digests', 'crontab': crontab_schedule, 'enabled': True},
            # Marks today's outward commute ticket used once a user's configured Active
            # Today cutoff passes; a no-op until a user enables the widget and sets a
            # home station. Reuses the hourly schedule - this is bookkeeping the widget's
            # own live read doesn't depend on, so within-the-hour timing is fine.
            {'name': 'Active Today Outward-Leg Cleanup', 'task': 'myapp.tasks.mark_expired_commute_outward_tickets', 'crontab': hourly_schedule, 'enabled': True},
            # Advances renewal_date + expiry_date for items whose renewal_date has
            # passed, resets is_used, and fires a renewal_advanced notification.
            # A no-op until a user marks an item as recurring with a renewal_date set.
            {'name': 'Advance Recurring Items', 'task': 'notify.tasks.advance_recurring_items', 'crontab': crontab_schedule, 'enabled': True},
            # Re-queues Firefly III transaction pushes that previously failed (blank
            # firefly_transaction_id). Safe to re-run: once populated it's excluded.
            # A no-op for installs that don't use the Firefly backend.
            {'name': 'Retry Failed Firefly Pushes', 'task': 'notify.tasks.retry_failed_firefly_pushes', 'crontab': hourly_schedule, 'enabled': True},
            # Polls enabled DMS providers for new documents tagged for VoucherVault
            # and imports them. A no-op until a user configures a DMS provider with
            # auto_pull=True.
            {'name': 'DMS Auto Pull', 'task': 'dms.tasks.dms_scheduled_pull_all', 'crontab': hourly_schedule, 'enabled': True},
            # Fires 'item_inactive' for money-type items not used/viewed for longer than
            # SiteConfiguration.inactivity_threshold_days. A no-op until a user creates
            # a NotificationRule subscribed to that event.
            {'name': 'Gift Card Inactivity Check', 'task': 'notify.tasks.check_and_notify_inactivity', 'crontab': weekly_schedule, 'enabled': True},
            # Looks up each unique item issuer on Companies House and fires
            # 'merchant_health_alert' if the company is in administration/liquidation.
            # A no-op unless SiteConfiguration.companies_house_api_key is set.
            {'name': 'Merchant Health Check', 'task': 'notify.tasks.check_merchant_health', 'crontab': weekly_schedule, 'enabled': True},
            # Deletes ImportJob records (and their uploaded files) older than 30 days
            # that are in a terminal state (complete/failed). Runs weekly to keep
            # the database and uploads/ directory clean without hammering storage.
            {'name': 'Purge Old Import Jobs', 'task': 'imports.tasks.purge_old_import_jobs', 'crontab': weekly_schedule, 'enabled': True},
        ]

        for task_data in tasks:
            # Keyed on (name, task) only, *not* crontab - this command runs
            # on every container start (see docker/entrypoint.sh), and the
            # old crontab=... lookup meant changing a schedule here (like
            # the hourly one above) would create a second, duplicate
            # PeriodicTask on top of whatever an existing install already
            # had, rather than updating it in place - doubling the checks
            # instead of rescheduling them. `enabled` is deliberately only
            # set on first creation, not on every restart, so an admin who
            # disabled a task via Django admin doesn't get overridden.
            task, created = PeriodicTask.objects.get_or_create(
                name=task_data['name'],
                task=task_data['task'],
                defaults={'crontab': task_data['crontab'], 'enabled': task_data.get('enabled', False)},
            )
            if not created and task.crontab_id != task_data['crontab'].id:
                task.crontab = task_data['crontab']
                task.save(update_fields=['crontab'])

        self.stdout.write(self.style.SUCCESS('Default periodic tasks created successfully.'))
