/**
 * PWA Badge API - App Icon Notification Badge
 * Shows unread notification count on the app icon (supported on Android, Windows)
 * Gracefully degrades on unsupported platforms
 */

(function() {
    'use strict';

    class PWABadgeManager {
        constructor() {
            this.supported = 'setAppBadge' in navigator;
            this.unreadCount = 0;
            this.init();
        }

        async init() {
            if (!this.supported) {
                console.log('[PWA Badge] Badge API not supported on this platform');
                return;
            }

            // Check for initial unread count from the page
            this.checkInitialBadge();

            // Listen for badge count changes
            this.setupBadgeListeners();

            console.log('[PWA Badge] Initialized successfully');
        }

        /**
         * Check for initial badge count in DOM (set by Django template)
         */
        checkInitialBadge() {
            const badge = document.querySelector('[data-unread-count]');
            if (badge) {
                const count = parseInt(badge.getAttribute('data-unread-count'), 10);
                if (count > 0) {
                    this.updateBadge(count);
                }
            }
        }

        /**
         * Setup listeners for DOM changes that indicate unread count changes
         */
        setupBadgeListeners() {
            // Listen for notification updates (via data attribute)
            const observer = new MutationObserver(() => {
                const badge = document.querySelector('[data-unread-count]');
                if (badge) {
                    const count = parseInt(badge.getAttribute('data-unread-count'), 10);
                    if (count !== this.unreadCount) {
                        this.updateBadge(count);
                    }
                }
            });

            const badgeElement = document.querySelector('[data-unread-count]');
            if (badgeElement) {
                observer.observe(badgeElement, {
                    attributes: true,
                    attributeFilter: ['data-unread-count'],
                    subtree: false
                });
            }

            // Also listen for custom events (app might dispatch these)
            document.addEventListener('notification-count-changed', (e) => {
                const count = e.detail?.count || 0;
                this.updateBadge(count);
            });
        }

        /**
         * Update the app badge with the given count
         * @param {number} count - Unread notification count (0 to clear badge)
         */
        async updateBadge(count) {
            if (!this.supported) return;

            try {
                this.unreadCount = count;

                if (count > 0) {
                    // Show badge with count (capped at 99+ on most systems)
                    await navigator.setAppBadge(count);
                    console.log('[PWA Badge] Badge set to:', count);
                } else {
                    // Clear badge
                    await navigator.clearAppBadge();
                    console.log('[PWA Badge] Badge cleared');
                }
            } catch (error) {
                console.error('[PWA Badge] Failed to update badge:', error);
            }
        }

        /**
         * Public method to update badge from external code
         */
        setBadgeCount(count) {
            this.updateBadge(Math.max(0, count));
        }

        /**
         * Increment badge count
         */
        incrementBadge() {
            this.updateBadge(this.unreadCount + 1);
        }

        /**
         * Decrement badge count
         */
        decrementBadge() {
            if (this.unreadCount > 0) {
                this.updateBadge(this.unreadCount - 1);
            }
        }
    }

    // Initialize and expose globally
    window.pwaBadgeManager = new PWABadgeManager();
}());
