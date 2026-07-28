# myapp/signals.py

from django.db.models.signals import post_save
from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.dispatch import receiver
from django.contrib.auth.models import User
from .models import *


def _client_ip(request):
    x_forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if x_forwarded:
        return x_forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR') or None


@receiver(user_logged_in)
def audit_login_success(sender, request, user, **kwargs):
    try:
        LoginAuditLog.objects.create(
            user=user,
            username_attempted=user.username,
            ip_address=_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
            success=True,
        )
    except Exception:
        pass


@receiver(user_login_failed)
def audit_login_failure(sender, credentials, request, **kwargs):
    try:
        username = credentials.get('username', '')
        from django.contrib.auth.models import User as _User
        user = None
        try:
            user = _User.objects.get(username=username)
        except _User.DoesNotExist:
            pass
        LoginAuditLog.objects.create(
            user=user,
            username_attempted=username[:150],
            ip_address=_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
            success=False,
            failure_reason='Invalid credentials',
        )
    except Exception:
        pass

@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.create(user=instance)


@receiver(user_logged_in)
def flag_pwa_cache_clear_on_login(sender, request, user, **kwargs):
    """
    The service worker caches authenticated pages by URL only, with no
    per-user/session scoping (see myapp/serviceworker.js) - on a shared or
    kiosk browser, a session that ends without a clean logout (closed tab,
    crash) can leave the next user served the previous user's cached
    pages. Logout already clears proactively (base.html); this flags the
    very next page render after a fresh login to clear all PWA caches too,
    as defense-in-depth for whatever a prior session left behind.
    """
    request.session['clear_pwa_cache'] = True

@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    instance.userprofile.save()

@receiver(post_save, sender=User)
def create_user_preference(sender, instance, created, **kwargs):
    if created:
        UserPreference.objects.create(user=instance)


@receiver(post_save, sender=Item)
def auto_categorize_item(sender, instance, created, **kwargs):
    """Auto-categorize items on create/update using smart_features."""
    try:
        from .smart_features import apply_category_to_item
        apply_category_to_item(instance)
    except Exception:
        pass


@receiver(post_save, sender=Item)
def refresh_item_recommendations(sender, instance, created, **kwargs):
    """
    Keep Smart Suggestions in sync with the item the moment something
    save-worthy changes about it (balance topped up, marked used, expiry
    corrected) rather than waiting for the next daily sweep - see
    myapp/smart_features.py. The sweep (myapp/tasks.py) still runs daily
    to catch conditions that change with time alone, with no save involved.
    """
    try:
        from .smart_features import generate_item_recommendations
        generate_item_recommendations(instance)
    except Exception:
        pass


@receiver(post_save, sender=Transaction)
def update_wallet_budget_on_transaction(sender, instance, created, **kwargs):
    """Recalculate wallet budget spending when a transaction is created/updated."""
    try:
        from .smart_features import update_wallet_spent_this_month
        if instance.item and instance.item.wallet:
            update_wallet_spent_this_month(instance.item.wallet)
    except Exception:
        pass