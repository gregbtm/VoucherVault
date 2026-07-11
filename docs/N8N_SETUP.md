# Connecting n8n to VoucherVault Plus+

This is a zero-build integration: no VoucherVault code, no custom n8n
node. Every existing `/api/v1/` endpoint — items, transactions, wallets,
tags, analytics, imports/exports — is already described by an
auto-generated OpenAPI schema, and n8n's built-in **HTTP Request Tool**
(and the AI Agent node's tool-calling) can consume that schema directly.

## Two directions of integration

- **n8n pulling from / writing to VoucherVault** (this doc): a workflow
  or an AI Agent calls the REST API to read items, create a voucher, log
  a spend, etc. Covered below.
- **VoucherVault pushing to n8n**: the existing webhook system
  (`Notifications → Rules`, backend type "Webhook") already fires an HTTP
  POST to any URL you configure whenever an item is created/used/archived/
  its balance changes/shared. Point one at an n8n **Webhook** trigger node
  to kick off a workflow from VoucherVault-side events, no polling needed.
  See `FORK_CHANGES.md`'s Phase 12.2 section for the exact payload shape.

The rest of this doc covers the first direction.

## Step 1 — Generate an API token

Same token used by the [MCP server](MCP_SERVER_SETUP.md) and the mobile/
scripting use cases — one token per person, full read/write access to
that person's vault:

```bash
docker compose exec app python manage.py drf_create_token <your-username>
```

This prints a token like `dfa67dca5410ebaeacbd4443519c5680b6abfba4`.
Treat it as a password. If you'd rather not touch the container shell,
`POST username`/`password` to `/api/v1/auth/token/` instead — it returns
the same kind of token for that user.

## Step 2 — Add a credential in n8n

In n8n: **Credentials → New → Header Auth** (or "Generic Credential Type"
→ HTTP Header Auth, depending on your n8n version):

| Field | Value |
|---|---|
| Name | `Authorization` |
| Value | `Token dfa67dca5410ebaeacbd4443519c5680b6abfba4` |

(Note the literal word `Token`, not `Bearer` — that's DRF's
`TokenAuthentication` scheme, which VoucherVault Plus+ uses.)

## Step 3a — Simple case: a single HTTP Request node

For a fixed, known call (e.g. "list items expiring in the next 7 days"
on a schedule), skip the OpenAPI import entirely — just add an **HTTP
Request** node:

- Method: `GET`
- URL: `https://<your-domain>/api/v1/items/?expires_before=2026-07-18`
- Authentication: the Header Auth credential from Step 2

Browse `https://<your-domain>/api/v1/docs/` (Swagger UI) first to see
every available endpoint, its query parameters, and example
request/response bodies — that page **is** the schema, rendered.

## Step 3b — Richer case: an AI Agent with the full API as tools

If you want an LLM-driven agent (e.g. "check my VoucherVault and tell me
what's expiring, then log that I used the Tesco voucher") to have the
whole API available as callable tools, rather than wiring up each call by
hand:

1. Add an **AI Agent** node, with whichever model/chat trigger you're
   already using.
2. Add a **HTTP Request Tool** node as one of its tools.
3. Set its authentication to the Header Auth credential from Step 2.
4. Set **Specify via** (or the equivalent field, wording varies slightly
   by n8n version) to **OpenAPI / Import from URL**, and point it at:

   ```
   https://<your-domain>/api/v1/schema/
   ```

n8n reads the schema and exposes every operation in it (list items,
create an item, get analytics, trigger an import, etc.) as a tool the
agent can choose to call, with parameters it infers from your prompt.
No manual per-endpoint wiring.

## Notes and limitations

- Every call is scoped to whichever user the token belongs to — same
  permission model as the web UI and the MCP server. There's no
  separate "n8n user"; it acts as you.
- The schema is regenerated live by `drf-spectacular` from the actual
  DRF viewsets/serializers, so it never drifts out of sync with the real
  API — if a field is renamed or an endpoint added in a future phase,
  re-importing the schema URL in n8n picks it up automatically.
- Keep your VoucherVault instance reachable from wherever n8n runs (same
  LAN, same Docker network, or via your reverse proxy/VPN) but don't
  expose the API token itself anywhere public — the same "keep it off
  the public internet" guidance in [`MCP_SERVER_SETUP.md`](MCP_SERVER_SETUP.md)
  applies here too.
