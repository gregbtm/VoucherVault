import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(__dirname, 'scanner.js'), 'utf-8');

// Mirrors the real element IDs scanner.js queries at top level in
// create-item.html/edit-item.html - a handful (loadingMessage,
// outputMessage) are touched unconditionally at load and would throw on a
// null element, so every one of them needs a real node here even though
// this suite only exercises the format-guessing/confidence-styling
// functions, not the camera/file-scan flows.
//
// #code_type's <option> list must stay in sync with create-item.html's -
// assigning select.value to a string with no matching <option> silently
// no-ops (leaves the select unchanged) rather than throwing, exactly like
// a real browser, so a missing option here reads as a confusing scanner.js
// failure instead of the fixture gap it actually is.
const FIXTURE_HTML = `
  <input type="text" id="redeem_code">
  <select id="type">
    <option value="giftcard">Gift Card</option>
    <option value="travelpass">Travel Pass</option>
  </select>
  <input type="text" id="issuer">
  <select id="code_type">
    <option value="qrcode">QR Code</option>
    <option value="none">No Barcode</option>
    <option value="ean13">EAN-13</option>
    <option value="ean8">EAN-8</option>
    <option value="code128">Code 128</option>
    <option value="code39">Code 39</option>
    <option value="code93">Code 93</option>
    <option value="codabar">Codabar</option>
    <option value="upca">UPC-A</option>
    <option value="upce">UPC-E</option>
    <option value="isbn13">ISBN-13</option>
    <option value="issn">ISSN</option>
    <option value="pdf417">PDF417</option>
    <option value="datamatrix">Data Matrix</option>
    <option value="azteccode">Aztec Code</option>
    <option value="interleaved2of5">Interleaved 2 of 5</option>
  </select>
  <button id="startScanner"></button>
  <button id="scanFromFile"></button>
  <input type="file" id="fileInput">
  <i id="fileIcon"></i>
  <div id="qrScannerSection"></div>
  <video id="video"></video>
  <select id="sourceSelect"></select>
  <i id="cameraIcon"></i>
  <div id="loadingMessage"></div>
  <div id="outputMessage"></div>
  <div id="cropSection"></div>
  <canvas id="cropCanvas"></canvas>
  <button id="scanCroppedBtn"></button>
  <button id="cancelCropBtn"></button>
  <button id="resetSelectionBtn"></button>
  <div id="cropPreviewSection"></div>
  <canvas id="cropPreviewCanvas"></canvas>
  <input type="file" id="file" data-undo-correction-url="/api/v1/ocr/undo-correction/">
  <small id="duplicate-image-warning"></small>
  <p id="codeTypeHint"></p>
  <input type="hidden" name="csrfmiddlewaretoken" value="test-csrf-token">
  <div id="statusEl"></div>
`;

// scanner.js is a classic (non-module) script, loaded via <script src> in
// create-item.html/edit-item.html - indirect eval runs the real production
// file the same way a <script> tag would (top-level `function`
// declarations become globals; top-level `const`/`let` stay private to
// the functions closed over them, exactly as in a real page), so this is
// testing the actual shipped code, not a reimplementation of it.
function loadScanner() {
  window.ZXing = {
    BrowserMultiFormatReader: class {},
    NotFoundException: class extends Error {},
    DecodeHintType: { TRY_HARDER: 'TRY_HARDER' },
  };
  (0, eval)(source);
}

beforeEach(() => {
  document.body.innerHTML = FIXTURE_HTML;
  for (const fn of [
    'markAutoFilled', 'markSuggested', 'applyDetectedFormat', 'guessCodeTypeFromValue',
    'showCodeTypeHint', 'renderHealedFieldsChip', 'undoHealedField',
  ]) {
    delete window[fn];
  }
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ retracted: true }) }));
});

