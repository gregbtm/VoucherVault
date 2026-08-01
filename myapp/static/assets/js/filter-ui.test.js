import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { beforeEach, describe, expect, it } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(__dirname, 'filter-ui.js'), 'utf-8');

// filter-ui.js is a classic (non-module) script that attaches
// window.autoFocusSearchOnDesktop/window.applyMobileDropdownFilters as a
// side effect, exactly as it's loaded via <script src> in base.html -
// indirect eval runs it the same way a real <script> tag would, so the
// real production file is under test, not a reimplementation of it.
function loadFilterUi() {
  (0, eval)(source);
}

function setInnerWidth(width) {
  Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: width });
}

// jsdom doesn't implement real navigation, and window.location itself is
// non-configurable in a real browser - swapping in a plain URL instance
// (which already has a working get/set .href) lets applyMobileDropdownFilters'
// `new URL(window.location.href)` read and its `window.location.href = ...`
// write both behave like the real thing, without triggering jsdom's
// "Not implemented: navigation" warning.
function setLocation(href) {
  Object.defineProperty(window, 'location', { writable: true, configurable: true, value: new URL(href) });
}

beforeEach(() => {
  document.body.innerHTML = '';
  delete window.autoFocusSearchOnDesktop;
  delete window.applyMobileDropdownFilters;
  setInnerWidth(1024);
  setLocation('https://example.com/inventory');
});

describe('autoFocusSearchOnDesktop', () => {
  function makeSearchInput(value) {
    const input = document.createElement('input');
    input.value = value || '';
    document.body.appendChild(input);
    return input;
  }

  it('focuses and places the cursor at the end on desktop, even with an empty value', () => {
    setInnerWidth(1024);
    loadFilterUi();
    const input = makeSearchInput('');
    window.autoFocusSearchOnDesktop(input);
    expect(document.activeElement).toBe(input);
  });

  it('does not focus on mobile when the input is empty', () => {
    setInnerWidth(375);
    loadFilterUi();
    const input = makeSearchInput('');
    window.autoFocusSearchOnDesktop(input);
    expect(document.activeElement).not.toBe(input);
  });

  it('focuses on mobile when the input already has a value (preserves cursor position after a search)', () => {
    setInnerWidth(375);
    loadFilterUi();
    const input = makeSearchInput('gift');
    window.autoFocusSearchOnDesktop(input);
    expect(document.activeElement).toBe(input);
    expect(input.selectionStart).toBe(4);
  });

  it('does nothing when passed a null input', () => {
    loadFilterUi();
    expect(() => window.autoFocusSearchOnDesktop(null)).not.toThrow();
  });
});

describe('applyMobileDropdownFilters', () => {
  function makeSelect(id, value) {
    const select = document.createElement('select');
    select.id = id;
    const option = document.createElement('option');
    option.value = value;
    option.selected = true;
    select.appendChild(option);
    document.body.appendChild(select);
    return select;
  }

  function makeSearchInput(value) {
    const input = document.createElement('input');
    input.id = 'searchInput';
    input.value = value || '';
    document.body.appendChild(input);
    return input;
  }

  it('sets a query param from a single mobile filter select (sharing_center usage)', () => {
    loadFilterUi();
    makeSelect('mobileFilterSelect', 'with_me');
    window.applyMobileDropdownFilters({ filter: 'mobileFilterSelect' });
    expect(window.location.href).toBe('https://example.com/inventory?filter=with_me');
  });

  it('sets query params from two mobile filter selects (inventory usage)', () => {
    loadFilterUi();
    makeSelect('mobileStatusFilter', 'active');
    makeSelect('mobileTypeFilter', 'giftcard');
    window.applyMobileDropdownFilters({ status: 'mobileStatusFilter', type: 'mobileTypeFilter' });
    const url = new URL(window.location.href);
    expect(url.searchParams.get('status')).toBe('active');
    expect(url.searchParams.get('type')).toBe('giftcard');
  });

  it('deletes the param instead of setting an empty value', () => {
    setLocation('https://example.com/inventory?status=active');
    loadFilterUi();
    makeSelect('mobileStatusFilter', '');
    window.applyMobileDropdownFilters({ status: 'mobileStatusFilter' });
    const url = new URL(window.location.href);
    expect(url.searchParams.has('status')).toBe(false);
  });

  it('preserves the search query by default', () => {
    loadFilterUi();
    makeSelect('mobileFilterSelect', 'all');
    makeSearchInput('  gift card  ');
    window.applyMobileDropdownFilters({ filter: 'mobileFilterSelect' });
    const url = new URL(window.location.href);
    expect(url.searchParams.get('query')).toBe('gift card');
  });

  it('does not carry the search query when preserveQuery is false', () => {
    loadFilterUi();
    makeSelect('mobileFilterSelect', 'all');
    makeSearchInput('gift card');
    window.applyMobileDropdownFilters({ filter: 'mobileFilterSelect' }, { preserveQuery: false });
    const url = new URL(window.location.href);
    expect(url.searchParams.has('query')).toBe(false);
  });

  it('skips a paramMap entry whose element is missing, without throwing', () => {
    loadFilterUi();
    expect(() => window.applyMobileDropdownFilters({ status: 'doesNotExist' })).not.toThrow();
  });
});
