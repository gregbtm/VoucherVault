# Merchant Logos

VoucherVault can fetch a brand logo for each item card automatically, based on the issuer name or a logo domain hint.

## How it works

1. When an item is saved with an issuer (e.g. "Amazon") and no logo already cached, a background task looks up that merchant's logo.
2. The result is cached per merchant so repeat lookups for the same issuer are instant.
3. If a card's `logo_slug` field is set (a bare domain, e.g. `amazon.co.uk`), that's used as the lookup hint instead of guessing from the issuer name — more reliable for merchants whose brand name doesn't match their domain.

## Sources, in order of preference

1. **logo.dev** — used if a publishable key is configured (Site Settings → Merchant Logos → **logo.dev publishable key**). Higher resolution (800px, WebP) and covers more merchants. Get a free key at [logo.dev](https://www.logo.dev).
2. **Clearbit's public logo API** — used automatically as a fallback if no logo.dev key is set, or if logo.dev has no match.
3. **Generated initial avatar** — if neither source has a match, a colored circle with the merchant's initial is generated instead of a blank/broken image.

## Turning it off

Untick **Auto-fetch merchant logos on item cards** in Site Settings → Merchant Logos. Items keep whatever logo they already fetched; only new lookups stop.

## Forcing a refetch

If a merchant's logo changes or was fetched incorrectly, edit the item and change (or re-save) the **Logo domain hint** field — this invalidates the cached logo for that merchant and triggers a fresh lookup on next save.
