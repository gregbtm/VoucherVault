# Setting up "Scan with AI" (OCR)

This is a **one-time setup done by whoever runs the VoucherVault container**
— not something each person using your instance has to do. Once you've
picked a backend and (if needed) supplied an API key, everyone who logs
into your VoucherVault gets a "Scan with AI" button on the create/edit item
forms: upload a photo of a physical voucher/coupon/gift card, or a
screenshot (an emailed gift card, a retailer app screen), and it pre-fills
the redeem code, barcode type, merchant, and expiry date for you.

There are three interchangeable backends. Pick whichever fits — you can
switch at any time from Site Settings, and turning it off entirely (the
default) falls back to the same barcode-only camera/photo scanner with no
AI involvement.

| Backend | Cost | Runs where | Needs an API key |
|---|---|---|---|
| **Claude** (Anthropic) | Pay-per-use, a few cents per scan | Anthropic's servers | Yes |
| **OpenAI** | Pay-per-use, a few cents per scan | OpenAI's servers | Yes |
| **Tesseract** | Free | Locally, inside the container | No |

## Claude backend

1. Create an account at [console.anthropic.com](https://console.anthropic.com/)
   and add billing (pay-as-you-go; a scan costs a fraction of a cent to a
   few cents depending on image size).
2. Generate an API key under **API Keys** in the console.
3. In **Site Settings** (`/admin-tools/site-settings/`), set **OCR
   backend** to `claude` and paste the key into **Anthropic API key**.
   Takes effect immediately.
4. Optional: override **Claude model override** if you want a specific
   model version rather than the default.

Equivalent environment variables (what Site Settings falls back to until
set there): `OCR_BACKEND=claude`, `ANTHROPIC_API_KEY=...`,
`ANTHROPIC_OCR_MODEL=claude-sonnet-5` (optional).

## OpenAI backend

1. Create an account at [platform.openai.com](https://platform.openai.com/api-keys)
   and add billing.
2. Generate an API key.
3. In **Site Settings**, set **OCR backend** to `openai` and paste the key
   into **OpenAI API key**.
4. Optional: override **OpenAI model override** — defaults to
   `gpt-4o-mini`, a good balance of cost and accuracy for this task.

Equivalent environment variables: `OCR_BACKEND=openai`,
`OPENAI_API_KEY=...`, `OPENAI_OCR_MODEL=gpt-4o-mini` (optional).

## Tesseract backend (free, local, no API key)

Runs entirely inside the container using the `tesseract-ocr` package
already bundled in the Docker image — nothing to sign up for. The
trade-off: it only does raw text extraction (finds a plausible-looking
code in the photo), with no understanding of what a "merchant name" or
"expiry date" actually is, so those fields are never auto-filled by this
backend — only the redeem code and (via the same client-side barcode
decode every backend gets) the barcode itself.

1. In **Site Settings**, set **OCR backend** to `tesseract`. Nothing else
   to configure.

Equivalent environment variable: `OCR_BACKEND=tesseract`.

## Turning it off

Set **OCR backend** to `none` (the default) — the "Scan with AI" upload
box disappears from the create/edit forms entirely, leaving just the
plain barcode camera/photo scanner.

## Checking it's working

Site Settings shows a "Ready"/"Not ready" badge next to the OCR backend
selector: for Claude/OpenAI it confirms the relevant API key is actually
set, and for Tesseract it confirms the `tesseract` binary is actually
found inside the running container (it should be, since it's baked into
the image — a "Not ready" state there would mean a non-standard build).

## Troubleshooting

- **"Scan with AI" section doesn't appear**: backend is set to `none`, or
  the change hasn't been saved yet.
- **Upload succeeds but nothing gets pre-filled**: try a clearer/closer
  photo — all three backends work directly off what's visible in the
  image, so a blurry or heavily-angled shot of small print won't extract
  reliably. Tesseract in particular only extracts a best-guess code, not
  merchant/expiry, by design (see above).
- **"Claude OCR response was not valid JSON" / similar in the logs**: the
  model's response didn't parse as expected for that image — usually a
  transient issue with an unusual photo; try again or try a different
  photo.
