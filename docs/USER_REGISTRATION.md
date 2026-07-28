# User Registration

Controls whether the registration page accepts new signups. This is independent of SSO — existing accounts, API tokens, and OIDC/PocketID logins are unaffected either way (see [OIDC / PocketID setup](OIDC_SETUP.md) for SSO-specific registration behavior).

## Allow new user registrations

When ticked, anyone who reaches the registration page can create an account. When unticked, the instance becomes **invite-only**: the registration page still exists but rejects signups without a valid invite link.

Turning this off doesn't affect anyone already registered, and doesn't touch API tokens or active sessions.

## Invite links

With registration closed, new users need an invite link to create an account:

- **Invite link expiry (days)** — how long a generated invite stays valid. `0` = never expires.
- **Manage invite links** — opens the invite management page, where you generate a one-time signup link to send to whoever you're inviting. Each link can be revoked before it's used.

## Typical setups

- **Public instance**: leave registration open.
- **Household/private instance**: turn registration off, invite family/friends individually via invite links.
- **SSO-only instance**: turn registration off here *and* leave **Create account on first OIDC login** off in the OIDC section — accounts are then only created by an admin or via PocketID provisioning (see the PocketID Admin API section).
