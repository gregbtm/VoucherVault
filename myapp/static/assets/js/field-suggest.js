/**
 * Progressive-enhancement "suggest from your recent items" button, for a
 * handful of item-form fields (issuer, logo slug, wallet, discount
 * applied - see the data-vv-suggest-field attribute on those fields in
 * create-item.html/edit-item.html). Replaces the old behavior of silently
 * auto-filling whatever an AI scan left blank: instead, a small lightbulb
 * button appears next to the field's label only while the field is empty,
 * and clicking it opens a popover of up to 5 ranked suggestions (backed by
 * views.suggest_field_options) to pick from explicitly - never a silent
 * fill. The button disappears the moment the field has any value, from
 * whatever source (typed, scanned, or picked from the popover).
 */

function vvCloseAllSuggestPopovers(exceptWrapper) {
  document.querySelectorAll('.vv-sg-popover').forEach((popover) => {
    if (!exceptWrapper || !exceptWrapper.contains(popover)) popover.hidden = true;
  });
  document.querySelectorAll('.vv-sg-btn').forEach((btn) => {
    if (!exceptWrapper || !exceptWrapper.contains(btn)) btn.setAttribute('aria-expanded', 'false');
  });
  vvActiveSuggestReposition = null;
}

// Set to the open popover's positionPopover function while one is open, so
// the scroll listener below can track it instead of closing it - see why
// scroll needs handling at all where this is used.
let vvActiveSuggestReposition = null;

function vvEscapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

const vvSuggestFieldSyncFns = [];

