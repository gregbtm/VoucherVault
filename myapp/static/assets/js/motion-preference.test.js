import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(__dirname, 'motion-preference.js'), 'utf-8');

// motion-preference.js is a one-line script that attaches
// window.VVPrefersReducedMotion as a side effect, exactly as it's loaded
// via <script src> in base.html - indirect eval runs it the same way a
// real <script> tag would, so the real production file is under test.
function loadMotionPreference() {
  (0, eval)(source);
}

function stubMatchMedia(reducedMotion) {
  window.matchMedia = vi.fn().mockImplementation((query) => ({
    matches: query === '(prefers-reduced-motion: reduce)' ? reducedMotion : false,
    media: query,
  }));
}

beforeEach(() => {
  delete window.VVPrefersReducedMotion;
});

it('is true when the OS prefers reduced motion', () => {
  stubMatchMedia(true);
  loadMotionPreference();
  expect(window.VVPrefersReducedMotion).toBe(true);
});

it('is false when the OS does not prefer reduced motion', () => {
  stubMatchMedia(false);
  loadMotionPreference();
  expect(window.VVPrefersReducedMotion).toBe(false);
});

it('is false without throwing when window.matchMedia is entirely absent', () => {
  const original = window.matchMedia;
  delete window.matchMedia;
  expect(() => loadMotionPreference()).not.toThrow();
  expect(window.VVPrefersReducedMotion).toBe(false);
  window.matchMedia = original;
});
