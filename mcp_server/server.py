import os
from datetime import date, timedelta

from mcp.server.fastmcp import FastMCP

try:
    from .client import VoucherVaultClient  # run as `python -m mcp_server.server`
except ImportError:
    from client import VoucherVaultClient  # run as `python server.py` inside this directory (Docker)

mcp = FastMCP(
    'VoucherVault',
    host=os.environ.get('MCP_HOST', '0.0.0.0'),
    port=int(os.environ.get('MCP_PORT', '8100')),
)


def _client() -> VoucherVaultClient:
    # Constructed per call rather than at import time so a missing/invalid
    # env var surfaces as a normal tool error instead of crashing the
    # server on startup.
    return VoucherVaultClient()


@mcp.tool()
def search_items(
    query: str | None = None,
    item_type: str | None = None,
    is_used: bool | None = None,
    is_archived: bool | None = None,
) -> list[dict]:
    """
    Search the authenticated user's VoucherVault items. `query` matches
    against name, redeem code, issuer, and description. `item_type` is one
    of voucher, giftcard, coupon, loyaltycard.
    """
    result = _client().list_items(search=query, type=item_type, is_used=is_used, is_archived=is_archived)
    return result.get('results', result)


@mcp.tool()
def get_item(item_id: str) -> dict:
    """Get full details for a single item by its ID."""
    return _client().get_item(item_id)


@mcp.tool()
def get_expiring_items(days: int = 30) -> list[dict]:
    """List unused, non-archived items expiring within the next `days` days."""
    cutoff = (date.today() + timedelta(days=days)).isoformat()
    result = _client().list_items(
        is_used=False, is_archived=False, expires_before=cutoff, ordering='expiry_date',
    )
    return result.get('results', result)


@mcp.tool()
def get_analytics_summary() -> dict:
    """KPI summary for the authenticated user's vault: item counts by status/type, and value by currency."""
    return _client().get_analytics_summary()


@mcp.tool()
def create_item(
    type: str,
    name: str,
    redeem_code: str,
    issuer: str,
    expiry_date: str,
    value: str = '0',
    currency: str = 'GBP',
    code_type: str = 'qrcode',
    value_type: str = 'money',
    pin: str | None = None,
    notes: str | None = None,
) -> dict:
    """
    Create a new item. `type` is one of voucher, giftcard, coupon,
    loyaltycard. `expiry_date` is an ISO date (YYYY-MM-DD). Goes through
    the same validation as the web UI and REST API (e.g. gift cards need a
    non-negative value; loyalty cards ignore value).
    """
    payload = {
        'type': type, 'name': name, 'redeem_code': redeem_code, 'issuer': issuer,
        'expiry_date': expiry_date, 'value': value, 'currency': currency,
        'code_type': code_type, 'value_type': value_type,
    }
    if pin:
        payload['pin'] = pin
    if notes:
        payload['notes'] = notes
    return _client().create_item(payload)


@mcp.tool()
def add_transaction(item_id: str, description: str, value: str) -> dict:
    """
    Record a spend against a gift card's balance. `value` must be a
    negative amount (e.g. "-4.50" for a £4.50 spend) and cannot take the
    balance below zero — the API rejects both the same way the web UI does.
    """
    return _client().add_transaction(item_id, description, value)


@mcp.tool()
def mark_item_used(item_id: str) -> dict:
    """Mark an item as used/redeemed."""
    return _client().redeem_item(item_id)


@mcp.tool()
def set_item_archived(item_id: str, archived: bool) -> dict:
    """Archive or unarchive an item, hiding/restoring it in the default Inventory view."""
    return _client().update_item(item_id, {'is_archived': archived})


@mcp.tool()
def get_expiry_timeline() -> dict:
    """Expiry counts grouped by week for the next 12 weeks — useful for spotting upcoming crunch points."""
    return _client().get_expiry_timeline()


