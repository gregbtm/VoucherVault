const VERSION = "__APP_VERSION__";
const CACHE_NAME = `vouchervault-${VERSION}`;
const RUNTIME_CACHE = `vouchervault-runtime-${VERSION}`;
const DATA_CACHE = `vouchervault-data-${VERSION}`;
const PAGE_CACHE = `vouchervault-pages-${VERSION}`;

// Cache expiration settings
const CACHE_DURATION = 48 * 60 * 60 * 1000; // 48 hours in milliseconds
const CACHE_KEY = 'offline_cache_timestamp';

// Static assets to cache on install (only truly static assets, not dynamic pages)
const SUPPORTED_LANGS = ['en', 'de', 'fr', 'it'];

const STATIC_CACHE_URLS = [
    '/offline/',
    ...SUPPORTED_LANGS.map(lang => `/${lang}/offline/`),
    '/static/assets/css/animations.css',
    '/static/assets/css/dark-mode.css',
    '/static/assets/css/style.css',
    '/static/assets/img/apple-icon-180.png',
    '/static/assets/img/apple-touch-icon.png',
    '/static/assets/img/favicon.ico',
    '/static/assets/img/logo.png',
    '/static/assets/img/logo.svg',
    '/static/assets/img/manifest-icon-192.maskable.png',
    '/static/assets/img/manifest-icon-192.png',
    '/static/assets/img/manifest-icon-512.maskable.png',
    '/static/assets/img/manifest-icon-512.png',
    '/static/assets/js/offline-sync.js',
    '/static/assets/js/page-cache-helper.js',
    '/static/assets/js/manual-cache.js',
    '/static/assets/js/main.js',
    '/static/assets/js/animations.js',
    '/static/assets/vendor/motion/motion.min.js',
    '/static/assets/vendor/bootstrap/css/bootstrap.min.css',
    '/static/assets/vendor/bootstrap-icons/bootstrap-icons.css',
    '/static/assets/vendor/bootstrap/js/bootstrap.bundle.min.js'
];

// Dynamic pages will be cached when visited via navigation handler

// API endpoints and pages to cache
const API_CACHE_PATTERNS = [
    /\/api\/get\/stats/,
    /\/api\//,
    /\/(en|de|fr|it)\/dashboard$/,
    /\/dashboard$/,
    /\/(en|de|fr|it)\/shared-items/,
    /\/shared-items/,
    /\/(en|de|fr|it)\/items\/view\/.+/, 
    /\/items\/view\/.+/,
    /\/(en|de|fr|it)\/items\/edit\/.+/, 
    /\/items\/edit\/.+/,
    /\/(en|de|fr|it)\/items\/view-image\/.+/, 
    /\/items\/view-image\/.+/,
    /^\/(en|de|fr|it)\/?$/,
    /^\/$/
];

// Pages that should always be cached when visited
const CACHE_PAGE_PATTERNS = [
    /\/(en|de|fr|it)\/items\//,
    /\/items\//,
    /\/(en|de|fr|it)\/dashboard/,
    /\/dashboard/,
    /\/(en|de|fr|it)\/shared-items/,
    /\/shared-items/,
    /^\/(en|de|fr|it)\/?$/,
    /^\/$/
];

/**
 * Check if the cached response for THIS request is expired, based on its
 * own sw-cached-time header - not some other entry's. The old version
 * checked only the first key returned by cache.keys() and used its age to
 * decide the fate of the entire PAGE_CACHE, so a single old entry could
 * evict everything (including a page cached seconds ago), and a single
 * fresh entry could keep genuinely stale pages being served.
 */
async function isCacheExpired(request) {
    try {
        const cache = await caches.open(PAGE_CACHE);
        const cachedResponse = await cache.match(request);

        if (!cachedResponse) {
            return true; // nothing cached for this request means it's "expired"
        }

        const cachedTime = cachedResponse.headers.get('sw-cached-time');
        if (!cachedTime) {
            console.log('[ServiceWorker] No timestamp on cached response, assuming expired');
            return true;
        }

        const age = Date.now() - parseInt(cachedTime, 10);
        const expired = age >= CACHE_DURATION;

        console.log(`[ServiceWorker] Cache age: ${Math.floor(age / 1000 / 60 / 60)}h ${Math.floor((age / 1000 / 60) % 60)}m, expired: ${expired}`);
        return expired;
    } catch (error) {
        console.error('[ServiceWorker] Error checking cache expiration:', error);
        return false; // Conservative: assume not expired on error
    }
}

/**
 * Evict just this request's own cache entry, not the whole PAGE_CACHE.
 */
