# Syncing gift card / voucher balances to Firefly III

A recipe for keeping a [Firefly III](https://www.firefly-iii.org/) asset
account's balance in sync with a gift card or voucher's remaining value
in VoucherVault Plus+ — zero VoucherVault code, built entirely on the
existing webhook notification backend and an n8n workflow, the same
"VoucherVault pushes to n8n" direction described in
[`N8N_SETUP.md`](N8N_SETUP.md).

## Why a webhook, not a Firefly III import parser

Firefly III has no concept of a "gift card" — the closest fit is a small
[asset account](https://docs.firefly-iii.org/references/firefly-iii/account-types/)
you spend down over time, exactly like a bank account. VoucherVault
already fires a `balance_changed` event (see `FORK_CHANGES.md`'s Phase
12.2 section) every time a transaction is recorded against an item —
that's the same moment a matching Firefly asset account's balance should
move, so this wires the existing event straight through rather than
building a new sync mechanism.

## Step 1 — Create a dedicated notification rule

Don't reuse an existing webhook rule for other things (ntfy alerts,
n8n automations, etc.) — create one scoped to just this event, so the
payload arriving at your Firefly workflow is never mixed with unrelated
noise:

1. **Notifications → Rules → New Rule**
2. Backend: **Webhook (n8n etc.)**
3. Event types: check **only** `Balance Changed`
4. URL: the n8n Webhook trigger node's URL (Step 2 below) — n8n gives
   you this once the node is added

The webhook POSTs this JSON body (see `notify/backends/webhook.py`):

```json
{
  "title": "💷 Coffee Gift Card balance changed",
  "message": "Latte: -4.50\nNew balance: 15.50 GBP",
  "item": {
    "id": "3f2e...-uuid",
    "name": "Coffee Gift Card",
    "type": "giftcard",
    "code": "GC12345",
    "expiry_date": "2026-12-31",
    "value": "20.00",
    "currency": "GBP"
  }
}
```

`item.value` is the item's original/starting value, not the live
remaining balance — pull the current balance from `item.message` (it's
appended after "New balance:") or, more reliably, call
`GET /api/v1/items/{id}/` (see [`N8N_SETUP.md`](N8N_SETUP.md) for
token setup) from within the n8n workflow to read
`current_balance` directly.

## Step 2 — Add the n8n side

1. **Webhook** trigger node — copy its Production URL into the
   VoucherVault rule from Step 1.
2. **HTTP Request** node calling VoucherVault's API to get the
   authoritative current balance:
   - Method: `GET`
   - URL: `https://<your-vouchervault-domain>/api/v1/items/{{ $json.body.item.id }}/`
   - Authentication: Header Auth credential (see
     [`N8N_SETUP.md`](N8N_SETUP.md) Step 2)
3. **HTTP Request** node calling Firefly III to update the matching
   asset account:
   - Method: `PUT`
   - URL: `https://<your-firefly-domain>/api/v1/accounts/<account-id>`
   - Authentication: Header Auth credential with `Authorization: Bearer
     <Firefly Personal Access Token>` (Firefly III → Options → Profile →
     OAuth → Personal Access Tokens)
   - Body: `{"opening_balance": "{{ $json.current_balance }}", "opening_balance_date": "{{ $now.toISODate() }}"}`
     (Firefly represents an asset account's balance via its opening
     balance plus every transaction against it — the simplest correct
     sync is periodically resetting the opening balance to match, rather
     than trying to replay individual spend transactions)

## Mapping items to accounts

VoucherVault has no concept of a Firefly account ID, so the workflow
needs to know which Firefly account belongs to which item. Two
reasonable starting points, in order of effort:

- **One pooled "Gift Cards & Vouchers" asset account.** Simplest: every
  `balance_changed` event just re-sums all your active gift cards'
  current balances (one more `GET /api/v1/items/?type=giftcard` call)
  and writes that total as the account's opening balance. You lose
  per-card detail in Firefly, but it's a five-minute setup.
- **One Firefly account per VoucherVault wallet.** Add a **Switch** node
  keyed on `item.wallet` (include it in the webhook payload by editing
  the rule's config, or fetch it via the item API call in Step 2) that
  routes to the right Firefly account ID. More setup, but each wallet's
  spend tracks independently in your budget.

Either way, this is a workflow-side mapping table you maintain in n8n —
VoucherVault has no opinion on it.

## Limitations

- **One-way only.** Spending against the card in Firefly III (if you
  ever record a transaction there directly) doesn't flow back to
  VoucherVault — the sync only ever pushes VoucherVault's state out.
- **No delete/expiry handling.** If an item is deleted or archived in
  VoucherVault, its Firefly asset account isn't touched — that's a
  manual cleanup in Firefly, or an extra webhook rule on `item_archived`
  if you want to automate zeroing it out.
- Same reachability note as `N8N_SETUP.md`: keep both instances able to
  reach each other (same network/VPN/reverse proxy), and don't expose
  either API token publicly.
