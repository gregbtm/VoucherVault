/**
 * VoucherVault Mobile Interactions
 * Handles touch gestures, virtual keyboard, and mobile-specific UX
 */

(function() {
  'use strict';

  // ────────────────────────────────────────────────────────────────
  // 1. Dismiss Virtual Keyboard on Scroll
  // ────────────────────────────────────────────────────────────────
  document.addEventListener('touchmove', function() {
    if (document.activeElement &&
        document.activeElement.matches('input, textarea, select')) {
      document.activeElement.blur();
    }
  }, { passive: true });

  // ────────────────────────────────────────────────────────────────
  // 2. Swipe-to-Close Hamburger Menu
  // ────────────────────────────────────────────────────────────────
  (function() {
    let touchStartX = 0;
    let touchStartY = 0;
    const sidebar = document.querySelector('.sidebar');

    if (!sidebar) return;

    document.addEventListener('touchstart', function(e) {
      touchStartX = e.touches[0].clientX;
      touchStartY = e.touches[0].clientY;
    }, { passive: true });

    document.addEventListener('touchend', function(e) {
      const touchEndX = e.changedTouches[0].clientX;
      const touchEndY = e.changedTouches[0].clientY;

      const diffX = touchStartX - touchEndX;
      const diffY = Math.abs(touchStartY - touchEndY);

      // Swipe left with minimal vertical movement
      if (diffX > 100 && diffY < 50) {
        if (document.body.classList.contains('toggle-sidebar')) {
          document.body.classList.remove('toggle-sidebar');
        }
      }
    }, { passive: true });
  })();

  // ────────────────────────────────────────────────────────────────
  // 3. Prevent Pinch Zoom While Allowing User Zoom
  // ────────────────────────────────────────────────────────────────
  document.addEventListener('touchmove', function(e) {
    if (e.touches.length > 1) {
      // Allow pinch-zoom by not preventing default
      // But you can add logic here if needed
    }
  }, { passive: true });

  // ────────────────────────────────────────────────────────────────
  // 4. Haptic Feedback on Form Submission
  // ────────────────────────────────────────────────────────────────
  (function() {
    if (!navigator.vibrate) return;

    document.querySelectorAll('form').forEach(function(form) {
      form.addEventListener('submit', function() {
        // Short success vibration
        navigator.vibrate([10, 20, 10]);
      });
    });

    // Add error vibration for validation errors
    document.addEventListener('invalid', function(e) {
      if (navigator.vibrate) {
        navigator.vibrate([50, 100, 50]); // Longer error buzz
      }
    }, true);
  })();

  // ────────────────────────────────────────────────────────────────
  // 5. Detect Virtual Keyboard and Adjust Layout
  // ────────────────────────────────────────────────────────────────
  (function() {
    let windowHeight = window.innerHeight;

    window.addEventListener('resize', function() {
      const newHeight = window.innerHeight;
      const keyboardHeight = windowHeight - newHeight;

      if (keyboardHeight > 150) {
        // Keyboard is open
        document.body.classList.add('keyboard-open');
        document.body.style.setProperty('--keyboard-height', keyboardHeight + 'px');
      } else {
        // Keyboard is closed
        document.body.classList.remove('keyboard-open');
      }
    }, { passive: true });
  })();

  // ────────────────────────────────────────────────────────────────
  // 6. Lazy Load Images
  // ────────────────────────────────────────────────────────────────
  (function() {
    if ('IntersectionObserver' in window && 'loading' in HTMLImageElement.prototype) {
      // Browser supports native lazy loading and IntersectionObserver
      return; // Let the browser handle it
    }

    const images = document.querySelectorAll('img[loading="lazy"]');
    if (images.length === 0) return;

    const imageObserver = new IntersectionObserver(function(entries, observer) {
      entries.forEach(function(entry) {
        if (entry.isIntersecting) {
          const img = entry.target;
          img.src = img.dataset.src || img.src;
          img.classList.remove('lazy');
          observer.unobserve(img);
        }
      });
    });

    images.forEach(function(img) {
      imageObserver.observe(img);
    });
  })();

  // ────────────────────────────────────────────────────────────────
  // 7. Show Scroll Indicator for Horizontal Overflow
  // ────────────────────────────────────────────────────────────────
  (function() {
    const tableContainers = document.querySelectorAll('.table-responsive');

    tableContainers.forEach(function(container) {
      function updateScrollIndicator() {
        const hasHorizontalScroll = container.scrollWidth > container.clientWidth;
        if (hasHorizontalScroll) {
          container.classList.add('has-scroll');
        } else {
          container.classList.remove('has-scroll');
        }
      }

      // Check on load
      updateScrollIndicator();

      // Check on resize
      window.addEventListener('resize', updateScrollIndicator, { passive: true });

      // Check on scroll
      container.addEventListener('scroll', updateScrollIndicator, { passive: true });
    });
  })();

  // ────────────────────────────────────────────────────────────────
  // 8. Prevent iOS Scroll Bounce on Fixed Elements
  // ────────────────────────────────────────────────────────────────
  (function() {
    document.addEventListener('touchmove', function(e) {
      if (e.target.closest('.header, .footer')) {
        // Allow scrolling on header/footer but prevent bounce
        if (e.target.parentElement && e.target.parentElement.scrollHeight === e.target.parentElement.clientHeight) {
          e.preventDefault();
        }
      }
    }, { passive: false });
  })();

  // ────────────────────────────────────────────────────────────────
  // 9. Smart Input Focus Management
  // ────────────────────────────────────────────────────────────────
  (function() {
    const inputs = document.querySelectorAll('input, textarea, select');

    inputs.forEach(function(input) {
      input.addEventListener('focus', function() {
        // Prevent auto-scroll on mobile when focusing
        setTimeout(function() {
          input.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }, 300);
      });
    });
  })();

  // ────────────────────────────────────────────────────────────────
  // 10. Detect Device Orientation Changes
  // ────────────────────────────────────────────────────────────────
  (function() {
    if (!window.screen.orientation) return;

    window.screen.orientation.addEventListener('change', function() {
      const orientation = window.screen.orientation.type;
      document.body.classList.toggle('landscape', orientation.includes('landscape'));
      document.body.classList.toggle('portrait', orientation.includes('portrait'));
    });

    // Set initial orientation class
    const orientation = window.screen.orientation.type;
    document.body.classList.toggle('landscape', orientation.includes('landscape'));
    document.body.classList.toggle('portrait', orientation.includes('portrait'));
  })();

  // ────────────────────────────────────────────────────────────────
  // 11. Enhance Touch Feedback with Active States
  // ────────────────────────────────────────────────────────────────
  (function() {
    if (window.matchMedia('(hover: none)').matches) {
      // Touch device
      document.addEventListener('touchstart', function(e) {
        if (e.target.closest('button, a, .btn')) {
          e.target.closest('button, a, .btn').classList.add('active');
        }
      }, { passive: true });

      document.addEventListener('touchend', function(e) {
        const elem = e.target.closest('button, a, .btn');
        if (elem) {
          setTimeout(function() {
            elem.classList.remove('active');
          }, 100);
        }
      }, { passive: true });
    }
  })();

  // ────────────────────────────────────────────────────────────────
  // 12. Handle Focus Trap in Modals (a11y)
  // ────────────────────────────────────────────────────────────────
  function trapFocus(modalElement) {
    const focusableElements = modalElement.querySelectorAll(
      'a, button, input, select, textarea, [tabindex]:not([tabindex="-1"])'
    );

    if (focusableElements.length === 0) return;

    const firstElement = focusableElements[0];
    const lastElement = focusableElements[focusableElements.length - 1];

    modalElement.addEventListener('keydown', function(e) {
      if (e.key !== 'Tab') return;

      if (e.shiftKey) {
        if (document.activeElement === firstElement) {
          e.preventDefault();
          lastElement.focus();
        }
      } else {
        if (document.activeElement === lastElement) {
          e.preventDefault();
          firstElement.focus();
        }
      }
    });
  }

  // Wire up focus trapping for Bootstrap modals
  document.addEventListener('shown.bs.modal', function(e) {
    trapFocus(e.target);
  });

  // ────────────────────────────────────────────────────────────────
  // 13. Smooth Scroll Behavior Enhancement
  // ────────────────────────────────────────────────────────────────
  (function() {
    document.querySelectorAll('a[href^="#"]').forEach(function(link) {
      link.addEventListener('click', function(e) {
        const target = document.querySelector(this.getAttribute('href'));
        if (target) {
          e.preventDefault();
          target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      });
    });
  })();

  // ────────────────────────────────────────────────────────────────
  // 14. Network Status Indicator
  // ────────────────────────────────────────────────────────────────
  (function() {
    function updateNetworkStatus() {
      const indicator = document.querySelector('.network-status');
      if (!indicator) return;

      if (navigator.onLine) {
        indicator.textContent = 'Online';
        indicator.classList.add('online');
        indicator.classList.remove('offline');
      } else {
        indicator.textContent = 'Offline';
        indicator.classList.add('offline');
        indicator.classList.remove('online');
      }
    }

    window.addEventListener('online', updateNetworkStatus);
    window.addEventListener('offline', updateNetworkStatus);
    updateNetworkStatus();
  })();

  // ────────────────────────────────────────────────────────────────
  // 15. Prevent Double-Tap Zoom on Buttons (iOS)
  // ────────────────────────────────────────────────────────────────
  (function() {
    let lastTouchEnd = 0;
    document.addEventListener('touchend', function(e) {
      const now = Date.now();
      if (now - lastTouchEnd <= 300) {
        if (e.target.closest('button, a, .btn')) {
          e.preventDefault();
        }
      }
      lastTouchEnd = now;
    }, { passive: false });
  })();

  // ────────────────────────────────────────────────────────────────
  // 16. Log Mobile Capabilities for Debugging
  // ────────────────────────────────────────────────────────────────
  if (window.DEBUG) {
    console.log('Mobile Capabilities:', {
      touchSupport: 'ontouchstart' in window,
      vibrateSupport: 'vibrate' in navigator,
      serviceWorker: 'serviceWorker' in navigator,
      intersectionObserver: 'IntersectionObserver' in window,
      lazyLoadingSupport: 'loading' in HTMLImageElement.prototype,
      orientation: window.screen.orientation?.type,
      viewportHeight: window.innerHeight,
      viewportWidth: window.innerWidth,
      devicePixelRatio: window.devicePixelRatio,
    });
  }

  console.log('✓ Mobile interactions loaded');
})();
