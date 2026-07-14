# myapp/tasks.py
from celery import shared_task
from django.core.management import call_command

from .merchant_logos import fetch_merchant_logo, merchant_logos_enabled
from .update_check import check_for_update, check_upstream_version

@shared_task
def run_expiration_check():
    call_command('check_expiration')

@shared_task
def fetch_merchant_logo_task(name, domain_hint=None):
    if not name or not merchant_logos_enabled():
        return
    fetch_merchant_logo(name, domain_hint=domain_hint)

@shared_task
def check_for_update_task():
    check_for_update()

@shared_task
def check_upstream_version_task():
    check_upstream_version()
