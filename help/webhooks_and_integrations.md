# Webhooks & Integrations

VoucherVault sends webhooks to external services like n8n, Zapier, or your own endpoint when events happen. This guide covers the new retry system and integration options.

## Webhook Retry System

### How It Works

When a webhook delivery fails (network error, timeout, non-2xx response), VoucherVault automatically retries with exponential backoff:

| Attempt | Delay | Cumulative |
|---------|-------|-----------|
| 1 | 1 min | 1 min |
| 2 | 5 min | 6 min |
| 3 | 15 min | 21 min |
| 4 | 1 hour | 1h 21m |
| 5 | 4 hours | 5h 21m |

**Key Points:**
- Retries happen automatically (no manual intervention needed)
- Maximum 5 attempts over approximately 24 hours
- After 5 failures, the delivery is marked as failed in the Webhook Log
- Transient failures (temporary network issues) recover automatically

### Why Exponential Backoff?

Exponential backoff prevents overwhelming a temporarily unavailable service:
- **Early attempts** (1 min, 5 min) handle brief network blips quickly
- **Later attempts** (1 hour, 4 hours) give failing services time to recover
- Retries stop after 24 hours to avoid retry storms

## Webhook Events

Webhooks fire for key item lifecycle events:

```json
{
  "title": "Item expiring soon",
  "message": "Tesco Gift Card expires in 7 days",
  "event_type": "expiry_warning",
  "timestamp": "2026-07-27T17:30:00Z",
  "item": {
    "id": "123",
    "name": "Tesco Gift Card",
    "type": "gift_card",
    "value": "20.00",
    "currency": "GBP",
    "expiry_date": "2026-08-03"
  }
}
```

## Integration Examples

### Example 1: n8n Workflow (Email on Expiry)

1. Create an n8n workflow with a **Webhook** trigger node
2. Copy the webhook URL from VoucherVault (Notification Rules → Webhook backend)
3. Use the Webhook trigger to receive VoucherVault events
4. Add a **Filter** node: `event_type == "expiry_warning"`
5. Add a **Send Email** node
6. Test with a gift card expiring soon

**Result:** You get an email 7 days before expiry, automatically.

### Example 2: Bank Statement Import via n8n

1. Set up an n8n workflow that polls your bank API monthly
2. Parse the CSV/JSON statement using n8n's built-in nodes
3. Identify gift card purchases (look for keywords: "gift", "card", "voucher", "reload")
4. For each identified purchase, trigger VoucherVault's **Create Item** API endpoint
5. Set the item name, value, and currency

**Result:** Gift cards purchased via bank transfer are automatically imported.

### Example 3: Spreadsheet Sync (Google Sheets)

1. Create n8n workflow triggered on a schedule (daily/weekly)
2. Query VoucherVault API: `GET /api/v1/items/?user_id=me`
3. Transform response to rows: Name, Value, Currency, Expiry, Status
4. Append to Google Sheet
5. Set data validation & conditional formatting for visual tracking

**Result:** Your gift card inventory syncs to a shared Google Sheet daily.

## Debugging Failed Webhooks

If a webhook isn't working:

1. **Check Webhook Log:** Go to **Site Settings → Webhook Log** (admin only)
2. **Find the failed delivery:** Look for your webhook URL with HTTP error codes
3. **Read the response body:** Error messages show what went wrong
4. **Verify the endpoint:** Test your endpoint manually with `curl`:
   ```bash
   curl -X POST https://your-endpoint.com/webhook \
     -H "Content-Type: application/json" \
     -d '{"event_type":"test","message":"Test"}'
   ```
5. **Check Firewall/Auth:** Ensure your endpoint is publicly accessible and any authentication is configured

## Security

Every webhook includes an `X-VoucherVault-Signature` header:

```
X-VoucherVault-Signature: sha256=abc123def456...
```

Verify the signature in your endpoint:
```python
import hmac, hashlib

def verify(body: bytes, signature: str, secret: str):
    expected = hmac.new(
        secret.encode(), 
        body, 
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
```

## Firefly III Integration

VoucherVault can sync transactions to Firefly III for accounting:

1. Go to **Notification Rules** → **New Rule**
2. Select backend: **Firefly III**
3. Configure Firefly API URL, API key, and target account
4. Set events: `balance_changed`, `item_created`, etc.
5. Save

Transactions sync automatically. The system prevents duplicates by tracking which items have already been synced.

## Export Formats

Export items for use in other tools:

**YNAB (You Need A Budget):**
```csv
Date,Payee,Category,Amount
07/20/2026,Tesco,Gifts,-20.00
```

**Firefly III JSON:**
```json
{
  "transactions": [
    {
      "type": "withdrawal",
      "date": "2026-07-20",
      "amount": "20.00",
      "description": "Tesco Gift Card",
      "tags": ["gift_card"]
    }
  ]
}
```

**Spreadsheet CSV:**
Includes all fields: Name, Type, Value, Currency, Issuer, Code, Issue Date, Expiry, Status, Description, Wallet, Tags, Category.

## Tips

- **Test webhooks first:** Before deploying to production, test with a simple webhook echo service
- **Monitor the log:** Check Webhook Log weekly to catch configuration issues early
- **Use filters:** In n8n, filter events to reduce noise (e.g., only email on final expiry warning)
- **Set timeouts:** Your endpoint should respond within 10 seconds; VoucherVault times out after that
- **Secure your endpoint:** Use authentication tokens or IP whitelisting if possible

