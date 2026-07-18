import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(__dirname, 'field-suggest.js'), 'utf-8');

// field-suggest.js is a classic (non-module) script that wires itself up
// via DOMContentLoaded, exactly as it's loaded via <script src> in
// base.html - indirect eval runs the real production file, not a
// reimplementation of it.
function loadFieldSuggest() {
  (0, eval)(source);
}

function buildForm({ value = '', disabled = false } = {}) {
  document.body.innerHTML = `
    <select id="type"><option value="giftcard" selected>Gift Card</option></select>
    <div class="form-group">
      <label class="form-label" for="issuer">Issuer</label>
      <input type="text" id="issuer" value="${value}" ${disabled ? 'disabled' : ''}
             data-vv-suggest-field="issuer" data-vv-suggest-url="/items/suggest-field-options/"
             data-vv-suggest-type-source="#type">
    </div>
  `;
}

beforeEach(() => {
  delete window.vvRefreshFieldSuggestButtons;
  vi.stubGlobal('fetch', vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

describe('field suggestion button visibility', () => {
  it('shows the suggest button next to an empty field', () => {
    buildForm({ value: '' });
    loadFieldSuggest();
    document.dispatchEvent(new Event('DOMContentLoaded'));
    const btn = document.querySelector('.vv-sg-btn');
    expect(btn).not.toBeNull();
    expect(btn.hidden).toBe(false);
  });

  it('does not show the button for a field that already has a value', () => {
    buildForm({ value: 'Amazon' });
    loadFieldSuggest();
    document.dispatchEvent(new Event('DOMContentLoaded'));
    expect(document.querySelector('.vv-sg-btn').hidden).toBe(true);
  });

  it('does not show the button for a disabled field', () => {
    buildForm({ value: '', disabled: true });
    loadFieldSuggest();
    document.dispatchEvent(new Event('DOMContentLoaded'));
    expect(document.querySelector('.vv-sg-btn').hidden).toBe(true);
  });

  it('is inserted as a sibling of the label, not a child of it, so it survives label.innerHTML being replaced', () => {
    buildForm({ value: '' });
    loadFieldSuggest();
    document.dispatchEvent(new Event('DOMContentLoaded'));
    const label = document.querySelector('label[for="issuer"]');
    label.innerHTML = 'Store <span class="required">*</span>';
    expect(document.querySelector('.vv-sg-btn')).not.toBeNull();
    expect(document.querySelector('.vv-sg-btn').hidden).toBe(false);
  });

  it('hides the button once the field gains a value via a real input event', () => {
    buildForm({ value: '' });
    loadFieldSuggest();
    document.dispatchEvent(new Event('DOMContentLoaded'));
    const field = document.getElementById('issuer');
    field.value = 'Tesco';
    field.dispatchEvent(new Event('input'));
    expect(document.querySelector('.vv-sg-btn').hidden).toBe(true);
  });

  it('vvRefreshFieldSuggestButtons re-syncs visibility after a value is set without dispatching events', () => {
    buildForm({ value: '' });
    loadFieldSuggest();
    document.dispatchEvent(new Event('DOMContentLoaded'));
    const field = document.getElementById('issuer');
    field.value = 'Tesco'; // simulates an AI scan setting .value directly
    expect(document.querySelector('.vv-sg-btn').hidden).toBe(false); // stale until refreshed
    window.vvRefreshFieldSuggestButtons();
    expect(document.querySelector('.vv-sg-btn').hidden).toBe(true);
  });
});

describe('field suggestion popover', () => {
  it('fetches ranked options scoped to the current type and field on click, and renders them', async () => {
    buildForm({ value: '' });
    fetch.mockResolvedValue({
      ok: true,
      json: async () => ({ options: [{ value: 'Tesco', label: 'Tesco' }, { value: 'Asda', label: 'Asda' }] }),
    });
    loadFieldSuggest();
    document.dispatchEvent(new Event('DOMContentLoaded'));

    document.querySelector('.vv-sg-btn').click();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(fetch).toHaveBeenCalledWith('/items/suggest-field-options/?type=giftcard&field=issuer');
    const options = document.querySelectorAll('.vv-sg-option');
    expect(options).toHaveLength(2);
    expect(options[0].textContent).toBe('Tesco');
  });

  it('fills the field and hides the popover and button when a suggestion is picked', async () => {
    buildForm({ value: '' });
    fetch.mockResolvedValue({
      ok: true,
      json: async () => ({ options: [{ value: 'Tesco', label: 'Tesco' }] }),
    });
    loadFieldSuggest();
    document.dispatchEvent(new Event('DOMContentLoaded'));

    document.querySelector('.vv-sg-btn').click();
    await new Promise((resolve) => setTimeout(resolve, 0));

    document.querySelector('.vv-sg-option').click();

    expect(document.getElementById('issuer').value).toBe('Tesco');
    expect(document.querySelector('.vv-sg-popover').hidden).toBe(true);
    expect(document.querySelector('.vv-sg-btn').hidden).toBe(true);
  });

  it('shows a "no suggestions" message when the endpoint returns none', async () => {
    buildForm({ value: '' });
    fetch.mockResolvedValue({ ok: true, json: async () => ({ options: [] }) });
    loadFieldSuggest();
    document.dispatchEvent(new Event('DOMContentLoaded'));

    document.querySelector('.vv-sg-btn').click();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(document.querySelector('.vv-sg-popover-title').textContent).toMatch(/no suggestions/i);
  });

  it('closes the popover on Escape', async () => {
    buildForm({ value: '' });
    fetch.mockResolvedValue({ ok: true, json: async () => ({ options: [{ value: 'Tesco', label: 'Tesco' }] }) });
    loadFieldSuggest();
    document.dispatchEvent(new Event('DOMContentLoaded'));

    document.querySelector('.vv-sg-btn').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.querySelector('.vv-sg-popover').hidden).toBe(false);

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(document.querySelector('.vv-sg-popover').hidden).toBe(true);
  });

  it('closes the popover on a click outside it', async () => {
    buildForm({ value: '' });
    fetch.mockResolvedValue({ ok: true, json: async () => ({ options: [{ value: 'Tesco', label: 'Tesco' }] }) });
    loadFieldSuggest();
    document.dispatchEvent(new Event('DOMContentLoaded'));

    document.querySelector('.vv-sg-btn').click();
    await new Promise((resolve) => setTimeout(resolve, 0));

    document.body.click();
    expect(document.querySelector('.vv-sg-popover').hidden).toBe(true);
  });

  it('stays open on scroll instead of closing (repositions instead)', async () => {
    // Regression test: html{scroll-behavior:smooth} means clicking a
    // suggest button that's off-screen keeps emitting scroll events for a
    // few hundred ms after the click (as the browser's native scroll-into-
    // view animates) - closing on scroll used to immediately hide the
    // popover the same click had just opened.
    buildForm({ value: '' });
    fetch.mockResolvedValue({ ok: true, json: async () => ({ options: [{ value: 'Tesco', label: 'Tesco' }] }) });
    loadFieldSuggest();
    document.dispatchEvent(new Event('DOMContentLoaded'));

    document.querySelector('.vv-sg-btn').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.querySelector('.vv-sg-popover').hidden).toBe(false);

    document.dispatchEvent(new Event('scroll'));
    expect(document.querySelector('.vv-sg-popover').hidden).toBe(false);
  });
});
