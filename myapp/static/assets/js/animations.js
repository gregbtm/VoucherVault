/*
 * Site-wide entrance animations, powered by the vendored Motion library
 * (assets/vendor/motion/motion.min.js - motion.dev, UMD build). The CSS
 * half lives in assets/css/animations.css.
 *
 * Convention-based: recurring UI patterns (cards, form sections,
 * widgets) get a short staggered fade-up as they enter the viewport, on
 * every page that uses them - including future ones - with zero
 * per-page wiring.
 *
 * Deliberately defensive:
 * - Does nothing at all when the OS asks for reduced motion.
 * - Does nothing if the Motion vendor file failed to load - elements are
 *   only ever hidden (.vv-anim-pre) AFTER the library is confirmed
 *   present, so a broken/blocked script can never blank the page.
 * - Animates only opacity/transform (compositor-friendly - Motion drives
 *   these through the Web Animations API, off the main JS thread) and
 *   caps how many elements animate per container so a 200-item
 *   inventory doesn't queue 200 animations.
 */
(function () {
    'use strict';

    var prefersReduced = window.matchMedia
        && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (prefersReduced) return;
    if (!window.Motion || typeof window.Motion.animate !== 'function'
        || typeof window.Motion.inView !== 'function') return;

    var animate = window.Motion.animate;
    var inView = window.Motion.inView;
    var stagger = window.Motion.stagger;

    // Recurring patterns that get an entrance. Order doesn't matter -
    // elements are grouped by their parent so each grid/section staggers
    // as one unit.
    var ENTRANCE_SELECTORS = [
        '.items-grid .item-card',   // inventory tiles
        '.stat-card',               // dashboard stat row
        '.form-section',            // create/edit item sections
        '.next-up-card',            // Next Up / Active Today widgets
        '.detail-fields .detail-field', // item detail quick facts
        'main .card',               // generic cards (manage pages, settings, notify, ...)
    ];

    // Long grids: only the first N children of a container animate; the
    // rest appear instantly. Nobody watches 200 tiles fade in, and the
    // browser shouldn't have to either.
    var MAX_PER_CONTAINER = 24;

    var byContainer = new Map();
    ENTRANCE_SELECTORS.forEach(function (selector) {
        document.querySelectorAll(selector).forEach(function (el) {
            if (el.dataset.vvAnim) return; // e.g. a .card that also matched another selector
            el.dataset.vvAnim = '1';
            var parent = el.parentElement || document.body;
            if (!byContainer.has(parent)) byContainer.set(parent, []);
            byContainer.get(parent).push(el);
        });
    });

    byContainer.forEach(function (elements, container) {
        var group = elements.slice(0, MAX_PER_CONTAINER);
        group.forEach(function (el) { el.classList.add('vv-anim-pre'); });

        // Not returning a cleanup function makes inView one-shot: each
        // group animates the first time it scrolls into view, then the
        // observer disconnects itself.
        inView(container, function () {
            group.forEach(function (el) { el.classList.remove('vv-anim-pre'); });
            animate(
                group,
                { opacity: [0, 1], y: [10, 0] },
                { duration: 0.3, delay: stagger(0.045), ease: [0.22, 1, 0.36, 1] }
            );
        }, { amount: 0.1 });
    });
})();
