# PWA & Offline Mode

VoucherVault Plus+ is a Progressive Web App (PWA). You can install it on any device and use it without a network connection for day-to-day browsing, with offline changes syncing back automatically when connectivity returns.

## Installing the app

### Android (Chrome / Edge)

1. Open VoucherVault in Chrome or Edge.
2. Tap the **Install** button in the address bar, or use the three-dot menu and choose **Add to Home Screen**.
3. Confirm the installation prompt.

The app opens in its own window, without browser chrome, and appears as a standalone icon on your home screen or app drawer.

### iOS (Safari)

1. Open VoucherVault in Safari.
2. Tap the **Share** icon (the box with an arrow).
3. Scroll down and tap **Add to Home Screen**.
4. Confirm.

### Desktop (Chrome / Edge)

1. Open VoucherVault in Chrome or Edge.
2. Click the install icon in the address bar (a computer with a down-arrow) or go to the three-dot menu and choose **Install VoucherVault**.

## Offline browsing

Once installed, the service worker caches the app shell, static assets, and your item list on first load. When you open the app without a network connection:

- Your full item inventory is available to browse.
- Individual item detail pages load from cache.
- Barcode display works offline — useful at the checkout.

The cache is updated in the background whenever you open the app with a connection, so you always have a recent snapshot.

## Offline sync (Background Sync)

If you make changes (add, edit, or delete items) while offline, they are queued in the browser's local storage and replayed automatically as soon as connectivity is restored — even if the app is closed.

### How it works

1. When a form is submitted offline, the request is intercepted by the service worker and placed in a **sync queue** (stored in IndexedDB under `VoucherVaultOfflineDB`).
2. The service worker registers a **Background Sync** tag (`sync-offline-changes`) with the browser.
3. When the browser regains connectivity, it calls back into the service worker with this tag, which then replays each queued request against the live server in order.
4. Each queued item is marked `synced` (success) or `failed` (error from the server — e.g. a conflict) so you can inspect state if needed.

> **Note:** Background Sync requires Chrome or Edge on Android. Safari and Firefox do not yet support the Background Sync API; on those browsers, queued changes are replayed the next time you open the app manually with a connection.

## Share to VoucherVault (Android)

On Android, VoucherVault appears in the system Share sheet. You can share:

- **Web page URLs** — e.g. a retailer's voucher page; the URL is pre-filled in the item create form.
- **Text** — e.g. a promo code; pre-filled as the item name or redeem code.
- **Images** — e.g. a screenshot of a barcode; the image is attached for scanning.

To use it:

1. In any Android app, tap **Share**.
2. Choose **VoucherVault** from the share sheet.
3. A new item form opens, pre-filled with the shared content. Review and save.

The share target uses a secure POST + multipart upload, so images are transmitted safely rather than in a URL parameter.

## Troubleshooting

- **App won't install** — the install prompt only appears over HTTPS. If you're on a local network deployment, you need a valid TLS certificate (or a `localhost` origin) for PWA install to work.
- **Stale data showing after a change** — force-refresh the page (pull-to-refresh on mobile) to trigger a cache update, or clear the app's storage in browser settings.
- **Offline queue not syncing** — check that the browser hasn't restricted background activity for the app. On Android, ensure battery optimisation for the browser is disabled or excluded.
- **Share target not appearing** — the app must be installed (added to home screen) for it to appear in the Android share sheet. It won't appear when using the browser directly.
