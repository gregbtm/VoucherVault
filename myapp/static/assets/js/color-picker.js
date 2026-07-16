/**
 * Progressive-enhancement replacement for the browser/OS-native
 * <input type="color"> picker (which looks jarring and inconsistent
 * across devices - see e.g. Samsung Internet's raw HSV-slider dialog).
 * Wraps every type="color" input on the page in a swatch-preview
 * trigger button + popover (curated palette grid + a hex text field
 * for anything else), while leaving the original input in the DOM
 * untouched - same id/name/value, so form submission and any existing
 * code that reads/writes `.value` on it (e.g. a "reset to default"
 * button) keep working exactly as before. If this script fails to
 * load, the native color picker still works as a fallback.
 */

const VV_COLOR_PALETTE = [
  '#4154f1', '#0ea5e9', '#06b6d4', '#14b8a6',
  '#22c55e', '#84cc16', '#eab308', '#f59e0b',
  '#f97316', '#ef4444', '#ec4899', '#d946ef',
  '#a855f7', '#8b5cf6', '#6366f1', '#64748b',
  '#78716c', '#1e293b',
];

function vvCloseAllColorPopovers(exceptWrapper) {
  document.querySelectorAll('.vv-cp-popover').forEach((popover) => {
    if (!exceptWrapper || !exceptWrapper.contains(popover)) {
      popover.hidden = true;
    }
  });
  document.querySelectorAll('.vv-cp-trigger').forEach((trigger) => {
    if (!exceptWrapper || !exceptWrapper.contains(trigger)) {
      trigger.setAttribute('aria-expanded', 'false');
    }
  });
}

function vvEnhanceColorInput(input) {
  if (input.dataset.vvCpEnhanced) return;
  input.dataset.vvCpEnhanced = '1';

  const wrapper = document.createElement('div');
  wrapper.className = 'vv-cp-wrapper';
  input.parentNode.insertBefore(wrapper, input);
  wrapper.appendChild(input);
  input.classList.add('vv-cp-native-input');
  input.setAttribute('tabindex', '-1');
  input.setAttribute('aria-hidden', 'true');

  const trigger = document.createElement('button');
  trigger.type = 'button';
  trigger.className = 'vv-cp-trigger';
  trigger.setAttribute('aria-haspopup', 'true');
  trigger.setAttribute('aria-expanded', 'false');
  trigger.innerHTML =
    '<span class="vv-cp-swatch"></span>' +
    '<span class="vv-cp-hex"></span>' +
    '<i class="bi bi-chevron-down"></i>';
  wrapper.appendChild(trigger);

  const popover = document.createElement('div');
  popover.className = 'vv-cp-popover';
  popover.hidden = true;
  popover.innerHTML =
    '<div class="vv-cp-grid">' +
    VV_COLOR_PALETTE.map((hex) =>
      `<button type="button" class="vv-cp-option" data-color="${hex}" style="background:${hex}" aria-label="${hex}"></button>`
    ).join('') +
    '</div>' +
    '<div class="vv-cp-custom-row">' +
    '<span class="vv-cp-custom-preview"></span>' +
    '<input type="text" class="vv-cp-hex-input" placeholder="#RRGGBB" maxlength="7" aria-label="Custom hex colour">' +
    '</div>';
  wrapper.appendChild(popover);

  const swatchEl = trigger.querySelector('.vv-cp-swatch');
  const hexLabelEl = trigger.querySelector('.vv-cp-hex');
  const hexInput = popover.querySelector('.vv-cp-hex-input');
  const customPreview = popover.querySelector('.vv-cp-custom-preview');
  const options = Array.from(popover.querySelectorAll('.vv-cp-option'));

  function sync() {
    const value = input.value || '#4154f1';
    swatchEl.style.background = value;
    hexLabelEl.textContent = value.toUpperCase();
    if (document.activeElement !== hexInput) {
      hexInput.value = value;
    }
    customPreview.style.background = value;
    options.forEach((opt) => {
      opt.classList.toggle('vv-cp-option-selected', opt.dataset.color.toLowerCase() === value.toLowerCase());
    });
  }

  function setColor(hex) {
    input.value = hex;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    sync();
  }

  trigger.addEventListener('click', (event) => {
    event.stopPropagation();
    const isOpen = !popover.hidden;
    vvCloseAllColorPopovers();
    if (!isOpen) {
      popover.hidden = false;
      trigger.setAttribute('aria-expanded', 'true');
      hexInput.value = input.value;
    }
  });

  options.forEach((opt) => {
    opt.addEventListener('click', () => {
      setColor(opt.dataset.color);
      popover.hidden = true;
      trigger.setAttribute('aria-expanded', 'false');
    });
  });

  hexInput.addEventListener('input', () => {
    let value = hexInput.value.trim();
    if (value && !value.startsWith('#')) value = '#' + value;
    if (/^#[0-9a-fA-F]{6}$/.test(value)) {
      setColor(value);
    } else {
      customPreview.style.background = 'transparent';
    }
  });

  // Re-sync if something else changes the underlying input's value
  // directly (e.g. a "reset to default" button elsewhere on the page).
  input.addEventListener('input', sync);
  input.addEventListener('change', sync);

  sync();
}

document.addEventListener('click', (event) => {
  // A click anywhere inside an open picker (the hex input, whitespace
  // between swatches) shouldn't close it - only a click elsewhere on
  // the page should. Explicit closes (selecting a swatch) are handled
  // directly where they happen, regardless of this check.
  if (event.target.closest('.vv-cp-wrapper')) return;
  vvCloseAllColorPopovers();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') vvCloseAllColorPopovers();
});

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('input[type="color"]').forEach(vvEnhanceColorInput);
});
