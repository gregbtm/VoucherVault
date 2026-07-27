/**
 * PWA Periodic Background Sync
 * Enables background syncing of notifications and data updates
 * Implements periodic background sync API with graceful degradation
 */

(function() {
    'use strict';

    class PeriodicSyncManager {
        constructor() {
            this.supported = 'serviceWorker' in navigator && 'periodicSync' in ServiceWorkerRegistration.prototype;
            this.syncTags = {
                checkNotifications: 'check-notifications',
                syncOfflineChanges: 'sync-offline-changes',
                updateInventory: 'update-inventory'
            };
            this.syncIntervals = {
                checkNotifications: 1 * 60 * 60 * 1000, // 1 hour
                syncOfflineChanges: 30 * 60 * 1000,     // 30 minutes
                updateInventory: 6 * 60 * 60 * 1000     // 6 hours
            };

            this.init();
        }

        async init() {
            if (!this.supported) {
                console.log('[PWA Periodic Sync] API not supported on this platform');
                return;
            }

            try {
                const registration = await navigator.serviceWorker.ready;
                await this.registerPeriodicSync(registration);
                console.log('[PWA Periodic Sync] Initialized successfully');
            } catch (error) {
                console.error('[PWA Periodic Sync] Initialization failed:', error);
            }
        }

        /**
         * Register periodic sync tags with the service worker
         */
        async registerPeriodicSync(registration) {
            if (!this.supported || !registration.periodicSync) {
                return;
            }

            try {
                // Check which syncs are already registered
                const tags = await registration.periodicSync.getTags();
                console.log('[PWA Periodic Sync] Currently registered tags:', tags);

                // Register notification check sync
                if (!tags.includes(this.syncTags.checkNotifications)) {
                    try {
                        await registration.periodicSync.register(
                            this.syncTags.checkNotifications,
                            { minInterval: this.syncIntervals.checkNotifications }
                        );
                        console.log('[PWA Periodic Sync] Registered:', this.syncTags.checkNotifications);
                    } catch (error) {
                        if (error.name === 'NotAllowedError') {
                            console.warn('[PWA Periodic Sync] Permission denied for periodic sync');
                        } else {
                            throw error;
                        }
                    }
                }

                // Register offline sync
                if (!tags.includes(this.syncTags.syncOfflineChanges)) {
                    try {
                        await registration.periodicSync.register(
                            this.syncTags.syncOfflineChanges,
                            { minInterval: this.syncIntervals.syncOfflineChanges }
                        );
                        console.log('[PWA Periodic Sync] Registered:', this.syncTags.syncOfflineChanges);
                    } catch (error) {
                        if (error.name === 'NotAllowedError') {
                            console.warn('[PWA Periodic Sync] Permission denied for periodic sync');
                        }
                    }
                }

                // Register inventory update sync
                if (!tags.includes(this.syncTags.updateInventory)) {
                    try {
                        await registration.periodicSync.register(
                            this.syncTags.updateInventory,
                            { minInterval: this.syncIntervals.updateInventory }
                        );
                        console.log('[PWA Periodic Sync] Registered:', this.syncTags.updateInventory);
                    } catch (error) {
                        if (error.name === 'NotAllowedError') {
                            console.warn('[PWA Periodic Sync] Permission denied for periodic sync');
                        }
                    }
                }
            } catch (error) {
                console.error('[PWA Periodic Sync] Failed to register sync:', error);
            }
        }

        /**
         * Unregister a periodic sync tag
         */
        async unregisterSync(tag) {
            if (!this.supported) return false;

            try {
                const registration = await navigator.serviceWorker.ready;
                if (registration.periodicSync) {
                    await registration.periodicSync.unregister(tag);
                    console.log('[PWA Periodic Sync] Unregistered:', tag);
                    return true;
                }
            } catch (error) {
                console.error('[PWA Periodic Sync] Failed to unregister sync:', error);
            }
            return false;
        }

        /**
         * Get all registered periodic sync tags
         */
        async getRegisteredSyncs() {
            if (!this.supported) return [];

            try {
                const registration = await navigator.serviceWorker.ready;
                if (registration.periodicSync) {
                    return await registration.periodicSync.getTags();
                }
            } catch (error) {
                console.error('[PWA Periodic Sync] Failed to get tags:', error);
            }
            return [];
        }

        /**
         * Trigger an immediate sync (requires one-time permission)
         */
        async triggerSync(tag) {
            if (!this.supported) return false;

            try {
                const registration = await navigator.serviceWorker.ready;
                if (registration.sync) {
                    // Use the one-time sync API (Background Sync API v1)
                    await registration.sync.register(tag);
                    console.log('[PWA Periodic Sync] Triggered sync for:', tag);
                    return true;
                }
            } catch (error) {
                console.error('[PWA Periodic Sync] Failed to trigger sync:', error);
            }
            return false;
        }
    }

    // Initialize and expose globally
    window.periodicSyncManager = new PeriodicSyncManager();
}());
