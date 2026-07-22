import logging

from django.shortcuts import redirect
from mozilla_django_oidc.views import OIDCCallbackView

logger = logging.getLogger(__name__)


class VVOIDCCallbackView(OIDCCallbackView):
    """
    OIDC callback that optionally enforces TOTP for OIDC-authenticated users.

    When SiteConfiguration.oidc_require_totp is True and the authenticating
    user has a confirmed TOTP device, authentication is paused here and they
    are redirected to the TOTP verification screen — exactly as the password
    login flow does — before a session is created.  When the setting is False
    (the default) the standard OIDC callback runs unmodified.
    """

    def login_success(self):
        from myapp.models import SiteConfiguration, TOTPDevice
        config = SiteConfiguration.load()
        if config.oidc_require_totp:
            try:
                device = self.user.totp_device
                if device.confirmed:
                    self.request.session['_totp_user_id'] = self.user.pk
                    logger.debug(
                        'OIDC login for %s paused — TOTP verification required.',
                        self.user.username,
                    )
                    return redirect('totp_verify')
            except TOTPDevice.DoesNotExist:
                pass
        return super().login_success()
