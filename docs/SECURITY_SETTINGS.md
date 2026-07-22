# Security Settings

VoucherVault exposes several hardening controls in **Site Settings → Security Alerts** and through environment variables. This guide covers login spike alerts, API token expiry, and the system-check warnings.

---

## Login spike alerts (ntfy)

When the number of failed login attempts in the last 60 minutes exceeds a threshold, VoucherVault sends a single alert to an ntfy topic.

### Configure

In **Site Settings → Security Alerts**:

| Field | Description |
|---|---|
| **Alert ntfy topic** | The ntfy topic to publish to (e.g. `vv-security-alerts`). Leave blank to disable. |
| **Failure threshold (per hour)** | How many failed logins trigger the alert. Default: 10. |

VoucherVault uses the **Default ntfy Server** configured in the Notifications section. If that field is blank, `https://ntfy.sh` is used.

### Subscribe

To receive alerts on your phone, subscribe to the topic in the ntfy app:

1. Open the ntfy app → **+** → enter the topic name.
2. Alternatively: `https://ntfy.sh/<your-topic>` in a browser.

The alert title is **VoucherVault Security Alert**, priority **high**. It fires once per hourly check when the threshold is exceeded — it does not repeat mid-hour.

---

## API token expiry

By default, REST API tokens never expire. You can enforce a rolling expiry window with:

```
API_TOKEN_EXPIRY_DAYS=90
```

Set this in your `docker-compose.yml` or Portainer environment variables. Tokens older than this value are rejected at authentication time and deleted. Set to `0` (the default) to disable expiry.

### Purging expired tokens manually

A management command is available for housekeeping:

```bash
python manage.py purge_expired_tokens
```

This is safe to run at any time. It is a no-op when `API_TOKEN_EXPIRY_DAYS=0`.

---

## TOTP after OIDC login

When OIDC single sign-on is enabled, users authenticated via OIDC normally bypass the TOTP step (because the OIDC provider is assumed to handle 2FA). If your threat model requires an extra TOTP check inside VoucherVault:

Enable **Require TOTP after OIDC login** in **Site Settings → OIDC / PocketID Integration**.

Users who have TOTP configured will be redirected to the TOTP verification screen after a successful OIDC login. Users without TOTP configured are unaffected.

---

## System check warnings

VoucherVault adds two Django system checks that surface warnings at startup:

### `myapp.W001` — SECRET_KEY not from environment

```
SECRET_KEY is not set in the environment — a random key is generated on each startup,
which invalidates all existing sessions and signed cookies on restart.
```

**Fix:** Set `SECRET_KEY` in your `docker-compose.yml`:

```yaml
environment:
  - SECRET_KEY=your-long-random-secret-here
```

Generate a suitable value with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(50))"
```

### `myapp.W002` — WebDAV TLS verification disabled

```
WEBDAV_VERIFY_SSL is False — TLS certificates are not verified for DMS connections.
```

**Fix:** Remove `WEBDAV_VERIFY_SSL=False` from your environment, or set it to `True`. Only disable verification in a fully private network with self-signed certificates where you accept the risk.

---

## Login audit log

All login attempts (successful and failed) are recorded in the **Login Audit Log**, visible at **Profile → Security → Login History**. The log captures:

- Username attempted
- IP address
- Success/failure
- Timestamp

The login spike alert task counts from this log. The audit log is also visible to superusers in the Django admin.

---

## Dependency audit (CI)

A weekly GitHub Actions workflow (`dependency-audit.yml`) runs `pip-audit` against `requirements.txt` to scan for known CVEs. It also runs on every push that touches `requirements.txt` and on all pull requests.

To run the audit locally:

```bash
pip install pip-audit
pip-audit -r requirements.txt
```

---

## Content Security Policy

VoucherVault sets a strict CSP via `django-csp`:

- `script-src`: `'self'` + per-request nonces. `unsafe-inline` is removed; all inline scripts carry `nonce="{{ request.csp_nonce }}"`.
- `unsafe-eval` is retained for ECharts (the dashboard charting library).

The CSP headers are visible in browser developer tools → Network → any page response.