async function clearExpiredCacheEntry(request) {
    try {
        const cache = await caches.open(PAGE_CACHE);
        await cache.delete(request);
    } catch (error) {
        console.error('[ServiceWorker] Error clearing expired cache entry:', error);
    }
}

/**
 * Deletes every PAGE_CACHE entry whose path ends with `path` (so a bare
 * '/dashboard' also matches '/en/dashboard', '/de/dashboard', etc).
 */
async function invalidatePath(path) {
    try {
        const cache = await caches.open(PAGE_CACHE);
        const keys = await cache.keys();
        const matches = keys.filter(req => new URL(req.url).pathname.endsWith(path));
        await Promise.all(matches.map(req => cache.delete(req)));
        console.log(`[ServiceWorker] Invalidated ${matches.length} cached entr${matches.length === 1 ? 'y' : 'ies'} for path:`, path);
    } catch (error) {
        console.error('[ServiceWorker] Error invalidating path:', path, error);
    }
}

// Install event - cache static assets
self.addEventListener("install", event => {
    console.log('[ServiceWorker] Installing v' + VERSION);
    self.skipWaiting();
    
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then(cache => {
                console.log('[ServiceWorker] Caching static assets and pages');
                return Promise.allSettled(
                    STATIC_CACHE_URLS.map(url => 
                        cache.add(url).catch(err => {
                            console.warn('[ServiceWorker] Failed to cache:', url, err);
                        })
                    )
                );
            })
            .then(() => {
                console.log('[ServiceWorker] Installation complete');
            })
            .catch(err => {
                console.error('[ServiceWorker] Cache installation failed:', err);
            })
    );
});

// Activate event - clean up old caches
self.addEventListener('activate', event => {
    console.log('[ServiceWorker] Activating v' + VERSION);
    event.waitUntil(
        caches.keys().then(cacheNames => {
            return Promise.all(
                cacheNames
                    .filter(cacheName => {
                        return cacheName.startsWith('vouchervault-') || 
                               cacheName.startsWith('django-pwa-');
                    })
                    .filter(cacheName => {
                        return cacheName !== CACHE_NAME && 
                               cacheName !== RUNTIME_CACHE && 
                               cacheName !== DATA_CACHE &&
                               cacheName !== PAGE_CACHE;
                    })
                    .map(cacheName => {
                        console.log('[ServiceWorker] Deleting old cache:', cacheName);
                        return caches.delete(cacheName);
                    })
            );
        })
    );
    return self.clients.claim();
});

