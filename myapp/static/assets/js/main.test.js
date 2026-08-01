import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(__dirname, 'main.js'), 'utf-8');

// main.js is the vendored NiceAdmin template plus this fork's own additions
// (sidebar toggle, the mobile sidebar-overlay backdrop, the page-transition
// fade, and bfcache restore). These tests deliberately avoid any DOM that
// would exercise the vendor-only code paths requiring bootstrap.Tooltip/
// echarts globals (#main, .echart, [data-bs-toggle="tooltip"],
// .needs-validation) - those querySelectorAll calls return empty in the DOM
// built here, so those branches never execute and never need those globals,
// exactly as a page with none of those elements behaves for real.
function loadMain() {
  (0, eval)(source);
}

beforeEach(() => {
  document.body.innerHTML = '';
  document.body.className = '';
  window.VVPrefersReducedMotion = false;
  vi.useRealTimers();
});

describe('sidebar toggle', () => {
  it('toggles the .toggle-sidebar body class on click', () => {
    document.body.innerHTML = '<i class="toggle-sidebar-btn"></i>';
    loadMain();
    document.querySelector('.toggle-sidebar-btn').click();
    expect(document.body.classList.contains('toggle-sidebar')).toBe(true);
    document.querySelector('.toggle-sidebar-btn').click();
    expect(document.body.classList.contains('toggle-sidebar')).toBe(false);
  });

  it('does not throw when no .toggle-sidebar-btn exists on the page', () => {
    document.body.innerHTML = '<div></div>';
    expect(() => loadMain()).not.toThrow();
  });
});

describe('mobile sidebar overlay', () => {
  it('tapping the backdrop closes the mobile sidebar', () => {
    document.body.innerHTML = '<div id="sidebar-overlay"></div>';
    document.body.classList.add('toggle-sidebar');
    loadMain();
    document.getElementById('sidebar-overlay').click();
    expect(document.body.classList.contains('toggle-sidebar')).toBe(false);
  });
});

describe('search bar toggle', () => {
  it('toggles .search-bar-show on the search bar when the toggle is tapped', () => {
    document.body.innerHTML = '<div class="search-bar-toggle"></div><div class="search-bar"></div>';
    loadMain();
    document.querySelector('.search-bar-toggle').click();
    expect(document.querySelector('.search-bar').classList.contains('search-bar-show')).toBe(true);
  });
});

describe('internal-link page-transition fade', () => {
  it('fades out and defers navigation for a same-origin link click', () => {
    vi.useFakeTimers();
    document.body.innerHTML = '<a href="/en/wallets/" id="link">Wallets</a>';
    loadMain();

    const link = document.getElementById('link');
    const event = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 });
    link.dispatchEvent(event); // bubbles up to main.js's document-level listener

    expect(document.body.classList.contains('vv-page-leaving')).toBe(true);
  });

  it('ignores same-page anchor links (jump links, back-to-top)', () => {
    document.body.innerHTML = '<a href="#section" id="anchor-link">Jump</a>';
    loadMain();
    document.getElementById('anchor-link').click();
    expect(document.body.classList.contains('vv-page-leaving')).toBe(false);
  });

  it('ignores links with a download attribute', () => {
    document.body.innerHTML = '<a href="/file.pdf" download id="dl-link">Download</a>';
    loadMain();
    document.getElementById('dl-link').click();
    expect(document.body.classList.contains('vv-page-leaving')).toBe(false);
  });

  it('ignores a ctrl/cmd+click (opening in a new tab)', () => {
    document.body.innerHTML = '<a href="/en/wallets/" id="link">Wallets</a>';
    loadMain();
    const event = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0, ctrlKey: true });
    document.getElementById('link').dispatchEvent(event);
    expect(document.body.classList.contains('vv-page-leaving')).toBe(false);
  });

  it('never wires the fade click-listener at all when the OS asks for reduced motion', () => {
    // Every prior test in this describe block also calls loadMain(), and
    // each one attaches its own permanent document-level click listener -
    // there's no teardown hook to remove them between tests, since main.js
    // is only ever meant to load once per real page. So this asserts the
    // guard itself (document.addEventListener is never called for the fade
    // handler) rather than the end-to-end click effect, which would be
    // contaminated by earlier tests' still-attached, non-reduced-motion
    // listeners in this same shared jsdom document.
    window.VVPrefersReducedMotion = true;
    const addEventListenerSpy = vi.spyOn(document, 'addEventListener');
    loadMain();
    // main.js registers exactly one other permanent document 'click'
    // listener unconditionally (closing an open tooltip bubble) - so
    // reduced-motion should see exactly that one, not a second (the fade
    // handler) on top of it.
    const clickListenerCount = addEventListenerSpy.mock.calls.filter(([type]) => type === 'click').length;
    expect(clickListenerCount).toBe(1);
    addEventListenerSpy.mockRestore();
  });
});

describe('bfcache restore', () => {
  it('un-hides a frozen mid-fade page and any un-revealed entrance-animation elements on pageshow', () => {
    document.body.innerHTML = '<div class="vv-anim-pre" id="stuck"></div>';
    document.body.classList.add('vv-page-leaving');
    loadMain();
    window.dispatchEvent(new Event('pageshow', { bubbles: true }));
    // jsdom's Event doesn't support the `persisted` property directly via the
    // constructor - dispatch a PageTransitionEvent-shaped plain event instead.
    const event = new Event('pageshow');
    Object.defineProperty(event, 'persisted', { value: true });
    window.dispatchEvent(event);

    expect(document.body.classList.contains('vv-page-leaving')).toBe(false);
    expect(document.getElementById('stuck').classList.contains('vv-anim-pre')).toBe(false);
  });

  it('leaves state untouched when the page was not restored from bfcache', () => {
    document.body.innerHTML = '<div class="vv-anim-pre" id="stuck"></div>';
    document.body.classList.add('vv-page-leaving');
    loadMain();
    const event = new Event('pageshow');
    Object.defineProperty(event, 'persisted', { value: false });
    window.dispatchEvent(event);

    expect(document.body.classList.contains('vv-page-leaving')).toBe(true);
    expect(document.getElementById('stuck').classList.contains('vv-anim-pre')).toBe(true);
  });
});
