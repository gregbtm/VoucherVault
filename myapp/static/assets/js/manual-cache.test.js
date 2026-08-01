import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(__dirname, 'manual-cache.js'), 'utf-8');

// manual-cache.js declares `let manualCacheManager` at top level (not
// `var`/an explicit window.* assignment), so unlike a function declaration
// it never becomes a window property when run via indirect eval - it only
// lives in the shared global lexical environment indirect eval writes to.
// Bridging it onto window right after load (itself via another indirect
// eval, since this test file is an ES module and can't see that lexical
// environment directly) lets the tests get a handle on the real instance,
// same object a real page's inline scripts would see.
function loadManualCache() {
  delete window.__bridge_manualCacheManager;
  // The bridge statement must run in the exact same eval() call as the
  // source - a separate eval() call does not reliably see the first
  // call's top-level `let manualCacheManager` binding in this environment.
  (0, eval)(source + '\nwindow.__bridge_manualCacheManager = manualCacheManager;');
}

function manager() {
  return window.__bridge_manualCacheManager;
}

function stubBootstrapToast() {
  // A real constructor function, not vi.fn()/an arrow - vi.fn()'s `new`
  // handling doesn't reliably honor an implementation's returned object the
  // way plain `new` semantics do, so a spy-based Toast silently produces an
  // instance missing .show() instead of the stubbed one.
  function FakeToast(el) {
    this.el = el;
    this.show = vi.fn();
  }
  window.bootstrap = { Toast: FakeToast };
}

function stubCaches(cacheNames = [], cacheKeys = []) {
  const fakeCache = {
    keys: vi.fn().mockResolvedValue(cacheKeys),
    put: vi.fn().mockResolvedValue(undefined),
  };
  window.caches = {
    open: vi.fn().mockResolvedValue(fakeCache),
    keys: vi.fn().mockResolvedValue(cacheNames),
    delete: vi.fn().mockResolvedValue(true),
  };
  return { fakeCache };
}

