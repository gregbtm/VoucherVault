# Fixer.io — Currency Rates

Optional, per-user integration that lets the Dashboard combine items in different currencies into a single total, converted to your default currency at current exchange rates.

## Do you need this?

Only if your vault has items in more than one currency and you want the Dashboard's total value to combine them. If every item uses the same currency, this integration has nothing to do — skip it.

## Setup

1. Sign up for a free API key at [fixer.io](https://fixer.io/signup/free/monthly) (no card required for the free tier).
2. Go to **Preferences → Currency** and paste the key into **Fixer.io API Key**.
3. Set your **Default Currency** — this is what mixed-currency totals are converted *into*.

## What it does

Once a key is set, VoucherVault periodically fetches exchange rates from Fixer.io and caches them. When the Dashboard computes a combined total across items in different currencies, each item's value is converted to your default currency using the cached rate before summing.

## Without a key

Mixed-currency totals simply aren't combined — each currency is shown separately rather than converted and summed. Nothing else in the app depends on this; item values, balances, and transaction history all work normally regardless.

## Testing

Developer Hub → **Your integrations** → **Fixer.io — currency rates** → **Test** confirms the configured key is valid and rates are loading.
