# How upstream sync works

This fork (`gregbtm/VoucherVault`) is built directly on top of upstream
[l4rm4nd/VoucherVault](https://github.com/l4rm4nd/VoucherVault) - every
phase in [`FORK_CHANGES.md`](../FORK_CHANGES.md) adds new models, views,
and migrations alongside upstream's own code rather than rewriting it, so
this fork stays a strict superset that can be kept in sync with upstream
indefinitely.

## Two independent version numbers

The footer and Site Settings show two version numbers, and they mean
different things:

- **This fork's own version** (`VERSION` file, e.g. `v1.1.11`) - bumped
  automatically by CI on every merge to `main`, regardless of whether the
  change came from this fork or from an upstream sync. This is what the
  in-app "Redeploy now" banner and update check compare against.
- **Upstream version this fork is synced to** (`UPSTREAM_VERSION` file,
  e.g. `1.29.0`) - only bumped when an actual upstream sync happens (see
  below). Compared against upstream's own latest release (checked daily,
  purely informational) to show whether a newer upstream release is
  available to sync from.

Both are plain text files committed at the repo root, read into
`settings.VERSION` / `settings.UPSTREAM_VERSION` exactly the same way.

## How a sync happens

Upstream is pulled in deliberately, not automatically merged:

1. **Detection** - a monthly check (see below) looks at whether upstream
   has a newer release than the `UPSTREAM_VERSION` file records, and
   whether that release is at least 10 days old (a cooldown window, so a
   sync never pulls in something that hasn't had a chance to get a
   follow-up hotfix yet).
2. **Assessment** - the commits between the last synced version and
   upstream's current `main` are reviewed for risk: whether they touch
   files this fork has heavily modified (`views.py`, `models.py`,
   templates - likely conflicts) versus files this fork hasn't touched at
   all (near-zero risk).
3. **Advance notice** - before anything is merged, a summary of what's
   new and its risk level is posted, so there's always a heads-up before
   any repo state changes.
4. **Merge** - a `sync/upstream-YYYYMMDD` branch is created and
   `upstream/main` is merged into it. Trivial conflicts (whitespace,
   `CHANGELOG.md`, doc-only collisions) are resolved automatically.
   Anything touching real logic is left for a joint review rather than
   guessed at.
5. **Verification** - the full test suite runs against the merge result.
6. **PR** - a pull request is opened summarizing what came in, what (if
   anything) needed manual conflict resolution, and the test result. nothing
   lands on `main` without this PR being reviewed and merged like any
   other change.
7. `UPSTREAM_VERSION` is bumped as part of that PR, to whatever upstream
   version was actually merged.

## Doing a sync manually

The same steps work by hand at any time, without waiting for the monthly
check:

```bash
git remote add upstream https://github.com/l4rm4nd/VoucherVault.git   # if not already added
git fetch upstream main
# Fetch upstream's tags into their own namespace rather than the shared
# refs/tags/* - this fork's own v1.1.x release tags and upstream's own,
# unrelated v1.1.x tags (upstream's version history predates this fork
# and goes back to v0.1.0) collide by name if fetched into the same
# place ("would clobber existing tag").
git fetch upstream 'refs/tags/*:refs/upstream-tags/*'

LAST_SYNCED=$(cat UPSTREAM_VERSION)
git log "refs/upstream-tags/v${LAST_SYNCED}..upstream/main" --oneline   # see what's new

git checkout -b sync/upstream-manual
git merge upstream/main
# resolve conflicts, run the test suite
```

## Why merge instead of pulling a Docker image

This fork is not published as its own Docker image (see
[`UPGRADE.md`](UPGRADE.md)) - it's built from source via a git-based
Portainer stack. That means "getting upstream's improvements" is a normal
git merge, not a separate image-pull step, and this fork's own additive
features (Wallets, notifications, the REST API, and everything else in
`FORK_CHANGES.md`) simply carry forward through the merge untouched,
since nothing upstream touches was ever rewritten to begin with.
