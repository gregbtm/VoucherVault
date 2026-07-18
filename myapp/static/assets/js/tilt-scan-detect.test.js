import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(__dirname, 'tilt-scan-detect.js'), 'utf-8');

// tilt-scan-detect.js is a classic (non-module) script that defines
// window.vvInitTiltScanDetect, exactly as it's loaded via <script src> in
// view-item.html - indirect eval runs the real production file.
function loadTiltScanDetect() {
  (0, eval)(source);
}

function buildDom() {
  document.body.innerHTML = `
    <form>
      <input type="hidden" name="csrfmiddlewaretoken" value="test-csrf-token">
    </form>
    <button type="button" id="tilt-scan-enable-btn" hidden></button>
    <div id="tilt-scan-banner" hidden>
      <button type="button" id="tilt-scan-mark-used-btn"></button>
      <button type="button" id="tilt-scan-dismiss-btn"></button>
    </div>
  `;
}

function tiltEvent(beta) {
  const event = new Event('deviceorientation');
  event.beta = beta;
  return event;
}

function flush(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

beforeEach(() => {
  buildDom();
  delete window.DeviceOrientationEvent;
  vi.stubGlobal('fetch', vi.fn());
  vi.stubGlobal('location', { reload: vi.fn() });
});

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

describe('permission gating', () => {
  it('attaches immediately and hides the enable button when no explicit permission API exists', () => {
    loadTiltScanDetect();
    window.vvInitTiltScanDetect({ toggleUrl: '/toggle/', holdMs: 10, cooldownMs: 20 });

    expect(document.getElementById('tilt-scan-enable-btn').hidden).toBe(true);
    window.dispatchEvent(tiltEvent(5));
    return flush(20).then(() => {
      expect(document.getElementById('tilt-scan-banner').hidden).toBe(false);
    });
  });

  it('shows the enable button and does not attach until permission is granted when the API requires an explicit gesture', async () => {
    window.DeviceOrientationEvent = { requestPermission: vi.fn().mockResolvedValue('granted') };
    loadTiltScanDetect();
    window.vvInitTiltScanDetect({ toggleUrl: '/toggle/', holdMs: 10, cooldownMs: 20 });

    const enableBtn = document.getElementById('tilt-scan-enable-btn');
    expect(enableBtn.hidden).toBe(false);

    window.dispatchEvent(tiltEvent(5));
    await flush(20);
    expect(document.getElementById('tilt-scan-banner').hidden).toBe(true);

    enableBtn.click();
    await Promise.resolve();
    await Promise.resolve();
    expect(window.DeviceOrientationEvent.requestPermission).toHaveBeenCalled();
    expect(enableBtn.hidden).toBe(true);

    window.dispatchEvent(tiltEvent(5));
    await flush(20);
    expect(document.getElementById('tilt-scan-banner').hidden).toBe(false);
  });

  it('does not attach when permission is denied', async () => {
    window.DeviceOrientationEvent = { requestPermission: vi.fn().mockResolvedValue('denied') };
    loadTiltScanDetect();
    window.vvInitTiltScanDetect({ toggleUrl: '/toggle/', holdMs: 10, cooldownMs: 20 });

    document.getElementById('tilt-scan-enable-btn').click();
    await Promise.resolve();
    await Promise.resolve();

    window.dispatchEvent(tiltEvent(5));
    await flush(20);
    expect(document.getElementById('tilt-scan-banner').hidden).toBe(true);
  });
});

describe('tilt threshold detection', () => {
  it('shows the banner once tilted below the threshold and held for holdMs', async () => {
    loadTiltScanDetect();
    window.vvInitTiltScanDetect({ toggleUrl: '/toggle/', holdMs: 30, cooldownMs: 200 });

    window.dispatchEvent(tiltEvent(5));
    await flush(10);
    expect(document.getElementById('tilt-scan-banner').hidden).toBe(true); // not held long enough yet

    window.dispatchEvent(tiltEvent(5));
    await flush(30);
    expect(document.getElementById('tilt-scan-banner').hidden).toBe(false);
  });

  it('does not trigger from a brief dip that returns above the threshold before holdMs', async () => {
    loadTiltScanDetect();
    window.vvInitTiltScanDetect({ toggleUrl: '/toggle/', holdMs: 50, cooldownMs: 200 });

    window.dispatchEvent(tiltEvent(5));
    await flush(10);
    window.dispatchEvent(tiltEvent(80)); // back to a normal holding angle
    await flush(50);
    window.dispatchEvent(tiltEvent(80));
    expect(document.getElementById('tilt-scan-banner').hidden).toBe(true);
  });

  it('ignores events with no beta value', async () => {
    loadTiltScanDetect();
    window.vvInitTiltScanDetect({ toggleUrl: '/toggle/', holdMs: 10, cooldownMs: 20 });

    const event = new Event('deviceorientation'); // beta left undefined
    window.dispatchEvent(event);
    await flush(20);
    expect(document.getElementById('tilt-scan-banner').hidden).toBe(true);
  });

  it('does not re-trigger again during the cooldown window', async () => {
    loadTiltScanDetect();
    window.vvInitTiltScanDetect({ toggleUrl: '/toggle/', holdMs: 10, cooldownMs: 200 });

    window.dispatchEvent(tiltEvent(5));
    await flush(15);
    expect(document.getElementById('tilt-scan-banner').hidden).toBe(false);

    document.getElementById('tilt-scan-dismiss-btn').click();
    expect(document.getElementById('tilt-scan-banner').hidden).toBe(true);

    window.dispatchEvent(tiltEvent(5));
    await flush(15);
    expect(document.getElementById('tilt-scan-banner').hidden).toBe(true); // still cooling down
  });
});

describe('banner actions', () => {
  it('dismiss hides the banner without calling fetch', async () => {
    loadTiltScanDetect();
    window.vvInitTiltScanDetect({ toggleUrl: '/toggle/', holdMs: 10, cooldownMs: 20 });
    window.dispatchEvent(tiltEvent(5));
    await flush(15);

    document.getElementById('tilt-scan-dismiss-btn').click();
    expect(document.getElementById('tilt-scan-banner').hidden).toBe(true);
    expect(fetch).not.toHaveBeenCalled();
  });

  it('mark used posts to toggleUrl with the CSRF token and AJAX header, then reloads on success', async () => {
    fetch.mockResolvedValue({ ok: true, json: async () => ({ success: true, is_used: true }) });
    loadTiltScanDetect();
    window.vvInitTiltScanDetect({ toggleUrl: '/en/items/toggle_status/abc/', holdMs: 10, cooldownMs: 20 });
    window.dispatchEvent(tiltEvent(5));
    await flush(15);

    document.getElementById('tilt-scan-mark-used-btn').click();
    expect(document.getElementById('tilt-scan-banner').hidden).toBe(true);

    expect(fetch).toHaveBeenCalledWith('/en/items/toggle_status/abc/', {
      method: 'POST',
      headers: {
        'X-Requested-With': 'XMLHttpRequest',
        'X-CSRFToken': 'test-csrf-token',
      },
    });

    await flush(0);
    expect(window.location.reload).toHaveBeenCalled();
  });

  it('does not reload if the toggle request fails', async () => {
    fetch.mockResolvedValue({ ok: false });
    loadTiltScanDetect();
    window.vvInitTiltScanDetect({ toggleUrl: '/toggle/', holdMs: 10, cooldownMs: 20 });
    window.dispatchEvent(tiltEvent(5));
    await flush(15);

    document.getElementById('tilt-scan-mark-used-btn').click();
    await Promise.resolve();
    await Promise.resolve();
    expect(window.location.reload).not.toHaveBeenCalled();
  });
});
