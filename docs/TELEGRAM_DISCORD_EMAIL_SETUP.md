# Telegram, Discord & Email Notifications

In addition to ntfy, web push, Apprise, and webhooks, VoucherVault Plus+ can send expiry alerts directly to Telegram, a Discord channel, or any email address via SMTP.

## Telegram

### 1. Create a bot

1. Open Telegram and start a chat with **@BotFather**.
2. Send `/newbot` and follow the prompts to choose a name and username.
3. BotFather replies with a **bot token** — copy it (it looks like `123456789:ABCdef...`).

### 2. Find your chat ID

- For a **personal chat**: send a message to your new bot, then visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser. The `chat.id` field in the JSON response is your chat ID.
- For a **group or channel**: add your bot to the group/channel, send a message, then check `getUpdates` for the group's chat ID (groups have a negative ID like `-100123456789`).

### 3. Add a notification rule

In **Notifications → Rules**, create a new rule and set:

| Config key | Value |
|---|---|
| `bot_token` | The bot token from BotFather |
| `chat_id` | Your personal or group chat ID |
| `parse_mode` | `HTML` to make the title **bold** (optional) |

---

## Discord

### 1. Create a webhook

1. In Discord, open the channel you want notifications in.
2. Go to **Edit Channel → Integrations → Webhooks → New Webhook**.
3. Give it a name (e.g. VoucherVault) and click **Copy Webhook URL**.

### 2. Add a notification rule

In **Notifications → Rules**, create a new rule and set:

| Config key | Value |
|---|---|
| `webhook_url` | The full webhook URL from Discord |
| `username` | Display name for the bot (default: `VoucherVault`) |
| `avatar_url` | Optional avatar image URL |

Notifications arrive as rich embeds with the item name, type, value, and expiry date.

---

## Email (SMTP)

VoucherVault can send notifications via any SMTP server — Gmail, Outlook, Fastmail, a self-hosted mail server, etc.

### Gmail example

1. Enable **2-Step Verification** on your Google account.
2. Go to **Google Account → Security → App passwords** and generate a password for "Mail".
3. Use `smtp.gmail.com`, port `587`, with STARTTLS enabled.

### Add a notification rule

In **Notifications → Rules**, create a new rule and set:

| Config key | Value |
|---|---|
| `smtp_host` | SMTP server (e.g. `smtp.gmail.com`) |
| `smtp_port` | Port — `587` for STARTTLS, `465` for SSL |
| `smtp_user` | Your email address or SMTP login |
| `smtp_password` | Your password or app password |
| `use_tls` | `true` for STARTTLS (port 587) |
| `use_ssl` | `true` for direct SSL (port 465); set `use_tls` to `false` |
| `from_address` | Sender address (defaults to `smtp_user`) |
| `to_addresses` | Comma-separated recipient addresses |

### Outlook / Microsoft 365

Use `smtp.office365.com`, port `587`, `use_tls: true`. Use your full email address as `smtp_user`.

### Self-hosted (Postfix, Mailcow, etc.)

Use your server's hostname or IP, and whichever port and authentication settings your server is configured for.

---

## Troubleshooting

- **No messages arriving** — check the Notification Log (`Notifications → Log`) for the error detail.
- **Telegram 401 Unauthorized** — the bot token is wrong or has been revoked by BotFather.
- **Telegram 400 Bad Request: chat not found** — the bot hasn't sent a message to the chat yet, or the chat ID is wrong. For groups, make sure the bot is a member.
- **Discord 404** — the webhook URL may have been deleted from the Discord channel settings.
- **Email authentication failure** — for Gmail, check that an App Password is being used (not the account password directly) and that 2-Step Verification is enabled.
- **SMTP timeout** — the mail server may be blocking outbound port 587/465. Check firewall rules on the machine running VoucherVault.
