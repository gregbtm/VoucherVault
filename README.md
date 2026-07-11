<div align="center" width="100%">
    <h1>VoucherVault</h1>
    <img width="150px" src="myapp/static/assets/img/logo.svg">
    <p>Django web application to store and manage vouchers, coupons, loyalty and gift cards digitally</p><p>
    <a target="_blank" href="https://github.com/l4rm4nd"><img src="https://img.shields.io/badge/maintainer-LRVT-orange" /></a>
    <a target="_blank" href="https://GitHub.com/l4rm4nd/VoucherVault/graphs/contributors/"><img src="https://img.shields.io/github/contributors/l4rm4nd/VoucherVault.svg" /></a>
    <a target="_blank" href="https://github.com/PyCQA/bandit"><img src="https://img.shields.io/badge/security-bandit-yellow.svg"/></a><br>
    <a target="_blank" href="https://GitHub.com/l4rm4nd/VoucherVault/commits/"><img src="https://img.shields.io/github/last-commit/l4rm4nd/VoucherVault.svg" /></a>
    <a target="_blank" href="https://GitHub.com/l4rm4nd/VoucherVault/issues/"><img src="https://img.shields.io/github/issues/l4rm4nd/VoucherVault.svg" /></a>
    <a target="_blank" href="https://github.com/l4rm4nd/VoucherVault/issues?q=is%3Aissue+is%3Aclosed"><img src="https://img.shields.io/github/issues-closed/l4rm4nd/VoucherVault.svg" /></a><br>
        <a target="_blank" href="https://github.com/l4rm4nd/VoucherVault/stargazers"><img src="https://img.shields.io/github/stars/l4rm4nd/VoucherVault.svg?style=social&label=Star" /></a>
    <a target="_blank" href="https://github.com/l4rm4nd/VoucherVault/network/members"><img src="https://img.shields.io/github/forks/l4rm4nd/VoucherVault.svg?style=social&label=Fork" /></a>
    <a target="_blank" href="https://github.com/l4rm4nd/VoucherVault/watchers"><img src="https://img.shields.io/github/watchers/l4rm4nd/VoucherVault.svg?style=social&label=Watch" /></a><br>
    <a target="_blank" href="https://hub.docker.com/r/l4rm4nd/vouchervault"><img src="https://badgen.net/badge/icon/l4rm4nd%2Fvouchervault:latest?icon=docker&label" /></a><br><p>
    <a href="https://www.buymeacoffee.com/LRVT" target="_blank"><img src="https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png" alt="Buy Me A Coffee" style="height: 41px !important;width: 174px !important;box-shadow: 0px 3px 2px 0px rgba(190, 190, 190, 0.5) !important;-webkit-box-shadow: 0px 3px 2px 0px rgba(190, 190, 190, 0.5) !important;" ></a>
</div>