describe('markAutoFilled / markSuggested', () => {
  it('markAutoFilled adds the confident highlight and clears any prior suggested-fill', () => {
    loadScanner();
    const field = document.getElementById('code_type');
    field.classList.add('suggested-fill'); // simulate an earlier guess-fill on this same field
    window.markAutoFilled(field);
    expect(field.classList.contains('auto-filled')).toBe(true);
    expect(field.classList.contains('suggested-fill')).toBe(false);
  });

  it('markSuggested adds the guess highlight and clears any prior auto-filled', () => {
    loadScanner();
    const field = document.getElementById('code_type');
    field.classList.add('auto-filled'); // simulate an earlier exact-fill on this same field
    window.markSuggested(field);
    expect(field.classList.contains('suggested-fill')).toBe(true);
    expect(field.classList.contains('auto-filled')).toBe(false);
  });

  it('regression: re-filling a field never leaves both highlight classes on at once', () => {
    // The exact bug shipped and fixed in Phase 86 - applyDetectedFormat
    // called with 'exact' confidence, then again with 'guess' confidence
    // (e.g. a re-scan), used to stack .auto-filled and .suggested-fill
    // together because neither mark* function cleared the other's class.
    loadScanner();
    const field = document.getElementById('code_type');
    window.markAutoFilled(field);
    window.markSuggested(field);
    const both = field.classList.contains('auto-filled') && field.classList.contains('suggested-fill');
    expect(both).toBe(false);
  });

  it('clears the highlight the moment the user edits the field', () => {
    loadScanner();
    const field = document.getElementById('code_type');
    window.markAutoFilled(field);
    field.dispatchEvent(new Event('change'));
    expect(field.classList.contains('auto-filled')).toBe(false);
  });
});

describe('applyDetectedFormat', () => {
  it('exact confidence (default) uses the confident style and a plain hint', () => {
    loadScanner();
    window.applyDetectedFormat('azteccode', 'barcode in photo');
    const field = document.getElementById('code_type');
    const hint = document.getElementById('codeTypeHint');
    expect(field.value).toBe('azteccode');
    expect(field.classList.contains('auto-filled')).toBe(true);
    expect(field.classList.contains('suggested-fill')).toBe(false);
    expect(hint.textContent).toBe('Detected from barcode in photo: Aztec Code');
    expect(hint.classList.contains('text-warning')).toBe(false);
  });

  it('guess confidence uses the weaker style, a warning-colored hint, and says so', () => {
    loadScanner();
    window.applyDetectedFormat('qrcode', 'AI photo scan', 'guess');
    const field = document.getElementById('code_type');
    const hint = document.getElementById('codeTypeHint');
    expect(field.value).toBe('qrcode');
    expect(field.classList.contains('suggested-fill')).toBe(true);
    expect(field.classList.contains('auto-filled')).toBe(false);
    expect(hint.textContent).toContain("AI's best guess from AI photo scan");
    expect(hint.textContent).toContain('scan the barcode directly for a sure match');
    expect(hint.classList.contains('text-warning')).toBe(true);
  });

  it('a later exact decode overrides an earlier guess on the same field', () => {
    loadScanner();
    window.applyDetectedFormat('qrcode', 'AI photo scan', 'guess');
    window.applyDetectedFormat('azteccode', 'barcode in photo');
    const field = document.getElementById('code_type');
    expect(field.value).toBe('azteccode');
    expect(field.classList.contains('auto-filled')).toBe(true);
    expect(field.classList.contains('suggested-fill')).toBe(false);
  });

  it('does nothing when the format is falsy', () => {
    loadScanner();
    const field = document.getElementById('code_type');
    field.value = 'none';
    window.applyDetectedFormat('', 'somewhere');
    expect(field.value).toBe('none');
  });
});

describe('guessCodeTypeFromValue (via the redeem_code input listener)', () => {
  function type(value) {
    const input = document.getElementById('redeem_code');
    input.value = value;
    input.dispatchEvent(new Event('input'));
  }

  it('guesses ean13 for a 13-digit numeric code, marked as a guess', () => {
    loadScanner();
    type('9780134685991');
    const field = document.getElementById('code_type');
    expect(field.value).toBe('ean13');
    expect(field.classList.contains('suggested-fill')).toBe(true);
  });

  it('guesses code39 for an uppercase alphanumeric code', () => {
    loadScanner();
    type('ABC-123');
    expect(document.getElementById('code_type').value).toBe('code39');
  });

  it('falls back to code128 for anything else', () => {
    loadScanner();
    type('mixedCase-value!');
    expect(document.getElementById('code_type').value).toBe('code128');
  });

  it('never overrides a type the user already picked manually', () => {
    loadScanner();
    const select = document.getElementById('code_type');
    select.value = 'datamatrix';
    select.dispatchEvent(new Event('change')); // user's own explicit pick
    type('9780134685991'); // would normally guess ean13
    expect(select.value).toBe('datamatrix');
  });
});

