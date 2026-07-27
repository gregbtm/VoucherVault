/* Phase 6: Accessibility Enhancements */

class AccessibilityManager {
  constructor() {
    this.isKeyboardUser = false;
    this.focusedElement = null;
    this.init();
  }

  init() {
    this.detectKeyboardUser();
    this.setupSkipLinks();
    this.setupFocusManagement();
    this.setupAriaLabels();
    this.setupDropdownAccessibility();
    this.setupModalAccessibility();
    this.setupTabNavigation();
    this.setupAnnouncements();
  }

  detectKeyboardUser() {
    document.addEventListener('keydown', () => {
      if (!this.isKeyboardUser) {
        this.isKeyboardUser = true;
        document.body.classList.add('using-keyboard');
      }
    });

    document.addEventListener('mousedown', () => {
      this.isKeyboardUser = false;
      document.body.classList.remove('using-keyboard');
    });
  }

  setupSkipLinks() {
    const skipLink = document.createElement('a');
    skipLink.href = '#main-content';
    skipLink.className = 'skip-link';
    skipLink.textContent = 'Skip to main content';
    skipLink.setAttribute('aria-label', 'Skip to main content');

    const nav = document.querySelector('nav') || document.body.firstChild;
    if (nav) {
      nav.parentNode.insertBefore(skipLink, nav);
    }

    skipLink.addEventListener('click', (e) => {
      e.preventDefault();
      const mainContent = document.getElementById('main-content') || document.querySelector('main');
      if (mainContent) {
        mainContent.focus();
        mainContent.scrollIntoView({ behavior: 'smooth' });
      }
    });
  }

  setupFocusManagement() {
    document.addEventListener('focus', (e) => {
      if (e.target && e.target !== document.body) {
        this.focusedElement = e.target;
      }
    }, true);

    // Announce focus changes to screen readers
    document.addEventListener('focus', (e) => {
      const el = e.target;
      if (el && el.getAttribute('aria-label')) {
        this.announce(el.getAttribute('aria-label'), 'polite');
      }
    }, true);
  }

  setupAriaLabels() {
    // Add aria-labels to icon-only buttons
    const iconOnlyButtons = document.querySelectorAll('button:not([aria-label]):not([title])');
    iconOnlyButtons.forEach((btn) => {
      const icon = btn.querySelector('i');
      if (icon && btn.childNodes.length === 1) {
        const iconClass = icon.className;
        const label = this.getButtonLabelFromIcon(iconClass);
        if (label) {
          btn.setAttribute('aria-label', label);
        }
      }
    });

    // Add aria-labels to links with only icons
    const iconOnlyLinks = document.querySelectorAll('a:not([aria-label]):not([title])');
    iconOnlyLinks.forEach((link) => {
      const icon = link.querySelector('i');
      if (icon && link.childNodes.length === 1) {
        const iconClass = icon.className;
        const label = this.getButtonLabelFromIcon(iconClass);
        if (label) {
          link.setAttribute('aria-label', label);
        }
      }
    });
  }

  getButtonLabelFromIcon(iconClass) {
    const iconMap = {
      'bi-plus': 'Add',
      'bi-plus-circle': 'Add',
      'bi-trash': 'Delete',
      'bi-pencil': 'Edit',
      'bi-eye': 'View',
      'bi-heart': 'Like',
      'bi-share': 'Share',
      'bi-download': 'Download',
      'bi-upload': 'Upload',
      'bi-search': 'Search',
      'bi-menu': 'Menu',
      'bi-x': 'Close',
      'bi-check': 'Confirm',
      'bi-arrow-left': 'Back',
      'bi-arrow-right': 'Next',
      'bi-star': 'Favorite',
      'bi-bookmark': 'Bookmark',
      'bi-print': 'Print',
      'bi-gear': 'Settings',
      'bi-question-circle': 'Help',
      'bi-info-circle': 'Information',
      'bi-warning': 'Warning',
      'bi-check-circle': 'Success',
      'bi-x-circle': 'Error',
      'bi-phone': 'Phone',
      'bi-envelope': 'Email',
      'bi-clock': 'Time',
      'bi-calendar': 'Calendar',
      'bi-filter': 'Filter',
      'bi-sort': 'Sort',
    };

    for (const [iconKey, label] of Object.entries(iconMap)) {
      if (iconClass.includes(iconKey)) {
        return label;
      }
    }

    return null;
  }

