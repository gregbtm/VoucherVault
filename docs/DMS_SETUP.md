# Document Management System (DMS) Integration

VoucherVault can push documents to, and pull documents from, three self-hosted
document management systems: **Paperless-ngx**, **Docspell**, and **PaperMerge**.

This is a **per-user** feature — each person who logs into your instance
configures their own DMS connection(s) under **Document Archive**.

---

## What it does

| Feature | Description |
|---|---|
| **Push** | Upload a document attached to any VoucherVault item directly into your DMS, tagged and titled automatically |
| **Pull** | Browse your DMS from inside VoucherVault and import documents as new items |
| **Auto-push** | Whenever you attach a file to an item, it is silently queued and uploaded to your DMS in the background |
| **Auto-pull** | On an hourly schedule, VoucherVault checks your DMS for newly-tagged documents and imports them automatically |
| **Test connection** | Verify credentials and connectivity from the provider settings page at any time |
| **Config polling** | Load available tags and correspondents live from your running DMS to fill filter dropdowns |

---

## Quick-start

1. Open **Document Archive** in the sidebar (or go to `/dms/`).
2. Click **Add Provider**.
3. Choose your DMS type, fill in the URL and credentials, and click **Test Connection** to verify.
4. Click **Save**.
5. Open any item, scroll to the **Archive to DMS** section, and click **Push to DMS**.

---

## Paperless-ngx

### Requirements

- Paperless-ngx **1.7+** (REST API v3 or later)
- An API token for your account

### Creating an API token

1. Log in to your Paperless-ngx instance.
2. Go to **Settings → API Tokens** (or `⟨your-url⟩/api/token/`).
3. Click **Generate**, copy the token.

### Provider settings

| Field | Value |
|---|---|
| **Provider Type** | Paperless-ngx |
| **Base URL** | Your Paperless-ngx URL, e.g. `https://paperless.home` — no trailing slash |
| **API Token** | The token you just generated |

### Push behaviour

Documents are uploaded via `POST /api/documents/post_document/` as multipart
data. The document's filename becomes the title in Paperless-ngx. If you have
set a **Pull Tag Filter**, that tag is applied to every pushed document so you
can find them again from Paperless-ngx.

### Pull behaviour

VoucherVault calls `GET /api/documents/` (with pagination). If you set a
**Pull Tag Filter** or **Correspondent Filter**, only matching documents are
returned. The **Load from DMS** button beside each filter field fetches the
live list of tags / correspondents from your Paperless-ngx instance and offers
them as autocomplete suggestions.

### Auto-push env var (optional)

None required — push uses the per-provider API token stored in the database.

---

## Docspell

### Requirements

- Docspell **0.36+**
- Your collective name, username, and password
- An Upload Source ID (for push)

### Creating an Upload Source

1. Log in to Docspell.
2. Go to **Profile → Upload Sources** (top-right menu).
3. Click **New Upload Source**, give it any name, and copy the **Source ID**.

### Provider settings

| Field | Value |
|---|---|
| **Provider Type** | Docspell |
| **Base URL** | Your Docspell URL, e.g. `https://docspell.home` |
| **Collective** | Your tenant/workspace name (shown at the top-left of the Docspell UI) |
| **Username** | Your Docspell username |
| **Password** | Your Docspell password |
| **Source ID** | The Upload Source ID from step above |

### How authentication works

VoucherVault calls `POST /api/v1/open/auth/login` with
`account: collective/username` on each request to obtain a short-lived JWT
token, then passes it as `X-Docspell-Auth: <token>`. No token is stored
between sessions.

### Push behaviour

Files are uploaded to the anonymous upload endpoint:
`POST /api/v1/open/upload/item/<sourceId>`. The original filename is preserved.

### Pull behaviour

VoucherVault searches `POST /api/v1/sec/item/search` using the
**Pull Tag Filter** as the query string. Leave it blank to pull all documents
visible to your account.

---

## PaperMerge

### Requirements

- PaperMerge **3.x** (REST API v3)
- An API token, **or** your username and password

### Creating an API token

1. Log in to PaperMerge.
2. Go to **Profile → API Tokens**.
3. Click **Create Token**, give it a name, copy the token.

### Provider settings

| Field | Value |
|---|---|
| **Provider Type** | PaperMerge |
| **Base URL** | Your PaperMerge URL, e.g. `https://papermerge.home` |
| **API Token** | Preferred — paste the token from above |
| **Username / Password** | Alternative — if no token is provided, VoucherVault obtains one automatically via `POST /api/auth/token/` |

### Push behaviour

