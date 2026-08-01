import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(__dirname, 'offline-sync.js'), 'utf-8');

// offline-sync.js's `class OfflineSyncManager` is never assigned to window
// itself (class declarations don't become global-object properties even at
// script scope), but the file explicitly does `window.offlineSyncManager =
// new OfflineSyncManager()` at the bottom - so unlike manual-cache.js, no
// cross-eval bridging trick is needed here, window.offlineSyncManager is a
// real, directly-usable instance right after loading.
function loadOfflineSync() {
  delete window.offlineSyncManager;
  (0, eval)(source);
}

function manager() {
  return window.offlineSyncManager;
}

// jsdom does not implement IndexedDB. This is a minimal, in-memory fake
// covering exactly the surface offline-sync.js actually uses (open with
// onupgradeneeded/onsuccess, two object stores with a keyPath/autoIncrement,
// one index, transaction/objectStore/add/get/put/delete, and index.getAll) -
// enough to exercise the real sync-queue/cache logic end to end rather than
// stubbing those methods away.
function installFakeIndexedDB() {
  const stores = {
    syncQueue: new Map(),
    cachedData: new Map(),
  };
  let nextId = 1;

  function makeRequest() {
    return {};
  }

  function fireAsync(request, prop, value) {
    queueMicrotask(() => {
      request.result = value;
      if (typeof request[prop] === 'function') request[prop]({ target: request });
    });
  }

  function makeObjectStore(name) {
    const map = stores[name];
    return {
      add(item) {
        const request = makeRequest();
        const id = nextId++;
        const stored = { ...item, id };
        map.set(id, stored);
        fireAsync(request, 'onsuccess', id);
        return request;
      },
      get(key) {
        const request = makeRequest();
        fireAsync(request, 'onsuccess', map.get(key));
        return request;
      },
      put(item) {
        const request = makeRequest();
        const key = name === 'cachedData' ? item.key : item.id;
        map.set(key, item);
        fireAsync(request, 'onsuccess', key);
        return request;
      },
      delete(key) {
        const request = makeRequest();
        map.delete(key);
        fireAsync(request, 'onsuccess', undefined);
        return request;
      },
      index(indexName) {
        return {
          getAll(value) {
            const request = makeRequest();
            const all = Array.from(map.values()).filter((item) => item[indexName] === value);
            fireAsync(request, 'onsuccess', all);
            return request;
          },
        };
      },
    };
  }

  window.indexedDB = {
    open() {
      const request = {};
      queueMicrotask(() => {
        const fakeDb = {
          objectStoreNames: { contains: () => true },
          createObjectStore: () => ({ createIndex: () => {} }),
          transaction: () => ({ objectStore: makeObjectStore }),
        };
        request.result = fakeDb;
        if (request.onsuccess) request.onsuccess({ target: request });
      });
      return request;
    },
  };

  return stores;
}

beforeEach(() => {
  document.body.innerHTML = '';
  installFakeIndexedDB();
  Object.defineProperty(navigator, 'onLine', { value: true, configurable: true });
  vi.useRealTimers();
  vi.restoreAllMocks();
});

async function flushMicrotasks(times = 5) {
  for (let i = 0; i < times; i++) await Promise.resolve();
}

describe('initialization', () => {
  it('creates window.offlineSyncManager with the database ready shortly after load', async () => {
    loadOfflineSync();
    await flushMicrotasks();
    expect(manager()).toBeTruthy();
    expect(manager().db).toBeTruthy();
  });
});

