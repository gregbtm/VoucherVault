import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(__dirname, 'pwa-install.js'), 'utf-8');

// pwa-install.js is a classic IIFE script with no window.* export - it only
// wires window-level 'beforeinstallprompt'/'appinstalled' listeners, so
// tests drive it by dispatching those events and inspecting the resulting
// DOM/localStorage side effects, the same way a real browser would trigger it.
function loadPwaInstall() {
  (0, eval)(source);
}

function makeInstallPromptEvent(userChoiceOutcome = 'accepted') {
  const event = new Event('beforeinstallprompt', { cancelable: true });
  event.prompt = vi.fn();
  event.userChoice = Promise.resolve({ outcome: userChoiceOutcome });
  return event;
}

beforeEach(() => {
  document.body.innerHTML = '';
  localStorage.clear();
  vi.useRealTimers();
});

describe('beforeinstallprompt handling', () => {
  it('shows the install banner after the delay, once dismissed state is not set', async () => {
    vi.useFakeTimers();
    loadPwaInstall();
    window.dispatchEvent(makeInstallPromptEvent());

    expect(document.getElementById('pwa-install-banner')).toBeNull();
    await vi.advanceTimersByTimeAsync(2500);
    expect(document.getElementById('pwa-install-banner')).not.toBeNull();
    expect(document.getElementById('pwa-install-btn')).not.toBeNull();
    vi.useRealTimers();
  });

  it('never shows the banner if the user dismissed it within the last 30 days', async () => {
    localStorage.setItem('pwa_install_dismissed', String(Date.now()));
    vi.useFakeTimers();
    loadPwaInstall();
    window.dispatchEvent(makeInstallPromptEvent());
    await vi.advanceTimersByTimeAsync(3000);
    expect(document.getElementById('pwa-install-banner')).toBeNull();
    vi.useRealTimers();
  });

  it('shows the banner again once the 30-day dismissal window has passed', async () => {
    const thirtyOneDaysAgo = Date.now() - 31 * 86400000;
    localStorage.setItem('pwa_install_dismissed', String(thirtyOneDaysAgo));
    vi.useFakeTimers();
    loadPwaInstall();
    window.dispatchEvent(makeInstallPromptEvent());
    await vi.advanceTimersByTimeAsync(3000);
    expect(document.getElementById('pwa-install-banner')).not.toBeNull();
    vi.useRealTimers();
  });

  it('calls preventDefault so the browser does not show its own native prompt', () => {
    loadPwaInstall();
    const event = makeInstallPromptEvent();
    const spy = vi.spyOn(event, 'preventDefault');
    window.dispatchEvent(event);
    expect(spy).toHaveBeenCalled();
  });
});

describe('install banner interactions', () => {
  async function showBanner() {
    vi.useFakeTimers();
    loadPwaInstall();
    const event = makeInstallPromptEvent();
    window.dispatchEvent(event);
    await vi.advanceTimersByTimeAsync(2500);
    vi.useRealTimers();
    return event;
  }

  it('dismiss button removes the banner and records the dismissal timestamp', async () => {
    await showBanner();
    document.getElementById('pwa-install-dismiss').click();
    expect(document.getElementById('pwa-install-banner')).toBeNull();
    expect(localStorage.getItem('pwa_install_dismissed')).not.toBeNull();
  });

  it('install button triggers the deferred prompt and dismisses the banner on choice', async () => {
    const event = await showBanner();
    document.getElementById('pwa-install-btn').click();
    await event.userChoice;
    // Flush the microtask queue so the .then() continuation (dismiss()) runs.
    await Promise.resolve();
    await Promise.resolve();
    expect(event.prompt).toHaveBeenCalled();
    expect(document.getElementById('pwa-install-banner')).toBeNull();
  });

  it('does not create a second banner if beforeinstallprompt fires again while one is showing', async () => {
    vi.useFakeTimers();
    loadPwaInstall();
    window.dispatchEvent(makeInstallPromptEvent());
    await vi.advanceTimersByTimeAsync(2500);
    window.dispatchEvent(makeInstallPromptEvent());
    await vi.advanceTimersByTimeAsync(2500);
    expect(document.querySelectorAll('#pwa-install-banner').length).toBe(1);
    vi.useRealTimers();
  });
});

describe('appinstalled handling', () => {
  it('removes the banner and marks it dismissed once the app is installed', async () => {
    vi.useFakeTimers();
    loadPwaInstall();
    window.dispatchEvent(makeInstallPromptEvent());
    await vi.advanceTimersByTimeAsync(2500);
    vi.useRealTimers();

    expect(document.getElementById('pwa-install-banner')).not.toBeNull();
    window.dispatchEvent(new Event('appinstalled'));
    expect(document.getElementById('pwa-install-banner')).toBeNull();
    expect(localStorage.getItem('pwa_install_dismissed')).not.toBeNull();
  });
});
