# Notification Rules & Expiry Alerts

VoucherVault Plus+ can push a notification when one of your items is approaching expiry — before the day arrives, not after. Rules live at **Notifications → Rules** in the sidebar.

## How it works

A notification rule defines:

- **Backend** — where the alert goes (ntfy, webhook, apprise, web push, or Firefly III)
- **Trigger** — what event to fire on: item expiring soon, already expired, or a Next Up item due today
- **Lead time** — how many days before expiry to send the alert (the global default; individual items can override it)
- **Enabled** — a quick on/off switch without deleting the rule

Celery Beat runs the check once per day. When an item's expiry date is within the rule's threshold, the backend fires and the result is logged at **Notifications → Log**.

## Deduplication

A successful send is recorded. If Celery runs again the same day (or you have multiple rules), the same alert will not fire twice for the same item and event type. To force a re-send, delete the log entry for that item.

## Backends

### ntfy

ntfy is a free, open-source push-notification service. Point the rule at your topic URL (e.g. `https://ntfy.sh/my-private-topic`) and subscribe in the ntfy app on your phone. Works offline-first — no account required for self-hosted ntfy.

### Webhook

Posts a JSON payload to any URL. Useful for piping alerts into n8n, Zapier, IFTTT, or your own endpoint. The payload includes item name, expiry date, days remaining, and the issuer.

### Apprise

Supports 60+ notification services (Slack, Telegram, Discord, Matrix, email, …) via a single URL format. See [Apprise wiki](https://github.com/caronc/apprise/wiki) for the full list of URL schemas. Set the Apprise URL in Site Settings under the Notifications section.

### Web Push

Delivers a browser notification to this device. Browser push must be enabled once per device via **Notifications → Rules → Enable web push on this device**. Works even when the tab is closed, as long as the browser is running.

### Firefly III

Instead of pushing a notification, this backend posts a withdrawal transaction to your Firefly III ledger when an item is marked Used. Requires a Firefly III notification rule to be set up and an account ID linked to the item. See the [Firefly III guide](./FIREFLY_III_SETUP.md) for setup.

## Per-item override

On any item's edit form, the **Notify Me Before Expiry** field overrides the rule's global lead time for that specific item. Leave it blank to use the rule default.

## "Next Up Item Due Today" trigger

Add a rule with this event type to receive a morning alert on the day an item in your Next Up widget is valid. Set up the Next Up widget first in **Preferences → Inventory Widgets**.

## Troubleshooting

- **No notifications arriving** — check the Notification Log for error details. Common causes: wrong ntfy topic URL, a firewall blocking outbound requests, or the Celery Beat container not running.
- **Duplicate notifications** — each (item, event_type, rule) combination deduplicates within a day. If you see duplicates across days, check whether the log entry for the previous send was deleted.
- **Rule fires for expired items only** — set the lead time to a positive number of days; 0 means "fire on the expiry day itself".
