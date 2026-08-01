import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { beforeEach, describe, expect, it } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(__dirname, 'color-picker.js'), 'utf-8');

// color-picker.js is a classic (non-module, no 'use strict') script whose
// top-level function declarations (vvEnhanceColorInput, vvCloseAllColorPopovers)
// become real window properties when run this way, exactly as a real
// <script src> tag would - same technique as ui-helpers.test.js.
function loadColorPicker() {
  (0, eval)(source);
}

function makeColorInput(value = '#4154f1') {
  const wrapper = document.createElement('div');
  const input = document.createElement('input');
  input.type = 'color';
  input.value = value;
  wrapper.appendChild(input);
  document.body.appendChild(wrapper);
  return input;
}

beforeEach(() => {
  document.body.innerHTML = '';
});

describe('vvEnhanceColorInput', () => {
  it('wraps the input and builds a trigger + popover with the full palette', () => {
    loadColorPicker();
    const input = makeColorInput();
    window.vvEnhanceColorInput(input);

    const wrapper = input.closest('.vv-cp-wrapper');
    expect(wrapper).not.toBeNull();
    expect(wrapper.querySelector('.vv-cp-trigger')).not.toBeNull();
    expect(wrapper.querySelectorAll('.vv-cp-option').length).toBe(18); // VV_COLOR_PALETTE size
    expect(input.classList.contains('vv-cp-native-input')).toBe(true);
  });

  it('is idempotent - enhancing the same input twice does not double-wrap it', () => {
    loadColorPicker();
    const input = makeColorInput();
    window.vvEnhanceColorInput(input);
    window.vvEnhanceColorInput(input);
    expect(document.querySelectorAll('.vv-cp-wrapper').length).toBe(1);
  });

  it('syncs the trigger swatch/hex label to the input value on load', () => {
    loadColorPicker();
    const input = makeColorInput('#ff0000');
    window.vvEnhanceColorInput(input);
    const wrapper = input.closest('.vv-cp-wrapper');
    expect(wrapper.querySelector('.vv-cp-hex').textContent).toBe('#FF0000');
  });

  it('clicking a palette swatch sets the underlying input value and fires input/change events', () => {
    loadColorPicker();
    const input = makeColorInput('#4154f1');
    window.vvEnhanceColorInput(input);
    const wrapper = input.closest('.vv-cp-wrapper');

    let inputFired = false;
    let changeFired = false;
    input.addEventListener('input', () => { inputFired = true; });
    input.addEventListener('change', () => { changeFired = true; });

    const swatch = wrapper.querySelector('.vv-cp-option[data-color="#22c55e"]');
    swatch.click();

    expect(input.value).toBe('#22c55e');
    expect(inputFired).toBe(true);
    expect(changeFired).toBe(true);
    expect(wrapper.querySelector('.vv-cp-hex').textContent).toBe('#22C55E');
  });

  it('selecting a swatch marks it selected and un-marks the previously selected one', () => {
    loadColorPicker();
    const input = makeColorInput('#4154f1');
    window.vvEnhanceColorInput(input);
    const wrapper = input.closest('.vv-cp-wrapper');

    wrapper.querySelector('.vv-cp-option[data-color="#22c55e"]').click();
    expect(wrapper.querySelector('.vv-cp-option[data-color="#22c55e"]').classList.contains('vv-cp-option-selected')).toBe(true);
    expect(wrapper.querySelector('.vv-cp-option[data-color="#4154f1"]').classList.contains('vv-cp-option-selected')).toBe(false);
  });

  it('typing a valid 6-digit hex into the custom field sets the color', () => {
    loadColorPicker();
    const input = makeColorInput('#4154f1');
    window.vvEnhanceColorInput(input);
    const wrapper = input.closest('.vv-cp-wrapper');
    const hexInput = wrapper.querySelector('.vv-cp-hex-input');

    hexInput.value = 'abcdef'; // no leading #, should be auto-prefixed
    hexInput.dispatchEvent(new Event('input', { bubbles: true }));

    expect(input.value.toLowerCase()).toBe('#abcdef');
  });

  it('does not update the color for an incomplete/invalid hex, just clears the custom preview', () => {
    loadColorPicker();
    const input = makeColorInput('#4154f1');
    window.vvEnhanceColorInput(input);
    const wrapper = input.closest('.vv-cp-wrapper');
    const hexInput = wrapper.querySelector('.vv-cp-hex-input');

    hexInput.value = '#zzz';
    hexInput.dispatchEvent(new Event('input', { bubbles: true }));

    expect(input.value).toBe('#4154f1'); // unchanged
  });

  it('re-syncs when the underlying input value changes from elsewhere (e.g. a reset button)', () => {
    loadColorPicker();
    const input = makeColorInput('#4154f1');
    window.vvEnhanceColorInput(input);
    const wrapper = input.closest('.vv-cp-wrapper');

    input.value = '#000000';
    input.dispatchEvent(new Event('change', { bubbles: true }));

    expect(wrapper.querySelector('.vv-cp-hex').textContent).toBe('#000000');
  });

  it('opens the popover on trigger click and closes other open popovers', () => {
    loadColorPicker();
    const input1 = makeColorInput('#4154f1');
    const input2 = makeColorInput('#22c55e');
    window.vvEnhanceColorInput(input1);
    window.vvEnhanceColorInput(input2);

    const trigger1 = input1.closest('.vv-cp-wrapper').querySelector('.vv-cp-trigger');
    const popover1 = input1.closest('.vv-cp-wrapper').querySelector('.vv-cp-popover');
    const trigger2 = input2.closest('.vv-cp-wrapper').querySelector('.vv-cp-trigger');
    const popover2 = input2.closest('.vv-cp-wrapper').querySelector('.vv-cp-popover');

    trigger1.click();
    expect(popover1.hidden).toBe(false);
    expect(trigger1.getAttribute('aria-expanded')).toBe('true');

    trigger2.click();
    expect(popover2.hidden).toBe(false);
    expect(popover1.hidden).toBe(true);
    expect(trigger1.getAttribute('aria-expanded')).toBe('false');
  });

  it('clicking the same trigger again toggles the popover closed', () => {
    loadColorPicker();
    const input = makeColorInput();
    window.vvEnhanceColorInput(input);
    const trigger = input.closest('.vv-cp-wrapper').querySelector('.vv-cp-trigger');
    const popover = input.closest('.vv-cp-wrapper').querySelector('.vv-cp-popover');

    trigger.click();
    expect(popover.hidden).toBe(false);
    trigger.click();
    expect(popover.hidden).toBe(true);
  });
});

