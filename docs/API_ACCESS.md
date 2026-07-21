# REST API Access

VoucherVault Plus+ ships a full REST API at `/api/v1/`. It uses token authentication — the same token works with the MCP server, n8n, and any other HTTP client.

## Getting your API token

Go to **Profile → API Access** (top-right user menu). From there you can:

- **Generate** a new token (shown once — copy it immediately)
- **Regenerate** to revoke the old token and issue a new one
- **Revoke** to delete the token entirely

Alternatively, generate a token from the command line:

```bash
docker compose exec app python manage.py drf_create_token <username>
```

## Using the token

Pass the token in the `Authorization` header of every request:

```bash
curl -H "Authorization: Token YOUR_TOKEN_HERE" \
     https://your-instance/api/v1/items/
```

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/items/` | List all your items |
| `POST` | `/api/v1/items/` | Create a new item |
| `GET` | `/api/v1/items/{id}/` | Get a single item |
| `PATCH` | `/api/v1/items/{id}/` | Update fields on an item |
| `DELETE` | `/api/v1/items/{id}/` | Delete an item |
| `GET` | `/api/v1/items/{id}/documents/` | List attachments on an item |
| `GET` | `/api/v1/wallets/` | List wallets |
| `GET` | `/api/v1/tags/` | List tags |
| `GET` | `/api/v1/notifications/rules/` | List notification rules |
| `GET` | `/api/v1/notifications/logs/` | List notification log entries |
| `GET` | `/api/v1/dms/providers/` | List DMS providers |
| `GET` | `/api/v1/analytics/` | Inventory analytics summary |

Full interactive docs are at `/api/v1/docs/` (Swagger UI) and the OpenAPI schema at `/api/v1/schema/`.

## Filtering and pagination

Items support filtering via query parameters:

```
/api/v1/items/?type=giftcard
/api/v1/items/?wallet=3
/api/v1/items/?search=tesco
/api/v1/items/?ordering=-expiry_date
```

Results are paginated (25 per page by default). Use `?page=2` to advance.

## Scope

The API is scoped to the authenticated user. You can only read and write your own items — there is no admin-level token that spans all users.

## MCP server

If you use Claude, Claude Code, or another MCP-compatible AI assistant, you can give it direct read/write access to your vault. See the [MCP server guide](./MCP_SERVER_SETUP.md).

## n8n

To trigger VoucherVault actions from n8n workflows (or read items into a workflow), use an HTTP Request node with Token auth. See the [n8n guide](./N8N_SETUP.md).