beforeEach(() => {
  document.body.innerHTML = '';
  localStorage.clear();
  stubBootstrapToast();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('isCacheValid / getRemainingTime', () => {
  it('is invalid when no timestamp has ever been recorded', async () => {
    stubCaches();
    loadManualCache();
    expect(await manager().isCacheValid()).toBe(false);
  });

  it('is invalid once the 48-hour window has elapsed, even with cache entries present', async () => {
    localStorage.setItem('offline_cache_timestamp', String(Date.now() - 49 * 60 * 60 * 1000));
    stubCaches([], ['/en/']);
    loadManualCache();
    expect(await manager().isCacheValid()).toBe(false);
  });

  it('is invalid if the timestamp is fresh but the cache itself is empty', async () => {
    localStorage.setItem('offline_cache_timestamp', String(Date.now()));
    stubCaches([], []);
    loadManualCache();
    expect(await manager().isCacheValid()).toBe(false);
  });

  it('is valid with a fresh timestamp and at least one cached entry', async () => {
    localStorage.setItem('offline_cache_timestamp', String(Date.now()));
    stubCaches([], ['/en/']);
    loadManualCache();
    expect(await manager().isCacheValid()).toBe(true);
  });

  it('computes remaining time from the stored timestamp', () => {
    // 90 minutes ago, not exactly on an hour boundary - avoids a flaky
    // off-by-one from the test's own few-ms execution time nudging the
    // computed age across an exact-hour line (e.g. 60 min ago landing at
    // 60min + 2ms by the time getRemainingTime() actually runs).
    const ninetyMinutesAgo = Date.now() - 90 * 60 * 1000;
    localStorage.setItem('offline_cache_timestamp', String(ninetyMinutesAgo));
    stubCaches();
    loadManualCache();
    const { hours } = manager().getRemainingTime();
    expect(hours).toBe(46); // 48h duration minus 1.5h elapsed, floored
  });

  it('never returns a negative remaining time for an old timestamp', () => {
    localStorage.setItem('offline_cache_timestamp', String(Date.now() - 100 * 60 * 60 * 1000));
    stubCaches();
    loadManualCache();
    const remaining = manager().getRemainingTime();
    expect(remaining).toEqual({ hours: 0, minutes: 0, seconds: 0 });
  });
});

describe('updateCacheStatus', () => {
  it('shows the remaining time and "Refresh Cache" label when valid', async () => {
    document.body.innerHTML = '<span id="cache-status"></span><span id="cache-button-text"></span>';
    localStorage.setItem('offline_cache_timestamp', String(Date.now()));
    stubCaches([], ['/en/']);
    loadManualCache();
    await manager().updateCacheStatus();
    expect(document.getElementById('cache-status').textContent).toMatch(/\d+h \d+m \d+s left/);
    expect(document.getElementById('cache-button-text').textContent).toBe('Refresh Cache');
  });

  it('shows "Not cached" and clears a stale timestamp when invalid', async () => {
    document.body.innerHTML = '<span id="cache-status"></span><span id="cache-button-text"></span>';
    localStorage.setItem('offline_cache_timestamp', String(Date.now() - 100 * 60 * 60 * 1000));
    stubCaches([], []);
    loadManualCache();
    await manager().updateCacheStatus();
    expect(document.getElementById('cache-status').textContent).toBe('Not cached');
    expect(document.getElementById('cache-button-text').textContent).toBe('Cache for Offline');
    expect(localStorage.getItem('offline_cache_timestamp')).toBeNull();
  });

  it('does nothing if the status element is not present on the page', async () => {
    stubCaches();
    loadManualCache();
    await expect(manager().updateCacheStatus()).resolves.toBeUndefined();
  });
});

describe('clearCache', () => {
  it('deletes every vouchervault-named cache and clears the stored timestamp', async () => {
    localStorage.setItem('offline_cache_timestamp', '12345');
    stubCaches(['vouchervault-pages-v2.0.0', 'vouchervault-pages-v1.0.0', 'some-other-cache']);
    loadManualCache();

    await manager().clearCache();

    expect(window.caches.delete).toHaveBeenCalledWith('vouchervault-pages-v2.0.0');
    expect(window.caches.delete).toHaveBeenCalledWith('vouchervault-pages-v1.0.0');
    expect(window.caches.delete).not.toHaveBeenCalledWith('some-other-cache');
    expect(localStorage.getItem('offline_cache_timestamp')).toBeNull();
  });

  it('shows a success toast naming how many caches were cleared', async () => {
    stubCaches(['vouchervault-pages-v2.0.0']);
    loadManualCache();
    const showToastSpy = vi.spyOn(manager(), 'showToast');
    await manager().clearCache();
    expect(showToastSpy).toHaveBeenCalledWith('Success', expect.stringContaining('1 cache cleared'), 'success');
  });
});

describe('handlePrefsSavedSignal', () => {
  function setLocation(search) {
    window.history.replaceState({}, '', `/en/site-settings/${search}`);
  }

  it('does nothing when the prefs_saved query param is absent', () => {
    setLocation('');
    stubCaches();
    loadManualCache();
    const clearCacheSpy = vi.spyOn(manager(), 'clearCache');
    window.handlePrefsSavedSignal();
    expect(clearCacheSpy).not.toHaveBeenCalled();
  });

  it('purges the cache and strips both params when cache_purge=1 is present', async () => {
    // Asserts clearCache's real, observable side effect (every
    // vouchervault-named cache actually gets deleted) rather than spying on
    // the method call itself - handlePrefsSavedSignal's own async clearCache()
    // call isn't awaited by anything the test can hook into, so checking the
    // real effect is both simpler and more direct than racing a spy against it.
    setLocation('?prefs_saved=1&cache_purge=1');
    stubCaches(['vouchervault-pages-v2.0.0']);
    loadManualCache();
    window.handlePrefsSavedSignal();
    await vi.waitFor(() => expect(window.caches.delete).toHaveBeenCalledWith('vouchervault-pages-v2.0.0'));
    expect(window.location.search).toBe('');
  });

  it('strips prefs_saved without purging when cache_purge is absent', () => {
    setLocation('?prefs_saved=1&foo=bar');
    stubCaches();
    loadManualCache();
    const clearCacheSpy = vi.spyOn(manager(), 'clearCache');
    window.handlePrefsSavedSignal();
    expect(clearCacheSpy).not.toHaveBeenCalled();
    expect(window.location.search).toBe('?foo=bar');
  });
});

describe('wireCacheNavButtons', () => {
  it('wires the cache-data button to call manager.cacheData()', () => {
    document.body.innerHTML = '<a id="cache-data-btn" href="#"></a>';
    stubCaches();
    loadManualCache();
    const cacheDataSpy = vi.spyOn(manager(), 'cacheData').mockResolvedValue(undefined);
    window.wireCacheNavButtons();
    document.getElementById('cache-data-btn').dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    expect(cacheDataSpy).toHaveBeenCalled();
  });

  it('wires the clear-cache button to call manager.clearCache()', () => {
    document.body.innerHTML = '<a id="clear-cache-btn" href="#"></a>';
    stubCaches();
    loadManualCache();
    const clearCacheSpy = vi.spyOn(manager(), 'clearCache').mockResolvedValue(undefined);
    window.wireCacheNavButtons();
    document.getElementById('clear-cache-btn').dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    expect(clearCacheSpy).toHaveBeenCalled();
  });
});
