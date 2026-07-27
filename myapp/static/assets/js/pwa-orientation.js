/**
 * PWA Screen Orientation Lock
 * Locks device to landscape orientation while viewing barcodes/QR codes
 * for optimal scanning experience and readability
 * Gracefully degrades on unsupported platforms
 */

(function() {
    'use strict';

    class ScreenOrientationManager {
        constructor() {
            this.supported = 'orientation' in window.screen && 'lock' in window.screen.orientation;
            this.isLocked = false;
            this.currentContext = null;
            console.log('[PWA Orientation]', this.supported ? 'Supported' : 'Not supported');
        }

        /**
         * Lock screen to landscape orientation
         * @param {string} context - Context identifier (e.g., 'barcode', 'qr-code')
         */
        async lockLandscape(context = 'barcode') {
            if (!this.supported) {
                console.log('[PWA Orientation] Orientation lock not supported');
                return false;
            }

            try {
                await window.screen.orientation.lock('landscape');
                this.isLocked = true;
                this.currentContext = context;
                console.log('[PWA Orientation] Locked to landscape for:', context);
                return true;
            } catch (error) {
                console.error('[PWA Orientation] Failed to lock orientation:', error);
                return false;
            }
        }

        /**
         * Lock screen to landscape-primary (force specific landscape mode)
         * Useful for ensuring barcode is right-side up
         */
        async lockLandscapePrimary(context = 'barcode') {
            if (!this.supported) {
                console.log('[PWA Orientation] Orientation lock not supported');
                return false;
            }

            try {
                await window.screen.orientation.lock('landscape-primary');
                this.isLocked = true;
                this.currentContext = context;
                console.log('[PWA Orientation] Locked to landscape-primary for:', context);
                return true;
            } catch (error) {
                console.error('[PWA Orientation] Failed to lock orientation:', error);
                return false;
            }
        }

        /**
         * Unlock screen orientation (return to natural orientation)
         */
        async unlock() {
            if (!this.supported || !this.isLocked) {
                return false;
            }

            try {
                await window.screen.orientation.unlock();
                this.isLocked = false;
                const context = this.currentContext;
                this.currentContext = null;
                console.log('[PWA Orientation] Unlocked orientation (was:', context + ')');
                return true;
            } catch (error) {
                console.error('[PWA Orientation] Failed to unlock orientation:', error);
                return false;
            }
        }

        /**
         * Get current orientation
         */
        getCurrentOrientation() {
            if (!this.supported) {
                return null;
            }
            return window.screen.orientation.type;
        }
    }

    // Initialize and expose globally
    window.screenOrientationManager = new ScreenOrientationManager();

    // Auto-lock/unlock on item detail page with barcode
    document.addEventListener('DOMContentLoaded', () => {
        // Lock on page load if we're on an item detail page with a barcode
        const qrCard = document.querySelector('.qr-card');
        if (qrCard) {
            window.screenOrientationManager.lockLandscape('item-detail');

            // Handle modal open/close for full-screen QR view
            const modal = document.querySelector('#qr-fullscreen-modal');
            if (modal) {
                modal.addEventListener('shown.bs.modal', () => {
                    window.screenOrientationManager.lockLandscapePrimary('qr-fullscreen');
                });

                modal.addEventListener('hidden.bs.modal', () => {
                    window.screenOrientationManager.unlock();
                    // Re-lock to landscape if still on item detail
                    window.screenOrientationManager.lockLandscape('item-detail');
                });
            }

            // Unlock when leaving the page
            window.addEventListener('beforeunload', () => {
                if (window.screenOrientationManager.isLocked) {
                    window.screenOrientationManager.unlock();
                }
            });
        }
    });
}());