@mcp.tool()
def list_wallets() -> list[dict]:
    """List all wallets (collections of items) belonging to the authenticated user."""
    result = _client().list_wallets()
    return result.get('results', result)


@mcp.tool()
def create_wallet(name: str, description: str | None = None, color: str | None = None) -> dict:
    """
    Create a new wallet. Useful for grouping items by category (e.g. "Supermarkets", "Travel").

    Args:
        name:        Unique wallet name.
        description: Optional short description.
        color:       Optional hex color string, e.g. "#4154f1".
    """
    payload: dict = {'name': name}
    if description:
        payload['description'] = description
    if color:
        payload['color'] = color
    return _client().create_wallet(payload)


@mcp.tool()
def list_tags() -> list[dict]:
    """List all tags belonging to the authenticated user, including item counts."""
    result = _client().list_tags()
    return result.get('results', result)


@mcp.tool()
def list_webhooks() -> list[dict]:
    """List all outbound webhooks configured by the authenticated user."""
    result = _client().list_webhooks()
    return result.get('results', result)


@mcp.tool()
def list_wallet_activity(wallet_id: str | None = None) -> list[dict]:
    """
    List the audit log of actions taken on items inside a wallet (or all wallets).

    Args:
        wallet_id: Filter to a specific wallet UUID (optional).
    """
    result = _client().list_wallet_activity(wallet_id=wallet_id)
    return result.get('results', result)


@mcp.tool()
def update_item(
    item_id: str,
    name: str | None = None,
    redeem_code: str | None = None,
    issuer: str | None = None,
    expiry_date: str | None = None,
    value: str | None = None,
    currency: str | None = None,
    pin: str | None = None,
    notes: str | None = None,
    is_pinned: bool | None = None,
    is_archived: bool | None = None,
    notifications_muted: bool | None = None,
    wallet: str | None = None,
) -> dict:
    """
    Update one or more fields on an existing item. Only the fields you supply
    are changed — unspecified fields are left as-is (PATCH semantics).

    Args:
        item_id:             UUID of the item to update.
        name:                Display name.
        redeem_code:         The code string (barcode payload or manual code).
        issuer:              Brand / merchant name.
        expiry_date:         ISO date (YYYY-MM-DD).
        value:               Decimal string, e.g. "25.00".
        currency:            3-letter ISO currency code, e.g. "GBP".
        pin:                 Optional PIN shown alongside the code.
        notes:               Free-form notes.
        is_pinned:           Pin the item to the top of Inventory.
        is_archived:         Archive (hide) or unarchive the item.
        notifications_muted: Suppress expiry notifications for this item.
        wallet:              UUID of the wallet to move the item into, or null.
    """
    payload = {k: v for k, v in {
        'name': name, 'redeem_code': redeem_code, 'issuer': issuer,
        'expiry_date': expiry_date, 'value': value, 'currency': currency,
        'pin': pin, 'notes': notes, 'is_pinned': is_pinned,
        'is_archived': is_archived, 'notifications_muted': notifications_muted,
        'wallet': wallet,
    }.items() if v is not None}
    return _client().update_item(item_id, payload)


@mcp.tool()
def delete_item(item_id: str) -> dict:
    """
    Permanently delete an item and all its associated data (transactions,
    documents, share links). This cannot be undone.

    Args:
        item_id: UUID of the item to delete.
    """
    return _client().delete_item(item_id)


@mcp.tool()
def list_notification_rules() -> list[dict]:
    """
    List all notification rules configured by the authenticated user.
    Each rule defines a backend (ntfy, webhook, apprise, firefly) plus the
    event types it fires on (expiry_default, expiry_final, item_created, etc.).
    """
    result = _client().list_notification_rules()
    return result.get('results', result)


