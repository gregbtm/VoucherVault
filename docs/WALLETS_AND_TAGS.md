# Wallets & Tags

Wallets and tags are the two ways to organise your items in VoucherVault Plus+. They serve different purposes and work well together.

## Wallets

A wallet is a named group — think of it like a physical card wallet or a drawer. Items belong to exactly one wallet (or none). Good uses:

- Group by retailer type: **Supermarkets**, **Restaurants**, **Travel**
- Group by purpose: **Christmas Gifts**, **Work Expenses**
- Group by person: **My Cards**, **Partner's Cards**

### Shared wallets

A wallet can be shared with other registered users. Anyone in a shared wallet can see all items in it. Share a wallet at **Manage Wallets → Share**. Useful for a household where both partners want to see the same gift cards.

### Next Up widget

Any wallet can be selected for the **Next Up** widget (Preferences → Inventory Widgets). The widget shows the soonest-expiring items from those wallets at the top of Inventory — useful for a "Train Tickets" wallet that always surfaces the next ticket to use.

### Wallet-level Firefly rule

If you use the Firefly III integration, you can pin a Firefly notification rule to a wallet. All items in that wallet will use it as the default unless the item itself overrides it.

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
- **Search bar** — full-text search across name, issuer, and description
- **Sort** controls — sort by name, value, expiry, or last used

Filters combine — you can show gift cards in the Supermarkets wallet that expire soonest, for example.

## Import and export

When importing from CSV (Catima or VoucherVault format), the `wallet` and `tags` columns are respected. When exporting to JSON, wallets and tags are included in the export and re-imported on restore.
