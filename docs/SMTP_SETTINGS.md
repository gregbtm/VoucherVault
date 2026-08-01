# Outbound Email (SMTP)

Configures an SMTP server so VoucherVault can send password-reset emails and PocketID invite emails. Leave the host blank to disable outbound email entirely — password resets and email-based invites simply won't be available, everything else in the app is unaffected.

## Fields

| Field | Purpose |
|---|---|
| **SMTP host** | Your provider's SMTP server, e.g. `smtp.gmail.com`, `smtp.sendgrid.net` |
| **Port** | `587` for STARTTLS, `465` for SSL (see the two switches below) |
| **Username** | Usually your full email address or an API-key-style username, depending on the provider |
| **Password** | The account/app password or API key. Shown as "Currently set" once saved — the value itself is never redisplayed |
| **From address** | The `From:` header on sent mail. Defaults to the Username field if left blank |
| **Use STARTTLS** | Tick for port 587 (most providers) |
| **Use SSL** | Tick for port 465 (implicit TLS) |

Only one of STARTTLS/SSL should be ticked, matching the port you've set.

## Testing

- **Test connection** — opens a connection to the SMTP host and authenticates, without sending anything. Confirms host/port/credentials are correct.
- **Send test email** — sends a real message to your own account's email address, so you can confirm delivery end-to-end (including spam-folder placement, DNS/SPF issues, etc. that a connection test alone can't catch).

## Common providers

- **Gmail**: `smtp.gmail.com`, port 587, STARTTLS. Requires an [App Password](https://myaccount.google.com/apppasswords), not your regular account password (Google blocks plain password SMTP login).
- **SendGrid**: `smtp.sendgrid.net`, port 587, STARTTLS, username `apikey`, password = your SendGrid API key.
- **Self-hosted (e.g. Postfix, Mailcow)**: point at your own relay's hostname; TLS settings depend on how it's configured.
