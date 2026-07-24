import logging

from mozilla_django_oidc.auth import OIDCAuthenticationBackend
from django.utils import timezone

logger = logging.getLogger(__name__)


class VoucherVaultOIDCBackend(OIDCAuthenticationBackend):
    """
    OIDC backend that:
    - Respects SiteConfiguration.allow_registration for new-user creation
    - Syncs first_name, last_name, email, avatar, and OIDC sub from claims
    - Maps PocketID group membership to superuser status
    """

    def create_user(self, claims):
        from myapp.models import SiteConfiguration
        config = SiteConfiguration.load()
        if not config.oidc_create_user:
            return None
        if not config.allow_registration:
            logger.info(
                "OIDC: blocked new-user creation for %s — registration is closed",
                claims.get('email', claims.get('sub', '?')),
            )
            return None
        user = super().create_user(claims)
        self._sync_user_profile(user, claims)
        return user

    def update_user(self, user, claims):
        user = super().update_user(user, claims)
        self._sync_user_profile(user, claims)
        return user

    def _sync_user_profile(self, user, claims):
        from myapp.models import UserProfile, SiteConfiguration
        from myapp.avatar import generate_initial_avatar
        import base64

        full_name = claims.get('name', '')
        name_parts = full_name.split() if full_name else []
        first_name = claims.get('given_name') or (name_parts[0] if name_parts else '')
        last_name = claims.get('family_name') or (' '.join(name_parts[1:]) if len(name_parts) > 1 else '')
        email = claims.get('email', user.email)

        user_changed = user.first_name != first_name or user.last_name != last_name or user.email != email

        # Group → superuser mapping
        config = SiteConfiguration.load()
        if config.oidc_admin_group:
            groups = claims.get('groups', [])
            is_admin = config.oidc_admin_group in groups
            if user.is_superuser != is_admin or user.is_staff != is_admin:
                user.is_superuser = is_admin
                user.is_staff = is_admin
                user_changed = True
                logger.info(
                    "OIDC: %s superuser=%s (group claim: %s)",
                    user.username, is_admin, groups,
                )

        if user_changed:
            user.first_name = first_name
            user.last_name = last_name
            user.email = email
            user.save(update_fields=['first_name', 'last_name', 'email', 'is_superuser', 'is_staff'])
            logger.debug('OIDC profile synced for user %s', user.username)

        # Sync to UserProfile
        profile, _ = UserProfile.objects.get_or_create(user=user)
        oidc_sub = claims.get('sub', '')
        avatar_url = claims.get('picture', '')
        now = timezone.now()

        # Generate a random avatar if OIDC provider didn't supply one
        if not avatar_url:
            avatar_bytes = generate_initial_avatar(user.username or email or 'User')
            avatar_url = f"data:image/png;base64,{base64.b64encode(avatar_bytes).decode()}"

        profile_changed = (
            profile.oidc_sub != oidc_sub
            or profile.oidc_avatar_url != avatar_url
        )
        if profile_changed or profile.oidc_last_login is None:
            profile.oidc_sub = oidc_sub
            profile.oidc_avatar_url = avatar_url
            profile.oidc_last_login = now
            profile.save(update_fields=['oidc_sub', 'oidc_avatar_url', 'oidc_last_login'])
