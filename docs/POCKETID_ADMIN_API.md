# PocketID Admin API

Optional, on top of the [OIDC / PocketID setup](OIDC_SETUP.md) that lets *existing* PocketID users log in. This section instead lets VoucherVault *create* new PocketID accounts and generate one-click onboarding links — useful for inviting someone who doesn't have a PocketID account yet.

## What it unlocks

Once configured, **Manage Invites** gains a **"Provision & Invite via PocketID"** form. Fill in the new user's details and VoucherVault:

1. Creates a PocketID account for them via the Admin API.
2. Generates a single link that sets up their passkey in PocketID.
3. Drops them straight into the VoucherVault invite-acceptance flow afterward.

The person you're inviting only has to click one link and set up a passkey — no separate PocketID account creation step, no password to invent.

## Setup

1. In PocketID, go to **Admin → API Keys** and generate a new key.
2. **PocketID base URL** — your PocketID instance's URL, no trailing slash (e.g. `https://id.example.com`).
3. **Admin API key** — paste the key from step 1. Stored encrypted; shown as "Currently set" once saved, never redisplayed.
4. **Test connection** — confirms the URL and key work before you rely on it.

## Requirements

This is separate from the base OIDC login setup — you need [OIDC / PocketID Integration](OIDC_SETUP.md) configured first (so logins actually work), *and* this Admin API key configured, before the provisioning form appears on Manage Invites.
