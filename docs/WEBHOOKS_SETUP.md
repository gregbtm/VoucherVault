# Outbound Webhooks

VoucherVault Plus+ fires a webhook POST when key events happen in your vault — items created, used, archived, expiring, shared, and more. This lets you pipe events into n8n, Zapier, Make, Home Assistant, or your own endpoint without polling the API. Webhooks are per-user, set up from **Webhooks** in the sidebar (also reachable via **Developer Hub**) — no admin role required, each user manages their own.

## Setting up a webhook

1. Go to **Webhooks** in the sidebar.
2. Click **Add Webhook**.
3. Enter a URL and tick which event types should fire it. You can subscribe one webhook to several events, or set up separate webhooks per event.
4. Save — the webhook is active immediately.

## Event types

| Event | Fires when |
|---|---|
| `item_created` | A new item is saved for the first time |
| `item_used` | An item is marked Used |
| `item_archived` | An item's status is set to Archived |
| `item_balance_changed` | A transaction changes an item's current balance |
| `item_expiry_warning` | An item is approaching its expiry date (same lead time as notification rules) |
| `item_shared_with_you` | Another user shares an item with you — fires for the **recipient**, not the owner |
| `next_up_reminder` | A Next Up widget item is due today |
| `wallet_invited` | You're added as a collaborator on a shared wallet |
| `merchant_health_alert` | A merchant tied to one of your items shows up as in administration/liquidation on Companies House |
| `renewal_advanced` | A recurring item's renewal date passes and it auto-renews for the next period |
| `budget_overspend` | A wallet with a monthly budget set goes over it for the first time that calendar month |

A handful of events (`wallet_invited`, `budget_overspend`) aren't about any single item — the payload for those omits the `item` key entirely rather than sending a null placeholder, and includes wallet details in its place (see below).

## Payload

Each POST sends a JSON body. For an item-scoped event:

```json
{
  "event": "item_created",
  "timestamp": "2026-04-01T09:00:00Z",
  "item": {
    "id": "3f1b6b2a-...-e1c9",
    "name": "Tesco Gift Card",
    "issuer": "Tesco",
    "type": "giftcard",
    "value": "25.00",
    "currency": "GBP",
    "expiry_date": "2027-01-01",
    "is_used": false,
    "is_archived": false
  }
}
```

Events with no single item (e.g. `budget_overspend`) merge in event-specific fields instead of an `item` key:

```json
{
  "event": "budget_overspend",
  "timestamp": "2026-04-01T09:00:00Z",
  "wallet": { "id": 7, "name": "Supermarkets" },
  "budget_amount": "100.00",
  "spent": "134.50"
}
```

## Security

Webhook deliveries include an `X-VoucherVault-Signature` header — a HMAC-SHA256 hex digest of the raw request body, signed with the secret you set in Site Settings. Verify the signature in your endpoint before acting on the payload.

```python
import hmac, hashlib

def verify(body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

## Retry policy

Failed deliveries (non-2xx response or connection error) are retried automatically with exponential backoff:

| Attempt | Delay | Cumulative Time |
|---------|-------|-----------------|
| 1 | 1 minute | 1 minute |
| 2 | 5 minutes | 6 minutes |
| 3 | 15 minutes | 21 minutes |
| 4 | 1 hour | 1 hour 21 minutes |
| 5 | 4 hours | 5 hours 21 minutes |

The retry system (Celery task `process_webhook_retries`) runs every 5 minutes and automatically reschedules failed deliveries. After 5 failed attempts over approximately 24 hours, the delivery is abandoned and logged as failed in the Webhook Log. This means transient failures (network blips, temporary service outages) are handled gracefully without manual intervention.

## Webhook Log

Every delivery — successful or not — is recorded at **Site Settings → Webhook Log**. Each entry shows the event type, target URL, HTTP status, response body, and timestamp. Use this to debug integration issues.

## n8n integration

The [n8n setup guide](./N8N_SETUP.md) shows how to receive VoucherVault webhooks in an n8n workflow — including setting up a Webhook trigger node and routing events to Slack, email, or a spreadsheet.
