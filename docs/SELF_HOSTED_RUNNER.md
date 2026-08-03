# Self-hosted GitHub Actions runner (Phase 1)

This sets up a self-hosted GitHub Actions runner for this repo, deployed
as a Portainer stack the same way you'd deploy VoucherVault itself (see
[`UPGRADE.md`](UPGRADE.md)) - git-based, on an always-on Docker host
rather than a git checkout you manage by hand.

**Phase 1 scope**: get one runner registered and showing **Idle** under
this repo's Settings -> Actions -> Runners. No CI jobs are pointed at it
yet - `.github/workflows/conventional-commits.yml` still runs entirely on
`ubuntu-latest`. Migrating individual jobs to this runner (`Test Suite`,
the security scanners) is Phase 2, done one `runs-on:` line at a time so
each is independently revertible.

## Why this is safe against a bad fork PR

Mounting the Docker socket (below) gives the runner container the same
power over your Docker host as anything else running there. The
meaningful mitigation for that isn't container-hardening flags - it's
making sure untrusted code never reaches this box in the first place:

1. Go to this repo's **Settings -> Actions -> General**.
2. Under **Fork pull request workflows from outside collaborators**,
   select **Require approval for all outside collaborators** (or
   disable fork PR workflows entirely if you don't expect any).

Do this before deploying the stack below, not after.

## Step 1 — Generate a scoped Personal Access Token

1. On GitHub, go to **Settings -> Developer settings -> Personal access
   tokens -> Fine-grained tokens -> Generate new token**.
2. **Repository access**: "Only select repositories" -> choose
   `gregbtm/VoucherVault` only. Do not grant access to any other repo.
3. **Permissions -> Repository permissions -> Administration**: set to
   **Read and write**. This is the specific permission runner
   registration needs - nothing else is required.
4. Generate and copy the token once. You won't see it again - if you
   lose it, generate a new one and update the Portainer stack's
   environment variable (Step 4).

## Step 2 — Confirm the Docker socket path on your NAS

`docker-compose.yml` below mounts `/var/run/docker.sock` from the host.
This is the standard location, but confirm it exists on your DSM version
before deploying:

```bash
ssh admin@<your-nas>
sudo find / -name docker.sock 2>/dev/null
```

If it's somewhere other than `/var/run/docker.sock`, edit the source
(left) side of that volume line in `docker/runner/docker-compose.yml`
accordingly before deploying.

## Step 3 — Create the stack in Portainer

1. In Portainer, go to **Stacks -> Add stack**.
2. Name it `vv-github-runner`.
3. Build method: **Repository** (same as your VoucherVault stack) ->
   Repository URL: `https://github.com/gregbtm/VoucherVault` -> Compose
   path: `docker/runner/docker-compose.yml`.
   - If you'd rather not have Portainer pull from the repo, **Web
     editor** works too - just paste the contents of
     `docker/runner/docker-compose.yml`.
4. Under **Environment variables**, add one variable:
   - `GITHUB_RUNNER_PAT` = the token from Step 1.

   See `docker/runner/env.example` for the exact name expected - never
   put the real token value into the compose file itself.
5. Deploy the stack.

## Step 4 — Verify

1. In Portainer, check the `vv-runner` container's logs - you should see
   it register successfully and start listening for jobs.
2. On GitHub, go to this repo's **Settings -> Actions -> Runners**. You
   should see a runner named `synology-vv-nas` with status **Idle**.

If it's not there after a minute or two, check the container logs first
(most failures are the PAT lacking the Administration permission, or the
Docker socket path from Step 2 being wrong) before touching anything
else.

## What's next

Nothing in `.github/workflows/conventional-commits.yml` targets this
runner yet - it's just sitting idle, proven to work. Phase 2 changes one
job's `runs-on: ubuntu-latest` to `runs-on: [self-hosted, docker,
vv-nas]` at a time, starting with `pip-audit` (simplest, no Docker-in-
Docker involved, easiest to confirm end-to-end before moving anything
bigger).
