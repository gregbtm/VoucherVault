# New Features in This Fork

A highlights tour of what this fork adds on top of upstream VoucherVault —
nothing upstream was rewritten, so everything below is opt-in or additive.
For the complete, up-to-date feature list see the README's
[What's New in This Fork](https://github.com/gregbtm/VoucherVault#-whats-new-in-this-fork)
section; this page won't try to keep up with every new round, so treat the
README as the source of truth if the two ever disagree.

## 🔌 API & AI Integrations

- A full **REST API** with interactive Swagger docs at `/api/v1/docs/` —
  script against your own vault, no Postman required
- An **MCP server** so Claude Desktop, Claude Code, and other AI
  assistants can search, log spend, and create items directly
- A **zero-code n8n integration** via the existing OpenAPI schema,
  including a ready-made recipe for syncing balances to Firefly III
- **Webhook lifecycle events** for wiring VoucherVault into anything

## 🤖 AI-Assisted Scanning

Upload a photo or screenshot of a card and let "Scan with AI" pre-fill
the form — a real barcode decode plus an AI text read in one pass, three
interchangeable backends (Claude, OpenAI, or fully free/local Tesseract),
duplicate-code and duplicate-photo detection, and confidence warnings for
an easily misread character.

## 🗂️ Organization

**Wallets** (named folders), colour-coded **Tags** with a clickable
filter, free-text **Notes**, **shared wallets** for multi-user
collaboration, **archiving**, **bulk actions**, a remembered
**gift-card balance-check link** per merchant, a "Next Up" widget
that surfaces a queue of your soonest-expiring items ready to scan, and
an "Active Today" widget built for a daily round-trip commute ticket -
shows the outward leg in the morning, switches to the return leg after
your cutoff time. New items can also **auto-file into a wallet by
issuer** (e.g. all "National Rail" tickets straight into "Train
Tickets").

## 🔔 Notifications

A rules-based engine on top of the original Apprise check — **ntfy**,
generic **webhooks**, and native **Web Push** — with per-item thresholds,
an optional **daily digest** to batch a busy rule into one summary
instead of one push per event, and a full delivery log, firing on the
whole item lifecycle, not just expiry.

## 🍏🟢 Digital Wallet Passes

**Apple Wallet** export and import, and **Google Wallet** export — the
item page shows the right button automatically for your device, and a
Google Wallet pass keeps updating live as the item changes rather than
freezing at the moment it was issued.

## 🔐 Security

**Login brute-force lockout** — locks an account out after repeated
failed attempts, by username rather than IP so one attacker on a shared
network can't lock out everyone behind it.

## 📤 Sharing & Public Links

Native OS/browser sharing, a no-login-required **public link** with view
tracking and an optional access PIN, and real merchant brand logos in
link previews instead of a generic icon.

## 📊 Analytics, Import & Backup

An analytics dashboard, Catima/CSV/JSON import and export, a full-fidelity
**zip backup** with rotating nightly snapshots, and a subscribe-able
**.ics calendar feed** of expiry dates.

## ⚙️ Admin

An in-app **Site Settings** page for every app-level setting — no
Portainer env var editing needed — plus an update-available banner with a
one-click redeploy button.

---

See [`FORK_CHANGES.md`](https://github.com/gregbtm/VoucherVault/blob/main/FORK_CHANGES.md)
for the technical changelog with commit links, and the README's
[Environment Variables](https://github.com/gregbtm/VoucherVault#-environment-variables)
table for every setting mentioned above.

---
<sub>This page is generated from [`wiki/`](https://github.com/gregbtm/VoucherVault/tree/main/wiki)
in the main repo and kept in sync automatically. Edits made directly on
this wiki will be overwritten on the next sync — open a pull request
against `wiki/` instead.</sub>
