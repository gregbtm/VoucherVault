import logging

import requests

from .base import NotificationBackend

logger = logging.getLogger(__name__)


class TelegramBackend(NotificationBackend):
    """
    Config:
        bot_token:  Telegram Bot API token (from @BotFather)
        chat_id:    target chat/channel ID (numeric or @username)
        parse_mode: optional — 'HTML' or 'MarkdownV2' (default: plain text)
    """

    def send(self, title: str, message: str, item=None, transaction=None) -> bool:
        token = self.config.get('bot_token', '').strip()
        chat_id = self.config.get('chat_id', '').strip()
        if not token or not chat_id:
            logger.error('telegram backend misconfigured: bot_token and chat_id are required.')
            return False

        text = f'<b>{title}</b>\n{message}' if self.config.get('parse_mode') == 'HTML' else f'{title}\n{message}'
        payload = {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': self.config.get('parse_mode', ''),
        }
        if not payload['parse_mode']:
            del payload['parse_mode']

        url = f'https://api.telegram.org/bot{token}/sendMessage'
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            logger.warning('telegram notification failed: %s', exc)
            return False
