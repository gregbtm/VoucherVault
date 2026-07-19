# Syncing gift card / voucher spend to Firefly III

VoucherVault Plus+ has a **native Firefly III notification backend** that posts
a withdrawal transaction directly to your Firefly III instance every time a
spend transaction is recorded against a gift card or voucher. No n8n, no
middleware — just two config values and one click per card.

---

## How it works

When you record a spend transaction against a gift card or voucher, VoucherVault
fires a `balance_changed` event. If a **Firefly III** notification rule is
enabled for that event, the backend:

1. Looks up the item's `Firefly III Account ID` (stored per-item).
2. Posts a `withdrawal` transaction to `/api/v1/transactions` on your Firefly
   instance, using that asset account as the source.
3. Sets `destination_name` to the item's issuer (e.g. "Starbucks"), so spend
   appears as a named expense category in your budget.

The withdrawal amount equals `abs(transaction.value)` — the exact amount spent,
not a recalculated balance. Each spend creates one Firefly transaction, giving
you a full history in Firefly rather than just a running total.

---

## Step 1 — Create a Firefly III notification rule

1. **Notifications → Rules → New Rule**
2. Backend: **Firefly III**
3. Event types: check **only** `Balance Changed`
4. Config:
   - `url`: base URL of your Firefly III instance, e.g.
     `https://firefly.example.com` (no trailing slash)
   - `token`: a Firefly III Personal Access Token — create one at
     **Firefly III → Options → Profile → OAuth → Personal Access Tokens**

> The `url` and `token` are shared across all items that use this rule. You
> don't need one rule per card.

---

## Step 2 — Link each gift card to a Firefly III account

Each gift card or voucher needs its own Firefly III asset account. The
**Firefly III Account ID** field is on the card's edit form (visible only
for Gift Card and Voucher types).

### Option A — Auto-link (recommended)

On the edit form for your gift card:

1. Make sure the Firefly III Account ID field is visible (it appears when the
   type is Gift Card or Voucher).
2. Click **Link to Firefly III**.

VoucherVault will:
- Call `POST /api/v1/accounts` on your Firefly III instance.
- Create an asset account named after the card (e.g. "Amazon Gift Card (Amazon)").
- Set the opening balance to the card's current value.
- Store the returned account ID in the item's **Firefly III Account ID** field.

### Option B — Manual

If you already have a Firefly III asset account for this card, find its
numeric ID (visible in the account URL on Firefly III, e.g. `.../accounts/42`)
and paste it into the **Firefly III Account ID** field on the edit form.

---

## Verifying it works

After linking a card, record a spend transaction (Spend → enter an amount).
Then check Firefly III — a new withdrawal should appear under the linked
asset account, with:
- Amount: the exact spend amount
- Description: the transaction description (or card name if blank)
- Source: the asset account you linked
- Destination: the card's issuer (or "Uncategorised" if blank)
- Currency: the card's currency

---

## Limitations

- **One-way only.** Spending recorded in Firefly III directly doesn't flow
  back to VoucherVault.
- **balance\_changed events only.** Creating, archiving, or deleting an item
  doesn't touch Firefly — only spend transactions trigger withdrawals.
- **Negative transactions are skipped.** The backend only posts when
  `abs(transaction.value) > 0`. If you record a zero-value correction it's
  silently ignored.
- **No delete/archive cleanup.** If you delete or archive a VoucherVault item,
  its Firefly asset account isn't touched.

---

## Legacy n8n approach

The previous version of this document described a webhook → n8n → Firefly III
pipeline. That approach still works for users who prefer it — see
[`N8N_SETUP.md`](N8N_SETUP.md). The native backend is simpler and requires no
n8n instance.
