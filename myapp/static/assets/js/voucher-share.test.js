import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(__dirname, 'voucher-share.js'), 'utf-8');

function loadVoucherShare() {
  (0, eval)(source);
}

// Loaded exactly once for the whole file (not per-test): the script's
// trailing `document.addEventListener('click', ...)` runs every time it's
// eval'd, and jsdom's `document` persists across every `it()` in this file -
// re-loading per test would stack up duplicate click listeners, each firing
// (and each re-running shareClassic/openShareChooser/etc.) on a single click.
beforeAll(() => {
  loadVoucherShare();
});

beforeEach(() => {
  document.body.innerHTML = '';
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
    configurable: true,
  });
  vi.spyOn(window, 'alert').mockImplementation(() => {});
  window.VV_SHARE_SMART_ENABLED = false;
});

afterEach(() => {
  // openShareChooser/closeShareChooser are globals after the single eval
  // above - reset any chooser left open by a test so it can't leak into
  // the next one.
  closeShareChooser();
  vi.restoreAllMocks();
  delete window.VV_SHARE_SMART_ENABLED;
  delete global.fetch;
});

function makeShareButton(overrides = {}) {
  const btn = document.createElement('button');
  btn.className = 'share-voucher-btn';
  btn.dataset.itemId = overrides.itemId ?? 'item-1';
  btn.dataset.title = overrides.title ?? 'Gift Card';
  btn.dataset.merchant = overrides.merchant ?? 'Costa Coffee';
  btn.dataset.url = overrides.url ?? 'https://voucher.example/items/view/item-1';
  btn.dataset.publicShareUrl = overrides.publicShareUrl ?? '/en/items/item-1/public-share/';
  document.body.appendChild(btn);
  return btn;
}

describe('buildVoucherShareText', () => {
  const base = {
    merchant: 'Costa Coffee',
    name: 'Gift Card',
    code: 'ABC123',
    url: 'https://voucher.example/s/abc',
  };

  it('always includes the merchant/name header, code, and link', () => {
    const text = buildVoucherShareText(base);
    expect(text).toContain('Costa Coffee - Gift Card');
    expect(text).toContain('Code: ABC123');
    expect(text).toContain('View voucher: https://voucher.example/s/abc');
  });

  it('omits the PIN line when there is no PIN', () => {
    const text = buildVoucherShareText(base);
    expect(text).not.toContain('PIN:');
  });

  it('includes the PIN line when a PIN is present', () => {
    const text = buildVoucherShareText({ ...base, pin: '4321' });
    expect(text).toContain('PIN: 4321');
  });

  it('omits the balance line when balance is null/undefined', () => {
    expect(buildVoucherShareText({ ...base, balance: null })).not.toContain('Remaining balance');
    expect(buildVoucherShareText(base)).not.toContain('Remaining balance');
  });

  it('includes the balance line, with currency, when balance is present - even zero', () => {
    const text = buildVoucherShareText({ ...base, balance: 0, currency: 'GBP' });
    // balance !== null && balance !== undefined - 0 is a real, sharable balance, not "no balance".
    expect(text).toContain('Remaining balance: 0 GBP');
  });
});

describe('classic vs smart share branching', () => {
  it('shares classically when smart share is disabled, without opening the chooser', async () => {
    window.VV_SHARE_SMART_ENABLED = false;
    const btn = makeShareButton();
    btn.click();
    await Promise.resolve();

    expect(document.querySelector('.vv-share-chooser')).toBeNull();
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      expect.stringContaining(btn.dataset.url)
    );
  });

  it('shares classically when smart share is enabled but the button has no item id', async () => {
    window.VV_SHARE_SMART_ENABLED = true;
    const btn = makeShareButton();
    delete btn.dataset.itemId;
    btn.click();
    await Promise.resolve();

    expect(document.querySelector('.vv-share-chooser')).toBeNull();
    expect(navigator.clipboard.writeText).toHaveBeenCalled();
  });

  it('opens the chooser when smart share is enabled and an item id is present', () => {
    window.VV_SHARE_SMART_ENABLED = true;
    const btn = makeShareButton();
    btn.click();

    expect(document.querySelector('.vv-share-chooser')).not.toBeNull();
    expect(navigator.clipboard.writeText).not.toHaveBeenCalled();
  });
});

