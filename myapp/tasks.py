# myapp/tasks.py
from celery import shared_task
from django.core.management import call_command
from django.utils import timezone

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

@shared_task
def mark_expired_commute_outward_tickets():
    """
    Bookkeeping companion to analytics.get_active_today_item(): once a
    user's configured Active Today cutoff time has passed, marks today's
    outward-leg commute ticket (journey_origin matching their
    commute_home_station) is_used=True, so it stops counting as available
    everywhere else in the app (Inventory counts, Next Up, etc). Purely a
    bookkeeping flip - the Active Today widget itself decides what to
    *display* directly from the current time vs cutoff on every read,
    independent of whether this task has run yet, so a delay here never
    leaves the widget showing something stale.
    """
    from notify.tasks import notify_item_used

    from .models import Item, UserPreference

    today = timezone.localtime().date()
    now_time = timezone.localtime().time()
    preferences = UserPreference.objects.filter(active_today_enabled=True).exclude(commute_home_station='')
    for prefs in preferences:
        if now_time < prefs.active_today_cutoff_time:
            continue
        outward = Item.objects.filter(
            user=prefs.user, is_used=False, is_archived=False, expiry_date=today,
            journey_origin__iexact=prefs.commute_home_station.strip(),
        ).exclude(journey_destination='').first()
        if outward:
            outward.is_used = True
            outward.save(update_fields=['is_used'])
            notify_item_used(outward)
