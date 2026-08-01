/**
 * Mobile Performance Optimizations
 * - Lazy-load images and components
 * - Optimize for touch interactions
 * - Reduce motion for users who prefer reduced motion
 */

(function() {
  'use strict';

  // Shared across scripts - see motion-preference.js
  const prefersReducedMotion = window.VVPrefersReducedMotion;

  /**
   * Lazy load images using Intersection Observer
   */
  function initLazyLoading() {
    const images = document.querySelectorAll('img[data-src]');
    if (!images.length) return;

    const imageObserver = new IntersectionObserver((entries, observer) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          const img = entry.target;
          img.src = img.dataset.src;
          img.removeAttribute('data-src');
          observer.unobserve(img);
        }
      });
    }, {
      rootMargin: '50px'
    });

    images.forEach(img => imageObserver.observe(img));
  }

  /**
   * Optimize touch interactions: increase touch target sizes
   */
  function initTouchOptimization() {
    // Ensure buttons and clickable elements are at least 44x44 pixels
    const clickableElements = document.querySelectorAll('button, a, [role="button"], .btn');
    clickableElements.forEach(el => {
      const style = window.getComputedStyle(el);
      const height = parseFloat(style.height);
      const width = parseFloat(style.width);

      if (height < 44 || width < 44) {
        el.style.minHeight = '44px';
        el.style.minWidth = '44px';
        el.style.padding = 'max(0.5rem, calc((44px - ' + style.fontSize + ') / 2))';
      }
    });
  }

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
      initLazyLoading();
      initTouchOptimization();
      initMotionPreferences();
      initPassiveEventListeners();
    });
  } else {
    initLazyLoading();
    initTouchOptimization();
    initMotionPreferences();
    initPassiveEventListeners();
  }

  // Expose for manual use
  window.MobileOptimizations = {
    lazyLoad: initLazyLoading,
    touchOptimize: initTouchOptimization,
    virtualScroll: initVirtualScrolling,
    prefersReducedMotion
  };
})();
