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