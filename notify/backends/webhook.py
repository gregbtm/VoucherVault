import logging
from datetime import timedelta

import requests
from django.utils import timezone

from .base import NotificationBackend

logger = logging.getLogger(__name__)


class WebhookBackend(NotificationBackend):
    """
    Generic HTTP webhook backend with exponential backoff retry.
    Config:
        url:     the webhook URL to POST a JSON payload to
        headers: optional dict of extra headers (e.g. a shared secret header)
    """

    def send(self, title: str, message: str, item=None, event_type='', transaction=None) -> bool:
        url = self.config.get('url')
        if not url or not url.startswith(('http://', 'https://')):
            logger.error('webhook backend misconfigured: url must be an http(s) URL.')
            return False

        payload = {
            'title': title,
            'message': message,
            'event_type': event_type,
            'timestamp': timezone.now().isoformat(),
            'item': {
                'id': str(item.pk),
                'name': item.name,
                'type': item.type,
                'code': item.redeem_code,
                'expiry_date': str(item.expiry_date) if item.expiry_date else None,
                'value': str(item.value),
                'currency': item.currency,
            } if item else None,
        }

        headers = self.config.get('headers') or {}
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            response.raise_for_status()
            return True
        except requests.RequestException as exc:
            logger.warning('webhook notification failed: %s', exc)
            self._schedule_retry(payload, str(exc))
            return False

    def _schedule_retry(self, payload, error_msg):
        """Schedule a retry with exponential backoff."""
        from notify.models import WebhookRetry

        retry = WebhookRetry.objects.create(
            rule=self.rule,
            event_type=payload.get('event_type', ''),
            payload=payload,
            last_error=error_msg,
            attempt=0,
        )
        self._schedule_next_retry(retry)

    @staticmethod
    def _schedule_next_retry(retry):
        """Calculate and set next retry time using exponential backoff."""
        backoff = WebhookRetry.RETRY_BACKOFF
        if retry.attempt < len(backoff):
            delay = backoff[retry.attempt]
            retry.next_retry_at = timezone.now() + timedelta(seconds=delay)
            retry.save()
