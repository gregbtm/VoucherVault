# Synology NAS Integration

VoucherVault has four complementary integration points with a Synology NAS running DSM 7.x.
None of them are required — each is independently opt-in.

| What | Env var(s) | Status |
|---|---|---|
| [Deploy on Synology](#deploying-on-synology-container-manager) | — | Works out of the box |
| [Log in with Synology SSO](#option-a-synology-sso-server-oidc-login) | `OIDC_ENABLED`, `OIDC_DISCOVERY_URL`, … | Built-in OIDC support |
| [Store files on Synology WebDAV](#option-b-webdav-file-storage) | `USE_WEBDAV_STORAGE`, `WEBDAV_*` | Custom backend |
| [Store files in MinIO on Synology](#option-c-minio-on-synology-s3-compatible) | `USE_S3_STORAGE`, `S3_*` | Built-in S3 support |
| [Volume-mount a Synology share](#option-d-volume-mount-simplest) | — (docker-compose only) | No code changes |
| [Run n8n on Synology](#n8n-on-synology) | — | For the rail-ticket workflow |

---

## Deploying on Synology Container Manager

Synology DSM 7.2+ ships **Container Manager**, which runs Docker Compose stacks.

### 1. Enable Container Manager

DSM → Package Center → search "Container Manager" → Install.

### 2. Upload the compose file

```
ssh admin@your-nas-ip
mkdir -p /volume1/docker/vouchervault
```

Copy `docker/docker-compose-sqlite.yml` (or `-psql.yml` for Postgres) and
`docker/env.example` into that folder.  Rename `env.example` to `.env` and
fill in your values.

### 3. Persistent volumes

The compose file maps two host paths into the container.  For Synology, create
the corresponding folders first:

```
mkdir -p /volume1/docker/vouchervault/database
mkdir -p /volume1/docker/vouchervault/uploads   # item images & attachments
```

Edit the `volumes:` block in the compose file to point to these paths, e.g.:

```yaml
volumes:
  - /volume1/docker/vouchervault/database:/app/database
  - /volume1/docker/vouchervault/uploads:/app/uploads
```

### 4. Start the stack

In Container Manager → Project → Create → Import Compose → select the file.
Or via SSH:

```bash
cd /volume1/docker/vouchervault
docker compose -f docker-compose-sqlite.yml up -d
```

### 5. Reverse proxy

DSM → Control Panel → Login Portal → Advanced → Reverse Proxy.

| Field | Value |
|---|---|
| Reverse Proxy Name | VoucherVault |
| Source Protocol | HTTPS |
| Source Port | 443 (or a custom port) |
| Source Hostname | vv.your-domain.com |
| Destination Protocol | HTTP |
| Destination Hostname | localhost |
| Destination Port | 8000 (or the port your container exposes) |

Also add a custom header:

```
X-Forwarded-Proto: https
```

Then set `SECURE_COOKIES=True` and `DOMAIN=vv.your-domain.com` in `.env`.

---

## Authentication

### Option A: Synology SSO Server (OIDC login)

Lets users log in to VoucherVault with their DSM credentials — the same username
and password they use for the NAS itself.

#### 1. Install SSO Server on your NAS

DSM → Package Center → search "SSO Server" → Install.

#### 2. Create an OIDC application

Open SSO Server → Application → Add.

| Field | Value |
|---|---|
| Application name | VoucherVault |
| Redirect URIs | `https://vv.your-domain.com/oidc/callback/` |
| Grant type | Authorization Code |
| Signing algorithm | RS256 |

Save.  Copy the **Client ID** and **Client Secret** shown after saving.

#### 3. Find your discovery URL

In SSO Server → Settings → General you'll see the **SSO Server URL**.  Append
`/.well-known/openid-configuration` to it:

```
https://your-nas-domain:5001/webman/sso/.well-known/openid-configuration
```

Verify it returns JSON in a browser before continuing.  If your NAS uses a
custom FQDN and port 443 (via the reverse proxy above), it may be:

```
https://sso.your-domain.com/.well-known/openid-configuration
```

#### 4. Configure VoucherVault

Add to `.env`:

```env
OIDC_ENABLED=True
OIDC_PROVIDER_NAME=Synology SSO
OIDC_RP_CLIENT_ID=<client id from step 2>
OIDC_RP_CLIENT_SECRET=<client secret from step 2>
OIDC_DISCOVERY_URL=https://your-nas-domain:5001/webman/sso/.well-known/openid-configuration
OIDC_RP_SIGN_ALGO=RS256
OIDC_CREATE_USER=True
```

Restart the stack.  The login button will now read **"Login with Synology SSO"**
and redirect through DSM.

> **Tip:** Set `OIDC_AUTOLOGIN=True` to skip the login page entirely and jump
> straight to the SSO flow — useful if the VoucherVault instance is for a single
> family or household where everyone has a DSM account.

---

## File Storage

By default, uploaded item images and document attachments live in the
`/app/uploads` directory inside the container, bound to the host via a Docker
volume. For most home users this is fine — the uploads are on the NAS disk via
the bind mount and are backed up by Hyper Backup alongside everything else.

If you want **uploads managed by NAS services** (Synology Drive sync,
multi-NAS replication, Glacier archival, etc.) there are three options.

### Option B: WebDAV file storage

Synology ships a built-in **WebDAV Server** package.  This option stores every
uploaded file directly on the NAS via WebDAV, so it appears in File Station and
participates in any folder-level sync or backup you have configured.

#### 1. Enable WebDAV Server

DSM → Package Center → WebDAV Server → Install (or Enable).

DSM → Control Panel → File Services → WebDAV — confirm it's enabled.  Note the
ports: **5005** (HTTP) and **5006** (HTTPS).

#### 2. Create a dedicated folder

In File Station, create `/homes/vaultuser/vouchervault` (or any path you prefer)
and confirm the DSM user you'll use for the connection has Read/Write permission
on it.

#### 3. Configure VoucherVault

Add to `.env`:

```env
USE_WEBDAV_STORAGE=True
WEBDAV_URL=https://your-nas-domain:5006/homes/vaultuser/vouchervault
WEBDAV_USERNAME=vaultuser
WEBDAV_PASSWORD=your-dsm-password
WEBDAV_VERIFY_SSL=True
```

If your NAS uses a self-signed certificate (common for local-only setups), you
can set `WEBDAV_VERIFY_SSL=False` — but it's better to upload the DSM-generated
CA to the container's trust store, or use a real cert via Let's Encrypt (DSM has
built-in ACME support).

Optionally, if you front WebDAV with a reverse proxy that serves files publicly:

```env
WEBDAV_PUBLIC_URL=https://files.your-domain.com/vouchervault
```

#### 4. How it works

The `myapp/webdav_storage.py` backend translates Django's storage API into
plain HTTP methods (PUT / GET / HEAD / DELETE / MKCOL / PROPFIND) over the
`requests` library (already installed).  No extra packages required.

#### Notes

- Sub-directories are created automatically via MKCOL before the first file is
  written into them.
- File URLs returned to the browser point at `WEBDAV_PUBLIC_URL` (or
  `WEBDAV_URL` if that isn't set), so the NAS must be reachable from the
  browser at that address.
- Django admin's media serving still works because `_open()` fetches file
  content on the fly.

---

### Option C: MinIO on Synology (S3-compatible)

[MinIO](https://min.io) is an open-source S3-compatible object store that runs
well in Docker on a Synology.  It gives you an S3 API on your own hardware —
useful if you already use MinIO for other services, or want S3 SDK compatibility
for future tooling.

#### 1. Run MinIO in Container Manager

Create `/volume1/docker/minio/data` on the NAS, then:

```yaml
# minio-compose.yml
services:
  minio:
    image: quay.io/minio/minio:latest
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: changeme
    ports:
      - "9000:9000"   # S3 API
      - "9001:9001"   # MinIO Console
    volumes:
      - /volume1/docker/minio/data:/data
    restart: unless-stopped
```

Import this as a second Container Manager project.

#### 2. Create a bucket

Open `http://your-nas-ip:9001` → Login → Buckets → Create bucket → name it
`vouchervault`.  If you want files to be publicly readable without signed URLs,
set the bucket Access Policy to **Public**.

Create a service account (MinIO Console → Identity → Service Accounts) and note
the Access Key and Secret Key.

#### 3. Configure VoucherVault

```env
USE_S3_STORAGE=True
S3_BUCKET_NAME=vouchervault
S3_ENDPOINT_URL=http://your-nas-ip:9000
S3_ACCESS_KEY_ID=<your access key>
S3_SECRET_ACCESS_KEY=<your secret key>
S3_REGION_NAME=us-east-1
S3_PUBLIC_BUCKET=True
```

Set `S3_PUBLIC_BUCKET=False` if you left the bucket private — uploads will then
use short-lived signed URLs instead of permanent public links.

> **MinIO vs WebDAV:** MinIO is the better choice if you need signed-URL
> expiry, multipart uploads for large files, or want to use the same bucket for
> other services.  WebDAV is simpler to set up and integrates naturally with
> File Station and Synology Drive.

---

### Option D: Volume mount (simplest)

If you just want uploads on the NAS disk without any protocol plumbing, keep
Django's default `FileSystemStorage` and mount the NAS folder directly:

```yaml
# in your docker-compose yml, under the app service:
volumes:
  - /volume1/docker/vouchervault/database:/app/database
  - /volume1/docker/vouchervault/uploads:/app/uploads   # ← NAS path here
```

Files land on the NAS filesystem and are visible in File Station.  Synology
Drive, Hyper Backup, and snapshot replication all work at the volume level, so
they cover this folder without any extra configuration.

This is the right choice for most home / family deployments.

---

## n8n on Synology

[n8n](https://n8n.io) is an automation platform with a first-party Synology
Docker deployment.  VoucherVault ships a ready-made n8n workflow that auto-imports
rail/travel tickets from email attachments — see `docs/N8N_SETUP.md` and
`docs/RAIL_TICKET_IMPORT_SETUP.md` for the full walkthrough.

### Quick start on Synology

```yaml
# n8n-compose.yml
services:
  n8n:
    image: n8nio/n8n:latest
    ports:
      - "5678:5678"
    environment:
      - N8N_HOST=n8n.your-domain.com
      - N8N_PORT=5678
      - WEBHOOK_URL=https://n8n.your-domain.com/
    volumes:
      - /volume1/docker/n8n:/home/node/.n8n
    restart: unless-stopped
```

Add a reverse-proxy entry pointing `n8n.your-domain.com` → `localhost:5678`.

Once n8n is running, import the workflow from
`docs/n8n-workflows/vouchervault-rail-ticket-import.json` (n8n → Workflows →
Import from File).  Set the `VoucherVault Base URL` and `API Token` credentials
to point at your VoucherVault instance.

---

## Summary of all new env vars

| Env var | Default | Purpose |
|---|---|---|
| `USE_WEBDAV_STORAGE` | `False` | Store uploaded files on WebDAV |
| `WEBDAV_URL` | — | WebDAV collection base URL |
| `WEBDAV_PUBLIC_URL` | `WEBDAV_URL` | Browser-facing URL for files |
| `WEBDAV_USERNAME` | — | WebDAV auth username |
| `WEBDAV_PASSWORD` | — | WebDAV auth password |
| `WEBDAV_VERIFY_SSL` | `True` | Verify server TLS cert |
| `USE_S3_STORAGE` | `False` | Store uploaded files on S3/MinIO |
| `S3_BUCKET_NAME` | — | Bucket name |
| `S3_ENDPOINT_URL` | — | Override endpoint (MinIO, R2, B2) |
| `S3_ACCESS_KEY_ID` | — | Access key |
| `S3_SECRET_ACCESS_KEY` | — | Secret key |
| `S3_REGION_NAME` | `us-east-1` | Region (ignored by MinIO) |
| `S3_CUSTOM_DOMAIN` | — | CDN / reverse-proxy domain for files |
| `S3_PUBLIC_BUCKET` | `False` | Disable signed URLs for public buckets |
| `OIDC_ENABLED` | `False` | Enable OIDC / Synology SSO login |
| `OIDC_PROVIDER_NAME` | `SSO` | Button label on the login page |
| `OIDC_DISCOVERY_URL` | — | `.well-known/openid-configuration` URL |
| `OIDC_RP_CLIENT_ID` | — | OIDC client ID |
| `OIDC_RP_CLIENT_SECRET` | — | OIDC client secret |
| `OIDC_RP_SIGN_ALGO` | `HS256` | `RS256` for Synology SSO |
| `OIDC_AUTOLOGIN` | `False` | Skip login page, go straight to SSO |
| `OIDC_CREATE_USER` | `True` | Create VV accounts for new SSO users |

---

## What's not supported (and why)

| Feature | Status |
|---|---|
| Synology Photos auto-import | Not implemented. The Photos API is private/undocumented. n8n watching a watched folder via SMB is a workaround. |
| Synology Drive sync as storage | Not possible directly — Drive is a sync client, not a storage API. Use the volume-mount approach (Option D) and let Synology Drive sync the host folder. |
| NFS / SMB as Django storage | Django storage requires a Python API. Mount NFS/SMB at the OS level and use the volume-mount approach instead. |
| Synology C2 Object Storage | S3-compatible. Use `USE_S3_STORAGE=True` with the C2 Object Storage endpoint URL. |
| DSM group-based access control | Not implemented. VoucherVault has its own per-user data isolation; all OIDC users log in as themselves and see only their own items. |

---

## Suggested future improvements

- **Synology Photos trigger**: Add an n8n workflow node that watches a Synology
  Photos album (via the Synology API) and sends new photos to VoucherVault's
  OCR endpoint for automatic item creation.

- **Synology Drive as backup target**: The scheduled backup feature
  (`SCHEDULED_BACKUP_ENABLED`) writes zip files to `database/backups/`. If the
  volume is mounted into a Synology Drive shared folder, Drive will sync those
  zips automatically — no extra code needed, just the right volume path.

- **DSM group → VoucherVault user role mapping**: For team/business deployments,
  map DSM groups to VoucherVault staff/superuser status via a custom OIDC claim.

- **Hyper Backup integration**: Document a Hyper Backup task that includes
  the VoucherVault `database/` and `uploads/` folders so the entire app state
  is covered by an existing NAS backup job.

- **Active Backup for Business**: For organisations already running Synology
  Active Backup, the PostgreSQL Backup Agent could snapshot the VoucherVault
  database on a schedule without needing the custom backup Celery task at all.