function vvEnhanceSuggestField(field) {
  if (field.dataset.vvSgEnhanced) return;
  field.dataset.vvSgEnhanced = '1';

  const fieldName = field.dataset.vvSuggestField;
  const endpoint = field.dataset.vvSuggestUrl;
  const typeSourceSelector = field.dataset.vvSuggestTypeSource;
  const label = field.id && document.querySelector(`label[for="${field.id}"]`);
  if (!label || !endpoint) return;

  // Inserted as the label's next sibling, not a child of it: some forms
  // (create/edit-item's type-conditional field logic) overwrite a label's
  // innerHTML wholesale when the item type changes (e.g. "Issuer" ->
  // "Store" for a loyalty card) - a button appended inside the label
  // would get silently wiped out the first time that ran.
  const formGroup = field.closest('.form-group') || field.parentElement;
  const previousPosition = getComputedStyle(formGroup).position;
  if (previousPosition === 'static') formGroup.style.position = 'relative';

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'vv-sg-btn';
  btn.setAttribute('aria-haspopup', 'true');
  btn.setAttribute('aria-expanded', 'false');
  btn.title = 'Suggestions from your recent items';
  btn.innerHTML = '<i class="bi bi-lightbulb"></i>';
  label.insertAdjacentElement('afterend', btn);

  // Appended to <body> and positioned with position:fixed (computed from
  // the button's own screen position on open), not nested under the field
  // the way .vv-sg-btn is - .form-card has its own overflow:hidden (for
  // its rounded corners), which would silently clip a popover positioned
  // relative to an ancestor inside it the same way .header's overflow:hidden
  // once clipped the profile dropdown. Fixed positioning escapes that
  // entirely, at the cost of needing to reposition on scroll (handled
  // below) since it won't track the button's position on its own.
  const popover = document.createElement('div');
  popover.className = 'vv-sg-popover';
  popover.hidden = true;
  popover.innerHTML = '<div class="vv-sg-popover-title"></div><div class="vv-sg-options"></div>';
  document.body.appendChild(popover);

  const titleEl = popover.querySelector('.vv-sg-popover-title');
  const optionsEl = popover.querySelector('.vv-sg-options');

  let cache = null;
  let cachedType = null;

  function currentType() {
    const typeSource = typeSourceSelector && document.querySelector(typeSourceSelector);
    return typeSource ? typeSource.value : '';
  }

  function syncVisibility() {
    // A disabled field (e.g. Wallet, locked to "Travel Pass" for that item
    // type) can't be filled at all - no point offering a suggestion for it.
    const hasValue = !!field.value.trim() || field.disabled;
    btn.hidden = hasValue;
    if (hasValue) {
      popover.hidden = true;
      btn.setAttribute('aria-expanded', 'false');
      if (vvActiveSuggestReposition === positionPopover) vvActiveSuggestReposition = null;
    }
  }
  vvSuggestFieldSyncFns.push(syncVisibility);

  function renderOptions(options) {
    if (!options.length) {
      titleEl.textContent = 'No suggestions yet';
      optionsEl.innerHTML = '';
      return;
    }
    titleEl.textContent = 'Suggestions from your recent items';
    optionsEl.innerHTML = options
      .map((opt, i) => `<button type="button" class="vv-sg-option" data-index="${i}">${vvEscapeHtml(opt.label)}</button>`)
      .join('');
    optionsEl.querySelectorAll('.vv-sg-option').forEach((el, i) => {
      el.addEventListener('click', () => {
        field.value = options[i].value;
        field.dispatchEvent(new Event('input', { bubbles: true }));
        field.dispatchEvent(new Event('change', { bubbles: true }));
        popover.hidden = true;
        btn.setAttribute('aria-expanded', 'false');
        vvActiveSuggestReposition = null;
        syncVisibility();
      });
    });
  }

  // Called once before the popover is shown (rough position, so it's never
  // rendered at 0,0 for a frame) and again after its real content is in
  // place (renderOptions may change its height by several suggestion rows)
  // - the second pass flips it above the button instead of below when the
  // current content would overflow the bottom of the viewport, since a
  // position:fixed element that renders past the viewport edge can't be
  // reached by scrolling the page the way an absolutely-positioned one could.
  function positionPopover() {
    const rect = btn.getBoundingClientRect();
    const popoverWidth = Math.min(280, window.innerWidth - 16);
    let left = rect.right - popoverWidth;
    left = Math.max(8, Math.min(left, window.innerWidth - popoverWidth - 8));
    popover.style.width = `${popoverWidth}px`;
    popover.style.left = `${left}px`;

    const popoverHeight = popover.offsetHeight;
    const opensBelow = rect.bottom + 4 + popoverHeight <= window.innerHeight - 8;
    if (opensBelow || rect.top - popoverHeight - 4 < 8) {
      popover.style.top = `${rect.bottom + 4}px`;
    } else {
      popover.style.top = `${rect.top - popoverHeight - 4}px`;
    }
  }

  btn.addEventListener('click', (event) => {
    event.stopPropagation();
    event.preventDefault();
    const isOpen = !popover.hidden;
    vvCloseAllSuggestPopovers();
    if (isOpen) return;

    const type = currentType();
    positionPopover();
    popover.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    vvActiveSuggestReposition = positionPopover;

    if (cache && cachedType === type) {
      renderOptions(cache);
      positionPopover();
      return;
    }
    titleEl.textContent = 'Loading…';
    optionsEl.innerHTML = '';
    positionPopover();
    fetch(`${endpoint}?type=${encodeURIComponent(type)}&field=${encodeURIComponent(fieldName)}`)
      .then((response) => (response.ok ? response.json() : { options: [] }))
      .then((data) => {
        cache = data.options || [];
        cachedType = type;
        renderOptions(cache);
        positionPopover();
      })
      .catch(() => {
        renderOptions([]);
        positionPopover();
      });
  });

  field.addEventListener('input', syncVisibility);
  field.addEventListener('change', syncVisibility);
  syncVisibility();
}

// Re-checks every enhanced field's empty/filled state - called after an AI
// scan programmatically sets .value on several fields at once (which
// doesn't fire input/change events the per-field listeners above rely on).
window.vvRefreshFieldSuggestButtons = function () {
  vvSuggestFieldSyncFns.forEach((sync) => sync());
};

document.addEventListener('click', (event) => {
  if (event.target.closest('.vv-sg-popover') || event.target.closest('.vv-sg-btn')) return;
  vvCloseAllSuggestPopovers();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') vvCloseAllSuggestPopovers();
});
// Fixed-position popovers don't track their button while scrolling (see
// why they're fixed rather than absolute above), so the open one is
// repositioned on every scroll event rather than closed - closing outright
// would also fire the moment a suggest button is clicked while off-screen,
// since page.css sets `html { scroll-behavior: smooth }` and the browser's
// own "scroll the clicked element into view" keeps emitting scroll events
// for a few hundred ms after the click that opened it.
document.addEventListener('scroll', () => {
  if (vvActiveSuggestReposition) vvActiveSuggestReposition();
}, true);

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-vv-suggest-field]').forEach(vvEnhanceSuggestField);
});
