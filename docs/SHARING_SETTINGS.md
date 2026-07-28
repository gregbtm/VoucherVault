# Sharing

Controls what the **Share via…** button on an item offers, and how public share links behave once created.

## Share via… options

With **Offer a choice on "Share via…"** enabled, tapping Share on an item gives the recipient two options:

- **Bare link** — just the item's public URL, works the same either way.
- **Rich share** — a public link that also shows the merchant, code, PIN, and remaining balance directly on the shared page, without the recipient needing a VoucherVault account.

Turn it off to go back to link-only sharing (no rich preview page).

The public share page is always read-only — a recipient can view what was shared but never sign in or edit anything through it, account or no account.

## Link expiry

**Link expiry (days)** controls how long a newly created (or regenerated) public share link stays valid before it 404s. Set to `0` for links that never expire. Changing this setting only affects links created afterward — existing links keep whatever expiry they were generated with.

## Access code

**Require a short access code to view a public share link** adds a 4-digit PIN gate to every new public share link. The code is generated per link and shown to you (the item owner) on the item's detail page — you relay it separately from the link itself (a phone call, a text, in person). It's never automatically included in the shared link or text, so possessing the link alone isn't enough to view the item.

## Where this is used

The "Share via…" button appears on the item detail page and public share pages are served at `/s/<share-id>/` — no authentication required, gated only by the optional access code above.