// Fetch event - implement caching strategies
self.addEventListener("fetch", event => {
    const { request } = event;
    const url = new URL(request.url);

    // Skip cross-origin requests
    if (url.origin !== location.origin) {
        return;
    }

    // Only handle GET requests for caching
    if (request.method !== 'GET') {
        event.respondWith(fetch(request));
        return;
    }

    // CRITICAL: Skip caching for OIDC authentication URLs to preserve session state
    // OIDC flow requires server-side session state which breaks with cached responses
    if (url.pathname.includes('/oidc/') ||
        url.pathname.includes('/accounts/login') ||
        url.pathname.includes('/accounts/logout')) {
        console.log('[ServiceWorker] Bypassing cache for auth URL:', url.pathname);
        event.respondWith(fetch(request));
        return;
    }

    // Manual cache requests should always hit the network (fresh data)
    if (request.headers.get('X-Manual-Cache') === '1') {
        event.respondWith(fetch(request));
        return;
    }

    // Connectivity check: always go to network and never serve from cache
    const isPingPath = /^(\/((en|de|fr|it))\/)?ping\/$/.test(url.pathname);
    if (isPingPath || url.searchParams.has('ping')) {
        event.respondWith(
            fetch(request).catch(() => new Response('', {
                status: 503,
                statusText: 'Service Unavailable'
            }))
        );
        return;
    }

    const skipCachePaths = [
        '/items/create',
        '/items/edit',
        '/items/duplicate',
        '/items/delete',
        '/items/toggle_status',
        '/items/share',
        '/items/unshare',
        '/transactions/delete',
        '/user/edit/notifications',
        '/user/edit/preferences',
        '/verify-apprise-urls',
        '/download',
        '/logout',
        '/post-logout'
    ];

    const shouldSkipCache = skipCachePaths.some(path => url.pathname.includes(path));

    // Handle API requests and page data - Cache First (but check expiration)
    if (API_CACHE_PATTERNS.some(pattern => pattern.test(url.pathname))) {
        event.respondWith(
            (async () => {
                // Check if cache is expired
                const expired = await isCacheExpired(request);

                if (expired) {
                    console.log('[ServiceWorker] Cache expired, clearing and fetching fresh data:', url.pathname + url.search);
                    await clearExpiredCacheEntry(request);

                    // Go straight to network
                    try {
                        return await fetch(request);
                    } catch (error) {
                        // Network failed, fall back to offline page for navigation
                        if (request.mode === 'navigate') {
                            return caches.match('/offline/') || new Response('Offline', { status: 503 });
                        }
                        return new Response('Offline', { status: 503 });
                    }
                }

                // Cache is still valid, use it
                const cachedResponse = await caches.match(request);
                if (cachedResponse) {
                    console.log('[ServiceWorker] ✓ Serving API/page from cache:', url.pathname + url.search);
                    return cachedResponse;
                }

                // No cache, go to network
                try {
                    return await fetch(request);
                } catch (error) {
                    if (request.mode === 'navigate') {
                        return caches.match('/offline/') || new Response('Offline', { status: 503 });
                    }
                    return new Response('Offline', { status: 503 });
                }
            })()
        );
        return;
    }

    // Handle navigation requests - Cache First (but check expiration)
    if (request.mode === 'navigate') {
        console.log('[ServiceWorker] Navigation request:', url.pathname + url.search);

        const bypassCache = url.searchParams.has('sw-bypass');
        const normalizedUrl = (() => {
            const normalized = new URL(request.url);
            normalized.searchParams.delete('sw-bypass');
            const normalizedString = normalized.toString();
            return normalizedString.endsWith('?') ? normalizedString.slice(0, -1) : normalizedString;
        })();

        if (bypassCache) {
            event.respondWith(
                fetch(request).then(response => {
                    return response;
                }).catch(() => {
                    return caches.match(normalizedUrl).then(cachedResponse => {
                        if (cachedResponse) {
                            console.log('[ServiceWorker] ✓ Bypass fallback to cache:', url.pathname + url.search);
                            return cachedResponse;
                        }
                        const langMatch = url.pathname.match(/^\/(en|de|fr|it)/);
                        const offlineUrl = langMatch ? `/${langMatch[1]}/offline/` : '/offline/';
                        return caches.match(offlineUrl);
                    });
                })
            );
            return;
        }

        event.respondWith(
            (async () => {
                // Check if cache is expired
                const expired = await isCacheExpired(request);

                if (expired) {
                    console.log('[ServiceWorker] Cache expired for navigation, clearing:', url.pathname + url.search);
                    await clearExpiredCacheEntry(request);

                    // Try network first since cache is expired
                    try {
                        return await fetch(request);
                    } catch (error) {
                        console.log('[ServiceWorker] ✗ Network failed and cache expired, showing offline page');
                        const langMatch = url.pathname.match(/^\/(en|de|fr|it)/);
                        const offlineUrl = langMatch ? `/${langMatch[1]}/offline/` : '/offline/';
                        return caches.match(offlineUrl) || new Response(
                            '<html><body><h1>Offline</h1><p>You are currently offline and the cache has expired.</p></body></html>',
                            {
                                status: 503,
                                statusText: 'Service Unavailable',
                                headers: new Headers({ 'Content-Type': 'text/html' })
                            }
                        );
                    }
                }

                // Cache is still valid, try to use it
                // Try to match with full URL (including query params)
                let cachedResponse = await caches.match(request.url);
                
                if (cachedResponse) {
                    const isRedirect = cachedResponse.type === 'opaqueredirect' || (cachedResponse.status >= 300 && cachedResponse.status < 400);
                    if (!isRedirect) {
                        console.log('[ServiceWorker] ✓ Found in cache:', url.pathname + url.search);
                        return cachedResponse;
                    }
                    console.log('[ServiceWorker] ⊘ Ignoring cached redirect:', url.pathname + url.search);
                    cachedResponse = null;
                }

                console.log('[ServiceWorker] ✗ Not in cache:', url.pathname + url.search);

                // If URL has trailing ? with no params, try without it
                if (request.url.endsWith('?')) {
                    const urlWithoutQuestion = request.url.slice(0, -1);
                    console.log('[ServiceWorker] Trying without trailing ?:', urlWithoutQuestion);
                    const alt = await caches.match(urlWithoutQuestion);
                    if (alt) {
                        console.log('[ServiceWorker] ✓ Found alternative in cache');
                        return alt;
                    }
                }

                // For root page requests, check language-specific roots too
                if (url.pathname === '/' || url.pathname === '') {
                    console.log('[ServiceWorker] Root page requested, searching for language-specific root...');

                    const langCodes = [...SUPPORTED_LANGS];
                    const langPromises = langCodes.map(lang =>
                        caches.match(`/${lang}/`).then(res => ({ lang, res }))
                    );

                    const results = await Promise.all(langPromises);
                    const cached = results.find(r => r.res);
                    if (cached) {
                        console.log('[ServiceWorker] ✓ Found cached language root:', `/${cached.lang}/`);
                        return cached.res;
                    }
                }

                // No cache found, try network
                try {
                    return await fetch(request);
                } catch (error) {
                    console.log('[ServiceWorker] ✗ No cached page, showing offline page');

                    // Try to get language-specific offline page first
                    const langMatch = url.pathname.match(/^\/(en|de|fr|it)/);
                    const offlineUrl = langMatch ? `/${langMatch[1]}/offline/` : '/offline/';

                    const offlinePage = await caches.match(offlineUrl);
                    if (offlinePage) {
                        console.log('[ServiceWorker] ✓ Serving offline page:', offlineUrl);
                        return offlinePage;
                    }
                    
                    // Try generic offline page as fallback
                    const genericOffline = await caches.match('/offline/');
                    if (genericOffline) {
                        console.log('[ServiceWorker] ✓ Serving generic offline page');
                        return genericOffline;
                    }
                    
                    // Last resort: return a basic offline response
                    console.log('[ServiceWorker] ✗ No offline page cached, using fallback HTML');
                    return new Response(
                        '<html><body><h1>Offline</h1><p>You are currently offline and this page is not cached.</p></body></html>',
                        {
                            status: 503,
                            statusText: 'Service Unavailable',
                            headers: new Headers({ 'Content-Type': 'text/html' })
                        }
                    );
                }
            })()
        );
        return;
    }

    // Handle static assets - Cache First strategy
    if (request.destination === 'style' || 
        request.destination === 'script' || 
        request.destination === 'image' ||
        request.destination === 'font') {
        event.respondWith(
            caches.match(request)
                .then(cachedResponse => {
                    if (cachedResponse) {
                        return cachedResponse;
                    }
                    return fetch(request)
                        .then(response => {
                            // Cache the fetched resource (never expires)
                            const responseClone = response.clone();
                            caches.open(RUNTIME_CACHE).then(cache => {
                                cache.put(request, responseClone);
                            });
                            return response;
                        })
                        .catch(() => {
                            // Return a fallback for images
                            if (request.destination === 'image') {
                                return caches.match('/static/assets/img/logo.png');
                            }
                        });
                })
        );
        return;
    }

    // Default: Cache First with network fallback
    event.respondWith(
        caches.match(request).then(cached => {
            if (cached) {
                return cached;
            }
            return fetch(request).then(response => {
                return response;
            }).catch(() => {
                // Return a basic 503 response for uncached resources when offline
                console.log('[ServiceWorker] Resource not cached and offline:', url.pathname);
                return new Response('', {
                    status: 503,
                    statusText: 'Service Unavailable'
                });
            });
        })
    );
});