describe('share chooser open/close', () => {
  it('renders all three share options', () => {
    const btn = makeShareButton();
    openShareChooser(btn);

    const options = document.querySelectorAll('.vv-share-option');
    expect(options).toHaveLength(3);
    expect(document.querySelector('[data-action="link"]')).not.toBeNull();
    expect(document.querySelector('[data-action="details"]')).not.toBeNull();
    expect(document.querySelector('[data-action="image-details"]')).not.toBeNull();
  });

  it('replaces an already-open chooser rather than stacking a second one', () => {
    const btn = makeShareButton();
    openShareChooser(btn);
    openShareChooser(btn);
    expect(document.querySelectorAll('.vv-share-chooser')).toHaveLength(1);
  });

  it('closes when clicking outside the chooser', async () => {
    const btn = makeShareButton();
    openShareChooser(btn);
    // The outside-click listener is attached via setTimeout(..., 0) so it
    // doesn't immediately catch the very click that opened the chooser.
    await new Promise((resolve) => setTimeout(resolve, 0));

    document.body.click();
    expect(document.querySelector('.vv-share-chooser')).toBeNull();
  });

  it('does not close when clicking inside the chooser', async () => {
    const btn = makeShareButton();
    openShareChooser(btn);
    await new Promise((resolve) => setTimeout(resolve, 0));

    document.querySelector('.vv-share-chooser').click();
    expect(document.querySelector('.vv-share-chooser')).not.toBeNull();
  });

  it('picking "Share link" closes the chooser and fetches a public share link', async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: { get: () => 'application/json' },
      json: async () => ({ merchant: 'Costa Coffee', name: 'Gift Card', url: 'https://voucher.example/s/xyz' }),
    });
    const btn = makeShareButton();
    openShareChooser(btn);

    document.querySelector('[data-action="link"]').click();
    await Promise.resolve();
    await Promise.resolve();

    expect(document.querySelector('.vv-share-chooser')).toBeNull();
    expect(global.fetch).toHaveBeenCalledWith(btn.dataset.publicShareUrl, expect.objectContaining({ method: 'POST' }));
  });
});

describe('fetchPublicShareInfo failure/fallback path', () => {
  it('shareViaLink silently falls back to the classic share on a failed fetch', async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: false, status: 500, headers: { get: () => 'text/html' } });
    const btn = makeShareButton();

    await shareViaLink(btn);

    // shareClassic's own clipboard-fallback confirmation ("copied to
    // clipboard") is expected here - only the "could not create a link"
    // error alert (shareViaDetails' path) must never fire for shareViaLink.
    expect(window.alert).not.toHaveBeenCalledWith(expect.stringContaining('Could not create a share link'));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(expect.stringContaining(btn.dataset.url));
  });

  it('shareViaLink falls back to classic share when the response is not JSON', async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: true, headers: { get: () => 'text/html' } });
    const btn = makeShareButton();

    await shareViaLink(btn);

    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(expect.stringContaining(btn.dataset.url));
  });

  it('shareViaDetails shows an alert instead of falling back, on a failed fetch', async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: false, status: 500, headers: { get: () => 'text/html' } });
    const btn = makeShareButton();

    await shareViaDetails(btn);

    expect(window.alert).toHaveBeenCalledWith(expect.stringContaining('Could not create a share link'));
    expect(navigator.clipboard.writeText).not.toHaveBeenCalled();
  });

  it('shareViaImageAndDetails shows an alert and never attempts the logo fetch, on a failed public-share fetch', async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: false, status: 500, headers: { get: () => 'text/html' } });
    const btn = makeShareButton();

    await shareViaImageAndDetails(btn);

    expect(window.alert).toHaveBeenCalledWith(expect.stringContaining('Could not create a share link'));
    expect(global.fetch).toHaveBeenCalledTimes(1); // only the public-share call, no logo fetch attempted
  });
});
