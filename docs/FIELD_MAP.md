# VoucherVault Field Map

A reference for every form field in VoucherVault: what it is, when it appears, and which fields have context-aware suggestion buttons.

**Interactive version** — an in-app tool with live filtering, the type-visibility matrix, and 💡 suggestion indicators is available at `/en/help/field-map/` when logged in.

---

## Item Form

The most complex form in the app. Fields appear across **create-item** and **edit-item** and are heavily driven by item type.

Source files: `create-item.html` / `edit-item.html` · `ItemForm` in `myapp/forms.py`

### Context-Aware Suggestion System

Six fields carry a **💡 suggestion button** powered by `views.suggest_field_options` and `field-suggest.js`:

| Field | ID | Trigger |
|---|---|---|
| Issuer | `issuer` | Appears when field is empty |
| Logo Slug | `logo_slug` | Appears when field is empty |
| Wallet | `wallet` | Always visible (SELECT field) |
| Discount / Railcard Applied | `discount_applied` | Appears when field is empty |
| Currency | `currency` | Always visible (SELECT field) |
| Code Type | `code_type` | Always visible (SELECT field) |

**How ranking works:**

1. The server fetches up to 25 recent items of the same type for the current user and ranks unique values by frequency (most-used first) and recency.
2. If the `issuer` field is already filled, suggestions from items sharing that issuer are boosted to the top.
3. If fewer than 3 ranked suggestions exist for the selected type, items of other types are included as fallback (ranked below type-specific results).
4. Up to 5 suggestions are returned.

**Self-healing feedback loop:**

When a suggestion is accepted, a hidden `_sg_suggested_FIELD` input is injected into the form. On save, the server diffs the accepted value against what was actually saved:

- If the user **kept** the suggestion → any stale `ScanFieldCorrection` row for that suggestion is deleted (the suggestion was correct, no correction needed).
- If the user **changed** it → a `ScanFieldCorrection` row is upserted (`user`, `item_type`, `field`, `ai_value`, `corrected_value`, `times_seen`) so the system learns from the correction.

Implementation: `_record_suggestion_feedback` in `myapp/views.py`, called after `_record_scan_learning` in both `create_item` and `edit_item`.

---

### Scan Helpers (pre-form)

| Field | ID | Type | Status | Notes |
|---|---|---|---|---|
| AI Scan | `aiScanInput` | file | Conditional | Requires `ocr_backend ≠ none` in Site Settings |
| Apple Wallet Import | `pkpassImportInput` | file | Conditional | Shown on Apple devices only (user-agent detection) |
| Camera Scan | `startScanner` | button | Always | Launches device camera for barcode scan |
| File Scan | `scanFromFile` | button | Always | Scan barcode from an image file |

### Basic Information

| Field | ID | Type | Status | Notes |
|---|---|---|---|---|
| Type | `type` | select | **Driver** | Controls all other field visibility |
| Tile Color | `tile_color` | color | Always | Card background override |
| Name | `name` | text | Always | Required |
| Issuer | `issuer` | text+datalist | Always | 💡 Suggestion button. Label → "Store" for loyalty cards. Triggers logo fetch. |

### Code Information

| Field | ID | Type | Status | Notes |
|---|---|---|---|---|
| Redeem Code | `redeem_code` | text | Always | Label → "Loyalty ID" for loyalty cards |
| Card / Member Number | `card_number` | text | Conditional | Gift card · loyalty card |
| Journey From | `journey_origin` | text | Conditional | Travel pass only |
| Journey To | `journey_destination` | text | Conditional | Travel pass only |
| Time of Travel | `travel_time` | time | Conditional | Travel pass only |
| Seat / Coach | `seat_number` | text | Conditional | Travel pass only |
| Membership Tier | `membership_tier` | text | Conditional | Loyalty card only |
| Code Type | `code_type` | select | Always | 💡 Suggestion button (always visible). 16 symbologies. Default: No Barcode. |
| PIN Code | `pin` | text | Conditional | Gift card · voucher · loyalty card (read-only) |

### Dates

| Field | ID | Type | Status | Notes |
|---|---|---|---|---|
| Issue Date | `issue_date` | date | Conditional | Required for all types except travel pass |
| Expiry Date | `expiry_date` | date | Always | Defaults to +50 years if blank |

### Value

| Field | ID | Type | Status | Notes |
|---|---|---|---|---|
| Value | `value` | number | Conditional | Hidden for travel pass; read-only (forced 0) for loyalty card |
| Value Type Toggle | `toggle-value-type` | button | Conditional | Coupon only — cycles money/percentage/multiplier |
| Currency | `currency` | select | Conditional | 💡 Suggestion button (always visible). Hidden for loyalty card and non-money value types. |
| Balance Check URL | `balance_check_url` | url | Conditional | Gift card only |
| Face Value (at purchase) | `initial_value` | number | Conditional | Gift card only |
| Points Balance | `points_balance` | number | Conditional | Loyalty card only |
| Firefly III Account ID | `firefly_account_id` | text | ⚡ Proposed | Gate on: user has at least one Firefly rule |
| Firefly III Rule (override) | `firefly_rule` | select | ⚡ Proposed | Edit form only; same gate as above |

