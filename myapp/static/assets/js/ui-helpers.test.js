import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(__dirname, 'ui-helpers.js'), 'utf-8');

// ui-helpers.js is a classic (non-module) script that attaches
// window.showToast/window.setButtonLoading as a side effect, exactly as
// it's loaded via <script src> in base.html - indirect eval runs it the
// same way a real <script> tag would, so the real production file is
// under test, not a reimplementation of it.
function loadUiHelpers() {
  (0, eval)(source);
}

beforeEach(() => {
  document.body.innerHTML = '<div id="toast-stack"></div>';
  delete window.showToast;
  delete window.setButtonLoading;
});

describe('showToast', () => {
  it('appends a dismissible alert with the right tag class and icon', () => {
    loadUiHelpers();
    const el = window.showToast('Saved.', 'success');
    expect(el.className).toContain('alert-success');
    expect(el.textContent).toContain('Saved.');
    expect(el.querySelector('.btn-close')).not.toBeNull();
    expect(document.getElementById('toast-stack').contains(el)).toBe(true);
  });

  it('defaults to the info tag when none is given', () => {
    loadUiHelpers();
    const el = window.showToast('Heads up.');
    expect(el.className).toContain('alert-info');
  });

  it('auto-dismisses after the timeout', () => {
    vi.useFakeTimers();
    loadUiHelpers();
    const el = window.showToast('Bye soon.', 'info');
    expect(document.getElementById('toast-stack').contains(el)).toBe(true);
    vi.advanceTimersByTime(5000);
    expect(document.getElementById('toast-stack').contains(el)).toBe(false);
    vi.useRealTimers();
  });
});

describe('setButtonLoading', () => {
  function makeButton() {
    const btn = document.createElement('button');
    btn.innerHTML = '<i class="bi bi-arrow-repeat"></i> Check for updates';
    return btn;
  }

  it('disables the button and prepends a spinner while loading', () => {
    loadUiHelpers();
    const btn = makeButton();
    const originalHtml = btn.innerHTML;

    window.setButtonLoading(btn, true);

    expect(btn.disabled).toBe(true);
    expect(btn.querySelector('.spinner-border')).not.toBeNull();
    expect(btn.innerHTML).toContain(originalHtml);
  });

  it('restores the exact original content and re-enables the button', () => {
    loadUiHelpers();
    const btn = makeButton();
    const originalHtml = btn.innerHTML;

    window.setButtonLoading(btn, true);
    window.setButtonLoading(btn, false);

    expect(btn.disabled).toBe(false);
    expect(btn.innerHTML).toBe(originalHtml);
    expect(btn.querySelector('.spinner-border')).toBeNull();
  });

  it('is idempotent - calling loading=true twice never loses the real original content', () => {
    loadUiHelpers();
    const btn = makeButton();
    const originalHtml = btn.innerHTML;

    window.setButtonLoading(btn, true);
    window.setButtonLoading(btn, true); // simulates a second in-flight call before the first resolves
    window.setButtonLoading(btn, false);

    expect(btn.innerHTML).toBe(originalHtml);
  });

  it('does nothing when passed a null button', () => {
    loadUiHelpers();
    expect(() => window.setButtonLoading(null, true)).not.toThrow();
  });
});