describe('document-level close handlers', () => {
  it('a click outside any picker closes every open popover', () => {
    loadColorPicker();
    const input = makeColorInput();
    window.vvEnhanceColorInput(input);
    const trigger = input.closest('.vv-cp-wrapper').querySelector('.vv-cp-trigger');
    const popover = input.closest('.vv-cp-wrapper').querySelector('.vv-cp-popover');

    trigger.click();
    expect(popover.hidden).toBe(false);

    document.body.click();
    expect(popover.hidden).toBe(true);
  });

  it('the Escape key closes every open popover', () => {
    loadColorPicker();
    const input = makeColorInput();
    window.vvEnhanceColorInput(input);
    const trigger = input.closest('.vv-cp-wrapper').querySelector('.vv-cp-trigger');
    const popover = input.closest('.vv-cp-wrapper').querySelector('.vv-cp-popover');

    trigger.click();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(popover.hidden).toBe(true);
  });
});

describe('DOMContentLoaded auto-enhancement', () => {
  it('enhances every input[type="color"] already on the page once DOMContentLoaded fires', () => {
    const input = makeColorInput();
    loadColorPicker();
    document.dispatchEvent(new Event('DOMContentLoaded', { bubbles: true, cancelable: true }));
    expect(input.dataset.vvCpEnhanced).toBe('1');
  });
});
