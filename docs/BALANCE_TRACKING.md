# Balance & Redemption Tracking

VoucherVault Plus+ keeps a running ledger of every spend on your gift cards, vouchers, and coupons — so you always know exactly how much value remains.

## How it works

### Initial balance

When you add or edit an item, set the **Value** field to the starting balance (e.g. `£50.00`). The currency is set on the same form.

### Recording a spend

On any gift card, voucher, or coupon detail page, scroll down to the **Transaction History** section and tap **Add Transaction**. Fill in:

| Field | Purpose |
|---|---|
| **Description** | What you spent the money on (e.g. "Weekly shop") |
| **Value** | A negative number for a spend (e.g. `-12.50`), positive to top-up |
| **Date** | Defaults to now; tap the field to pick a past date/time |

The item's displayed balance automatically reflects every logged transaction, giving you a live remaining balance figure.

### Balance history chart

The detail page shows a mini sparkline chart of balance over time. Each transaction creates one data point. The chart grows as more transactions are recorded.

### Balance History log

Every transaction is also recorded to the **BalanceHistory** table, which is accessible via the REST API (`/api/v1/balance-history/`). This gives you a raw, append-only audit trail of every balance change — including initial value changes made directly on the item, not just transactions.

## Spending analytics dashboard

The **Dashboard** page has a **Spending** section that shows:

- **Total Spent** — the absolute sum of all negative transactions across all your items
- **Redeemed Value** — the face value of all items you have marked as fully Used
- **Monthly Spend** — a bar chart of spend-out for the last 12 calendar months

The same data is available via the API at `/api/v1/analytics/summary/` under the `spend_stats` key.

## Supported item types

Transactions can be logged on **gift cards**, **vouchers**, and **coupons**. Loyalty cards track points rather than monetary value and don't support the transaction ledger (use the Value field for a raw points balance instead).

## Tips

- Record a **positive** transaction if a store tops up your gift card balance (e.g. a refund).
- Leave the Date blank — it defaults to right now, which is usually correct.
- Delete a transaction from the item detail page if you logged it in error; the running balance updates immediately.
- For items where the exact remaining balance is shown on the retailer's website, use the **Balance Check URL** field on the item to save a direct link to the balance-check page.
