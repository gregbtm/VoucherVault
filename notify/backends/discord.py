import logging

import requests

from .base import NotificationBackend

logger = logging.getLogger(__name__)


class DiscordBackend(NotificationBackend):
    """
    Config:
        webhook_url:  Discord channel webhook URL
        username:     optional bot display name (default: VoucherVault)
        avatar_url:   optional bot avatar URL
    """

    def send(self, title: str, message: str, item=None, transaction=None) -> bool:
        webhook_url = (self.config.get('webhook_url') or '').strip()
        if not webhook_url.startswith(('http://', 'https://')):
            logger.error('discord backend misconfigured: webhook_url must be an http(s) URL.')
            return False

        embed = {
            'title': title,
            'description': message,
            'color': 0x6366F1,
        }
        if item:
            embed['fields'] = [
                {'name': 'Type', 'value': item.type, 'inline': True},
                {'name': 'Value', 'value': f'{item.value} {item.currency}', 'inline': True},
            ]
            if item.expiry_date:
                embed['fields'].append({'name': 'Expires', 'value': str(item.expiry_date), 'inline': True})

        payload = {
            'username': self.config.get('username') or 'VoucherVault',
            'embeds': [embed],
        }
        if self.config.get('avatar_url'):
            payload['avatar_url'] = self.config['avatar_url']

        try:
            resp = requests.post(webhook_url, json=payload, timeout=10)
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            logger.warning('discord notification failed: %s', exc)
            return False
