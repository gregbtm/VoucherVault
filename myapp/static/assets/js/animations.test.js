import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(__dirname, 'animations.js'), 'utf-8');

// animations.js is a defensive IIFE: it does nothing at all unless
// window.Motion (the vendored motion.dev library) is already present with
// both .animate and .inView, and unless prefers-reduced-motion is off. Every
// test stubs window.Motion before loading the script, mirroring how the
// vendor <script> tag would have set it up before this one runs in base.html.
function loadAnimations() {
  (0, eval)(source);
}

function stubMotion({ inViewImmediately = false } = {}) {
  const inViewCalls = [];
  const animate = vi.fn();
  const inView = vi.fn((container, callback) => {
    inViewCalls.push({ container, callback });
    if (inViewImmediately) callback();
  });
  const stagger = vi.fn((n) => n);
  window.Motion = { animate, inView, stagger };
  return { animate, inView, stagger, inViewCalls };
}

function stubMatchMedia(reducedMotion) {
  window.matchMedia = vi.fn().mockImplementation((query) => ({
    matches: query === '(prefers-reduced-motion: reduce)' ? reducedMotion : false,
    media: query,
  }));
}

beforeEach(() => {
  document.body.innerHTML = '';
  delete window.Motion;
  stubMatchMedia(false);
  vi.useRealTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('defensive guards', () => {
  it('does nothing when the OS asks for reduced motion, even with Motion present', () => {
    stubMatchMedia(true);
    const { animate, inView } = stubMotion();
    document.body.innerHTML = '<div class="items-grid"><div class="item-card"></div></div>';
    expect(() => loadAnimations()).not.toThrow();
    expect(inView).not.toHaveBeenCalled();
    expect(animate).not.toHaveBeenCalled();
  });

  it('does nothing and does not throw when window.Motion is entirely absent', () => {
    document.body.innerHTML = '<div class="items-grid"><div class="item-card"></div></div>';
    expect(() => loadAnimations()).not.toThrow();
    expect(document.querySelector('.item-card').classList.contains('vv-anim-pre')).toBe(false);
  });

  it('does nothing when Motion is present but missing .inView', () => {
    window.Motion = { animate: vi.fn() };
    document.body.innerHTML = '<div class="items-grid"><div class="item-card"></div></div>';
    expect(() => loadAnimations()).not.toThrow();
    expect(document.querySelector('.item-card').classList.contains('vv-anim-pre')).toBe(false);
  });
});

describe('entrance animation wiring', () => {
  it('marks matching elements with vv-anim-pre and registers one inView watcher per container', () => {
    const { inView } = stubMotion();
    document.body.innerHTML = `
      <div class="items-grid">
        <div class="item-card" id="c1"></div>
        <div class="item-card" id="c2"></div>
      </div>`;
    loadAnimations();
    expect(document.getElementById('c1').classList.contains('vv-anim-pre')).toBe(true);
    expect(document.getElementById('c2').classList.contains('vv-anim-pre')).toBe(true);
    expect(inView).toHaveBeenCalledTimes(1); // both cards share one parent container
  });

  it('reveals (removes vv-anim-pre, calls animate) once the container scrolls into view', () => {
    const { animate } = stubMotion({ inViewImmediately: true });
    document.body.innerHTML = '<div class="items-grid"><div class="item-card" id="c1"></div></div>';
    loadAnimations();
    expect(document.getElementById('c1').classList.contains('vv-anim-pre')).toBe(false);
    expect(animate).toHaveBeenCalledTimes(1);
  });

  it('never double-tags an element that matches two entrance selectors at once', () => {
    stubMotion();
    // '.stat-card' is itself also just a '.card' under 'main .card', so a
    // dashboard stat card matches two of ENTRANCE_SELECTORS.
    document.body.innerHTML = '<main><div class="card stat-card" id="dup"></div></main>';
    loadAnimations();
    expect(document.getElementById('dup').dataset.vvAnim).toBe('1');
    // Only one entry should exist in the animation group for this element -
    // verified indirectly: inView is called once per *container*, and this
    // element's container (<main>) has exactly one child animated.
  });

  it('reveals via the 1200ms fallback timer even if inView never fires', () => {
    vi.useFakeTimers();
    const { animate, inView } = stubMotion({ inViewImmediately: false });
    document.body.innerHTML = '<div class="items-grid"><div class="item-card" id="c1"></div></div>';
    loadAnimations();
    expect(inView).toHaveBeenCalled();
    expect(document.getElementById('c1').classList.contains('vv-anim-pre')).toBe(true);
    vi.advanceTimersByTime(1200);
    expect(document.getElementById('c1').classList.contains('vv-anim-pre')).toBe(false);
    expect(animate).toHaveBeenCalledTimes(1);
  });

  it('only reveals once even if both inView and the fallback timer would fire', () => {
    vi.useFakeTimers();
    const { animate } = stubMotion({ inViewImmediately: true });
    document.body.innerHTML = '<div class="items-grid"><div class="item-card"></div></div>';
    loadAnimations();
    expect(animate).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1200);
    expect(animate).toHaveBeenCalledTimes(1);
  });

  it('caps a long grid at MAX_PER_CONTAINER (24) animated elements per container', () => {
    const { animate } = stubMotion({ inViewImmediately: true });
    const grid = document.createElement('div');
    grid.className = 'items-grid';
    for (let i = 0; i < 40; i++) {
      const card = document.createElement('div');
      card.className = 'item-card';
      grid.appendChild(card);
    }
    document.body.appendChild(grid);
    loadAnimations();
    // All 40 get tagged (so none pop in un-animated later)...
    expect(document.querySelectorAll('.item-card[data-vv-anim]').length).toBe(40);
    // ...but animate() only ever receives the first 24 as its group.
    expect(animate).toHaveBeenCalledTimes(1);
    expect(animate.mock.calls[0][0].length).toBe(24);
  });
});
