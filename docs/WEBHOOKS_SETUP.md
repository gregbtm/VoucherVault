# Outbound Webhooks

VoucherVault Plus+ fires a webhook POST when key events happen in your vault — items created, updated, archived, deleted, and when a spend is logged. This lets you pipe events into n8n, Zapier, Make, Home Assistant, or your own endpoint without polling the API.

## Setting up a webhook

1. Go to **Site Settings → Webhooks** (admin only).
2. Click **Add Webhook**.
3. Enter a URL and choose which event types to subscribe to.
4. Save — the webhook is active immediately.

## Event types

| Event | Fires when |
|---|---|
| `item.created` | A new item is saved for the first time |
| `item.updated` | Any field on an existing item changes |
| `item.archived` | An item's status is set to Archived |
| `item.deleted` | An item is permanently deleted |
| `item.used` | An item is marked Used (status toggle) |

## Payload

Each POST sends a JSON body:

```json
{
  "event": "item.created",
  "timestamp": "2025-04-01T09:00:00Z",
  "item": {
    "id": 42,
    "name": "Tesco Gift Card",
    "issuer": "Tesco",
    "type": "giftcard",
    "currency": "GBP",
    "current_value": "25.00",
    "expiry_date": "2026-01-01",
    "barcode": "5012345678901",
    "wallet": "Supermarkets"
  }
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

Failed deliveries (non-2xx response or connection error) are retried up to three times with exponential backoff (5 s, 25 s, 125 s). After three failures the delivery is marked as failed in the Webhook Log; no further retries occur.

## Webhook Log

Every delivery — successful or not — is recorded at **Site Settings → Webhook Log**. Each entry shows the event type, target URL, HTTP status, response body, and timestamp. Use this to debug integration issues.

## n8n integration

The [n8n setup guide](./N8N_SETUP.md) shows how to receive VoucherVault webhooks in an n8n workflow — including setting up a Webhook trigger node and routing events to Slack, email, or a spreadsheet.
