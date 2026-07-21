import logging

from mozilla_django_oidc.auth import OIDCAuthenticationBackend

logger = logging.getLogger(__name__)


class VoucherVaultOIDCBackend(OIDCAuthenticationBackend):
    """OIDC backend that syncs first_name, last_name, and email from claims."""

    def create_user(self, claims):
        user = super().create_user(claims)
        self._sync_user_profile(user, claims)
        return user

    def update_user(self, user, claims):
        user = super().update_user(user, claims)
        self._sync_user_profile(user, claims)
        return user

    def _sync_user_profile(self, user, claims):
        full_name = claims.get('name', '')
        name_parts = full_name.split() if full_name else []
        first_name = claims.get('given_name') or (name_parts[0] if name_parts else '')
        last_name = claims.get('family_name') or (' '.join(name_parts[1:]) if len(name_parts) > 1 else '')
        email = claims.get('email', user.email)

        changed = user.first_name != first_name or user.last_name != last_name or user.email != email
        if changed:
            user.first_name = first_name
            user.last_name = last_name
            user.email = email
            user.save(update_fields=['first_name', 'last_name', 'email'])
            logger.debug('OIDC profile synced for user %s', user.username)
