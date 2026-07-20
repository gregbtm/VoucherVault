import logging
from datetime import date
from decimal import Decimal

import requests

from .base import NotificationBackend

logger = logging.getLogger(__name__)

_ITEM_TYPE_CATEGORIES = {
    'giftcard': 'Gift Cards',
    'voucher': 'Vouchers',
    'coupon': 'Coupons',
    'loyaltycard': 'Loyalty Cards',
    'travelpass': 'Travel',
}


class FireflyBackend(NotificationBackend):
    """
    Firefly III native backend — posts a transaction directly to a Firefly III
    instance when a gift card or voucher balance changes.

    Transaction direction:
        negative value  → withdrawal  (spending from the card balance)
        positive value  → deposit     (top-up / refund added to the balance)

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

    def send(self, title: str, message: str, item=None, transaction=None) -> bool:
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

        # Use the transaction object passed directly from notify_balance_changed
        # rather than re-querying the latest row, which would be a race condition
        # if two transactions are recorded in rapid succession.
        if transaction is None:
            logger.warning('Firefly III: no transaction object supplied for item %s, skipping.', item.pk)
            return True

        tx_value = Decimal(str(transaction.value or 0))
        amount = abs(tx_value)
        description = transaction.description or item.name
        tx_date = str(transaction.date) if transaction.date else str(date.today())

        # Map sign to Firefly transaction type
        is_deposit = tx_value > 0
        if is_deposit:
            tx_type = 'deposit'
            # For a deposit the "source" is an external revenue account, not the asset.
            source_id = None
            destination_id = str(account_id)
            source_name = item.issuer or 'Top-up'
            destination_name = None
        else:
            tx_type = 'withdrawal'
            source_id = str(account_id)
            destination_id = None
            source_name = None
            destination_name = item.issuer or 'Uncategorised'

        category = _ITEM_TYPE_CATEGORIES.get(item.type, '')

        # VoucherVault tags → Firefly tags (item tags + item type)
        try:
            item_tags = list(item.tags.values_list('name', flat=True))
        except Exception:
            item_tags = []
        firefly_tags = item_tags + ([item.get_type_display()] if item.type else [])

        tx_row = {
            'type': tx_type,
            'date': tx_date,
            'amount': str(amount),
            'description': description,
            'currency_code': item.currency,
            'notes': f'Synced from VoucherVault (item {item.pk})',
        }
        if source_id is not None:
            tx_row['source_id'] = source_id
        if source_name is not None:
            tx_row['source_name'] = source_name
        if destination_id is not None:
            tx_row['destination_id'] = destination_id
        if destination_name is not None:
            tx_row['destination_name'] = destination_name
        if category:
            tx_row['category_name'] = category
        if firefly_tags:
            tx_row['tags'] = firefly_tags

        payload = {'transactions': [tx_row]}

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
            try:
                firefly_tx_id = resp.json()['data']['id']
                logger.info('Firefly III transaction %s created for item %s.', firefly_tx_id, item.pk)
            except (KeyError, ValueError):
                pass
            return True
        except requests.RequestException as exc:
            logger.warning('Firefly III transaction push failed: %s', exc)
            return False
