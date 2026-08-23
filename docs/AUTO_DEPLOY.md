# Redeploying without clicking through Portainer

This fork can run either of two ways: a **git-based Portainer stack**
that builds the image from this repo's `docker/Dockerfile` (the original
model - see [`docs/UPGRADE.md`](UPGRADE.md)), or a **prebuilt-image
stack** that pulls `ghcr.io/gregbtm/vouchervault` from GHCR (built and
pushed automatically by the `deploy` job in
[`conventional-commits.yml`](../.github/workflows/conventional-commits.yml)
on every push to `main` that passes CI). Either way, "update to the
latest code" normally means opening Portainer, finding the stack, and
clicking pull-and-redeploy by hand. This fork wires two different
triggers up to automate that:

- An **in-app "Redeploy now" button** on the update-available banner
  (`myapp/portainer.py`) - a superuser sees the banner, clicks the button,
  and the app container calls Portainer's stack webhook itself, server-side,
  over the same Docker network Portainer and the app already share.
  Nothing needs to be reachable from outside your host for this to work.
  This still uses the plain webhook (Steps 1-2 below) regardless of which
  model you're running - on a prebuilt-image stack, make sure the webhook
  is configured with Portainer's "force pull image" option (where your
  Portainer version supports it), or the button may redeploy without
  actually picking up the new image.
- A **GitHub Action** ([`.github/workflows/portainer-redeploy.yml`](../.github/workflows/portainer-redeploy.yml))
  that fires after every push to `main` (Step 3 below). It only supports
  the **prebuilt-image** model: it calls Portainer's stack-update API
  directly with `pullImage: true`, which reliably forces a fresh pull
  regardless of webhook/Portainer-version quirks. If you're still on a
  git-based stack, rely on the in-app button (or Portainer's own webhook)
  instead - this workflow no-ops without its own set of secrets.

Both triggers are optional and off by default - the app runs exactly as
before if you don't set anything up.

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

## Step 3 — Wire up the GitHub Action (prebuilt-image stacks, needs public exposure)

This step only applies if you've switched your Portainer stack(s) to pull
`ghcr.io/gregbtm/vouchervault` instead of building from source, and only
makes sense if Portainer's API is reachable from the public internet -
GitHub Actions runs on GitHub's own cloud infrastructure and cannot reach
an address that only resolves on your home network. If your Portainer
instance isn't exposed at all, skip this step and stick to the in-app
button, or trigger a redeploy by hand from your own network.

If you do want this:

1. Expose Portainer's API (or at least reach it via a reverse-proxied
   hostname) from the public internet. Unlike the single-purpose webhook
   path used by the in-app button, this workflow calls the general
   `/api/stacks/{id}` endpoints authenticated with an API key, so treat
   that exposure with the same care you'd give the Portainer UI itself.
2. In Portainer, create an **API access token** (your user's account
   settings → Access Tokens) scoped to the account that can manage the
   VoucherVault stack(s).
3. Note each stack's numeric **stack ID** and **environment (endpoint)
   ID** - both are visible in the URL when you open the stack in the
   Portainer UI (`.../#!/<endpointId>/docker/stacks/<name>?id=<stackId>`).
   VoucherVault normally runs as two separate live deployments
   (`vouchervault-475` and `vouchervault-476`), so you'll typically have
   two of each.
4. In your GitHub repository, go to **Settings → Secrets and variables →
   Actions → New repository secret** and add:
   - `PORTAINER_URL` - e.g. `https://portainer.example.com`
   - `PORTAINER_API_KEY` - the token from step 2
   - `PORTAINER_STACK_IDS` - space-separated stack IDs, e.g. `12 13`
   - `PORTAINER_ENDPOINT_IDS` - space-separated endpoint IDs, same order/
     count as `PORTAINER_STACK_IDS`, e.g. `3 3`
5. That's it - [`.github/workflows/portainer-redeploy.yml`](../.github/workflows/portainer-redeploy.yml)
   already exists in this repo and fires on every push to `main`. If any
   of the four secrets is unset, the workflow logs a message and exits
   successfully without failing your CI - it's safe to leave the workflow
   file in place even if you never configure the secrets.
6. You can also fire it manually from the **Actions** tab (**Trigger
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
- **GitHub Action fails**: check the job log - it prints Portainer's raw
  API response on every call. Common causes: `PORTAINER_URL` not
  reachable from GitHub's runners, an expired/wrong-scoped API token, or
  a stack/endpoint ID that no longer matches (IDs change if a stack is
  deleted and recreated).
- **Redeploy succeeds but the container still runs the old image**:
  double-check `pullImage: true` actually reached Portainer (visible in
  the request body logged by the workflow) and that the stack's
  `image:` line uses a mutable tag like `:latest` rather than a tag or
  digest pinned to an older build.