  setupDropdownAccessibility() {
    const dropdowns = document.querySelectorAll('[data-bs-toggle="dropdown"]');
    dropdowns.forEach((trigger) => {
      trigger.setAttribute('aria-haspopup', 'true');
      trigger.setAttribute('aria-expanded', 'false');

      const menu = document.querySelector(`[data-bs-target="${trigger.id}"]`) ||
                   trigger.nextElementSibling;

      if (menu && menu.classList.contains('dropdown-menu')) {
        menu.setAttribute('role', 'menu');
        menu.setAttribute('aria-labelledby', trigger.id || '');

        trigger.addEventListener('show.bs.dropdown', () => {
          trigger.setAttribute('aria-expanded', 'true');
          menu.setAttribute('aria-hidden', 'false');
        });

        trigger.addEventListener('hide.bs.dropdown', () => {
          trigger.setAttribute('aria-expanded', 'false');
          menu.setAttribute('aria-hidden', 'true');
        });

        // Keyboard navigation in dropdowns
        const items = menu.querySelectorAll('[role="menuitem"], .dropdown-item');
        items.forEach((item, index) => {
          item.addEventListener('keydown', (e) => {
            let nextItem = null;

            if (e.key === 'ArrowDown') {
              e.preventDefault();
              nextItem = items[Math.min(index + 1, items.length - 1)];
            } else if (e.key === 'ArrowUp') {
              e.preventDefault();
              nextItem = items[Math.max(index - 1, 0)];
            } else if (e.key === 'Home') {
              e.preventDefault();
              nextItem = items[0];
            } else if (e.key === 'End') {
              e.preventDefault();
              nextItem = items[items.length - 1];
            } else if (e.key === 'Escape') {
              e.preventDefault();
              trigger.focus();
            }

            if (nextItem) {
              nextItem.focus();
            }
          });
        });
      }
    });
  }

  setupModalAccessibility() {
    const modals = document.querySelectorAll('.modal');
    modals.forEach((modal) => {
      modal.setAttribute('role', 'dialog');
      modal.setAttribute('aria-modal', 'true');

      const title = modal.querySelector('.modal-title');
      if (title && title.id) {
        modal.setAttribute('aria-labelledby', title.id);
      }

      const description = modal.querySelector('.modal-body');
      if (description && description.id) {
        modal.setAttribute('aria-describedby', description.id);
      }

      // Focus trap within modal
      const focusableElements = modal.querySelectorAll(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
      );

      if (focusableElements.length > 0) {
        const firstElement = focusableElements[0];
        const lastElement = focusableElements[focusableElements.length - 1];

        modal.addEventListener('keydown', (e) => {
          if (e.key === 'Tab') {
            if (e.shiftKey && document.activeElement === firstElement) {
              e.preventDefault();
              lastElement.focus();
            } else if (!e.shiftKey && document.activeElement === lastElement) {
              e.preventDefault();
              firstElement.focus();
            }
          }
        });

        modal.addEventListener('show.bs.modal', () => {
          firstElement.focus();
        });
      }
    });
  }

