# Redeploying without clicking through Portainer

If you're running this fork the way [`docs/UPGRADE.md`](UPGRADE.md)
describes - a git-based Portainer stack that builds the image from this
repo's `docker/Dockerfile` rather than pulling a published one - "update
to the latest code" normally means opening Portainer, finding the stack,
and clicking pull-and-redeploy by hand. Portainer has a built-in webhook
for exactly this, and this fork wires two different triggers up to it:

- An **in-app "Redeploy now" button** on the update-available banner
  (`myapp/portainer.py`) - a superuser sees the banner, clicks the button,
  and the app container calls the webhook itself, server-side, over the
  same Docker network Portainer and the app already share. Nothing needs
  to be reachable from outside your host for this to work.
- A **GitHub Action** ([`.github/workflows/portainer-redeploy.yml`](../.github/workflows/portainer-redeploy.yml))
  that calls the same webhook automatically after every push to `main`,
  so a merge alone triggers a redeploy. This one *does* need Portainer's
  webhook endpoint reachable from the public internet, since GitHub's
  runners are cloud-hosted, not on your LAN.

Both are optional and off by default - the app runs exactly as before if
you don't set anything up.

## Step 1 — Enable the stack webhook in Portainer

1. In Portainer, go to **Stacks** and open your VoucherVault stack.
2. Near the top of the stack's detail page, toggle **Webhook** on. If you
   don't see this option, your Portainer edition/version may not support
   stack webhooks for git-based stacks - check your Portainer version.
3. Portainer generates a URL that looks like:
   ```
   https://<your-portainer-host>:9443/api/webhooks/<uuid>
   ```
   Copy it. Treat it like a password - anyone who has this URL can trigger
   a rebuild of your stack, and depending on your Portainer version it may
   not require any further authentication.
4. Test it once by hand before wiring anything else up:
   ```bash
   curl -X POST "https://<your-portainer-host>:9443/api/webhooks/<uuid>"
   ```
   Watch the stack's logs in Portainer - you should see it start pulling
   and rebuilding within a few seconds.

## Step 2 — Wire up the in-app button

**The easiest way**: log in as a superuser, open **Site Settings**
(`/admin-tools/site-settings/`, linked from the sidebar), and paste the
webhook URL from Step 1 into the **Portainer Redeploy Webhook** field.
This takes effect immediately - no redeploy, no `.env` file, no
Portainer config edit.

Alternatively, set the webhook URL as an environment variable on the
**app** container (the same place as every other `PORTAINER_WEBHOOK_URL`-
style setting in `docker/docker-compose-sqlite-build.yml`), which is what
Site Settings falls back to until you (or a fresh install) set it there
instead:

```yaml
environment:
  # from Step 1 above
  - PORTAINER_WEBHOOK_URL=${PORTAINER_WEBHOOK_URL:-}
```

...and add it to your `.env` file (see `docker/env.example`):

```
PORTAINER_WEBHOOK_URL=https://<your-portainer-host>:9443/api/webhooks/<uuid>
```

Redeploy the stack once (the normal way, through Portainer) to pick up
the new variable. From then on, whenever the update-check banner shows a
newer release is available, superusers see a **Redeploy now** button next
to it. Clicking it POSTs to the webhook from inside the app container and
shows a success or failure message - it doesn't wait for the rebuild to
finish, since that happens out-of-band in Portainer and will briefly
restart the app container itself.

This button is only ever shown to superusers (`request.user.is_superuser`
is checked in the view itself, not just hidden by the template), and does
nothing at all if `PORTAINER_WEBHOOK_URL` isn't set.

## Step 3 — Wire up the GitHub Action (optional, needs public exposure)

This step only makes sense if Portainer's webhook endpoint is reachable
from the public internet - GitHub Actions runs on GitHub's own cloud
infrastructure and cannot reach an address that only resolves on your
home network. If your Portainer instance isn't exposed at all, skip this
step and stick to the in-app button, or trigger the webhook by hand /
from your own network (e.g. a cron job on the Docker host itself, or a
phone shortcut on your home Wi-Fi).

If you do want this:

1. Expose **only the webhook path**, not the whole Portainer UI/API,
   through your reverse proxy if at all possible. A rule like
   "`/api/webhooks/<uuid>` on `portainer.example.com` proxies to
   Portainer; everything else is blocked" keeps the actual admin UI off
   the public internet while still letting this one URL through. Exactly
   how to do this depends on your reverse proxy (Traefik, Nginx Proxy
   Manager, Caddy, etc.) - the UUID in the path is the only thing
   standing in for auth here, so this is materially safer than exposing
   the whole Portainer login page.
2. In your GitHub repository, go to **Settings → Secrets and variables →
   Actions → New repository secret**, name it `PORTAINER_WEBHOOK_URL`,
   and paste the URL from Step 1.
3. That's it - [`.github/workflows/portainer-redeploy.yml`](../.github/workflows/portainer-redeploy.yml)
   already exists in this repo and fires on every push to `main`. If the
   secret isn't set, the workflow logs a message and exits successfully
   without failing your CI - it's safe to leave the workflow file in
   place even if you never configure the secret.
4. You can also fire it manually from the **Actions** tab (**Trigger
   Portainer redeploy → Run workflow**) without waiting for a push.

## Troubleshooting

- **Button doesn't appear**: `PORTAINER_WEBHOOK_URL` isn't set on the app
  container, or you're not logged in as a superuser. Check
  `docker compose exec app env | grep PORTAINER`.
- **Button shows a failure message**: the app container couldn't reach
  the webhook URL. If Portainer's UI is only reachable via a different
  hostname/port from inside the Docker network than from outside it
  (common with reverse-proxy setups), use the internal address for
  `PORTAINER_WEBHOOK_URL` rather than the public one.
- **GitHub Action fails**: almost always the webhook endpoint not being
  reachable from the public internet - re-check Step 3's reverse-proxy
  rule, or fall back to the in-app button only.
- **Webhook fires but nothing changes**: Portainer's git-based rebuild
  only re-pulls the branch/ref the stack is already configured to track.
  Double check the stack's configured branch matches where you're
  actually merging (`main`, per this fork's normal workflow).
