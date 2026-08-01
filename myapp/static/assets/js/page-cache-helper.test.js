import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(__dirname, 'page-cache-helper.js'), 'utf-8');

// page-cache-helper.js's PageCacheHelper class is assigned explicitly to
// window.pageCacheHelper once loaded (document.readyState is 'complete' in
// jsdom by default, so the immediate-instantiation branch runs). Its
// constructor only logs - every actual caching method is dormant until
// called directly (automatic caching was deliberately disabled - see the
// file's own comments), which is exactly how these tests exercise them.
function loadPageCacheHelper() {
  delete window.pageCacheHelper;
  (0, eval)(source);
}

function setOnline(isOnline) {
  Object.defineProperty(navigator, 'onLine', { value: isOnline, configurable: true });
}

beforeEach(() => {
  document.body.innerHTML = '';
  delete window.offlineSyncManager;
  setOnline(true);
  vi.restoreAllMocks();
});

describe('initialization', () => {
  it('instantiates as window.pageCacheHelper without doing any automatic caching', () => {
    const fetchSpy = vi.spyOn(global, 'fetch');
    loadPageCacheHelper();
    expect(window.pageCacheHelper).toBeInstanceOf(Object);
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe('cacheDashboardData', () => {
  it('skips fetching entirely while offline', async () => {
    setOnline(false);
    const fetchSpy = vi.spyOn(global, 'fetch');
    loadPageCacheHelper();
    await window.pageCacheHelper.cacheDashboardData();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('fetches fresh stats and stores them via offlineSyncManager when online', async () => {
    const stats = { total_items: 42 };
    global.fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => stats });
    window.offlineSyncManager = { cacheData: vi.fn() };
    loadPageCacheHelper();

    await window.pageCacheHelper.cacheDashboardData();

    expect(global.fetch).toHaveBeenCalledWith('/api/get/stats');
    expect(window.offlineSyncManager.cacheData).toHaveBeenCalledWith('dashboard_stats', stats);
  });

  it('does nothing (no throw) if offlineSyncManager is not present', async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    loadPageCacheHelper();
    await expect(window.pageCacheHelper.cacheDashboardData()).resolves.toBeUndefined();
  });

  it('swallows fetch errors quietly (falls back to existing cache)', async () => {
    global.fetch = vi.fn().mockRejectedValue(new Error('network down'));
    loadPageCacheHelper();
    await expect(window.pageCacheHelper.cacheDashboardData()).resolves.toBeUndefined();
  });
});

describe('cacheItemsListData', () => {
  it('extracts unique item URLs from the page and pre-caches them', async () => {
    document.body.innerHTML = `
      <a href="/en/items/view/abc">Item A</a>
      <a href="/en/items/view/def">Item B</a>
      <a href="/en/items/view/abc">Item A again</a>`;
    global.fetch = vi.fn().mockResolvedValue({ ok: true, text: async () => '' });
    window.offlineSyncManager = { cacheData: vi.fn() };
    loadPageCacheHelper();

    await window.pageCacheHelper.cacheItemsListData();

    expect(window.offlineSyncManager.cacheData).toHaveBeenCalledWith(
      'cached_item_urls',
      ['/en/items/view/abc', '/en/items/view/def']
    );
    // preCacheAllItemUrls fetches each unique URL once.
    expect(global.fetch).toHaveBeenCalledTimes(2);
  });

  it('does nothing when there are no item links on the page', async () => {
    window.offlineSyncManager = { cacheData: vi.fn() };
    loadPageCacheHelper();
    await window.pageCacheHelper.cacheItemsListData();
    expect(window.offlineSyncManager.cacheData).not.toHaveBeenCalled();
  });

  it('skips entirely while offline', async () => {
    setOnline(false);
    document.body.innerHTML = '<a href="/en/items/view/abc">Item A</a>';
    const fetchSpy = vi.spyOn(global, 'fetch');
    loadPageCacheHelper();
    await window.pageCacheHelper.cacheItemsListData();
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe('cacheItemDetailData', () => {
  it('adds a newly-viewed item to the front of the recent-items list', async () => {
    window.offlineSyncManager = {
      getCachedData: vi.fn().mockResolvedValue(['old-item']),
      cacheData: vi.fn(),
    };
    loadPageCacheHelper();
    await window.pageCacheHelper.cacheItemDetailData('new-item');
    expect(window.offlineSyncManager.cacheData).toHaveBeenCalledWith('recent_items', ['new-item', 'old-item']);
  });

  it('does not duplicate an item already at the front of recent items', async () => {
    window.offlineSyncManager = {
      getCachedData: vi.fn().mockResolvedValue(['item-1', 'item-2']),
      cacheData: vi.fn(),
    };
    loadPageCacheHelper();
    await window.pageCacheHelper.cacheItemDetailData('item-1');
    expect(window.offlineSyncManager.cacheData).not.toHaveBeenCalled();
  });

  it('caps the recent-items list at 20 entries', async () => {
    const existing = Array.from({ length: 20 }, (_, i) => `item-${i}`);
    window.offlineSyncManager = {
      getCachedData: vi.fn().mockResolvedValue(existing),
      cacheData: vi.fn(),
    };
    loadPageCacheHelper();
    await window.pageCacheHelper.cacheItemDetailData('brand-new');
    const [, savedList] = window.offlineSyncManager.cacheData.mock.calls[0];
    expect(savedList.length).toBe(20);
    expect(savedList[0]).toBe('brand-new');
    expect(savedList).not.toContain('item-19'); // oldest entry dropped
  });
});

describe('preCacheAllItemUrls', () => {
  it('fetches every URL and consumes the response body so the service worker can cache it', async () => {
    const textSpy = vi.fn().mockResolvedValue('<html></html>');
    global.fetch = vi.fn().mockResolvedValue({ ok: true, text: textSpy });
    loadPageCacheHelper();
    await window.pageCacheHelper.preCacheAllItemUrls(['/a', '/b']);
    expect(global.fetch).toHaveBeenCalledTimes(2);
    expect(textSpy).toHaveBeenCalledTimes(2);
  });

  it('keeps going even if one URL fails to fetch', async () => {
    global.fetch = vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 404 })
      .mockResolvedValueOnce({ ok: true, text: async () => '' });
    loadPageCacheHelper();
    await expect(window.pageCacheHelper.preCacheAllItemUrls(['/missing', '/ok'])).resolves.toBeUndefined();
    expect(global.fetch).toHaveBeenCalledTimes(2);
  });
});
