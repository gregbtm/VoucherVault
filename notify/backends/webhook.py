import logging

import requests

from .base import NotificationBackend

logger = logging.getLogger(__name__)


class WebhookBackend(NotificationBackend):
    """
    Generic HTTP webhook backend, n8n-friendly.

    Config:
        url:     the webhook URL to POST a JSON payload to
        headers: optional dict of extra headers (e.g. a shared secret header)
    """

    def send(self, title: str, message: str, item=None, transaction=None) -> bool:
        url = self.config.get('url')
        if not url or not url.startswith(('http://', 'https://')):
            logger.error('webhook backend misconfigured: url must be an http(s) URL.')
            return False

        payload = {
            'title': title,
            'message': message,
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
            return False
