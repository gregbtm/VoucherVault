# Setting up the MCP server

This lets Claude Desktop, Claude Code, or any other [MCP](https://modelcontextprotocol.io/)
client read and manage your VoucherVault items directly — "what's expiring
this week", "add this gift card", "log a £5 spend on my Tesco card" — by
calling the same REST API your web UI already uses.

## Important: this is per-person, not per-deployment

Unlike Apple/Google Wallet export (a one-time setup shared by everyone
using your instance), the MCP server acts **as a single VoucherVault user**
— whichever account's API token it's configured with. It reads and writes
that person's items only.

If more than one person in your household wants their own AI assistant
talking to their own vault, run one MCP container per person, each with
their own token and its own port (`8100`, `8101`, ...). Don't share one
token across multiple people's assistants unless you're fine with all of
them seeing and editing the same vault.

## What it can do

Read: search items, get an item's full details, list items expiring soon,
get the analytics summary (KPI counts, value by currency).

Write: create an item, record a transaction (spend) against a gift card,
mark an item used/redeemed, archive/unarchive an item.

Every tool call is just an HTTP request to your existing `/api/v1/`
endpoints — the same permission checks, validation rules, and side effects
(QR/barcode generation, webhook events from Phase 12.2, etc.) that already
apply to the web UI and REST API apply here too. The MCP server has no
direct database access and no special privileges of its own.

## Step 1 — Generate an API token for yourself

Easiest: log in to VoucherVault, open the **alice ▾ → API Access** menu
(top right), and click **Generate API Token**. The token is shown once —
copy it now.

Prefer the command line? Same result:

```bash
docker compose exec app python manage.py drf_create_token <your-username>
```

This prints a token like `dfa67dca5410ebaeacbd4443519c5680b6abfba4`. It
grants full read/write access to that user's entire vault — treat it as a
password, not a public identifier.

## Step 2 — Configure and start the MCP container

Uncomment the `mcp` service in your `docker-compose-sqlite-build.yml` (or
`-full-build.yml`), then set the token, either directly in the compose file
or via a `.env` file next to it:

```
VOUCHERVAULT_API_TOKEN=dfa67dca5410ebaeacbd4443519c5680b6abfba4
```

Redeploy the stack. The MCP server starts on port `8100` inside the same
Docker network as the app, talking to it at `http://app:8000` (no need to
route it through your reverse proxy).

## Step 3 — Point your MCP client at it

The server speaks MCP over Streamable HTTP at:

```
http://<your-docker-host>:8100/mcp
```

For Claude Code, add it with:

```bash
claude mcp add --transport http vouchervault http://<your-docker-host>:8100/mcp
```

For Claude Desktop, add an entry to your MCP config pointing at that same
URL (see Anthropic's docs for the current config file format, since this
changes between app versions).

## Security note

This server has no authentication layer of its own beyond the VoucherVault
API token baked into its environment — anyone who can reach port `8100`
can act as that one VoucherVault user. Keep it off the public internet:
LAN/VPN access only, same as you'd treat the Django admin panel. Don't
port-forward `8100` on your router.

## Running it standalone (without Docker Compose)

```bash
cd mcp_server
pip install -r requirements.txt
VOUCHERVAULT_BASE_URL=http://localhost:8000 \
VOUCHERVAULT_API_TOKEN=<your-token> \
python server.py
```

## Running the tests

The MCP server has no Django dependency, so its tests run standalone and
aren't part of `manage.py test` (the file is named `run_tests.py`, not
`tests.py`, specifically so Django's test discovery never tries to import
it and fail on a missing `mcp` package):

```bash
cd mcp_server
python -m unittest run_tests -v
```
