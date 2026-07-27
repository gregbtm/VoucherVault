/**
 * App Shell Architecture
 * Implements efficient shell caching and progressive content loading
 * Provides instant UI rendering while content loads in background
 */

(function() {
    'use strict';

    class AppShell {
        constructor() {
            this.shellCache = new Map();
            this.isNavigating = false;
            this.currentRoute = window.location.pathname;

            this.init();
        }

        init() {
            this.cacheShell();
            this.setupNavigation();
            this.setupProgressBar();
            console.log('[App Shell] Initialized');
        }

        cacheShell() {
            const shell = this.extractShell(document);
            if (shell) {
                this.shellCache.set('default', shell);
            }
        }

        extractShell() {
            const shell = {
                header: document.querySelector('.header')?.cloneNode(true),
                sidebar: document.querySelector('.sidebar')?.cloneNode(true),
                nav: document.querySelector('nav')?.cloneNode(true),
                footer: document.querySelector('footer')?.cloneNode(true)
            };

            return shell;
        }

        setupNavigation() {
            document.addEventListener('click', (e) => {
                const link = e.target.closest('a');
                if (!link) return;

                const href = link.getAttribute('href');
                if (!href || href.startsWith('#') || href.startsWith('javascript:') || href.startsWith('http')) {
                    return;
                }

                if (this.isInternalNavigation(href)) {
                    e.preventDefault();
                    this.navigateTo(href);
                }
            });

            window.addEventListener('popstate', (e) => {
                if (e.state?.shellLoaded) {
                    this.loadPage(window.location.pathname, false);
                }
            });
        }

        isInternalNavigation(href) {
            const current = new URL(window.location.href);
            try {
                const target = new URL(href, window.location.href);
                return current.origin === target.origin && !href.includes('/admin/');
            } catch {
                return false;
            }
        }

        navigateTo(url) {
            if (this.isNavigating) return;

            this.isNavigating = true;
            this.showTransition();
            this.loadPage(url, true);
        }

        loadPage(url, updateHistory) {
            const contentContainer = document.querySelector('[data-app-content]') || document.querySelector('main');

            if (!contentContainer) {
                window.location.href = url;
                return;
            }

            this.showLoadingState(contentContainer);

            fetch(url, {
                headers: { 'X-Requested-With': 'XMLHttpRequest' }
            })
            .then(response => {
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                return response.text();
            })
            .then(html => {
                const parser = new DOMParser();
                const doc = parser.parseFromString(html, 'text/html');

                this.updateContent(contentContainer, doc);
                this.updateMeta(doc);

                if (updateHistory) {
                    window.history.pushState(
                        { shellLoaded: true, url },
                        document.title,
                        url
                    );
                }

                this.currentRoute = url;
                this.hideTransition();
                this.isNavigating = false;

                this.reinitializeScripts(contentContainer);
            })
            .catch(err => {
                console.error('[App Shell] Load failed:', err);
                window.location.href = url;
            });
        }

        updateContent(container, doc) {
            const newContent = doc.querySelector('[data-app-content]') || doc.querySelector('main');
            if (newContent) {
                container.innerHTML = newContent.innerHTML;
            }
        }

        updateMeta(doc) {
            const title = doc.querySelector('title')?.textContent;
            if (title) {
                document.title = title;
            }

            const metaTags = doc.querySelectorAll('meta[property^="og:"], meta[name^="description"]');
            metaTags.forEach(tag => {
                const existing = document.querySelector(
                    `meta[${tag.hasAttribute('property') ? 'property' : 'name'}="${tag.getAttribute(tag.hasAttribute('property') ? 'property' : 'name')}"]`
                );
                if (existing) {
                    existing.remove();
                }
                document.head.appendChild(tag.cloneNode(true));
            });
        }

        showLoadingState(container) {
            const skeleton = document.createElement('div');
            skeleton.className = 'skeleton-loader';
            skeleton.innerHTML = this.generateSkeleton();
            container.appendChild(skeleton);
        }

        generateSkeleton() {
            return `
                <div class="skeleton-item">
                    <div class="skeleton-line short"></div>
                    <div class="skeleton-line"></div>
                    <div class="skeleton-line"></div>
                </div>
                <div class="skeleton-item">
                    <div class="skeleton-line short"></div>
                    <div class="skeleton-line"></div>
                    <div class="skeleton-line"></div>
                </div>
                <div class="skeleton-item">
                    <div class="skeleton-line short"></div>
                    <div class="skeleton-line"></div>
                    <div class="skeleton-line"></div>
                </div>
            `;
        }

        setupProgressBar() {
            const style = document.createElement('style');
            style.textContent = `
                .app-progress-bar {
                    position: fixed;
                    top: 0;
                    left: 0;
                    height: 3px;
                    background: linear-gradient(90deg, #4CAF50, #45a049);
                    width: 0%;
                    z-index: 9999;
                    transition: width 0.3s ease;
                    pointer-events: none;
                }
            `;
            document.head.appendChild(style);
        }

        showTransition() {
            const bar = document.querySelector('.app-progress-bar');
            if (!bar) {
                const progressBar = document.createElement('div');
                progressBar.className = 'app-progress-bar';
                document.body.appendChild(progressBar);
                progressBar.style.width = '30%';
            } else {
                bar.style.width = '30%';
            }
        }

        hideTransition() {
            const bar = document.querySelector('.app-progress-bar');
            if (bar) {
                bar.style.width = '100%';
                setTimeout(() => {
                    bar.style.opacity = '0';
                    bar.style.transition = 'opacity 0.3s ease';
                    setTimeout(() => {
                        bar.style.width = '0%';
                        bar.style.opacity = '1';
                        bar.style.transition = 'width 0.3s ease';
                    }, 300);
                }, 200);
            }
        }

        reinitializeScripts(container) {
            const scripts = container.querySelectorAll('script');
            scripts.forEach(script => {
                const newScript = document.createElement('script');
                if (script.src) {
                    newScript.src = script.src;
                } else {
                    newScript.textContent = script.textContent;
                }
                if (script.nonce) {
                    newScript.nonce = script.nonce;
                }
                document.head.appendChild(newScript);
                script.remove();
            });

            this.reinitializeBehaviors();
        }

        reinitializeBehaviors() {
            document.dispatchEvent(new CustomEvent('app-shell-loaded', {
                detail: { route: this.currentRoute }
            }));

            if (typeof Motion !== 'undefined') {
                Motion.refresh();
            }

            if (typeof initializeTooltips === 'function') {
                initializeTooltips();
            }

            document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(el => {
                new bootstrap.Tooltip(el);
            });

            if (window.gestureController) {
                window.gestureController.init();
            }
        }

        prefetchPages(urls) {
            if (!('serviceWorker' in navigator)) return;

            urls.forEach(url => {
                const link = document.createElement('link');
                link.rel = 'prefetch';
                link.href = url;
                document.head.appendChild(link);
            });
        }

        getShellHtml() {
            const shell = this.shellCache.get('default');
            if (!shell) return '';

            const container = document.createElement('div');
            Object.values(shell).forEach(element => {
                if (element) container.appendChild(element.cloneNode(true));
            });

            return container.innerHTML;
        }
    }

    window.appShell = new AppShell();
}());