### Organisation

| Field | ID | Type | Status | Notes |
|---|---|---|---|---|
| Wallet | `wallet` | select | Conditional | 💡 Suggestion button (always visible). Disabled + auto-assigned for travel pass. |
| Tags | `tags` | checkboxes | Always | Only rendered if user has tags |
| New Tags | `new_tags` | text | Always | Comma-separated; combined with existing tags |
| Notes | `notes` | textarea | Always | Free-text |
| Share Message | `share_message` | textarea | Always | Shown on the public share page |
| Recurring / Subscription | `is_recurring` | checkbox | **Driver** | Reveals renewal period and date |
| Renewal Period | `renewal_period` | select | Conditional | `is_recurring` checked |
| Next Renewal Date | `renewal_date` | date | Conditional | `is_recurring` checked |
| Notify Before Expiry (days) | `notify_days_before` | number | ⚡ Proposed | Consider hiding unless user has at least one active notification rule |

### Additional Information

| Field | ID | Type | Status | Notes |
|---|---|---|---|---|
| Description | `description` | textarea | Always | |
| Order / Booking Reference | `order_id` | text | Conditional | Gift card · travel pass |
| Discount / Railcard Applied | `discount_applied` | text | Conditional | 💡 Suggestion button when empty. Travel pass only. |
| Minimum Spend | `minimum_spend` | number | Conditional | Voucher · coupon |
| Logo Slug | `logo_slug` | text | ⚡ Proposed | 💡 Suggestion button when empty. Proposal: move to "Advanced" collapsible. |
| Upload File | `file` | file | Always | Card photo or PDF |

### Type Visibility Matrix

| Field | Gift Card | Voucher | Coupon | Loyalty Card | Travel Pass |
|---|---|---|---|---|---|
| Card / Member Number | ✓ | – | – | ✓ | – |
| PIN Code | ✓ | ✓ | – | ✓ (r/o) | – |
| Journey / Seat fields | – | – | – | – | ✓ |
| Membership Tier | – | – | – | ✓ | – |
| Value section | ✓ | ✓ | ✓ | ✓ (r/o) | – |
| Currency | ✓ | ✓ | ↕ money | – | – |
| Balance Check URL | ✓ | – | – | – | – |
| Face Value | ✓ | – | – | – | – |
| Points Balance | – | – | – | ✓ | – |
| Order / Booking Ref | ✓ | – | – | – | ✓ |
| Discount / Railcard | – | – | – | – | ✓ |
| Minimum Spend | – | ✓ | ✓ | – | – |
| Wallet | select | select | select | select | locked |
| Issue Date | required | required | required | required | optional |

---

## Site Settings

Admin-only form. Autosaved via AJAX. Source: `site_settings.html` · `SiteConfigurationForm`.

Key conditional groups:

- **VAPID / Web Push** — `webpush_vapid_claims_email` and `webpush_barcode_key_version` shown only when `webpush_vapid_public_key` is non-blank.
- **OCR backend** — Anthropic API key + model shown only when `ocr_backend = claude`; OpenAI key + model only when `ocr_backend = openai`.
- **OIDC** — `oidc_autologin` and `oidc_require_totp` shown only when both discovery URL and client ID are set.
- **Apple Wallet / Google Wallet** — each in a `<details>` element, collapsed by default.
- **Proposed conditionals** — Overpass API URL (gate on `nearby_places_enabled`), share link settings (gate on `share_via_smart_enabled`), backup retention (gate on `scheduled_backup_enabled`).

---

## User Preferences

Source: `update_preferences.html` · `UserPreferenceForm`.

Notable proposed conditionals:

- **Next Up Items to Show** — show only when at least one wallet is checked in `next_up_wallets`.
- **Active Today** sub-fields (`commute_home_station`, `active_today_cutoff_time`) — show only when `active_today_enabled` is checked.
- **Nearby** section — gate on site-level `nearby_places_enabled` first, then per-user `nearby_items_enabled`.

---

## Notification Rules

Source: `notify/templates/notify/rules.html` · `NotificationRuleForm`.

Backend selector (`apprise` / `firefly` / `ntfy` / `webhook` / `webpush`) drives which auth/config fields appear. `webpush` back-end only offered when VAPID keys are configured site-wide.

---

## Other Forms

| Form | Source | Conditional logic |
|---|---|---|
| Outbound Webhooks | `webhooks.html` | None — all fields always shown |
| DMS Provider | `dms/provider_form.html` | Provider type drives auth section; auto-pull toggle reveals filter fields |
| Wallet | `manage-wallets.html` | Collaborator sub-form only on edit (not create) |
| Tag | `manage-tags.html` | None |
| Import | `imports/upload.html` | None |
