# myapp/tasks.py
from celery import shared_task
from django.core.management import call_command

from .merchant_logos import fetch_merchant_logo, merchant_logos_enabled

@shared_task
def run_expiration_check():
    call_command('check_expiration')

@shared_task
def fetch_merchant_logo_task(name):
    if not name or not merchant_logos_enabled():
        return
    fetch_merchant_logo(name)
