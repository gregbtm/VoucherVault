/**
 * Mobile Performance Optimizations
 * - Reduce motion for users who prefer reduced motion
 * - Virtual scrolling / passive listeners for long lists
 *
 * Lazy-loading and touch-target sizing used to live here too, but both
 * were dead weight:
 * - initLazyLoading() targeted img[data-src], an attribute no template
 *   ever sets (the site uses native loading="lazy" instead - see
 *   mobile-interactions.js's fallback for browsers without it).
 * - initTouchOptimization() duplicated, at runtime via getComputedStyle,
 *   the same 44x44px minimum mobile.css already enforces declaratively
 *   via @media (hover: none) and (pointer: coarse) - the CSS rule applies
 *   before JS even runs and costs no layout thrashing.
 */

(function() {
  'use strict';

  // Detect if user prefers reduced motion
  const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /**
   * Respect prefers-reduced-motion by removing animations
   */
  function initMotionPreferences() {
    if (prefersReducedMotion) {
      document.documentElement.style.setProperty('--animation-duration', '0s');
      document.querySelectorAll('[style*="animation"]').forEach(el => {
        el.style.animation = 'none';
      });
    }
  }

  /**
   * Virtual scrolling for long lists (performance optimization)
   * Removes off-screen items from DOM and re-adds them as needed
   */
  function initVirtualScrolling(containerSelector, itemSelector, itemHeight = null) {
    const container = document.querySelector(containerSelector);
    if (!container) return;

    const items = container.querySelectorAll(itemSelector);
    if (items.length < 50) return; // Only optimize if there are many items

    const computedHeight = itemHeight || parseInt(window.getComputedStyle(items[0]).height);

    const viewport = container;
    const buffer = 5;

    function updateVisibility() {
      const scrollTop = viewport.scrollTop;
      const viewportHeight = viewport.clientHeight;

      items.forEach((item, index) => {
        const itemTop = index * computedHeight;
        const itemBottom = itemTop + computedHeight;

        const isVisible = itemBottom > scrollTop && itemTop < scrollTop + viewportHeight;
        item.style.display = isVisible ? '' : 'none';
      });
    }

    viewport.addEventListener('scroll', updateVisibility, { passive: true });
    updateVisibility();
  }

  /**
   * Debounce function for resize and scroll events
   */
  function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
      const later = () => {
        clearTimeout(timeout);
        func(...args);
      };
      clearTimeout(timeout);
      timeout = setTimeout(later, wait);
    };
  }

  /**
   * Enable passive event listeners for better scroll performance
   */
  function initPassiveEventListeners() {
    let passiveSupported = false;
    try {
      const options = {
        get passive() {
          passiveSupported = true;
          return false;
        }
      };
      window.addEventListener('test', null, options);
      window.removeEventListener('test', null, options);
    } catch (err) {
      passiveSupported = false;
    }

    return passiveSupported;
  }

  // Initialize all optimizations when DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      initMotionPreferences();
      initPassiveEventListeners();
    });
  } else {
    initMotionPreferences();
    initPassiveEventListeners();
  }

  // Expose for manual use
  window.MobileOptimizations = {
    virtualScroll: initVirtualScrolling,
    prefersReducedMotion
  };
})();
