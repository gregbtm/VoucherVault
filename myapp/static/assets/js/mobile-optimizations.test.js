import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(__dirname, 'mobile-optimizations.js'), 'utf-8');

// mobile-optimizations.js exposes window.MobileOptimizations = { virtualScroll,
// prefersReducedMotion } for manual re-use, and also auto-runs the
// motion-prefs pass once at load time (document.readyState is 'complete'
// in jsdom by default, so the immediate branch runs, not the
// DOMContentLoaded-deferred one).
function loadMobileOptimizations() {
  delete window.MobileOptimizations;
  (0, eval)(source);
}

beforeEach(() => {
  document.body.innerHTML = '';
  window.VVPrefersReducedMotion = false;
});

describe('window.MobileOptimizations export', () => {
  it('exposes virtualScroll/prefersReducedMotion', () => {
    loadMobileOptimizations();
    expect(typeof window.MobileOptimizations.virtualScroll).toBe('function');
    expect(window.MobileOptimizations.prefersReducedMotion).toBe(false);
  });

  it('captures prefers-reduced-motion at load time', () => {
    window.VVPrefersReducedMotion = true;
    loadMobileOptimizations();
    expect(window.MobileOptimizations.prefersReducedMotion).toBe(true);
  });
});

describe('virtualScroll', () => {
  it('does nothing if the container is not found', () => {
    loadMobileOptimizations();
    expect(() => window.MobileOptimizations.virtualScroll('#nope', '.item')).not.toThrow();
  });

  it('does nothing for a short list (under 50 items) - no perf win worth the complexity', () => {
    document.body.innerHTML = '<div id="list">' + '<div class="item"></div>'.repeat(10) + '</div>';
    loadMobileOptimizations();
    window.MobileOptimizations.virtualScroll('#list', '.item', 20);
    const items = document.querySelectorAll('.item');
    // None should have been hidden by a display:none toggle, since the
    // function returns before wiring any visibility logic at all.
    items.forEach((item) => expect(item.style.display).toBe(''));
  });

  it('hides items outside the viewport for a long list (50+ items)', () => {
    document.body.innerHTML = '<div id="list" style="height:100px;">' + '<div class="item"></div>'.repeat(60) + '</div>';
    const container = document.getElementById('list');
    Object.defineProperty(container, 'clientHeight', { value: 100, configurable: true });
    Object.defineProperty(container, 'scrollTop', { value: 0, configurable: true, writable: true });

    loadMobileOptimizations();
    window.MobileOptimizations.virtualScroll('#list', '.item', 20); // 20px per item, 100px viewport -> ~5 visible + buffer

    const items = document.querySelectorAll('.item');
    // Item 0 (top=0) is within the viewport - stays visible.
    expect(items[0].style.display).toBe('');
    // An item far below the viewport+buffer should be hidden.
    expect(items[59].style.display).toBe('none');
  });
});