@mcp.tool()
def create_notification_rule(
    name: str,
    backend: str,
    config: dict,
    event_types: list[str],
    enabled: bool = True,
) -> dict:
    """
    Create a new notification rule.

    Args:
        name:        Unique rule name.
        backend:     One of: ntfy, webhook, apprise, firefly.
        config:      Backend-specific config dict.
                     ntfy: {"server": "https://ntfy.sh", "topic": "my-topic"}
                     webhook: {"url": "https://…"}
                     apprise: {"urls": ["tgram://bottoken/ChatID"]}
                     firefly: {"url": "https://…", "token": "…"}
        event_types: List of events to trigger on, e.g. ["expiry_default", "item_created"].
        enabled:     Whether the rule is active (default true).
    """
    return _client().create_notification_rule({
        'name': name, 'backend': backend, 'config': config,
        'event_types': event_types, 'enabled': enabled,
    })


@mcp.tool()
def delete_notification_rule(rule_id: str) -> dict:
    """
    Permanently delete a notification rule.

    Args:
        rule_id: The integer ID of the rule to delete.
    """
    return _client().delete_notification_rule(rule_id)


@mcp.tool()
def list_item_documents(item_id: str) -> list[dict]:
    """
    List all supporting documents (receipts, proofs of purchase) attached
    to an item. Each entry includes the document ID, filename URL, and
    upload timestamp.

    Args:
        item_id: UUID of the item whose documents to list.
    """
    result = _client().list_item_documents(item_id)
    return result.get('results', result) if isinstance(result, dict) else result


@mcp.tool()
def get_user_preferences() -> dict:
    """
    Retrieve the authenticated user's display and notification preferences
    (sort order, view mode, currency, feature toggles, etc.).
    """
    return _client().get_preferences()


@mcp.tool()
def update_user_preferences(
    sort_by: str | None = None,
    sort_order: str | None = None,
    view_mode: str | None = None,
    default_currency: str | None = None,
    show_issue_date: bool | None = None,
    show_expiry_date: bool | None = None,
    show_value: bool | None = None,
    show_description: bool | None = None,
    keep_screen_awake: bool | None = None,
    blur_codes_enabled: bool | None = None,
    notifications_muted: bool | None = None,
) -> dict:
    """
    Update one or more user preferences. Only supplied fields are changed.

    Args:
        sort_by:            Inventory sort field: expiry_date, name, issue_date, value, last_used_at.
        sort_order:         asc or desc.
        view_mode:          compact or standard.
        default_currency:   3-letter ISO code, e.g. GBP.
        show_issue_date:    Show issue date column in Inventory.
        show_expiry_date:   Show expiry date column in Inventory.
        show_value:         Show value column in Inventory.
        show_description:   Show description in Inventory tiles.
        keep_screen_awake:  Keep display on while viewing a barcode.
        blur_codes_enabled: Blur barcodes until tapped.
        notifications_muted: Mute all expiry notifications globally.
    """
    payload = {k: v for k, v in {
        'sort_by': sort_by, 'sort_order': sort_order, 'view_mode': view_mode,
        'default_currency': default_currency, 'show_issue_date': show_issue_date,
        'show_expiry_date': show_expiry_date, 'show_value': show_value,
        'show_description': show_description, 'keep_screen_awake': keep_screen_awake,
        'blur_codes_enabled': blur_codes_enabled,
    }.items() if v is not None}
    return _client().update_preferences(payload)


@mcp.tool()
def list_dms_providers() -> list[dict]:
    """
    List the Document Management System (DMS) providers configured by the
    authenticated user. Supports Paperless-ngx, Docspell, and PaperMerge.
    Each entry includes connection status and sync settings.
    """
    result = _client().list_dms_providers()
    return result.get('results', result) if isinstance(result, dict) else result


@mcp.tool()
def list_dms_sync_logs(provider_id: str | None = None) -> list[dict]:
    """
    List recent DMS sync history — documents pushed to or pulled from a
    Document Management System.

    Args:
        provider_id: Optional UUID of a specific DMS provider to filter by.
    """
    result = _client().list_dms_sync_logs(provider_id=provider_id)
    return result.get('results', result) if isinstance(result, dict) else result


if __name__ == '__main__':
    mcp.run(transport='streamable-http')
