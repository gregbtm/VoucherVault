"""
Keeps a django_celery_beat PeriodicTask in sync with each EnrichmentConfig's
`schedule` field. See myapp/signals.py:sync_enrichment_periodic_task, which
calls sync_periodic_task_for_config() on every EnrichmentConfig save.
"""
import json
import logging

logger = logging.getLogger(__name__)

PERIODIC_TASK_NAME_PREFIX = "Enrichment: "

# EnrichmentConfig.SCHEDULE_CHOICES -> CrontabSchedule kwargs, matching each
# choice's own admin-facing description ("Every Day at 2 AM", "Every Monday
# at 2 AM", "First of Month at 2 AM"). Cron has no native "every 2 weeks"
# expression, so 'biweekly' is approximated as weekly-at-2am, with actual
# firing gated by ISO-week parity inside run_scheduled_enrichment_if_due
# (myapp/tasks.py) rather than the crontab itself.
_CRONTAB_KWARGS = {
    'daily':    dict(minute='0', hour='2', day_of_week='*', day_of_month='*', month_of_year='*'),
    'weekly':   dict(minute='0', hour='2', day_of_week='1', day_of_month='*', month_of_year='*'),
    'biweekly': dict(minute='0', hour='2', day_of_week='1', day_of_month='*', month_of_year='*'),
    'monthly':  dict(minute='0', hour='2', day_of_week='*', day_of_month='1', month_of_year='*'),
}

_TASK_PATH = {
    'daily': 'myapp.tasks.run_scheduled_enrichment',
    'weekly': 'myapp.tasks.run_scheduled_enrichment',
    'biweekly': 'myapp.tasks.run_scheduled_enrichment_if_due',
    'monthly': 'myapp.tasks.run_scheduled_enrichment',
}


def periodic_task_name(method):
    return f"{PERIODIC_TASK_NAME_PREFIX}{method}"


def sync_periodic_task_for_config(config):
    """
    Create/update/remove the single PeriodicTask that drives scheduled
    enrichment for this EnrichmentConfig's method. One row per method,
    keyed on name so switching schedules updates it in place rather than
    leaving stale duplicates behind.
    """
    from django_celery_beat.models import PeriodicTask, CrontabSchedule

    name = periodic_task_name(config.method)

    if not config.enabled or config.schedule == 'disabled':
        PeriodicTask.objects.filter(name=name).delete()
        return

    crontab_kwargs = _CRONTAB_KWARGS.get(config.schedule)
    task_path = _TASK_PATH.get(config.schedule)
    if crontab_kwargs is None or task_path is None:
        logger.warning(
            "Unknown enrichment schedule %r for method %s - leaving periodic task untouched",
            config.schedule, config.method,
        )
        return

    crontab, _created = CrontabSchedule.objects.get_or_create(**crontab_kwargs)

    PeriodicTask.objects.update_or_create(
        name=name,
        defaults={
            'task': task_path,
            'crontab': crontab,
            'args': json.dumps([config.method]),
            'enabled': True,
        },
    )
