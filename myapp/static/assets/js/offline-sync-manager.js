/* Phase 7: Advanced Offline Data Sync Manager */

class OfflineSyncManager {
  constructor() {
    this.dbName = 'VoucherVaultOfflineSync';
    this.dbVersion = 1;
    this.db = null;
    this.isOnline = navigator.onLine;
    this.isSyncing = false;
    this.syncQueue = [];
    this.conflictResolutions = new Map();

    this.init();
  }

  async init() {
    await this.initDB();
    this.setupEventListeners();
    this.setupPeriodicSync();
  }

  async initDB() {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(this.dbName, this.dbVersion);

      request.onerror = () => reject(request.error);
      request.onsuccess = () => {
        this.db = request.result;
        resolve(this.db);
      };

      request.onupgradeneeded = (event) => {
        const db = event.target.result;

        // Store offline changes
        if (!db.objectStoreNames.contains('offline_changes')) {
          const changeStore = db.createObjectStore('offline_changes', {
            keyPath: 'id',
            autoIncrement: true
          });
          changeStore.createIndex('timestamp', 'timestamp', { unique: false });
          changeStore.createIndex('resource_type', 'resource_type', { unique: false });
          changeStore.createIndex('resource_id', 'resource_id', { unique: false });
          changeStore.createIndex('status', 'status', { unique: false });
        }

        // Store sync conflicts
        if (!db.objectStoreNames.contains('sync_conflicts')) {
          const conflictStore = db.createObjectStore('sync_conflicts', {
            keyPath: 'id',
            autoIncrement: true
          });
          conflictStore.createIndex('resource_id', 'resource_id', { unique: false });
          conflictStore.createIndex('resolved', 'resolved', { unique: false });
        }

        // Cache server data for conflict detection
        if (!db.objectStoreNames.contains('cached_data')) {
          const cacheStore = db.createObjectStore('cached_data', {
            keyPath: ['resource_type', 'resource_id']
          });
          cacheStore.createIndex('timestamp', 'timestamp', { unique: false });
        }

        // Sync metadata
        if (!db.objectStoreNames.contains('sync_metadata')) {
          db.createObjectStore('sync_metadata', { keyPath: 'key' });
        }
      };
    });
  }

  setupEventListeners() {
    window.addEventListener('online', () => this.handleOnline());
    window.addEventListener('offline', () => this.handleOffline());
  }

  setupPeriodicSync() {
    if ('serviceWorker' in navigator && 'SyncManager' in window) {
      navigator.serviceWorker.ready.then((registration) => {
        registration.sync.register('sync-offline-changes');
      }).catch((err) => {
        console.log('Periodic sync not available:', err);
      });
    }
  }

  handleOnline() {
    this.isOnline = true;
    document.dispatchEvent(new CustomEvent('app-online'));
    this.announce('You are back online. Syncing changes...', 'polite');
    this.syncChanges();
  }

  handleOffline() {
    this.isOnline = false;
    document.dispatchEvent(new CustomEvent('app-offline'));
    this.announce('You are offline. Changes will sync when online.', 'polite');
  }

  async recordChange(resourceType, resourceId, operation, data, metadata = {}) {
    if (!this.db) await this.initDB();

    const change = {
      resource_type: resourceType,
      resource_id: resourceId,
      operation: operation, // 'create', 'update', 'delete'
      data: data,
      metadata: metadata,
      timestamp: Date.now(),
      status: 'pending', // 'pending', 'syncing', 'synced', 'conflict'
      retry_count: 0,
      last_error: null
    };

    return new Promise((resolve, reject) => {
      const transaction = this.db.transaction(['offline_changes'], 'readwrite');
      const store = transaction.objectStore('offline_changes');
      const request = store.add(change);

      request.onsuccess = () => {
        change.id = request.result;
        this.syncQueue.push(change);
        document.dispatchEvent(new CustomEvent('offline-change-recorded', { detail: change }));
        resolve(change);
      };

      request.onerror = () => reject(request.error);
    });
  }

  async getPendingChanges() {
    if (!this.db) await this.initDB();

    return new Promise((resolve, reject) => {
      const transaction = this.db.transaction(['offline_changes'], 'readonly');
      const store = transaction.objectStore('offline_changes');
      const index = store.index('status');
      const range = IDBKeyRange.only('pending');
      const request = index.getAll(range);

      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  async syncChanges() {
    if (!this.isOnline || this.isSyncing) return;

    this.isSyncing = true;
    document.dispatchEvent(new CustomEvent('sync-started'));

    try {
      const changes = await this.getPendingChanges();

      for (const change of changes) {
        await this.syncChange(change);
      }

      this.announce('All changes synced successfully', 'polite');
      document.dispatchEvent(new CustomEvent('sync-completed'));
    } catch (error) {
      console.error('Sync error:', error);
      this.announce('Sync failed: ' + error.message, 'alert');
      document.dispatchEvent(new CustomEvent('sync-error', { detail: error }));
    } finally {
      this.isSyncing = false;
    }
  }

  async syncChange(change) {
    if (!this.db) await this.initDB();

    await this.updateChangeStatus(change.id, 'syncing');

    try {
      const response = await this.sendChangeToServer(change);

      if (response.ok) {
        const result = await response.json();
        await this.updateChangeStatus(change.id, 'synced', result);
        await this.cacheServerData(change.resource_type, result);
      } else if (response.status === 409) {
        // Conflict detected
        const conflict = await response.json();
        await this.handleConflict(change, conflict);
      } else {
        throw new Error(`Server error: ${response.status}`);
      }
    } catch (error) {
      console.error('Sync change error:', error);
      await this.updateChangeStatus(change.id, 'pending', null, error.message);

      if (change.retry_count >= 3) {
        await this.updateChangeStatus(change.id, 'conflict', null, 'Max retries exceeded');
      }
    }
  }

  async sendChangeToServer(change) {
    const endpoint = this.getEndpointForResource(change.resource_type);

    const options = {
      method: this.getHttpMethod(change.operation),
      headers: {
        'Content-Type': 'application/json',
        'X-Offline-Change': 'true',
        'X-Change-Timestamp': change.timestamp.toString()
      },
      body: JSON.stringify({
        ...change.data,
        _operation: change.operation,
        _client_timestamp: change.timestamp,
        _offline_change_id: change.id
      })
    };

    const url = change.operation === 'delete'
      ? `${endpoint}${change.resource_id}/`
      : `${endpoint}${change.resource_id ? change.resource_id + '/' : ''}`;

    return fetch(url, options);
  }

  async handleConflict(localChange, serverData) {
    if (!this.db) await this.initDB();

    const conflictId = `${localChange.resource_type}:${localChange.resource_id}`;
    const resolution = this.conflictResolutions.get(conflictId);

    if (resolution === 'server') {
      // Keep server version, discard local change
      await this.deleteChange(localChange.id);
      await this.cacheServerData(localChange.resource_type, serverData);
    } else if (resolution === 'client') {
      // Keep trying to push client version
      await this.updateChangeRetry(localChange.id);
    } else {
      // Create conflict record for user resolution
      const conflict = {
        resource_type: localChange.resource_type,
        resource_id: localChange.resource_id,
        local_change: localChange,
        server_data: serverData,
        timestamp: Date.now(),
        resolved: false
      };

      await new Promise((resolve, reject) => {
        const transaction = this.db.transaction(['sync_conflicts'], 'readwrite');
        const store = transaction.objectStore('sync_conflicts');
        const request = store.add(conflict);

        request.onsuccess = () => {
          document.dispatchEvent(new CustomEvent('conflict-detected', { detail: conflict }));
          resolve();
        };

        request.onerror = () => reject(request.error);
      });

      await this.updateChangeStatus(localChange.id, 'conflict');
    }
  }

  async resolveConflict(conflictId, resolution, resolutionData = null) {
    if (!this.db) await this.initDB();

    return new Promise((resolve, reject) => {
      const transaction = this.db.transaction(['sync_conflicts', 'offline_changes'], 'readwrite');
      const conflictStore = transaction.objectStore('sync_conflicts');
      const changeStore = transaction.objectStore('offline_changes');

      // Mark conflict as resolved
      const conflictRequest = conflictStore.update({
        ...conflictId,
        resolved: true,
        resolution: resolution,
        resolution_data: resolutionData,
        resolved_at: Date.now()
      });

      conflictRequest.onsuccess = () => {
        if (resolution === 'client') {
          // Retry syncing with client data
          const changeRequest = changeStore.update({
            ...conflictId,
            status: 'pending',
            retry_count: 0
          });

          changeRequest.onerror = () => reject(changeRequest.error);
        }

        resolve();
      };

      conflictRequest.onerror = () => reject(conflictRequest.error);
    });
  }

  async cacheServerData(resourceType, data) {
    if (!this.db) await this.initDB();

    return new Promise((resolve, reject) => {
      const transaction = this.db.transaction(['cached_data'], 'readwrite');
      const store = transaction.objectStore('cached_data');

      const cacheEntry = {
        resource_type: resourceType,
        resource_id: data.id,
        data: data,
        timestamp: Date.now()
      };

      const request = store.put(cacheEntry);

      request.onsuccess = () => resolve();
      request.onerror = () => reject(request.error);
    });
  }

  async updateChangeStatus(changeId, status, serverResponse = null, error = null) {
    if (!this.db) await this.initDB();

    return new Promise((resolve, reject) => {
      const transaction = this.db.transaction(['offline_changes'], 'readwrite');
      const store = transaction.objectStore('offline_changes');
      const request = store.get(changeId);

      request.onsuccess = () => {
        const change = request.result;
        if (change) {
          change.status = status;
          change.last_error = error;
          if (serverResponse) {
            change.server_response = serverResponse;
          }
          if (status === 'pending') {
            change.retry_count = (change.retry_count || 0) + 1;
          }

          const updateRequest = store.put(change);
          updateRequest.onsuccess = () => resolve();
          updateRequest.onerror = () => reject(updateRequest.error);
        }
      };

      request.onerror = () => reject(request.error);
    });
  }

  async updateChangeRetry(changeId) {
    if (!this.db) await this.initDB();

    return new Promise((resolve, reject) => {
      const transaction = this.db.transaction(['offline_changes'], 'readwrite');
      const store = transaction.objectStore('offline_changes');
      const request = store.get(changeId);

      request.onsuccess = () => {
        const change = request.result;
        if (change) {
          change.retry_count = (change.retry_count || 0) + 1;
          if (change.retry_count < 3) {
            change.status = 'pending';
          } else {
            change.status = 'conflict';
          }

          const updateRequest = store.put(change);
          updateRequest.onsuccess = () => resolve();
          updateRequest.onerror = () => reject(updateRequest.error);
        }
      };

      request.onerror = () => reject(request.error);
    });
  }

  async deleteChange(changeId) {
    if (!this.db) await this.initDB();

    return new Promise((resolve, reject) => {
      const transaction = this.db.transaction(['offline_changes'], 'readwrite');
      const store = transaction.objectStore('offline_changes');
      const request = store.delete(changeId);

      request.onsuccess = () => resolve();
      request.onerror = () => reject(request.error);
    });
  }

  getEndpointForResource(resourceType) {
    const endpoints = {
      'item': '/api/v1/items/',
      'wallet': '/api/v1/wallets/',
      'tag': '/api/v1/tags/',
      'notification_rule': '/api/v1/notifications/rules/',
      'wallet_membership': '/api/v1/wallet-memberships/'
    };

    return endpoints[resourceType] || '/api/v1/';
  }

  getHttpMethod(operation) {
    const methods = {
      'create': 'POST',
      'update': 'PATCH',
      'delete': 'DELETE'
    };

    return methods[operation] || 'POST';
  }

  announce(message, priority = 'polite') {
    const liveRegion = document.getElementById('aria-live-region');
    if (liveRegion) {
      liveRegion.setAttribute('aria-live', priority);
      liveRegion.textContent = message;
    }
  }

  // Get sync status for UI
  async getSyncStatus() {
    const pending = await this.getPendingChanges();
    const conflicts = await this.getConflicts();

    return {
      isOnline: this.isOnline,
      isSyncing: this.isSyncing,
      pendingChanges: pending.length,
      conflicts: conflicts.length,
      changes: pending
    };
  }

  async getConflicts() {
    if (!this.db) await this.initDB();

    return new Promise((resolve, reject) => {
      const transaction = this.db.transaction(['sync_conflicts'], 'readonly');
      const store = transaction.objectStore('sync_conflicts');
      const index = store.index('resolved');
      const range = IDBKeyRange.only(false);
      const request = index.getAll(range);

      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  // Clear all offline data (for testing)
  async clearOfflineData() {
    if (!this.db) await this.initDB();

    return new Promise((resolve, reject) => {
      const transaction = this.db.transaction(
        ['offline_changes', 'sync_conflicts', 'cached_data'],
        'readwrite'
      );

      transaction.objectStore('offline_changes').clear();
      transaction.objectStore('sync_conflicts').clear();
      transaction.objectStore('cached_data').clear();

      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error);
    });
  }
}

// Initialize global instance
if (typeof window !== 'undefined') {
  window.offlineSyncManager = new OfflineSyncManager();
}
