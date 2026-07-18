# Auto-importing UK rail eTickets via n8n

Builds on [`N8N_SETUP.md`](N8N_SETUP.md) with one specific workflow:
watch an inbox for rail booking confirmation emails (e.g. Uber's UK
rail bookings, which run on Omio's backend, or a train operator like
Greater Anglia) and turn each one into a Travel Pass item automatically
— no manual entry, no photo to take.

**Fastest path:** import
[`docs/n8n-workflows/vouchervault-rail-ticket-import.json`](n8n-workflows/vouchervault-rail-ticket-import.json)
directly into n8n (⋯ menu → Import from File) instead of building the
workflow node-by-node below. It's a complete Gmail-trigger → decode →
create-item → label workflow, with a sticky note on the canvas covering
the same credential/label setup as Steps 1 and 6 here. You'll still need
to: point its HTTP Request node at your own domain, attach your own
Gmail and Header Auth credentials (the imported nodes reference
placeholder credential IDs), and adjust the sender filter to match who
your tickets actually come from. The rest of this doc explains what
each piece does and walks through building it by hand if you'd rather
not start from the import.

## Why this needs its own endpoint

Official ticket-history APIs are a dead end here — Uber, Omio, and UK
train operating companies don't expose one to consumers. The workaround
every eTicket actually supports is the confirmation email itself: it
always contains a PDF with the journey details and a scannable barcode
(a square Aztec code, not a QR code, on UK rail tickets specifically).

`POST /api/v1/imports/rail-ticket/` exists to turn that PDF into an
Item:

- It decodes the barcode **server-side** (`myapp/pdf_ticket.py`, via
  the `zxing-cpp` library) — there's no browser involved in an
  unattended email pipeline to run the same client-side scan the
  create-item page uses, so this is real barcode decoding, not a guess.
- It fills in whatever text fields (journey, price, ticket number...)
  weren't already supplied, using VoucherVault's existing OCR backend
  (`SiteConfiguration.ocr_backend`) against a rendered image of the
  PDF's first page — the same vision extraction the "Scan with AI"
  button on create-item already uses for photos.
- With `create=true`, it creates the Item directly - no human review
  step, since nobody's watching an inbox-polling workflow in real time.

## Step 1 — Prerequisites

Do [`N8N_SETUP.md`](N8N_SETUP.md)'s Steps 1–2 first: generate an API
token for the account these tickets should land in, and add it as a
Header Auth credential (`Authorization: Token <your-token>`) in n8n.
Everything below reuses that credential.

## Step 2 — Trigger: watch the inbox

Add a **Gmail Trigger** (or **IMAP Email** node, for a non-Gmail
inbox), polling on a schedule (every 15–30 minutes is plenty — these
emails aren't time-sensitive). Filter it down to just the emails worth
processing, e.g.:

- Gmail: a search query like `from:(uber.com OR omio.com OR
  greateranglia.co.uk) has:attachment filename:pdf`
- IMAP: filter on subject containing "ticket" / "booking confirmation"
  and `has attachment`

## Step 3 — Get the PDF attachment as binary data

Both trigger nodes expose email attachments as n8n binary data directly
— no extra node needed. If the confirmation PDF isn't the only
attachment, add a **Filter** or **Code** node to pick out the one whose
filename/mimetype is `application/pdf`.

## Step 4 (optional) — Pre-extract text fields yourself

VoucherVault's OCR fallback (Step 5 below) handles this for you, but if
you'd rather not depend on it — no `OCR_BACKEND` configured, or you
just want cheaper/more deterministic parsing — add n8n's built-in
**Extract from File** node (operation: **PDF → Text**) and a **Code**
node with a regex per field. Anything you extract here and pass in the
next step is used as-is; VoucherVault's own OCR pass only fills in
whatever you *don't* supply.

## Step 5 — POST to VoucherVault

Add an **HTTP Request** node:

- Method: `POST`
- URL: `https://<your-domain>/api/v1/imports/rail-ticket/`
- Authentication: the Header Auth credential from Step 1
- Body content type: **Form-Data (Multipart)**
- Body parameters:

| Field | Value | Required |
|---|---|---|
| `file` | the PDF binary from Step 3 | yes |
| `create` | `true` | yes — omit/`false` only returns the extracted fields without creating anything |
| `issuer` | e.g. `Greater Anglia` | recommended (from Step 4, or leave out to let OCR fill it) |
| `journey_origin` | e.g. `Hatfield Peverel` | optional |
| `journey_destination` | e.g. `London Terminals` | optional |
| `travel_date` | `YYYY-MM-DD` | optional |
| `travel_time` | 24-hour `HH:MM` | optional |
| `card_number` | the ticket number, e.g. `AABXC5V4LVT` | optional — also the fallback redeem code if the barcode can't be decoded |
| `order_id` | e.g. `WEB017891237` | optional |
| `discount_applied` | e.g. `Network Railcard` | optional |
| `value` | e.g. `16.25` | optional |
| `currency` | e.g. `GBP` | optional |

A successful call returns `201` with `{"created": true, "item": {...}}`
— the full created Item, same shape as any other `/api/v1/items/`
response. See `https://<your-domain>/api/v1/docs/` (Swagger UI) for the
complete request/response schema.

## Step 6 — Mark the email processed

Add a **Gmail** (or IMAP) node after the HTTP Request to archive the
email, remove it from the search label, or mark it read — whichever
matches your Step 2 filter. This endpoint doesn't deduplicate: running
the same email through it twice creates two items, so the workflow
needs to make sure it never revisits an email it already processed.

## Notes and limitations

- The created item always has `type: "travelpass"` and lands in the
  auto-assigned "Travel Pass" wallet, same as one entered by hand.
- If neither the barcode decodes nor OCR can read a code, the ticket
  number (`card_number`) is used as the redeem code with
  `code_type: "none"` — the item still gets created with everything
  else it has, rather than failing outright.
- `create=true` requires at minimum an issuer and *some* code
  (decoded, OCR-guessed, or the ticket number) — with neither, the call
  returns `422` instead of creating a blank item.
- The same endpoint (with `create` omitted) is what the create-item
  page's "Scan with AI" button uses for a manually-uploaded PDF, so
  behavior stays identical between the automated and manual paths.

## See also

[`N8N_SETUP.md`](N8N_SETUP.md) covers the general API/token setup this
doc builds on, plus the AI Agent / full-schema integration path if you'd
rather have an LLM agent call this (and every other) endpoint on its
own.
