import logging

import requests

from .base import NotificationBackend

logger = logging.getLogger(__name__)


class NtfyBackend(NotificationBackend):
    """
    Config:
        server:   e.g. https://ntfy.sh or a self-hosted server such as
                  https://ntfy.example.com
        topic:    the ntfy topic to publish to
        priority: optional, one of min/low/default/high/urgent (default: 'default')
        token:    optional bearer token for authenticated/protected topics
    """

    def send(self, title: str, message: str, item=None, transaction=None) -> bool:
        server = (self.config.get('server') or '').rstrip('/')
        topic = self.config.get('topic')
        if not server or not topic:
            logger.error('ntfy backend misconfigured: server and topic are required.')
            return False

        headers = {
            # Encoded as UTF-8 bytes: plain str headers are latin-1-encoded by
            # `requests`, which breaks on titles containing emoji etc. ntfy
            # itself expects/accepts UTF-8 header values.
            'Title': title.encode('utf-8'),
            'Priority': self.config.get('priority', 'default'),
            'Tags': 'credit_card',
        }
        token = self.config.get('token')
        if token:
            headers['Authorization'] = f'Bearer {token}'

        try:
            response = requests.post(
                f'{server}/{topic}',
                data=message.encode('utf-8'),
                headers=headers,
                timeout=10,
            )
            response.raise_for_status()
            return True
        except requests.RequestException as exc:
            logger.warning('ntfy notification failed: %s', exc)
            return False
