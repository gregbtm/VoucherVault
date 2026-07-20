import logging

import apprise

from .base import NotificationBackend

logger = logging.getLogger(__name__)


class AppriseBackend(NotificationBackend):
    """
    Wraps the existing Apprise integration as a notify/ backend option.
    This is additive — the legacy UserProfile.apprise_urls field and its
    own scheduled task keep working independently of notification rules.

    Config:
        urls: comma-separated string, or list, of Apprise service URLs
    """

    def send(self, title: str, message: str, item=None, transaction=None) -> bool:
        raw_urls = self.config.get('urls')
        if not raw_urls:
            logger.error('apprise backend misconfigured: urls is required.')
            return False

        urls = raw_urls if isinstance(raw_urls, list) else [u.strip() for u in raw_urls.split(',')]
        urls = [u for u in urls if u]
        if not urls:
            logger.error('apprise backend misconfigured: no valid urls found.')
            return False

        apobj = apprise.Apprise()
        for url in urls:
            apobj.add(url)

        try:
            return bool(apobj.notify(body=message, title=title, notify_type=apprise.NotifyType.INFO))
        except Exception as exc:  # Apprise plugins can raise a wide variety of errors
            logger.warning('apprise notification failed: %s', exc)
            return False
