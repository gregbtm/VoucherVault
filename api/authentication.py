from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework.authentication import TokenAuthentication
from rest_framework.exceptions import AuthenticationFailed


class ExpiringTokenAuthentication(TokenAuthentication):
    """DRF token auth that rejects tokens older than API_TOKEN_EXPIRY_DAYS.

    Set API_TOKEN_EXPIRY_DAYS=0 (the default) to disable expiry entirely.
    """

    def authenticate_credentials(self, key):
        user, token = super().authenticate_credentials(key)
        expiry_days = getattr(settings, 'API_TOKEN_EXPIRY_DAYS', 0)
        if expiry_days:
            cutoff = timezone.now() - timedelta(days=expiry_days)
            if token.created < cutoff:
                token.delete()
                raise AuthenticationFailed(
                    'API token has expired. Generate a new token from your profile.'
                )
        return user, token
