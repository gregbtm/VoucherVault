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

        if item is not None:
            self._add_item_headers(headers, item)

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

    def _add_item_headers(self, headers: dict, item) -> None:
        """Add Click, Actions, and Attach headers when a base URL is configured."""
        from myapp.models import SiteConfiguration
        base_url = (SiteConfiguration.load().vv_base_url or '').rstrip('/')
        if not base_url:
            return

        item_url = f'{base_url}/en/items/{item.id}/'
        # Tap anywhere on the notification to open the item
        headers['Click'] = item_url
        # Explicit action button visible in the ntfy app's expanded view
        headers['Actions'] = f'view, Open in VoucherVault, {item_url}'

        # Attach barcode image if the item has one
        if getattr(item, 'code_type', None) and item.code_type != 'none' and getattr(item, 'redeem_code', None):
            try:
                from django.core import signing
                token = signing.dumps(str(item.id), salt='ntfy-barcode')
                barcode_url = f'{base_url}/api/v1/items/{item.id}/notification-barcode/?s={token}'
                headers['Attach'] = barcode_url
                headers['Filename'] = 'barcode.png'
            except Exception as exc:  # pragma: no cover
                logger.warning('Could not generate barcode URL for ntfy notification: %s', exc)
