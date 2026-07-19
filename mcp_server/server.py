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


if __name__ == '__main__':
    mcp.run(transport='streamable-http')