// Background sync for offline changes
self.addEventListener('sync', event => {
    console.log('[ServiceWorker] Background sync triggered:', event.tag);
    
    if (event.tag === 'sync-offline-changes') {
        event.waitUntil(syncOfflineChanges());
    }
});

// Handle messages from clients
self.addEventListener('message', event => {
    console.log('[ServiceWorker] Message received:', event.data);
    
    if (event.data && event.data.type === 'SKIP_WAITING') {
        self.skipWaiting();
    }
    
    if (event.data && event.data.type === 'CLEAR_CACHE') {
        event.waitUntil(
            caches.keys().then(cacheNames => {
                return Promise.all(
                    cacheNames
                        .filter(cacheName => cacheName.startsWith('vouchervault-'))
                        .map(cacheName => caches.delete(cacheName))
                );
            })
        );
    }

    if (event.data && event.data.type === 'INVALIDATE_PATH' && event.data.path) {
        event.waitUntil(invalidatePath(event.data.path));
    }

    if (event.data && event.data.type === 'RECORD_ITEM_VISIT' && event.data.url) {
        event.waitUntil(recordItemPageVisit(event.data.url));
    }
});

// Open the same IndexedDB the client-side OfflineSyncManager writes to
function openSyncDB() {
    return new Promise((resolve, reject) => {
        const req = indexedDB.open('VoucherVaultOfflineDB', 1);
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
        // If the DB doesn't exist yet (SW fired before any page visit) just
        // resolve with null — nothing to sync.
        req.onupgradeneeded = () => { req.transaction.abort(); resolve(null); };
    });
}

