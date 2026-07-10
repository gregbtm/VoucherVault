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

        # Create default periodic tasks (disabled by default)
        tasks = [
            {'name': 'Periodic Expiry Check', 'task': 'myapp.tasks.run_expiration_check', 'crontab': crontab_schedule, 'enabled': True},
            # Per-item threshold + multi-backend (ntfy/webhook/apprise) notification rules.
            # A no-op until a user creates a NotificationRule, so it's safe to enable by default
            # alongside the legacy Apprise-only task above.
            {'name': 'Notification Rules Expiry Check', 'task': 'notify.tasks.check_and_notify_expiry', 'crontab': crontab_schedule, 'enabled': True},
            # Add more tasks as needed
        ]

        for task_data in tasks:
            PeriodicTask.objects.get_or_create(
                name=task_data['name'],
                task=task_data['task'],
                crontab=task_data['crontab'],
                enabled=task_data.get('enabled', False),
            )

        self.stdout.write(self.style.SUCCESS('Default periodic tasks created successfully.'))
