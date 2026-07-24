import logging

from django.core.mail.backends.smtp import EmailBackend

logger = logging.getLogger(__name__)


class SiteConfigEmailBackend(EmailBackend):
    """
    SMTP backend that reads connection parameters from SiteConfiguration at
    open time, falling back to Django settings when no host is configured.
    """

    def open(self):
        from myapp.models import SiteConfiguration
        config = SiteConfiguration.load()
        if config.email_host:
            self.host = config.email_host
            self.port = config.email_port or 587
            self.username = config.email_host_user
            self.password = config.email_host_password
            self.use_tls = config.email_use_tls
            self.use_ssl = config.email_use_ssl
        return super().open()