  setupTabNavigation() {
    const tabLists = document.querySelectorAll('[role="tablist"]');
    tabLists.forEach((tabList) => {
      const tabs = tabList.querySelectorAll('[role="tab"]');

      tabs.forEach((tab, index) => {
        tab.setAttribute('tabindex', index === 0 ? '0' : '-1');

        tab.addEventListener('keydown', (e) => {
          let targetTab = null;

          if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
            e.preventDefault();
            targetTab = tabs[(index + 1) % tabs.length];
          } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
            e.preventDefault();
            targetTab = tabs[(index - 1 + tabs.length) % tabs.length];
          } else if (e.key === 'Home') {
            e.preventDefault();
            targetTab = tabs[0];
          } else if (e.key === 'End') {
            e.preventDefault();
            targetTab = tabs[tabs.length - 1];
          }

          if (targetTab) {
            targetTab.focus();
            targetTab.click();
          }
        });

        tab.addEventListener('click', () => {
          tabs.forEach((t) => {
            t.setAttribute('aria-selected', 'false');
            t.setAttribute('tabindex', '-1');
            const panel = document.getElementById(t.getAttribute('aria-controls'));
            if (panel) {
              panel.setAttribute('hidden', '');
            }
          });

          tab.setAttribute('aria-selected', 'true');
          tab.setAttribute('tabindex', '0');
          const panel = document.getElementById(tab.getAttribute('aria-controls'));
          if (panel) {
            panel.removeAttribute('hidden');
          }
        });
      });
    });
  }

  setupAnnouncements() {
    if (!document.getElementById('aria-live-region')) {
      const liveRegion = document.createElement('div');
      liveRegion.id = 'aria-live-region';
      liveRegion.setAttribute('aria-live', 'polite');
      liveRegion.setAttribute('aria-atomic', 'true');
      liveRegion.className = 'sr-only';
      document.body.appendChild(liveRegion);
    }
  }

  announce(message, priority = 'polite') {
    const liveRegion = document.getElementById('aria-live-region');
    if (liveRegion) {
      liveRegion.setAttribute('aria-live', priority);
      liveRegion.textContent = message;
    }
  }

  // Method to restore focus after modal/dialog closes
  restoreFocus() {
    if (this.focusedElement && this.focusedElement.focus) {
      this.focusedElement.focus();
    }
  }

  // Enhance form validation feedback
  enhanceFormValidation() {
    const forms = document.querySelectorAll('form');
    forms.forEach((form) => {
      const inputs = form.querySelectorAll('input, select, textarea');

      inputs.forEach((input) => {
        input.addEventListener('invalid', (e) => {
          e.preventDefault();
          input.setAttribute('aria-invalid', 'true');

          const errorMessage = this.getValidationMessage(input);
          const errorId = `error-${input.id}`;

          let errorEl = document.getElementById(errorId);
          if (!errorEl) {
            errorEl = document.createElement('span');
            errorEl.id = errorId;
            errorEl.className = 'invalid-feedback';
            errorEl.setAttribute('role', 'alert');
            input.parentNode.appendChild(errorEl);
          }

          errorEl.textContent = errorMessage;
          input.setAttribute('aria-describedby', errorId);

          this.announce(errorMessage, 'assertive');
        });

        input.addEventListener('input', () => {
          if (input.validity.valid) {
            input.setAttribute('aria-invalid', 'false');
            input.removeAttribute('aria-describedby');

            const errorId = `error-${input.id}`;
            const errorEl = document.getElementById(errorId);
            if (errorEl) {
              errorEl.remove();
            }
          }
        });
      });

      form.addEventListener('submit', (e) => {
        let hasErrors = false;

        inputs.forEach((input) => {
          if (!input.validity.valid) {
            hasErrors = true;
            input.focus();
            return;
          }
        });

        if (hasErrors) {
          e.preventDefault();
          this.announce('Form has validation errors', 'assertive');
        }
      });
    });
  }

  getValidationMessage(input) {
    if (input.validity.valueMissing) {
      return `${input.labels[0]?.textContent || 'This field'} is required`;
    } else if (input.validity.typeMismatch) {
      return `Please enter a valid ${input.type}`;
    } else if (input.validity.tooShort) {
      return `Minimum length is ${input.minLength} characters`;
    } else if (input.validity.tooLong) {
      return `Maximum length is ${input.maxLength} characters`;
    } else if (input.validity.patternMismatch) {
      return input.title || 'Invalid format';
    } else if (input.validity.rangeUnderflow) {
      return `Value must be at least ${input.min}`;
    } else if (input.validity.rangeOverflow) {
      return `Value must be no more than ${input.max}`;
    }
    return input.validationMessage || 'Invalid input';
  }
}

// Initialize accessibility manager when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    window.accessibilityManager = new AccessibilityManager();
    window.accessibilityManager.enhanceFormValidation();
  });
} else {
  window.accessibilityManager = new AccessibilityManager();
  window.accessibilityManager.enhanceFormValidation();
}
