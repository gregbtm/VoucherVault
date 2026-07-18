import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(__dirname, 'nearby-items.js'), 'utf-8');

// nearby-items.js is a classic (non-module) script that defines
// window.vvInitNearbyItems, exactly as it's loaded via <script src> in
// inventory.html - indirect eval runs the real production file.
function loadNearbyItems() {
  (0, eval)(source);
}

function buildDom() {
  document.body.innerHTML = `
    <form>
      <input type="hidden" name="csrfmiddlewaretoken" value="test-csrf-token">
    </form>
    <div id="nearby-items-widget" hidden>
      <ul id="nearby-items-list"></ul>
    </div>
  `;
}

function flush() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

beforeEach(() => {
  buildDom();
  vi.stubGlobal('fetch', vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

describe('geolocation gating', () => {
  it('does nothing when the Geolocation API is unavailable', () => {
    vi.stubGlobal('navigator', {});
    loadNearbyItems();
    window.vvInitNearbyItems({ nearbyUrl: '/nearby-items/' });
    expect(fetch).not.toHaveBeenCalled();
  });

  it('requests a one-shot position (no watchPosition) with sane options', () => {
    const getCurrentPosition = vi.fn();
    vi.stubGlobal('navigator', { geolocation: { getCurrentPosition } });
    loadNearbyItems();
    window.vvInitNearbyItems({ nearbyUrl: '/nearby-items/' });

    expect(getCurrentPosition).toHaveBeenCalledTimes(1);
    const options = getCurrentPosition.mock.calls[0][2];
    expect(options.enableHighAccuracy).toBe(false);
    expect(options.maximumAge).toBeGreaterThan(0);
  });

  it('fails silently when permission is denied', () => {
    const getCurrentPosition = vi.fn((success, error) => error(new Error('denied')));
    vi.stubGlobal('navigator', { geolocation: { getCurrentPosition } });
    loadNearbyItems();
    expect(() => {
      window.vvInitNearbyItems({ nearbyUrl: '/nearby-items/' });
    }).not.toThrow();
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe('server round trip', () => {
  function stubPosition(lat, lon) {
    const getCurrentPosition = vi.fn((success) => {
      success({ coords: { latitude: lat, longitude: lon } });
    });
    vi.stubGlobal('navigator', { geolocation: { getCurrentPosition } });
  }

  it('posts the coordinates and CSRF token to nearbyUrl', async () => {
    stubPosition(51.5, -0.12);
    fetch.mockResolvedValue({ ok: true, json: async () => ({ items: [] }) });
    loadNearbyItems();
    window.vvInitNearbyItems({ nearbyUrl: '/nearby-items/' });
    await flush();

    expect(fetch).toHaveBeenCalledTimes(1);
    const [url, opts] = fetch.mock.calls[0];
    expect(url).toBe('/nearby-items/');
    expect(opts.method).toBe('POST');
    expect(opts.headers['X-CSRFToken']).toBe('test-csrf-token');
    expect(opts.headers['X-Requested-With']).toBe('XMLHttpRequest');
    expect(opts.body.get('lat')).toBe('51.5');
    expect(opts.body.get('lon')).toBe('-0.12');
  });

  it('renders matched items and reveals the widget', async () => {
    stubPosition(51.5, -0.12);
    fetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        items: [
          { id: '1', name: 'Tesco Clubcard', issuer: 'Tesco', url: '/items/view/1' },
          { id: '2', name: '£10 Gift Card', issuer: 'Tesco', url: '/items/view/2' },
        ],
      }),
    });
    loadNearbyItems();
    window.vvInitNearbyItems({ nearbyUrl: '/nearby-items/' });
    await flush();
    await flush();

    const widget = document.getElementById('nearby-items-widget');
    const links = document.querySelectorAll('.nearby-item-link');
    expect(widget.hidden).toBe(false);
    expect(links.length).toBe(2);
    expect(links[0].getAttribute('href')).toBe('/items/view/1');
    expect(links[0].querySelector('.nearby-item-name').textContent).toBe('Tesco Clubcard');
    expect(links[0].querySelector('.nearby-item-issuer').textContent).toBe('Tesco');
  });

  it('leaves the widget hidden when no items match', async () => {
    stubPosition(51.5, -0.12);
    fetch.mockResolvedValue({ ok: true, json: async () => ({ items: [] }) });
    loadNearbyItems();
    window.vvInitNearbyItems({ nearbyUrl: '/nearby-items/' });
    await flush();
    await flush();

    expect(document.getElementById('nearby-items-widget').hidden).toBe(true);
  });

  it('leaves the widget hidden on a failed request', async () => {
    stubPosition(51.5, -0.12);
    fetch.mockResolvedValue({ ok: false });
    loadNearbyItems();
    window.vvInitNearbyItems({ nearbyUrl: '/nearby-items/' });
    await flush();
    await flush();

    expect(document.getElementById('nearby-items-widget').hidden).toBe(true);
  });
});