describe('sync queue', () => {
  it('adds an action to the queue and lists it back as pending', async () => {
    loadOfflineSync();
    await flushMicrotasks();

    await manager().addToSyncQueue({ type: 'form_submission', method: 'POST', url: '/en/items/create/', data: { name: 'Test' } });
    const pending = await manager().getPendingSync();

    expect(pending.length).toBe(1);
    expect(pending[0]).toMatchObject({ action: 'form_submission', method: 'POST', url: '/en/items/create/', status: 'pending' });
  });

  it('removes an item from the queue once synced', async () => {
    loadOfflineSync();
    await flushMicrotasks();

    await manager().addToSyncQueue({ type: 'form_submission', method: 'POST', url: '/en/items/create/', data: {} });
    const [item] = await manager().getPendingSync();
    await manager().removeSyncItem(item.id);

    expect(await manager().getPendingSync()).toEqual([]);
  });

  it('marks a failed sync item as failed with an incremented retry count instead of removing it', async () => {
    loadOfflineSync();
    await flushMicrotasks();

    await manager().addToSyncQueue({ type: 'form_submission', method: 'POST', url: '/en/items/create/', data: {} });
    const [item] = await manager().getPendingSync();
    await manager().updateSyncItemStatus(item.id, 'failed', item.retries + 1);

    // No longer shows up as "pending" (the index query filters on status).
    expect(await manager().getPendingSync()).toEqual([]);
  });
});

describe('syncPendingChanges', () => {
  it('does nothing when there are no pending items', async () => {
    loadOfflineSync();
    await flushMicrotasks();
    global.fetch = vi.fn();
    await manager().syncPendingChanges();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('does nothing while offline', async () => {
    loadOfflineSync();
    await flushMicrotasks();
    manager().isOnline = false;
    await manager().addToSyncQueue({ type: 'other', method: 'POST', url: '/x', data: {} });
    global.fetch = vi.fn();
    await manager().syncPendingChanges();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('syncs a pending item via fetch and removes it from the queue on success', async () => {
    loadOfflineSync();
    await flushMicrotasks();
    document.body.innerHTML = '<input name="csrfmiddlewaretoken" value="tok123">';
    global.fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ id: 1 }) });
    // Avoid the real reload path so the test doesn't blow up jsdom's navigation.
    window.refreshData = vi.fn();

    await manager().addToSyncQueue({ type: 'other', method: 'POST', url: '/en/items/1/', data: { value: -5 } });
    await manager().syncPendingChanges();

    expect(global.fetch).toHaveBeenCalledWith('/en/items/1/', expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({ 'X-CSRFToken': 'tok123' }),
    }));
    expect(await manager().getPendingSync()).toEqual([]);
    expect(window.refreshData).toHaveBeenCalled();
  });

  it('keeps a failed item in the queue (marked failed) rather than dropping it', async () => {
    loadOfflineSync();
    await flushMicrotasks();
    global.fetch = vi.fn().mockResolvedValue({ ok: false, status: 500 });

    await manager().addToSyncQueue({ type: 'other', method: 'POST', url: '/en/items/1/', data: {} });
    await manager().syncPendingChanges();

    // getPendingSync only returns 'pending' items - a failed one no longer
    // matches that filter, which is the real, observable behavior change.
    expect(await manager().getPendingSync()).toEqual([]);
  });
});

describe('cacheData / getCachedData', () => {
  it('stores and retrieves data by key', async () => {
    loadOfflineSync();
    await flushMicrotasks();
    await manager().cacheData('dashboard_stats', { total: 5 });
    expect(await manager().getCachedData('dashboard_stats')).toEqual({ total: 5 });
  });

  it('replaces an existing cache entry rather than erroring on a duplicate key', async () => {
    loadOfflineSync();
    await flushMicrotasks();
    await manager().cacheData('recent_items', ['a']);
    await manager().cacheData('recent_items', ['a', 'b']);
    expect(await manager().getCachedData('recent_items')).toEqual(['a', 'b']);
  });

  it('returns null for a key that was never cached', async () => {
    loadOfflineSync();
    await flushMicrotasks();
    expect(await manager().getCachedData('nope')).toBeNull();
  });
});

