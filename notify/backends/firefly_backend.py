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


def _do_firefly_push(config: dict, item, transaction) -> bool:
    """
    Executes the HTTP POST to Firefly III for a single transaction.
    On success, writes the returned Firefly transaction ID back to
    transaction.firefly_transaction_id. Returns True on success.
    """
    url = (config.get('url') or '').rstrip('/')
    token = config.get('token', '')
    if not url or not token:
        logger.error('Firefly III: url and token are required in rule config.')
        return False

    account_id = item.firefly_account_id or config.get('account_id', '')
    if not account_id:
        logger.warning('Firefly III: no account_id for item %s.', item.pk)
        return False

    tx_value = Decimal(str(transaction.value or 0))
    amount = abs(tx_value)
    if not amount:
        return True
    description = transaction.description or item.name
    tx_date = str(transaction.date) if transaction.date else str(date.today())

    is_deposit = tx_value > 0
    if is_deposit:
        tx_type = 'deposit'
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
            # Write Firefly transaction ID back so we know this push succeeded.
            from myapp.models import Transaction
            Transaction.objects.filter(pk=transaction.pk).update(firefly_transaction_id=str(firefly_tx_id))
        except (KeyError, ValueError):
            pass
        return True
    except requests.RequestException as exc:
        logger.warning('Firefly III transaction push failed: %s', exc)
        return False


class FireflyBackend(NotificationBackend):
    """
    Firefly III native backend — posts a transaction directly to a Firefly III
    instance when a gift card or voucher balance changes.

    Transaction direction:
        negative value  → withdrawal  (spending from the card balance)
        positive value  → deposit     (top-up / refund added to the balance)

    Rule config (set once, shared across all items using this rule):
        url:                    base URL of your Firefly III instance
        token:                  Personal Access Token from Firefly III
        close_account_on_archive: true to mark the Firefly account inactive when
                                  the VoucherVault item is archived (default false)

    Per-item: set firefly_account_id on the Item. Use 'Link to Firefly III'
    on the edit form to auto-create the account, or enter the ID manually.

    When called via get_backend(rule) the rule_id is set, and balance-changed
    pushes are dispatched to the push_transaction_to_firefly Celery task so the
    HTTP call leaves the request path. When called directly without a rule_id
    (e.g. in tests) the push executes synchronously.
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

        if transaction is None:
            # No transaction: non-balance-changed event (e.g. item_created, item_archived).
            # Archive is handled separately by notify_item_archived; nothing to do here.
            return True

        if self.rule_id is not None:
            # Async path: dispatch to Celery so the HTTP call leaves the request.
            try:
                from notify.tasks import push_transaction_to_firefly
                push_transaction_to_firefly.delay(self.rule_id, str(item.pk), str(transaction.pk))
                return True
            except Exception as exc:
                logger.warning('Firefly III: could not enqueue async push, falling back to sync: %s', exc)

        # Sync fallback (no rule_id, e.g. direct instantiation, or Celery unavailable).
        return _do_firefly_push(self.config, item, transaction)
