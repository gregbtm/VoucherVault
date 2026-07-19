import logging
from datetime import date

import requests

from .base import NotificationBackend

logger = logging.getLogger(__name__)


class FireflyBackend(NotificationBackend):
    """
    Firefly III native backend — posts a withdrawal transaction directly to a
    Firefly III instance when a gift card or voucher balance changes.

    Rule config (set once, shared across all items using this rule):
        url:   base URL of your Firefly III instance, e.g. https://firefly.example.com
        token: Personal Access Token from Firefly III → Options → Profile → OAuth

    Per-item: set firefly_account_id on the Item (the numeric Firefly account ID
    for that specific card). Use the "Link to Firefly III" button on the edit form
    to auto-create the account and populate this field, or enter the ID manually.

    This backend is designed to be scoped to the `balance_changed` event only.
    For any other event type it returns True silently (no-op) so misconfigured
    rules don't produce spurious failures.
    """

    def send(self, title: str, message: str, item=None) -> bool:
        url = (self.config.get('url') or '').rstrip('/')
        token = self.config.get('token')

        if not url or not token:
            logger.error('Firefly III backend misconfigured: url and token are required.')
            return False

        if item is None:
            return True

        account_id = item.firefly_account_id or self.config.get('account_id', '')
        if not account_id:
            logger.warning(
                'Firefly III: no account_id for item %s — link it via the edit form first.',
                item.pk,
            )
            return True

        # The most-recently created transaction is the one that triggered this call.
        transaction = item.transactions.order_by('-id').first()
        if transaction is None:
            return True

        amount = abs(transaction.value)
        description = transaction.description or item.name
        tx_date = str(transaction.date) if transaction.date else str(date.today())

        payload = {
            'transactions': [{
                'type': 'withdrawal',
                'date': tx_date,
                'amount': str(amount),
                'description': description,
                'source_id': str(account_id),
                'destination_name': item.issuer or 'Uncategorised',
                'currency_code': item.currency,
                'notes': f'Synced from VoucherVault (item {item.pk})',
            }]
        }

        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

        try:
            resp = requests.post(
                f'{url}/api/v1/transactions',
                json=payload,
                headers=headers,
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            logger.warning('Firefly III transaction push failed: %s', exc)
            return False
