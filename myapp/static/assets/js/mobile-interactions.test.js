import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(__dirname, 'mobile-interactions.js'), 'utf-8');

// mobile-interactions.js is 16 independent, permanently-wired IIFE sections.
// Since it attaches straight to `document`/`window` with no teardown hook
// (same as main.js), each test below builds the DOM it needs BEFORE loading
// the script once, then drives it via real events - covering the sections
// with the most user-visible behavior rather than all 16 exhaustively.
function loadMobileInteractions() {
  (0, eval)(source);
}

// Every touch dispatch includes both touches and changedTouches with at
// least one point - section 2 (swipe-to-close) reads e.touches[0]/
// e.changedTouches[0] unconditionally on every touchstart/touchend, and it
// stays wired for the lifetime of the shared jsdom `document` across every
// test in this file (there's no teardown hook), so any touchstart/touchend
// dispatched anywhere below must satisfy that shape or it throws.
function makeTouchEvent(type, target, x = 0, y = 0) {
  const point = { clientX: x, clientY: y };
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'target', { value: target, configurable: true });
  Object.defineProperty(event, 'touches', { value: [point], configurable: true });
  Object.defineProperty(event, 'changedTouches', { value: [point], configurable: true });
  return event;
}

function dispatchTouch(type, target, x = 0, y = 0) {
  const event = makeTouchEvent(type, target, x, y);
  document.dispatchEvent(event);
  return event;
}

function stubMatchMediaHover(hoverNone) {
  window.matchMedia = vi.fn().mockImplementation((query) => ({
    matches: query === '(hover: none)' ? hoverNone : false,
  }));
}

beforeEach(() => {
  document.body.innerHTML = '';
  document.body.className = '';
  stubMatchMediaHover(false);
  delete navigator.vibrate;
});

describe('swipe-to-close hamburger menu', () => {
  it('closes the sidebar on a left swipe with minimal vertical movement', () => {
    document.body.innerHTML = '<div class="sidebar"></div>';
    document.body.classList.add('toggle-sidebar');
    loadMobileInteractions();

    dispatchTouch('touchstart', document.body, 300, 100);
    dispatchTouch('touchend', document.body, 150, 105);

    expect(document.body.classList.contains('toggle-sidebar')).toBe(false);
  });

  it('does not close the sidebar on a mostly-vertical swipe', () => {
    document.body.innerHTML = '<div class="sidebar"></div>';
    document.body.classList.add('toggle-sidebar');
    loadMobileInteractions();

    dispatchTouch('touchstart', document.body, 300, 100);
    dispatchTouch('touchend', document.body, 250, 300);

    expect(document.body.classList.contains('toggle-sidebar')).toBe(true);
  });

  it('does nothing at all when there is no .sidebar on the page', () => {
    loadMobileInteractions();
    expect(() => {
      dispatchTouch('touchstart', document.body, 300, 100);
      dispatchTouch('touchend', document.body, 150, 105);
    }).not.toThrow();
  });
});

describe('haptic feedback', () => {
  it('vibrates on form submit when the Vibration API is available', () => {
    document.body.innerHTML = '<form id="f"></form>';
    navigator.vibrate = vi.fn();
    loadMobileInteractions();
    // jsdom does not actually submit forms (no real navigation), but the
    // 'submit' listener still fires from a dispatched Event.
    document.getElementById('f').dispatchEvent(new Event('submit', { cancelable: true }));
    expect(navigator.vibrate).toHaveBeenCalledWith([10, 20, 10]);
  });

  it('never touches navigator.vibrate when the API is unavailable', () => {
    document.body.innerHTML = '<form id="f"></form>';
    delete navigator.vibrate;
    expect(() => {
      loadMobileInteractions();
      document.getElementById('f').dispatchEvent(new Event('submit', { cancelable: true }));
    }).not.toThrow();
  });

  it('fires a longer error buzz on a validation "invalid" event', () => {
    document.body.innerHTML = '<form id="f"><input required id="i"></form>';
    navigator.vibrate = vi.fn();
    loadMobileInteractions();
    document.getElementById('i').dispatchEvent(new Event('invalid', { bubbles: true, cancelable: true }));
    expect(navigator.vibrate).toHaveBeenCalledWith([50, 100, 50]);
  });
});