async function syncOfflineChanges() {
    console.log('[ServiceWorker] Background sync: replaying offline queue...');
    let db;
    try {
        db = await openSyncDB();
    } catch (e) {
        console.warn('[ServiceWorker] Could not open sync DB:', e);
        return;
    }
    if (!db) return;

    const tx = db.transaction(['syncQueue'], 'readwrite');
    const store = tx.objectStore('syncQueue');
    const idx = store.index('status');

    const pending = await new Promise((resolve, reject) => {
        const req = idx.getAll('pending');
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
    });

    if (!pending.length) {
        console.log('[ServiceWorker] No pending offline changes.');
        return;
    }

    console.log(`[ServiceWorker] Replaying ${pending.length} queued changes.`);
    for (const item of pending) {
        try {
            const init = { method: item.method || 'POST' };
            if (item.data) {
                init.headers = { 'Content-Type': 'application/json' };
                init.body = JSON.stringify(item.data);
            }
            const resp = await fetch(item.url, init);
            const updTx = db.transaction(['syncQueue'], 'readwrite');
            const updStore = updTx.objectStore('syncQueue');
            item.status = resp.ok ? 'synced' : 'failed';
            item.retries = (item.retries || 0) + 1;
            updStore.put(item);
            console.log(`[ServiceWorker] Replayed ${item.url}: ${resp.status}`);
        } catch (err) {
            console.warn('[ServiceWorker] Replay failed for', item.url, err);
        }
    }
}

// Offline item pre-caching — store last N viewed item page URLs in SW storage
const OFFLINE_ITEM_CACHE_KEY = 'vv-offline-items';
const OFFLINE_ITEM_MAX = 10;

async function recordItemPageVisit(url) {
    try {
        const db = await openItemHistoryDB();
        const tx = db.transaction('history', 'readwrite');
        const store = tx.objectStore('history');
        const existing = await idbGet(store, 'urls') || [];
        const filtered = existing.filter(u => u !== url);
        const updated = [url, ...filtered].slice(0, OFFLINE_ITEM_MAX);
        await idbPut(store, 'urls', updated);
        await tx.done;
        const cache = await caches.open(PAGE_CACHE);
        await cache.add(url).catch(() => {});
    } catch (e) {
        // IndexedDB may not be available in all browsers/contexts
    }
}

function openItemHistoryDB() {
    return new Promise((resolve, reject) => {
        const req = indexedDB.open('vv-item-history', 1);
        req.onupgradeneeded = e => e.target.result.createObjectStore('history');
        req.onsuccess = e => resolve(e.target.result);
        req.onerror = e => reject(e.target.error);
    });
}

function idbGet(store, key) {
    return new Promise((resolve, reject) => {
        const req = store.get(key);
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
    });
}

function idbPut(store, key, value) {
    return new Promise((resolve, reject) => {
        const req = store.put(value, key);
        req.onsuccess = () => resolve();
        req.onerror = () => reject(req.error);
    });
}

// Web Push - show a notification for incoming push messages
self.addEventListener('push', event => {
    let payload = { title: 'VoucherVault', body: '' };
    if (event.data) {
        try {
            payload = { ...payload, ...event.data.json() };
        } catch (error) {
            payload.body = event.data.text();
        }
    }

    const notifOptions = {
        body: payload.body,
        icon: '/static/assets/img/manifest-icon-192.png',
        badge: '/static/assets/img/manifest-icon-192.png',
        data: {
            url: payload.url || '/',
            mark_used_url: payload.mark_used_url || null,
        },
        actions: [
            { action: 'view', title: 'View' },
        ],
    };
    if (payload.mark_used_url) {
        notifOptions.actions.push({ action: 'mark_used', title: 'Mark used' });
    }
    if (payload.image_url) {
        notifOptions.image = payload.image_url;
    }

    event.waitUntil(self.registration.showNotification(payload.title, notifOptions));
});

// Web Push - handle notification click and action buttons
self.addEventListener('notificationclick', event => {
    event.notification.close();
    const data = event.notification.data || {};
    const action = event.action;

    if (action === 'mark_used' && data.mark_used_url) {
        event.waitUntil(
            fetch(data.mark_used_url, { method: 'POST', credentials: 'include' })
                .then(() => self.clients.openWindow(data.url || '/'))
                .catch(() => self.clients.openWindow(data.url || '/'))
        );
        return;
    }

    const targetUrl = data.url || '/';
    event.waitUntil(
        self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
            for (const client of clientList) {
                if (client.url === targetUrl && 'focus' in client) {
                    return client.focus();
                }
            }
            if (self.clients.openWindow) {
                return self.clients.openWindow(targetUrl);
            }
        })
    );
});