describe('renderHealedFieldsChip / undoHealedField', () => {
  it('renders one Undo button per healed field that has an original recorded', () => {
    loadScanner();
    const statusEl = document.getElementById('statusEl');
    window.renderHealedFieldsChip(statusEl, ['issuer'], {
      issuer: { ai_value: 'Merchant Ay', correction_id: 42 },
    });
    const chip = statusEl.querySelector('.scan-chip-learned');
    expect(chip.textContent).toContain('Learned from your past corrections: issuer');
    expect(chip.querySelectorAll('.scan-chip-undo-btn').length).toBe(1);
  });

  it('skips the Undo button for a healed field with no original recorded', () => {
    // Defensive: shouldn't happen in practice (apply_learned_corrections
    // always records an original alongside every healed field), but a
    // missing entry must not crash chip rendering.
    loadScanner();
    const statusEl = document.getElementById('statusEl');
    window.renderHealedFieldsChip(statusEl, ['issuer'], {});
    expect(statusEl.querySelectorAll('.scan-chip-undo-btn').length).toBe(0);
  });

  it('never puts the raw ai_value into the rendered HTML', () => {
    // The whole point of closing over healedOriginals instead of using a
    // data-* attribute: OCR-read text is untrusted and must never be
    // interpolated into innerHTML unescaped.
    loadScanner();
    const statusEl = document.getElementById('statusEl');
    const dangerous = '<img src=x onerror=alert(1)>';
    window.renderHealedFieldsChip(statusEl, ['issuer'], {
      issuer: { ai_value: dangerous, correction_id: 1 },
    });
    expect(statusEl.innerHTML).not.toContain(dangerous);
    expect(statusEl.querySelectorAll('img').length).toBe(0);
  });

  it('undo reverts the field to the original AI value and clears fill highlighting', () => {
    loadScanner();
    const field = document.getElementById('issuer');
    field.value = 'Merchant A';
    field.classList.add('auto-filled');
    const button = document.createElement('button');
    window.undoHealedField('issuer', { ai_value: 'Merchant Ay', correction_id: 42 }, button);
    expect(field.value).toBe('Merchant Ay');
    expect(field.classList.contains('auto-filled')).toBe(false);
  });

  it('undo dispatches a bubbling change event so field-specific listeners (e.g. #type toggling other sections) still fire', () => {
    loadScanner();
    const field = document.getElementById('type');
    const handler = vi.fn();
    document.body.addEventListener('change', handler);
    const button = document.createElement('button');
    window.undoHealedField('type', { ai_value: 'giftcard', correction_id: 7 }, button);
    expect(field.value).toBe('giftcard');
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('undo disables the button and marks it "Undone"', () => {
    loadScanner();
    const button = document.createElement('button');
    window.undoHealedField('issuer', { ai_value: 'Merchant Ay', correction_id: 42 }, button);
    expect(button.disabled).toBe(true);
    expect(button.textContent).toBe('Undone');
  });

  it('undo posts the correction_id to the undo-correction endpoint', () => {
    loadScanner();
    const button = document.createElement('button');
    window.undoHealedField('issuer', { ai_value: 'Merchant Ay', correction_id: 42 }, button);
    expect(fetch).toHaveBeenCalledWith('/api/v1/ocr/undo-correction/', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ correction_id: 42 }),
    }));
    const headers = fetch.mock.calls[0][1].headers;
    expect(headers['X-CSRFToken']).toBe('test-csrf-token');
  });

  it('clicking the rendered Undo button wires through to undoHealedField', () => {
    loadScanner();
    const statusEl = document.getElementById('statusEl');
    const field = document.getElementById('issuer');
    field.value = 'Merchant A';
    window.renderHealedFieldsChip(statusEl, ['issuer'], {
      issuer: { ai_value: 'Merchant Ay', correction_id: 42 },
    });
    statusEl.querySelector('.scan-chip-undo-btn').click();
    expect(field.value).toBe('Merchant Ay');
    expect(fetch).toHaveBeenCalledWith('/api/v1/ocr/undo-correction/', expect.objectContaining({
      body: JSON.stringify({ correction_id: 42 }),
    }));
  });
});