describe('network status indicator', () => {
  it('reflects navigator.onLine as Online/Offline text and classes on load', () => {
    document.body.innerHTML = '<div class="network-status"></div>';
    Object.defineProperty(navigator, 'onLine', { value: true, configurable: true });
    loadMobileInteractions();
    const indicator = document.querySelector('.network-status');
    expect(indicator.textContent).toBe('Online');
    expect(indicator.classList.contains('online')).toBe(true);
  });

  it('updates to Offline when the offline event fires', () => {
    document.body.innerHTML = '<div class="network-status"></div>';
    Object.defineProperty(navigator, 'onLine', { value: true, configurable: true });
    loadMobileInteractions();
    Object.defineProperty(navigator, 'onLine', { value: false, configurable: true });
    window.dispatchEvent(new Event('offline'));
    const indicator = document.querySelector('.network-status');
    expect(indicator.textContent).toBe('Offline');
    expect(indicator.classList.contains('offline')).toBe(true);
  });
});

describe('double-tap zoom prevention', () => {
  it('prevents default on a second touchend within 300ms on a button', () => {
    document.body.innerHTML = '<button id="btn"></button>';
    loadMobileInteractions();
    const btn = document.getElementById('btn');

    dispatchTouch('touchend', btn);
    const second = makeTouchEvent('touchend', btn);
    const preventDefaultSpy = vi.spyOn(second, 'preventDefault');
    document.dispatchEvent(second);

    expect(preventDefaultSpy).toHaveBeenCalled();
  });
});

describe('touch-pressed active-state class (touch devices only)', () => {
  it('adds .touch-pressed on touchstart and removes it after a short delay, on a touch device', () => {
    vi.useFakeTimers();
    stubMatchMediaHover(true);
    document.body.innerHTML = '<button id="btn"></button>';
    loadMobileInteractions();
    const btn = document.getElementById('btn');

    dispatchTouch('touchstart', btn);
    expect(btn.classList.contains('touch-pressed')).toBe(true);

    dispatchTouch('touchend', btn);
    vi.advanceTimersByTime(100);
    expect(btn.classList.contains('touch-pressed')).toBe(false);
    vi.useRealTimers();
  });

  it('never wires touch-pressed handling on a non-touch (hover-capable) device', () => {
    // Asserts the wiring decision directly (via a spy on the registration
    // call) rather than the end-to-end DOM effect: the prior test's
    // touch-device load left its own touchstart listener permanently
    // attached to this shared jsdom `document` (mobile-interactions.js has
    // no teardown hook, since real pages only ever load it once), so a
    // hover-capable load here would still see .touch-pressed added by that
    // still-live listener from the earlier test, regardless of what this
    // load itself wires.
    stubMatchMediaHover(false);
    document.body.innerHTML = '<button id="btn"></button>';
    const addEventListenerSpy = vi.spyOn(document, 'addEventListener');
    loadMobileInteractions();
    // Section 2 (swipe-to-close) only registers its own 'touchstart' listener
    // when a .sidebar exists on the page - absent here - so on a
    // hover-capable device with no sidebar, touch-pressed handling (the only
    // other 'touchstart' registrant) should add zero.
    const touchstartListenerCount = addEventListenerSpy.mock.calls.filter(([type]) => type === 'touchstart').length;
    expect(touchstartListenerCount).toBe(0);
    addEventListenerSpy.mockRestore();
  });
});

describe('focus trap in modals', () => {
  it('wraps Tab from the last focusable element back to the first', () => {
    document.body.innerHTML = `
      <div id="modal">
        <button id="first">First</button>
        <button id="last">Last</button>
      </div>`;
    loadMobileInteractions();
    const modal = document.getElementById('modal');
    const last = document.getElementById('last');
    const first = document.getElementById('first');
    last.focus();

    // trapFocus wires the keydown handler onto the modal element passed as
    // e.target; jsdom doesn't set .target automatically for a plain Event
    // dispatched directly on `document` the way a real bootstrap event
    // (which bubbles up from the modal) would, so set it explicitly here.
    const shownEvent = new Event('shown.bs.modal');
    Object.defineProperty(shownEvent, 'target', { value: modal });
    document.dispatchEvent(shownEvent);

    const tabEvent = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true });
    const preventDefaultSpy = vi.spyOn(tabEvent, 'preventDefault');
    last.dispatchEvent(tabEvent);

    expect(preventDefaultSpy).toHaveBeenCalled();
    expect(document.activeElement).toBe(first);
  });
});