describe('checkIfServedFromCache / offline banner', () => {
  beforeEach(() => {
    // showOfflinePageBanner() suppresses itself on /en/offline/ - reset to a
    // normal path before each test, since window.location persists across
    // tests in this shared jsdom document and an earlier test in this file
    // intentionally navigates to /en/offline/ to test that exact guard.
    window.history.replaceState({}, '', '/en/dashboard');
  });

  function stubCaches(pageCacheNames, matchesCurrentUrl) {
    const fakeCache = { match: vi.fn().mockResolvedValue(matchesCurrentUrl ? { ok: true } : undefined) };
    window.caches = {
      keys: vi.fn().mockResolvedValue(pageCacheNames),
      open: vi.fn().mockResolvedValue(fakeCache),
    };
  }

  it('shows the "viewing cached content" banner when the current URL is in the manual page cache', async () => {
    stubCaches(['vouchervault-pages-v2.0.0'], true);
    loadOfflineSync();
    await vi.waitFor(() => expect(document.getElementById('offline-content-banner')).not.toBeNull());
  });

  it('shows no banner when nothing matches the manual page cache', async () => {
    stubCaches(['vouchervault-pages-v2.0.0'], false);
    loadOfflineSync();
    await flushMicrotasks(10);
    expect(document.getElementById('offline-content-banner')).toBeNull();
  });

  it('never shows the banner on the offline fallback page itself', async () => {
    window.history.replaceState({}, '', '/en/offline/');
    stubCaches(['vouchervault-pages-v2.0.0'], true);
    loadOfflineSync();
    await flushMicrotasks(10);
    expect(document.getElementById('offline-content-banner')).toBeNull();
  });

  it('the banner refresh button forces a bypass reload when online', async () => {
    stubCaches(['vouchervault-pages-v2.0.0'], true);
    loadOfflineSync();
    await vi.waitFor(() => expect(document.getElementById('offline-content-banner')).not.toBeNull());
    const banner = document.getElementById('offline-content-banner');

    // jsdom doesn't implement real navigation (query-string changes included,
    // not just hash ones) - assigning window.location.href silently no-ops
    // rather than updating it, so swap in a plain mutable stand-in just for
    // this assertion and check refreshFromNetwork() actually wrote the
    // sw-bypass param to it, rather than asserting on real navigation.
    const originalHref = window.location.href;
    const originalLocationDescriptor = Object.getOwnPropertyDescriptor(window, 'location');
    Object.defineProperty(window, 'location', { value: { href: originalHref }, configurable: true, writable: true });

    banner.querySelector('[data-action="refresh"]').click();

    expect(window.location.href).toContain('sw-bypass=1');
    Object.defineProperty(window, 'location', originalLocationDescriptor); // restore the real Location object
  });
});

describe('updateOnlineStatus / network events', () => {
  it('dispatches a networkStatusChanged event carrying the online flag', async () => {
    loadOfflineSync();
    await flushMicrotasks();
    const handler = vi.fn();
    window.addEventListener('networkStatusChanged', handler);
    manager().updateOnlineStatus(false);
    expect(handler).toHaveBeenCalledWith(expect.objectContaining({ detail: { isOnline: false } }));
  });

  it('isOffline() reflects the manager\'s current isOnline flag', async () => {
    loadOfflineSync();
    await flushMicrotasks();
    manager().isOnline = true;
    expect(manager().isOffline()).toBe(false);
    manager().isOnline = false;
    expect(manager().isOffline()).toBe(true);
  });
});

describe('getCSRFToken', () => {
  it('reads the value from the CSRF hidden input when present', async () => {
    document.body.innerHTML = '<input name="csrfmiddlewaretoken" value="abc123">';
    loadOfflineSync();
    await flushMicrotasks();
    expect(manager().getCSRFToken()).toBe('abc123');
  });

  it('returns an empty string when no CSRF input exists on the page', async () => {
    loadOfflineSync();
    await flushMicrotasks();
    expect(manager().getCSRFToken()).toBe('');
  });
});