> [!NOTE]
> This is [gregbtm](https://github.com/gregbtm)'s fork of the upstream
> [l4rm4nd/VoucherVault](https://github.com/l4rm4nd/VoucherVault) project.
> A dozen-plus rounds of additive features have been layered on top without
> touching upstream's own code: a full **REST API**, **Wallets/Tags/Notes**,
> a rules-based **notification engine** (ntfy, webhook, Apprise, and native
> **Web Push**), bulk **Import/Export**, an **Analytics dashboard**,
> **auto-fetched merchant logos**, an AI-assisted **"Scan with AI"** photo
> capture, **Apple Wallet and Google Wallet import/export**, **document
> attachments**, **shared (multi-user) wallets**, **native OS/browser
> sharing**, a **clickable tag filter** on the Inventory page, GBP as the
> default currency, and a handful of Catima-parity touches (card numbers,
> archiving, screen wake lock, and a full-fidelity backup format).
> See [`FORK_CHANGES.md`](FORK_CHANGES.md) for the full changelog, the
> [Wiki](https://github.com/gregbtm/VoucherVault/wiki) for feature-by-feature guides,
> [`docs/UPGRADE.md`](docs/UPGRADE.md) if you're already running the
> upstream Docker image and want to switch to this fork,
> [`docs/GOOGLE_WALLET_SETUP.md`](docs/GOOGLE_WALLET_SETUP.md) for a
> step-by-step Google Wallet setup guide, and
> [`docs/BACKUP_RESTORE.md`](docs/BACKUP_RESTORE.md) for how the nightly
> scheduled backups work and how to restore one, and
> [`docs/N8N_SETUP.md`](docs/N8N_SETUP.md) for connecting n8n to the
> existing REST API with zero custom code.

## ⭐ Features

- User-friendly, mobile-optimized web portal with PWA support
- Manual offline mode with 48h caching supported
- Light and dark theme support
- Integration of vouchers, coupons, gift cards and loyalty cards
- Transaction history tracking (gift cards only)
- Item-specific file uploads (images and PDFs)
- Item sharing between users
- Display of redeem codes as QR codes or barcodes (many types supported)
- Client-side redeem code scanning (1D/2D) during item creation with automatic type detection using camera or file upload
- Expiry notifications via Apprise
- Multi-user support
- Multi-language support (English, German, French, Italian)
- Single Sign-On (SSO) via OIDC
- Database compatibility with SQLite3 and PostgreSQL
- Multi-currency support via free fixer.io API

### 🚀 New in this fork

- 🔌 **Full REST API** — token-authenticated CRUD for every item, wallet, tag and transaction, plus interactive Swagger/OpenAPI docs at `/api/v1/docs/`. Build your own Home Assistant dashboards, scripts, or integrations against it.
- 🗂️ **Wallets, Tags & Notes** — group items into named wallets ("Travel", "Groceries"), label them with colour-coded tags, and jot a free-text note on any item.
- 🔔 **Rules-based notification engine** — ntfy, generic webhooks, Apprise, and native browser **Web Push**, each configurable per item with its own expiry threshold and a full delivery log so you can see exactly what fired and when. Beyond expiry warnings, rules can also fire on an item being created, marked used, archived, shared, or having a transaction recorded against it — handy for wiring VoucherVault into n8n, Home Assistant, or any other webhook-driven automation.
- 📲 **Web Push notifications** — real browser/OS push alerts for expiring items straight from the installed PWA, no third-party relay required beyond the browser vendor's own push service (opt-in, requires VAPID keys — generate a pair with one command).
- 📥 **Import & Export** — bulk-import your existing vault from a Catima CSV export or this app's own CSV/JSON, and export everything back out for backups or migration, processed in the background with per-row error reporting.
- 📊 **Analytics dashboard** — KPI tiles, an expiry calendar heatmap, and a live "value at risk" figure so nothing quietly expires unnoticed.
- 🏷️ **Auto-fetched merchant logos** — item cards get real brand logos automatically (fetched and cached in the background), so page loads never wait on a network call.
- 🤖 **AI-assisted "Scan with AI"** — snap a photo of a physical voucher, coupon, or gift card and let Claude's vision model (or a fully local, free Tesseract OCR backend) pre-fill the redeem code, merchant, and expiry date for you.
- 🍏 **Apple Wallet import & export** — download a signed `.pkpass` for any item and add it straight to Apple Wallet (opt-in, requires your own Apple Developer certificate), or go the other way and pre-fill a new item by uploading an existing `.pkpass`.
- 🟢 **Google Wallet export** — a one-tap "Add to Google Wallet" link, set up once by whoever runs the container (opt-in, requires your own free Google Wallet API issuer account — see the [step-by-step setup guide](docs/GOOGLE_WALLET_SETUP.md)). The item detail page shows the Apple or Google Wallet button automatically depending on whether you're on an Apple device, Android, or a Chromium desktop browser — never both, never neither if either is configured.
- 📎 **Document attachments** — attach receipts and proof-of-purchase files to any item, upload/view/delete right from the item detail page.
- 🤝 **Shared (multi-user) wallets** — invite another user by username to collaborate on a wallet; they get full read/write on every item inside it, no admin access required.
- 📤 **Native OS/browser sharing** — a "Share via..." button on every item hands it off to your device's native share sheet (Messages, Mail, AirDrop, etc.), with a clipboard-copy fallback on desktop.
- 🏷️ **Card numbers, archiving & screen wake lock** — a printed member number can differ from the barcode's encoded value, items can be archived out of the default view without deleting them, and the screen stays on while a barcode is shown to a cashier.
- 🗜️ **Full Backup (with files)** — a `.zip` export/import that bundles every item's attached files and documents alongside the data, for a true full-fidelity backup/restore.
- 🔍 **Clickable tag filter on Inventory** — every tag you've created shows as a chip above the item grid with a live item count; click one or more to filter (items matching *any* selected tag show), combinable with the existing status/type/wallet filters.
- 💷 **GBP as the default currency** — new items and user preferences default to GBP instead of EUR; a one-time migration relabelled every pre-existing item and saved preference from EUR to GBP as well (a relabel only, not a currency conversion — amounts are untouched).
- 🤖 **MCP server** — an optional standalone service exposing VoucherVault as tools for Claude Desktop, Claude Code, and other MCP clients (search items, check what's expiring, log a gift-card spend, create an item — all through your existing API token). Runs as its own container, off by default; see the [setup guide](docs/MCP_SERVER_SETUP.md).
- 🔗 **Gift card balance-check link** — no gift card provider exposes a public API for balance/validity checks, so this is a bookmarked link you (or a teammate) provide once per merchant; it's remembered and auto-suggested on future gift cards from the same issuer, with a one-tap "Check Balance" button on the item page.

## 📷 Screenshots

<details>
<img src="screenshots/dashboard.png">
<img src="screenshots/items.png">
<img src="screenshots/item-details.png">
</details>

## 🐳 Usage

[READ THE WIKI](https://github.com/l4rm4nd/VoucherVault/wiki/01-%E2%80%90-Installation) - [UNRAID SUPPORTED](https://github.com/l4rm4nd/VoucherVault/wiki/01-%E2%80%90-Installation#unraid-installation)

````
# create volume dir for persistence
mkdir -p ./volume-data/database

# adjust volume ownership to www-data
sudo chown -R 33:33 volume-data/*

# spawn the container stack
docker compose -f docker/docker-compose-sqlite.yml up -d
````

Once the container is up and running, you can access the web portal at http://127.0.0.1:8000. 

The default username is `admin`. The default password is auto-generated. You can obtain the auto-generated password via the Docker container logs:

````
docker compose -f docker/docker-compose-sqlite.yml logs -f
````

> [!WARNING]
> The container runs as low-privileged `www-data` user with UID/GID `33`. So you have to adjust the permissions for the persistent database bind mount volume. A command like `sudo chown -R 33:33 <path-to-volume-data-dir>` should work. Afterwards, please restart the container.

> [!TIP]
> This repository follows the Conventional Commits standard. Therefore, you will find `patch`, `minor` and `major` release version tags on DockerHub.
> No one stops you from using the `latest` image tag but I recommend pinning a minor version series tag such as `1.29.x`.
>
> This is safer for automatic upgrades and you still get recent patches as well as bug fixes.

## 🌍 Environment Variables

The docker container takes various environment variables:

| Variable                         | Description                                                                                                     | Default                    | Optional/Mandatory  |
|----------------------------------|-----------------------------------------------------------------------------------------------------------------|----------------------------|---------------------|
| `DOMAIN`                         | Your Fully Qualified Domain Name (FQDN) or IP address. Used to define `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` for the Django framework. May define multiple ones by using a comma as delimiter. | `localhost` | Mandatory           |
| `SECURE_COOKIES`                 | Set to `True` if you use a reverse proxy with TLS. Enables the `secure` cookie flag and `HSTS` HTTP response header. | `False`               | Optional            |
| `SESSION_EXPIRE_AT_BROWSER_CLOSE`| Set to `False` if you want to keep sessions valid after browser close.                                          | `True`                     | Optional            |
| `SESSION_COOKIE_AGE`             | Define the maximum cookie age in minutes.                                                                       | `30`                       | Optional            |
| `EXPIRY_THRESHOLD_DAYS`          | Defines the days prior item expiry when an Apprise expiry notification should be sent out.                      | `30`                       | Optional            |
| `EXPIRY_LAST_NOTIFICATION_DAYS`          | Defines the days prior item expiry when another final Apprise expiry notification should be sent out.                      | `7`                       | Optional            |
| `TZ`                             | Defines the `TIME_ZONE` variable in Django's settings.py.                                                       | `Europe/Berlin`            | Optional            |
| `SECRET_KEY`                     | Defines a fixed secret key for the Django framework. If missing, a secure secret is auto-generated on the server-side each time the container starts. | `<auto-generated>`         | Optional            |
| `PORT`                           | Defines a custom port. Used to set `CSRF_TRUSTED_ORIGINS` in conjunction with the `DOMAIN` environment variable for the Django framework. Only necessary, if VoucherVault is operated on a different port than `8000`, `80` or `443`. | `8000`                     | Optional            |
| `REDIS_URL`                      | Defines the Redis URL to use for Django-Celery-Beat task processing.                                            | `redis://redis:6379/0`     | Optional            |
| `CSP_FRAME_ANCESTORS`            | Comma-separated list of allowed sources for the CSP `frame-ancestors` directive.                                | `'none'`                   | Optional            |
| `OIDC_ENABLED`                   | Set to `True` to enable OIDC authentication.                                                                    | `False`                    | Optional            |
| `OIDC_AUTOLOGIN`                 | Set to `True` if you want to automatically trigger OIDC flow on login page                                      | `False`                    | Optional            |
| `OIDC_CREATE_USER`               | Set to `True` to allow the creation of new users through OIDC.                                                  | `True`                     | Optional            |
| `OIDC_RP_SIGN_ALGO`              | The signing algorithm used by the OIDC provider (e.g., RS256, HS256).                                           | `HS256`                    | Optional            |
| `OIDC_OP_JWKS_ENDPOINT`          | URL of the JWKS endpoint for the OIDC provider. Mandatory if `RS256` signing algo is used.                      | `None`                     | Optional            |
| `OIDC_RP_CLIENT_ID`              | Client ID for your OIDC RP.                                                                                     | `None`                     | Optional            |
| `OIDC_RP_CLIENT_SECRET`          | Client secret for your OIDC RP.                                                                                 | `None`                     | Optional            |
| `OIDC_OP_AUTHORIZATION_ENDPOINT` | Authorization endpoint URL of the OIDC provider.                                                                | `None`                     | Optional            |
| `OIDC_OP_TOKEN_ENDPOINT`         | Token endpoint URL of the OIDC provider.                                                                        | `None`                     | Optional            |
| `OIDC_OP_USER_ENDPOINT`          | User info endpoint URL of the OIDC provider.                                                                    | `None`                     | Optional            |
| `DB_ENGINE`                      | Database engine to use (e.g., `postgres` for PostgreSQL or `sqlite3` for SQLite3).                              | `sqlite3`                  | Optional            |
| `POSTGRES_HOST`                  | Hostname for the PostgreSQL database.                                                                           | `db`                       | Optional            |
| `POSTGRES_PORT`                  | Port number for the PostgreSQL database.                                                                        | `5432`                     | Optional            |
| `POSTGRES_USER`                  | PostgreSQL database user.                                                                                       | `vouchervault`             | Optional            |
| `POSTGRES_PASSWORD`              | PostgreSQL database password.                                                                                   | `vouchervault`             | Optional            |
| `POSTGRES_DB`                    | PostgreSQL database name.                                                                                       | `vouchervault`             | Optional            |
| `CELERY_WORKER_CONCURRENCY`           | Celery worker concurrency.                                                                                 | `1`                        | Optional            |
| `CELERY_WORKER_PREFETCH_MULTIPLIER`   | Celery worker prefetch multiplier.                                                                         | `1`                        | Optional            |
| `DEBUG`                           | Enable HTTP debug logging.                                                                                     | `False`                    | Optional            |
| `NTFY_DEFAULT_SERVER`             | Default ntfy server pre-filled when a user creates a new ntfy notification rule.                               | `https://ntfy.sh`          | Optional            |
| `MERCHANT_LOGOS_ENABLED`          | Set to `False` to disable auto-fetching merchant logos on item cards.                                          | `True`                     | Optional            |
| `OCR_BACKEND`                     | Set to `claude`, `openai`, or `tesseract` to enable the "Scan with AI" button on the item form.                | `none`                     | Optional            |
| `ANTHROPIC_API_KEY`               | Required if `OCR_BACKEND=claude`. Get one at [console.anthropic.com](https://console.anthropic.com/).          | `None`                     | Optional            |
| `ANTHROPIC_OCR_MODEL`             | Overrides the Claude model used for OCR extraction.                                                            | `claude-sonnet-5`          | Optional            |
| `OPENAI_API_KEY`                  | Required if `OCR_BACKEND=openai`. Get one at [platform.openai.com](https://platform.openai.com/api-keys).      | `None`                     | Optional            |
| `OPENAI_OCR_MODEL`                | Overrides the OpenAI model used for OCR extraction.                                                            | `gpt-4o-mini`              | Optional            |
| `SCHEDULED_BACKUP_ENABLED`        | Set to `False` to disable the nightly local backup task. See [`docs/BACKUP_RESTORE.md`](docs/BACKUP_RESTORE.md). | `True`                     | Optional            |
| `BACKUP_RETENTION_COUNT`          | How many backups to keep per user before rotating out the oldest.                                              | `7`                        | Optional            |
| `PKPASS_CERT_PATH`                | Path to your Apple Pass Type ID certificate (`.p12`). Enables Apple Wallet export when set.                    | `None`                     | Optional            |
| `PKPASS_CERT_PASSWORD`            | Password for `PKPASS_CERT_PATH`, if any.                                                                       | `None`                     | Optional            |
| `PKPASS_WWDR_CERT_PATH`           | Path to Apple's WWDR intermediate certificate. Required if `PKPASS_CERT_PATH` is set.                          | `None`                     | Optional            |
| `PKPASS_TEAM_ID`                  | Your Apple Developer Team ID. Required if `PKPASS_CERT_PATH` is set.                                           | `None`                     | Optional            |
| `PKPASS_PASS_TYPE_ID`             | Your registered Pass Type ID, e.g. `pass.com.example.vouchervault`. Required if `PKPASS_CERT_PATH` is set.     | `None`                     | Optional            |
| `PKPASS_ORGANIZATION_NAME`        | Organization name shown on the generated pass.                                                                 | `VoucherVault Plus+`       | Optional            |
| `GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH` | Path to your Google Wallet API service account JSON key. Enables Google Wallet export when set along with the issuer ID below. | `None` | Optional |
| `GOOGLE_WALLET_ISSUER_ID`         | Your Google Wallet API issuer ID, from the [Google Wallet Business Console](https://pay.google.com/business/console). Required if `GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH` is set. | `None` | Optional |
| `GOOGLE_WALLET_CLASS_ID`          | Optional override for the generic pass class ID.                                                               | `<issuer id>.vouchervault_generic` | Optional    |
| `WEBPUSH_VAPID_PUBLIC_KEY`        | VAPID public key. Enables the "Web Push" notification backend when set along with the private key below. Generate a pair with `python manage.py generate_vapid_keys`. | `None` | Optional |
| `WEBPUSH_VAPID_PRIVATE_KEY`       | VAPID private key. See above.                                                                                  | `None`                     | Optional            |
| `WEBPUSH_VAPID_CLAIMS_EMAIL`      | Contact email sent to push services as the VAPID claim.                                                        | `mailto:admin@example.com` | Optional            |
| `UPDATE_CHECK_ENABLED`            | Set to `False` to disable the periodic GitHub Releases check (footer version + update banner for superusers).  | `True`                     | Optional            |
| `UPDATE_CHECK_REPO`               | `owner/repo` to check for releases. Only change this if you're running a fork of this fork.                    | `gregbtm/VoucherVault`     | Optional            |
| `VERSION`                         | Overrides the version shown in the footer. Normally unset - the `VERSION` file baked into the image is the source of truth. | `<VERSION file>` | Optional |

You can find detailed instructions on how to setup OIDC SSO in the [wiki](https://github.com/l4rm4nd/VoucherVault/wiki/02-%E2%80%90-Authentication#oidc-authentication).

For the `GOOGLE_WALLET_*` variables, see the full walkthrough in
[`docs/GOOGLE_WALLET_SETUP.md`](docs/GOOGLE_WALLET_SETUP.md) — it's a
one-time setup you do as the operator, not something each user of your
instance needs to do themselves.

## 🔔 Notifications

Notifications are handled by [Apprise](https://github.com/caronc/apprise). May read the [wiki](https://github.com/l4rm4nd/VoucherVault/wiki/03-%E2%80%90-Notifications).

You can define custom Apprise URLs in the user profile settings. The input form takes a single or a comma-separated list of multiple Apprise URLs.

The interval, how often items are checked against a potential expiry, is pre-defined (daily at 9AM) in the Django admin area. Here, we are utilizing Django-Celery-Beat + a Redis instance for periodic task execution.

An item will trigger an expiry notification if the expiry date is within the number of days defined by the environment variable `EXPIRY_THRESHOLD_DAYS`. By default, this threshold is set to 30 days. Additionally, a final reminder is sent out another time if the item expires within the next 7 days.

## 🔐 Multi-User Setup

VoucherVault is initialized with a default superuser account named `admin` and a secure auto-generated password. 

This administrative account has full privileges to the Django admin panel, located at `/admin`. 

Therefore, all database model entries can be read and modified by this user. Additionally, new user accounts and groups can be freely created too. 

Finally, Single-Sign-On (SSO) via OIDC is supported. Check out the environment variables above as well as the [wiki](https://github.com/l4rm4nd/VoucherVault/wiki/02-%E2%80%90-Authentication#oidc-authentication).

## 💾 Backups

All application data is stored within a Docker bind mount volume. 

This volume is defined in the example Docker Compose files given. The default location is defined as `./volume-data/database`.

Therefore, by backing up this bind mount volume, all your application data is saved.

> [!WARNING]
> Read the official [SQLite3 documentation](https://sqlite.org/backup.html) or [PostgreSQL documentation](https://www.postgresql.org/docs/current/backup.html) regarding backups.

## 💛 About This Fork & Support

All nine feature phases in this fork ([`FORK_CHANGES.md`](FORK_CHANGES.md)) were
implemented with [Claude](https://claude.com/claude-code) — but every plan,
feature scope, and priority behind them was mine, worked out phase by phase
before a line of code was written. If there's something you'd like to see
added — an integration, an export format, another notification backend —
open an issue and I'm happy to help scope and build it out.

If this fork has been useful to you, tips are always appreciated:

<!-- TODO(gregbtm): replace with your PayPal.me link -->
[![Donate via PayPal](https://img.shields.io/badge/Donate-PayPal-00457C.svg?logo=paypal&logoColor=white)](https://paypal.me/YOUR_PAYPAL_HERE)

And don't forget the original upstream maintainer, whose project this fork is built on:

<a href="https://www.buymeacoffee.com/LRVT" target="_blank"><img src="https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png" alt="Buy Me A Coffee" style="height: 41px !important;width: 174px !important;" ></a>

## 🤖 Repo Statistics
![Alt](https://repobeats.axiom.co/api/embed/a8e369506f50bb08a3295b495639d42f7e20d1ba.svg "Repobeats analytics image")

