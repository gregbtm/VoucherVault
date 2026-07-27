/**
 * Mobile Gesture Controller
 * Handles swipe, pull-to-refresh, long-press, and haptic feedback
 * Provides intuitive touch interactions for inventory and item lists
 */

(function() {
    'use strict';

    class GestureController {
        constructor() {
            this.touchStartX = 0;
            this.touchStartY = 0;
            this.touchStartTime = 0;
            this.longPressTimer = null;
            this.minSwipeDistance = 50;
            this.swipeThreshold = 150;
            this.longPressDuration = 500;
            this.pullThreshold = 100;
            this.pullStartY = 0;
            this.isPulling = false;

            this.init();
        }

        init() {
            this.setupSwipeGestures();
            this.setupPullToRefresh();
            this.setupLongPress();
            console.log('[Gesture Controller] Initialized');
        }

        setupSwipeGestures() {
            document.addEventListener('touchstart', (e) => this.handleTouchStart(e), false);
            document.addEventListener('touchmove', (e) => this.handleTouchMove(e), false);
            document.addEventListener('touchend', (e) => this.handleTouchEnd(e), false);
        }

        handleTouchStart(e) {
            this.touchStartX = e.touches[0].clientX;
            this.touchStartY = e.touches[0].clientY;
            this.touchStartTime = Date.now();
            this.pullStartY = e.touches[0].clientY;
        }

        handleTouchMove(e) {
            const currentY = e.touches[0].clientY;
            const scrollTop = window.scrollY || document.documentElement.scrollTop;

            if (scrollTop === 0 && currentY > this.pullStartY) {
                const pullDistance = currentY - this.pullStartY;
                if (pullDistance > 20) {
                    this.isPulling = true;
                    this.showPullIndicator(pullDistance);
                }
            }
        }

        handleTouchEnd(e) {
            const touchEndX = e.changedTouches[0].clientX;
            const touchEndY = e.changedTouches[0].clientY;
            const touchDuration = Date.now() - this.touchStartTime;
            const deltaX = touchEndX - this.touchStartX;
            const deltaY = Math.abs(touchEndY - this.touchStartY);

            if (this.isPulling && touchDuration < 500) {
                const pullDistance = this.pullStartY - touchEndY;
                if (pullDistance > this.pullThreshold) {
                    this.triggerPullToRefresh();
                }
                this.hidePullIndicator();
                this.isPulling = false;
                return;
            }

            if (touchDuration > 300) return;
            if (deltaY > 50) return;

            if (Math.abs(deltaX) > this.minSwipeDistance) {
                if (deltaX < -this.swipeThreshold) {
                    this.handleSwipeLeft(e.target);
                } else if (deltaX > this.swipeThreshold) {
                    this.handleSwipeRight(e.target);
                }
            }
        }

        handleSwipeLeft(target) {
            const itemRow = target.closest('.inventory-item, .item-card, .list-item');
            if (!itemRow) return;

            itemRow.classList.add('swiped-left');
            this.showSwipeActions(itemRow, 'left');
            this.triggerHaptic('light');
        }

        handleSwipeRight(target) {
            const itemRow = target.closest('.inventory-item, .item-card, .list-item');
            if (!itemRow) return;

            if (itemRow.classList.contains('swiped-left')) {
                itemRow.classList.remove('swiped-left');
                this.hideSwipeActions(itemRow);
                this.triggerHaptic('light');
            }
        }

        showSwipeActions(itemRow, direction) {
            let swipeContainer = itemRow.querySelector('.swipe-actions');
            if (!swipeContainer) {
                swipeContainer = document.createElement('div');
                swipeContainer.className = `swipe-actions swipe-${direction}`;

                const archiveBtn = document.createElement('button');
                archiveBtn.className = 'swipe-action swipe-archive';
                archiveBtn.innerHTML = '<i class="bi bi-archive"></i>';
                archiveBtn.title = 'Archive item';
                archiveBtn.addEventListener('click', (e) => {
                    e.preventDefault();
                    this.archiveItem(itemRow);
                });

                const deleteBtn = document.createElement('button');
                deleteBtn.className = 'swipe-action swipe-delete';
                deleteBtn.innerHTML = '<i class="bi bi-trash"></i>';
                deleteBtn.title = 'Delete item';
                deleteBtn.addEventListener('click', (e) => {
                    e.preventDefault();
                    this.deleteItem(itemRow);
                });

                swipeContainer.appendChild(archiveBtn);
                swipeContainer.appendChild(deleteBtn);
                itemRow.appendChild(swipeContainer);
            }

            swipeContainer.style.display = 'flex';
        }

        hideSwipeActions(itemRow) {
            const swipeContainer = itemRow.querySelector('.swipe-actions');
            if (swipeContainer) {
                swipeContainer.style.display = 'none';
            }
        }

        setupPullToRefresh() {
            const refreshContainer = document.querySelector('[data-pull-to-refresh]');
            if (refreshContainer) {
                refreshContainer.classList.add('pull-to-refresh-enabled');
            }
        }

        showPullIndicator(distance) {
            let indicator = document.querySelector('.pull-indicator');
            if (!indicator) {
                indicator = document.createElement('div');
                indicator.className = 'pull-indicator';
                indicator.innerHTML = '<div class="pull-spinner"></div><span>Pull to refresh</span>';
                document.body.prepend(indicator);
            }

            const progress = Math.min(distance / this.pullThreshold, 1);
            indicator.style.opacity = progress;
            indicator.style.transform = `translateY(${distance}px)`;

            if (progress >= 1) {
                indicator.classList.add('ready');
                indicator.querySelector('span').textContent = 'Release to refresh';
            } else {
                indicator.classList.remove('ready');
                indicator.querySelector('span').textContent = 'Pull to refresh';
            }
        }

        hidePullIndicator() {
            const indicator = document.querySelector('.pull-indicator');
            if (indicator) {
                indicator.style.opacity = '0';
                setTimeout(() => {
                    if (indicator.parentNode) {
                        indicator.remove();
                    }
                }, 300);
            }
        }

        triggerPullToRefresh() {
            const event = new CustomEvent('pull-to-refresh', { detail: { timestamp: Date.now() } });
            document.dispatchEvent(event);
            this.triggerHaptic('medium');

            const inventoryList = document.querySelector('[data-pull-to-refresh]');
            if (inventoryList) {
                inventoryList.classList.add('refreshing');

                fetch(window.location.pathname, {
                    headers: { 'X-Requested-With': 'XMLHttpRequest' }
                })
                .then(response => response.text())
                .then(html => {
                    const parser = new DOMParser();
                    const doc = parser.parseFromString(html, 'text/html');
                    const newList = doc.querySelector('[data-pull-to-refresh]');

                    if (newList) {
                        inventoryList.innerHTML = newList.innerHTML;
                        this.triggerHaptic('success');
                    }
                })
                .catch(err => {
                    console.error('[Gesture Controller] Pull-to-refresh failed:', err);
                    this.triggerHaptic('error');
                })
                .finally(() => {
                    inventoryList.classList.remove('refreshing');
                });
            }
        }

        setupLongPress() {
            document.addEventListener('touchstart', (e) => {
                this.clearLongPressTimer();
                const target = e.target.closest('.inventory-item, .item-card, .list-item');
                if (target) {
                    this.longPressTimer = setTimeout(() => {
                        this.handleLongPress(target);
                    }, this.longPressDuration);
                }
            });

            document.addEventListener('touchend', () => this.clearLongPressTimer());
            document.addEventListener('touchmove', () => this.clearLongPressTimer());
        }

        clearLongPressTimer() {
            if (this.longPressTimer) {
                clearTimeout(this.longPressTimer);
                this.longPressTimer = null;
            }
        }

        handleLongPress(target) {
            this.triggerHaptic('heavy');
            target.classList.add('long-pressed');

            const menu = this.createContextMenu(target);
            this.showContextMenu(menu, event);
        }

        createContextMenu(target) {
            const menu = document.createElement('div');
            menu.className = 'context-menu';

            const itemId = target.dataset.itemId || target.getAttribute('data-item-id');
            const itemName = target.querySelector('[data-item-name]')?.textContent || 'Item';

            const actions = [
                { label: 'View Details', icon: 'bi-eye', action: () => this.viewItem(itemId) },
                { label: 'Edit', icon: 'bi-pencil', action: () => this.editItem(itemId) },
                { label: 'Archive', icon: 'bi-archive', action: () => this.archiveItem(target) },
                { label: 'Delete', icon: 'bi-trash', action: () => this.deleteItem(target) }
            ];

            actions.forEach(act => {
                const btn = document.createElement('button');
                btn.className = 'context-menu-item';
                btn.innerHTML = `<i class="bi ${act.icon}"></i> ${act.label}`;
                btn.addEventListener('click', () => {
                    act.action();
                    this.hideContextMenu(menu);
                });
                menu.appendChild(btn);
            });

            return menu;
        }

        showContextMenu(menu, event) {
            document.body.appendChild(menu);
            menu.style.position = 'fixed';
            menu.style.left = `${event.touches?.[0]?.clientX || event.clientX}px`;
            menu.style.top = `${event.touches?.[0]?.clientY || event.clientY}px`;

            setTimeout(() => menu.classList.add('visible'), 10);

            document.addEventListener('click', () => this.hideContextMenu(menu), { once: true });
        }

        hideContextMenu(menu) {
            menu.classList.remove('visible');
            setTimeout(() => {
                if (menu.parentNode) {
                    menu.remove();
                }
            }, 300);
        }

        archiveItem(itemRow) {
            const itemId = itemRow.dataset.itemId || itemRow.getAttribute('data-item-id');
            if (!itemId) return;

            fetch(`/api/v1/items/${itemId}/`, {
                method: 'PATCH',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': this.getCsrfToken()
                },
                body: JSON.stringify({ is_archived: true })
            })
            .then(response => {
                if (response.ok) {
                    this.removeItemWithUndo(itemRow, 'archived');
                }
            })
            .catch(err => console.error('[Gesture Controller] Archive failed:', err));
        }

        deleteItem(itemRow) {
            const itemId = itemRow.dataset.itemId || itemRow.getAttribute('data-item-id');
            if (!itemId || !confirm('Are you sure you want to delete this item?')) return;

            fetch(`/api/v1/items/${itemId}/`, {
                method: 'DELETE',
                headers: { 'X-CSRFToken': this.getCsrfToken() }
            })
            .then(response => {
                if (response.ok) {
                    this.removeItemWithUndo(itemRow, 'deleted');
                }
            })
            .catch(err => console.error('[Gesture Controller] Delete failed:', err));
        }

        removeItemWithUndo(itemRow, action) {
            itemRow.classList.add('removing');
            this.triggerHaptic('success');

            setTimeout(() => {
                itemRow.remove();
                this.showUndoNotification(action, itemRow);
            }, 300);
        }

        showUndoNotification(action, itemRow) {
            const notification = document.createElement('div');
            notification.className = 'gesture-notification';
            notification.innerHTML = `<span>Item ${action}</span><button class="undo-btn">Undo</button>`;

            document.body.appendChild(notification);
            notification.classList.add('visible');

            notification.querySelector('.undo-btn').addEventListener('click', () => {
                itemRow.classList.remove('removing');
                notification.remove();
            });

            setTimeout(() => {
                notification.classList.remove('visible');
                setTimeout(() => notification.remove(), 300);
            }, 4000);
        }

        viewItem(itemId) {
            if (itemId) {
                window.location.href = `/en/items/${itemId}/`;
            }
        }

        editItem(itemId) {
            if (itemId) {
                window.location.href = `/en/items/${itemId}/edit/`;
            }
        }

        triggerHaptic(type = 'light') {
            if (!navigator.vibrate) return;

            const patterns = {
                light: 10,
                medium: 20,
                heavy: 40,
                success: [10, 20, 10],
                error: [30, 10, 30]
            };

            navigator.vibrate(patterns[type] || 10);
        }

        getCsrfToken() {
            return document.querySelector('[name="csrfmiddlewaretoken"]')?.value ||
                   document.cookie.split(';').find(c => c.trim().startsWith('csrftoken='))?.split('=')[1] ||
                   '';
        }
    }

    window.gestureController = new GestureController();
}());