Documents are uploaded to `POST /api/documents/` with the file content and
original filename. PaperMerge assigns its own internal ID, which is stored in
the sync log.

### Pull behaviour

VoucherVault lists documents via `GET /api/documents/` with pagination. There
is no tag/correspondent filter in PaperMerge 3.x — all accessible documents
are returned and the **Pull Tag Filter** field is ignored for this provider.

---

## Sync options

Both **Auto-push** and **Auto-pull** are off by default and can be enabled
independently per provider.

### Auto-push

When enabled, any file you attach to a VoucherVault item is automatically
queued for upload to this DMS provider. The upload happens asynchronously
in the background (via Celery) so it never blocks saving the item. If the
upload fails, it is retried up to three times with exponential back-off.
A sync log entry is written for every attempt.

Deduplication: if a document has already been successfully pushed to a
provider, it will not be pushed again on re-save.

### Auto-pull

When enabled, VoucherVault checks this provider every hour for new documents.
Any document that has not already been imported (tracked by its DMS document
ID in the sync log) is downloaded and created as a new VoucherVault item.
The item name is taken from the document title.

The **Pull Tag Filter** and **Correspondent Filter** fields let you restrict
which documents are pulled. For Paperless-ngx these are matched against the
tag/correspondent name. For Docspell the Pull Tag Filter is used as a full
query string.

The hourly task is called **DMS Auto Pull** in Django Admin → Periodic Tasks.

---

## Manual push and pull

### Pushing a document

1. Open any item and scroll to the **Archive to DMS** card.
2. Choose a provider from the dropdown and click **Push to DMS**.
3. A success message shows the DMS document ID assigned by the provider.

Individual file attachments can also be pushed from the file action menu on
each attachment row.

### Pulling a document

1. Open **Document Archive** → your provider → click **Browse DMS**.
2. A modal opens showing the first page of your DMS library. Use the search
   box or page navigation to find the document you want.
3. Click **Import** next to a document. VoucherVault downloads the file,
   creates a new item named after the document, and attaches the file to it.
4. You are redirected to the new item.

---

## Sync logs

All push and pull operations are recorded in **Document Archive → Sync Logs**
(`/dms/logs/`). Each entry shows:

- Direction (Push / Pull)
- Status (OK / Error)
- Provider name
- Document title
- Error detail (if any)
- Timestamp

Filter the log by provider, direction, or status using the dropdowns at the
top of the page.

---

## Troubleshooting

### "Connection refused" on Test Connection

Check that the **Base URL** is reachable from the VoucherVault container.
If you're running both services in Docker, use the container name as the
hostname rather than `localhost`, e.g. `http://paperless-webserver:8000`.

### 401 / 403 errors

For Paperless-ngx: verify the API token is for the correct user and has not
been revoked. Tokens appear in **Settings → API Tokens** in Paperless-ngx.

For Docspell: double-check the collective name — it is case-sensitive and
is the workspace name shown at the top-left of the UI, not your login email.

For PaperMerge: ensure the API token has not expired. Alternatively,
supply username/password and leave the token blank.

### "No items found in DMS" when loading tags

This uses the live API call to your DMS. If test connection passes but tag
loading fails, check that your DMS user has permission to list tags/labels.

### Auto-push not firing

1. Check that Celery workers are running (`docker logs vouchervault-celery`).
2. Open Django Admin → Periodic Tasks and confirm **DMS Auto Pull** is enabled
   and the last run time is recent.
3. Check the Sync Logs page for error entries with detail.

### Duplicate documents in DMS

VoucherVault deduplicates pushes by checking `DMSSyncLog` for a prior
successful push of the same document to the same provider. If you see
duplicates it means the log record was deleted or the document was pushed
from a second provider.

---

## Admin reference

### Database tables

| Table | Purpose |
|---|---|
| `dms_dmsprovider` | Per-user DMS connection configuration |
| `dms_dmssynclog` | Record of every push/pull operation |

Both tables are visible in Django Admin under the **Dms integration** section.

### Celery tasks

| Task | Schedule | Purpose |
|---|---|---|
| `dms.tasks.dms_scheduled_pull_all` | Hourly (`:15`) | Fan-out pull for all enabled auto-pull providers across all users |
| `dms.tasks.push_document_to_dms` | On demand (queued on file save) | Single-document push with retry |
| `dms.tasks.auto_pull_from_dms` | Called by the above | Pull new documents from one provider |

### Environment variables

No environment variables are required for DMS integration. All configuration
is stored per-user in the database. The Celery worker must be running for
background push/pull to work.
