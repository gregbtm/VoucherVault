# OIDC / PocketID Setup

VoucherVault supports OpenID Connect (OIDC) single sign-on. The setup below uses [PocketID](https://github.com/pocket-id/pocket-id) as the provider, but any OIDC-compliant server (Authentik, Authelia, Keycloak, Google, etc.) works.

---

## 1. Enable OIDC in the environment

Set this in your `docker-compose.yml` or Portainer environment variables:

```
OIDC_ENABLED=True
```

This is the only setting that requires a container restart. Everything else is editable live in **Site Settings → OIDC / PocketID Integration**.

---

## 2. Create an OIDC client in PocketID

1. Open PocketID → **Admin → Applications → New Application**.
2. Set the **Redirect URI** to:
   ```
   https://your-vouchervault-domain/oidc/callback/
   ```
3. Set the **Logout URI** to:
   ```
   https://your-vouchervault-domain/
   ```
4. Copy the **Client ID** and **Client Secret** shown after saving.
5. Note the **Discovery URL** — typically:
   ```
   https://your-pocketid-domain/.well-known/openid-configuration
   ```

---

## 3. Configure VoucherVault

In **Site Settings → OIDC / PocketID Integration**:

| Field | Value |
|---|---|
| **Provider display name** | Label shown on the login button, e.g. `PocketID` |
| **Discovery URL** | The `.well-known/openid-configuration` URL from step 2 |
| **Client ID** | Copied from PocketID |
| **Client secret** | Copied from PocketID |
| **Admin group claim** | Name of a PocketID group whose members are promoted to superuser (leave blank to disable) |
| **Create account on first OIDC login** | Tick to auto-provision new accounts; untick to require an invite first |
| **Auto-redirect to OIDC on login page** | Skips the password form and goes straight to PocketID |
| **Require TOTP after OIDC login** | Users who have TOTP configured must still complete the 2FA step even after OIDC authentication succeeds |

---

## 4. Verify the connection

1. Log out and visit the login page.
2. Click the **Sign in with PocketID** (or your provider name) button.
3. Complete authentication in PocketID — you should be redirected back and logged in.
4. Check **Site Settings → Connectivity** for a green OIDC status badge if displayed.

---

## Admin group → superuser mapping

If you set an **Admin group claim**, VoucherVault reads the `groups` claim from the OIDC token on every login and:

- **Adds** the user to Django superusers when the group is present.
- **Removes** superuser status when the group is absent on a subsequent login.

This keeps permissions in sync with PocketID without manual promotion.

---

## Invite links (invite-only mode)

With **Create account on first OIDC login** unchecked and public registration disabled, only users with a valid invite link can register. Admins manage invites at **Settings → Manage Invites**.

Each link is a single-use UUID token with an optional expiry date. Once accepted (or revoked), it cannot be reused.

---

## Unlinking a PocketID identity

Users can disconnect their OIDC identity from **Profile → Security → Connected Identity → Unlink**. After unlinking, password-based login continues to work normally.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "OIDC login failed" on redirect back | Client ID / secret mismatch, or redirect URI doesn't match exactly |
| Users promoted to superuser unexpectedly | **Admin group claim** matches a group the user is in — check PocketID group membership |
| TOTP step skipped after OIDC login | **Require TOTP after OIDC login** is not enabled in Site Settings |
| Login button missing | `OIDC_ENABLED` env var is not set to `True` (requires container restart) |
