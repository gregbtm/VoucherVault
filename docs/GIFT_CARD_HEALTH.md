# Gift Card Health

VoucherVault includes two automated health-monitoring features that help you avoid losing money on gift cards whose issuers have stopped trading, or that you have simply forgotten about.

---

## 1 — Inactivity Reminders

### What it does

VoucherVault checks every active, non-archived gift card in the vault that has not been used (or had a balance transaction logged) within a configurable number of days. When a card crosses that threshold, a **"Unused Gift Card Reminder"** notification fires to any rule that subscribes to that event.

### Configuration

| Setting | Location | Default |
|---|---|---|
| Inactivity threshold (days) | Admin Tools → Site Settings → Gift Card Health | 90 |

You can set the threshold to any positive number of days. A threshold of `0` disables the check.

### Notification rules

Inactivity reminders only deliver if you have an active notification rule subscribed to the **`item_inactive`** event type. Create or update a rule at **Notifications → Rules** and tick "Unused Gift Card Reminder" in the event list.

### Which items are checked?

- Item type: **Gift Card** or **Voucher** (money-type cards where the value can be lost)
- Status: **active** (not used, not archived)
- Last-used date: further back than the configured threshold

Loyalty cards and Travel Passes are excluded — they do not hold redeemable monetary value in the same way.

---

## 2 — Merchant Health Alerts

### What it does

A background task runs weekly and queries the **Companies House** register for every unique issuer across your active vault. If a matched company is found in one of the following states, a **"Merchant Health Alert"** notification fires to any rule subscribed to that event:

| Status | Meaning |
|---|---|
| `administration` | Company is under administration — at risk |
| `liquidation` | Company is being wound down |
| `dissolved` | Company has been formally closed |
| `receivership` | Creditors are in control of assets |
| `voluntary-arrangement` | Company is restructuring its debts |
| `converted-closed` | Company converted or closed |

A matched alert fires **once per item** — it will not re-fire weekly for the same issuer while it remains in a bad state (deduplication is applied).

### Why this matters

Gift cards issued by a company in administration or liquidation can become worthless overnight. Early warning gives you time to spend the balance before the retailer stops accepting cards.

### Configuration

#### Step 1 — Get a free Companies House API key

1. Go to [developer.company-information.service.gov.uk](https://developer.company-information.service.gov.uk/)
2. Register a free account (email only, no credit card)
3. Create an application — select **Live environment**
4. Copy the API key from your application dashboard

The free tier allows 600 requests per minute, which is more than sufficient for a typical vault's weekly health check.

#### Step 2 — Add the key to VoucherVault

Site Settings → Gift Card Health → **Companies House API key**

The key is stored encrypted at rest and never logged or transmitted beyond the Companies House API itself.

#### Step 3 — Set up a notification rule

Create a rule at **Notifications → Rules** and tick **"Merchant Health Alert"** in the event list. Point it at your preferred notification backend (ntfy, email, Telegram, webhook, etc.).

### Manual health check (Developer Hub)

You can also check any merchant on-demand from **Developer Hub → Merchant Health Monitor** without waiting for the weekly background task. The panel lists every unique issuer from your active vault and lets you look them up one by one, or run a bulk "Check All" sweep.

### How matching works

The issuer name in VoucherVault (e.g. "Tesco") is submitted to the Companies House search API. The top result is accepted as a match only if the registered company name contains the issuer name (or vice versa, case-insensitively). This conservative check avoids false positives from companies that share common words.

If a retailer trades under a different registered name, the match may not be found — the check errs on the side of caution (no false alert is better than a wrong one).

---

## Developer Hub

The **Developer Hub** (`User menu → Developer`) brings together:

- **REST API Token** — generate, regenerate, or revoke your personal API token
- **Outbound Webhooks** — manage webhook endpoints for item event delivery
- **API Connectivity Tests** — live-test the Companies House key, logo.dev key, and VoucherVault API in one click
- **Merchant Health Monitor** — on-demand Companies House lookups for every issuer in your vault
- **API Quick Reference** — key endpoints, authentication format, and code examples

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No alerts arriving | Missing notification rule | Create a rule subscribed to `merchant_health_alert` |
| No alerts arriving | CH key not configured | Add key in Site Settings |
| "No CH match" for a known retailer | Company trades under a different registered name | Normal — the check is conservative |
| Key test returns error | Invalid or expired API key | Re-generate key at developer.company-information.service.gov.uk |
