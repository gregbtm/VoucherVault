# Analytics & Duplicate Detection

Tuning knobs for the Dashboard widgets and the duplicate-photo detector on the item form. None of these change what data is stored — they only control how much is shown, and how strict duplicate matching is.

## Dashboard widgets

| Field | Purpose |
|---|---|
| **Expiring Soon list length** | Max number of items shown in the Dashboard's Expiring Soon list. Lower this on a large vault to keep the widget short; items beyond the limit aren't hidden anywhere else, just not listed here. |
| **Expiry calendar months** | How many months ahead the Dashboard's expiry calendar covers. |
| **Wallet chart limit** | How many wallets the spend-by-wallet chart shows individually before folding the rest into a single "Other" slice. |

## Duplicate photo sensitivity

When scanning or uploading a document, VoucherVault compares its perceptual hash against your existing items' photos to catch accidental re-scans of the same card. **Duplicate photo sensitivity** is the hamming-distance threshold for that comparison, on a 0–64 scale:

- **Lower** = stricter — only near-identical photos are flagged, fewer false positives, but a duplicate photographed at a different angle/lighting might slip through.
- **Higher** = looser — catches more genuine duplicates, but increases the chance of two different (but visually similar) cards being flagged as the same one.

This is one of two duplicate-detection layers — VoucherVault also does exact and fuzzy matching on the *decoded* redeem code, which isn't affected by this setting and catches duplicates even when the photos look completely different.
