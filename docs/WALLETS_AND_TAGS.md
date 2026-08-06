# Wallets & Tags

Wallets and tags are the two ways to organise your items in VoucherVault Plus+. They serve different purposes and work well together.

## Wallets

A wallet is a named group — think of it like a physical card wallet or a drawer. Items belong to exactly one wallet (or none). Good uses:

- Group by retailer type: **Supermarkets**, **Restaurants**, **Travel**
- Group by purpose: **Christmas Gifts**, **Work Expenses**
- Group by person: **My Cards**, **Partner's Cards**

### Shared wallets

A wallet can be shared with other registered users, as either an **Editor** (can view, add, edit, and delete every item in the wallet) or a **Viewer** (read-only). Share a wallet at **Manage Wallets → Share**, entering the collaborator's exact username and picking their role. Useful for a household where both partners want to see - and for an Editor, manage - the same gift cards. Both being added to, and removed from, a shared wallet send the other person a notification (if they have a matching notification rule set up).

## Sharing individual items

Rather than an entire wallet, you can share a single item with one other registered user from that item's page: **More actions → Share with Users**. Same idea as sharing a wallet - an exact username and an Editor/Viewer role choice - just scoped to one item instead of everything in a wallet. The owner can see (and revoke) everyone an item is shared with from the item's own page; both sharing and unsharing notify the other person the same way a wallet invite does.

This is different from the **public share link** ("Share via…" button, see [Sharing settings](SHARING_SETTINGS.md)): sharing with a user gives *that specific VoucherVault account* ongoing access, while a public share link is an anonymous, no-account-needed, read-only page you hand to anyone.

### Comments on a shared item

Any item's detail page has a comments section — dated, attributed notes any collaborator the item is shared with can read and add (owner, Editor, or read-only Viewer alike), useful for "already used the code once" or "PIN is on the back of the receipt" style notes that shouldn't overwrite the item's own Notes field. A comment can be deleted by whoever wrote it or by the item's owner; anyone else's delete attempt is rejected. This is separate from the single-value **Notes** field on the item itself — comments are append-only and keep every collaborator's message intact instead of the next edit clobbering the last one.

### Next Up widget

Any wallet can be selected for the **Next Up** widget (Preferences → Inventory Widgets). The widget shows the soonest-expiring items from those wallets at the top of Inventory — useful for a "Train Tickets" wallet that always surfaces the next ticket to use.

### Wallet-level Firefly rule

If you use the Firefly III integration, you can pin a Firefly notification rule to a wallet. All items in that wallet will use it as the default unless the item itself overrides it.

### Wallet budgets

Set a **Budget Amount** on a wallet (**Manage Wallets → edit a wallet**) to track monthly spend against it. The dashboard/analytics page shows a progress-bar badge of this month's spend against the budget. Going over budget also fires a **Wallet Budget Exceeded** notification/webhook once per calendar month (subscribe a rule to it the same way as any other event — see [Notifications](./NOTIFICATIONS_SETUP.md)), so you don't have to remember to check the page. Leave the field blank for wallets you don't want to budget.

## Tags

Tags are free-form labels — an item can have any number of them. Good uses:

- Status labels: **Used**, **Gifted**, **Pending activation**
- Occasion labels: **Birthday**, **Anniversary**, **Emergency**
- Feature flags: **Has PIN**, **Online only**, **Contactless**

Tags appear on item cards in the Inventory view and can be filtered from the tag dropdown at the top of the Inventory page.

### Managing tags

Go to **Manage Tags** in the sidebar to rename or delete tags. Renaming a tag renames it on all items that carry it. Deleting a tag removes it from all items.

## Filtering the inventory

The Inventory page has a filter bar at the top:

- **Wallet** dropdown — show only items in one wallet
- **Tag** dropdown — show only items with a specific tag
- **Search bar** — full-text search across name, issuer, redeem code, card number, description, notes, tags, and any text OCR-extracted from a document attached to the item (e.g. a scanned receipt)
- **Sort** controls — sort by name, value, expiry, or last used

Filters combine — you can show gift cards in the Supermarkets wallet that expire soonest, for example.

## Import and export

When importing from CSV (Catima or VoucherVault format), the `wallet` and `tags` columns are respected. When exporting to JSON, wallets and tags are included in the export and re-imported on restore.
