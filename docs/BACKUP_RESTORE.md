# Scheduled Backups & Restore

Every night at 03:00 (container time), a Celery periodic task backs up
every user with at least one item: a "Full Backup" zip — the exact same
format produced by the manual **Import / Export → Full Backup (with files) → Download Full Backup**
download — written to `database/backups/<username>/` and rotated so only
the newest **7** are kept per user.

This is a *local* backup: it protects you against accidental deletion,
data corruption, or a bad migration, and it lives on the same disk/volume
as everything else. It is **not** an offsite/disaster-recovery backup —
if the disk holding `database/` is lost, the backups are lost with it.
Copy them elsewhere periodically (see [Copying backups off-box](#copying-backups-off-box)
below) if you want protection against that too.

## Where backups live

```
database/backups/<username>/backup-YYYYMMDD-HHMMSS-ffffff.zip
```

`database/` is the same directory bind-mounted as `./volume-data/database`
in both `docker/docker-compose-sqlite-build.yml` and
`docker/docker-compose-full-build.yml` — the same place your SQLite
database, uploaded item photos, and document attachments already live —
so backups survive container restarts and redeploys automatically,
without any extra volume configuration.

Each zip contains:

- `items.json` — every item's full field data (name, code, value, dates,
  tags, wallet, notes, etc.)
- `files/` — the original photo/image attached to each item, if any
- `documents/` — any document attachments, if any

## Restoring

### Normal case: the app is still running

1. Log in as the user whose backup you want to restore.
2. Go to **Import / Export** in the sidebar.
3. Under **Full Backup (with files)**, choose the backup `.zip` (either
   grab it from `database/backups/<username>/` on the host, or from
   wherever you copied it to) and click **Restore**.
4. VoucherVault Plus+ restores every item, photo, and document from the
   zip.

**Important:** restoring **adds** items — it never overwrites or merges
with what's already there. Every item in the backup is recreated with a
brand-new ID. If you restore the same backup twice, you'll get two
copies of everything. This mirrors the existing manual Full Backup
restore behavior (Phase 11.8) and is by design: it's meant for disaster
recovery onto an empty (or partially-lost) account, not as a two-way sync
tool. If you're restoring after data loss, make sure the account is
actually empty of the items you're restoring first — or expect
duplicates.

### Disaster case: rebuilding the whole instance from scratch

If the whole container/database is gone and you're starting over:

1. Bring up a fresh VoucherVault Plus+ container per the normal
   [setup instructions](../README.md), and create your user account(s)
   again (same usernames as before, so the restored data association
   makes sense — though restoring doesn't strictly require matching
   usernames, since you're always restoring into whichever account
   you're logged in as).
2. If you'd copied the `database/backups/` directory off-box (see below),
   copy it back into the new instance's `volume-data/database/backups/`
   directory, or just locate the individual `.zip` you need.
3. Log in and use **Import / Export → Full Backup (with files) → Restore**
   as above, once per user.

## Copying backups off-box

This is the single most important thing to do beyond what the app does
for you: the scheduled backup and your live database sit on the **same**
disk/volume, so it protects you against accidental deletion or a bad
migration, but not against that disk/volume itself failing or being
wiped — in that scenario the live data and every rotated backup are lost
together. VoucherVault Plus+'s job stops at producing consistent, rotated
*local* backups; getting a copy off that box is a host-level concern,
outside the container, and worth setting up once.

Run these on the Docker/Portainer **host**, not inside the container —
they only need read access to `volume-data/database/backups/`.

### Option A — `rsync` to another machine on your network

```bash
# add to the host's crontab (crontab -e), once a day after the 03:00 backup runs
15 3 * * * rsync -a --delete /path/to/volume-data/database/backups/ user@backup-host:/backups/vouchervault/
```

`--delete` keeps the remote copy mirroring what's currently rotated
in locally — drop it if you'd rather keep a longer remote history than
the local `BACKUP_RETENTION_COUNT`.

### Option B — `rclone` to cloud/object storage (S3, Backblaze B2, a NAS share, etc.)

```bash
# one-time: rclone config, name the remote e.g. "vouchervault-backup"
15 3 * * * rclone sync /path/to/volume-data/database/backups/ vouchervault-backup:vouchervault/backups
```

`rclone sync` (not `copy`) mirrors deletions too, same rotation
consideration as `--delete` above. Use `rclone copy` instead if you want
the remote to accumulate every backup ever produced rather than tracking
local rotation.

### Verifying it's actually working

Both of the above are silent on success and (by default) silent on
failure too. Add `-v --log-file=/var/log/vouchervault-backup-sync.log` to
either command, or pipe the cron job's output through your host's usual
alerting (a `MAILTO=` in the crontab, a healthcheck ping via
`curl https://hc-ping.com/<uuid>` appended to the command, etc.) so a
silent failure doesn't go unnoticed for weeks.

## Configuration

Both settings below can be changed from the in-app **Site Settings** page
(`/admin-tools/site-settings/`, superuser-only) — no redeploy needed. The
environment variables are just the fallback/initial value for a fresh
install.

| Variable | Description | Default |
|---|---|---|
| `SCHEDULED_BACKUP_ENABLED` | Set to `False` to disable the nightly backup task entirely. | `True` |
| `BACKUP_RETENTION_COUNT` | How many backups to keep per user before rotating out the oldest. | `7` |

## Doing it manually / on-demand

You don't have to wait for the nightly schedule. The same **Import / Export
→ Full Backup (with files) → Download Full Backup** button on the web UI
produces an identical zip on demand — use that any time you want an extra
backup before a risky change (e.g. before a version upgrade).
