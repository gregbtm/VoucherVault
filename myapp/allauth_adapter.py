from allauth.account.adapter import DefaultAccountAdapter
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _


class RegistrationGateAdapter(DefaultAccountAdapter):
    """Blocks new signups when SiteConfiguration.allow_registration is False."""

    def is_open_for_signup(self, request):
        from myapp.models import SiteConfiguration
        config = SiteConfiguration.load()
        if not config.allow_registration:
            return False
        return super().is_open_for_signup(request)

    def pre_social_login(self, request, sociallogin):
        super().pre_social_login(request, sociallogin)
        if sociallogin.is_existing:
            return
        from myapp.models import SiteConfiguration
        config = SiteConfiguration.load()
        if not config.allow_registration:
            raise ValidationError(_('Registration is currently closed. Contact an administrator.'))
