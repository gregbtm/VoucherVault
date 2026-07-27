/**
 * Help System - Centralized In-App Documentation
 * Provides searchable, contextual help for all app features
 * Works across all browsers, accessible via modal or inline help
 */

(function() {
    'use strict';

    class HelpSystem {
        constructor() {
            this.helpContent = new Map();
            this.currentTopic = null;
            this.searchIndex = [];
            this.isOpen = false;

            this.init();
        }

        async init() {
            await this.loadHelpContent();
            this.buildSearchIndex();
            this.setupHelpUI();
            this.attachContextualHelp();
            console.log('[Help System] Initialized with', this.helpContent.size, 'topics');
        }

        async loadHelpContent() {
            try {
                const response = await fetch('/api/v1/help/topics/', {
                    headers: { 'X-Requested-With': 'XMLHttpRequest' }
                });
                if (!response.ok) {
                    console.warn('[Help System] Could not load help content');
                    return;
                }

                const data = await response.json();
                data.results.forEach(topic => {
                    this.helpContent.set(topic.slug, {
                        id: topic.id,
                        title: topic.title,
                        slug: topic.slug,
                        content: topic.content,
                        category: topic.category,
                        related: topic.related_topics || [],
                        tags: topic.tags || []
                    });
                });
            } catch (error) {
                console.error('[Help System] Failed to load help content:', error);
            }
        }

        buildSearchIndex() {
            this.searchIndex = Array.from(this.helpContent.values()).map(topic => ({
                slug: topic.slug,
                title: topic.title,
                content: topic.content,
                category: topic.category,
                tags: topic.tags,
                fullText: `${topic.title} ${topic.content} ${topic.tags.join(' ')}`.toLowerCase()
            }));
        }

        setupHelpUI() {
            this.createHelpModal();
            this.createHelpButton();
            this.setupSearchBar();
        }

        createHelpModal() {
            const modal = document.createElement('div');
            modal.id = 'help-modal';
            modal.className = 'help-modal';
            modal.innerHTML = `
                <div class="help-modal-overlay"></div>
                <div class="help-modal-container">
                    <div class="help-modal-header">
                        <h2>Help & Documentation</h2>
                        <button class="help-close-btn" aria-label="Close help">
                            <i class="bi bi-x-lg"></i>
                        </button>
                    </div>
                    <div class="help-modal-content">
                        <div class="help-search-box">
                            <input type="text"
                                   class="help-search-input"
                                   placeholder="Search help topics..."
                                   aria-label="Search help">
                            <div class="help-search-results" style="display: none;"></div>
                        </div>
                        <div class="help-categories">
                            <div class="help-category-list"></div>
                        </div>
                        <div class="help-viewer" style="display: none;">
                            <button class="help-back-btn">
                                <i class="bi bi-chevron-left"></i> Back
                            </button>
                            <div class="help-viewer-content"></div>
                        </div>
                    </div>
                </div>
            `;
            document.body.appendChild(modal);

            modal.querySelector('.help-close-btn').addEventListener('click', () => this.closeHelp());
            modal.querySelector('.help-modal-overlay').addEventListener('click', () => this.closeHelp());
            modal.querySelector('.help-back-btn').addEventListener('click', () => this.showCategories());

            this.setupSearchInput();
        }

        createHelpButton() {
            const helpButton = document.createElement('button');
            helpButton.className = 'help-main-button';
            helpButton.innerHTML = '<i class="bi bi-question-circle"></i>';
            helpButton.title = 'Open Help';
            helpButton.setAttribute('aria-label', 'Open Help');
            helpButton.addEventListener('click', () => this.openHelp());

            const header = document.querySelector('.header') || document.querySelector('header');
            if (header) {
                header.appendChild(helpButton);
            } else {
                document.body.appendChild(helpButton);
            }
        }

        setupSearchInput() {
            const searchInput = document.querySelector('.help-search-input');
            const resultsContainer = document.querySelector('.help-search-results');

            searchInput.addEventListener('input', (e) => {
                const query = e.target.value.toLowerCase().trim();

                if (!query) {
                    resultsContainer.style.display = 'none';
                    return;
                }

                const results = this.searchHelpContent(query);
                this.displaySearchResults(results, resultsContainer);
            });
        }

        searchHelpContent(query) {
            return this.searchIndex
                .filter(item => item.fullText.includes(query))
                .slice(0, 10)
                .sort((a, b) => {
                    const aTitle = a.title.toLowerCase();
                    const bTitle = b.title.toLowerCase();
                    const aMatch = aTitle.startsWith(query);
                    const bMatch = bTitle.startsWith(query);
                    return bMatch - aMatch;
                });
        }

        displaySearchResults(results, container) {
            if (results.length === 0) {
                container.innerHTML = '<div class="help-no-results">No topics found</div>';
                container.style.display = 'block';
                return;
            }

            container.innerHTML = results
                .map(result => `
                    <button class="help-search-result" data-slug="${result.slug}">
                        <div class="result-title">${this.highlightQuery(result.title)}</div>
                        <div class="result-category">${result.category}</div>
                    </button>
                `)
                .join('');

            container.style.display = 'block';

            container.querySelectorAll('.help-search-result').forEach(btn => {
                btn.addEventListener('click', () => {
                    const slug = btn.dataset.slug;
                    this.showTopic(slug);
                    container.style.display = 'none';
                });
            });
        }

        highlightQuery(text) {
            const query = document.querySelector('.help-search-input')?.value || '';
            if (!query) return text;
            const regex = new RegExp(`(${query})`, 'gi');
            return text.replace(regex, '<strong>$1</strong>');
        }

        openHelp() {
            const modal = document.getElementById('help-modal');
            modal.classList.add('visible');
            this.isOpen = true;
            this.showCategories();
            document.body.style.overflow = 'hidden';
        }

        closeHelp() {
            const modal = document.getElementById('help-modal');
            modal.classList.remove('visible');
            this.isOpen = false;
            document.body.style.overflow = '';
        }

        showCategories() {
            const modal = document.getElementById('help-modal');
            modal.querySelector('.help-categories').style.display = 'block';
            modal.querySelector('.help-viewer').style.display = 'none';

            const categoryList = modal.querySelector('.help-category-list');
            const categories = this.groupByCategory();

            categoryList.innerHTML = Object.entries(categories)
                .map(([category, topics]) => `
                    <div class="help-category">
                        <h3 class="help-category-title">${category}</h3>
                        <ul class="help-category-topics">
                            ${topics.map(topic => `
                                <li>
                                    <button class="help-topic-link" data-slug="${topic.slug}">
                                        ${topic.title}
                                    </button>
                                </li>
                            `).join('')}
                        </ul>
                    </div>
                `)
                .join('');

            categoryList.querySelectorAll('.help-topic-link').forEach(btn => {
                btn.addEventListener('click', () => {
                    const slug = btn.dataset.slug;
                    this.showTopic(slug);
                });
            });
        }

        showTopic(slug) {
            const topic = this.helpContent.get(slug);
            if (!topic) return;

            this.currentTopic = slug;
            const modal = document.getElementById('help-modal');
            modal.querySelector('.help-categories').style.display = 'none';
            modal.querySelector('.help-viewer').style.display = 'block';

            const viewerContent = modal.querySelector('.help-viewer-content');
            viewerContent.innerHTML = `
                <article class="help-article">
                    <header>
                        <h2>${topic.title}</h2>
                        <div class="help-meta">
                            <span class="help-category-badge">${topic.category}</span>
                        </div>
                    </header>
                    <div class="help-article-content">
                        ${topic.content}
                    </div>
                    ${topic.related.length > 0 ? `
                        <div class="help-related">
                            <h4>Related Topics</h4>
                            <ul>
                                ${topic.related.map(relSlug => {
                                    const relTopic = this.helpContent.get(relSlug);
                                    return relTopic ? `
                                        <li>
                                            <button class="help-related-link" data-slug="${relSlug}">
                                                ${relTopic.title}
                                            </button>
                                        </li>
                                    ` : '';
                                }).join('')}
                            </ul>
                        </div>
                    ` : ''}
                </article>
            `;

            viewerContent.querySelectorAll('.help-related-link').forEach(btn => {
                btn.addEventListener('click', () => {
                    const slug = btn.dataset.slug;
                    this.showTopic(slug);
                });
            });

            this.logHelpAccess(slug);
        }

        attachContextualHelp() {
            document.addEventListener('contextualHelp', (e) => {
                if (e.detail?.topic) {
                    this.openHelp();
                    this.showTopic(e.detail.topic);
                }
            });
        }

        groupByCategory() {
            const categories = {};
            this.helpContent.forEach(topic => {
                if (!categories[topic.category]) {
                    categories[topic.category] = [];
                }
                categories[topic.category].push(topic);
            });

            Object.keys(categories).forEach(cat => {
                categories[cat].sort((a, b) => a.title.localeCompare(b.title));
            });

            return categories;
        }

        logHelpAccess(topic) {
            if (navigator.sendBeacon) {
                navigator.sendBeacon('/api/v1/analytics/help-access/', JSON.stringify({
                    topic,
                    timestamp: new Date().toISOString()
                }));
            }
        }

        showContextualHelpButton(slug, container) {
            const btn = document.createElement('button');
            btn.className = 'contextual-help-btn';
            btn.innerHTML = '<i class="bi bi-question-circle"></i>';
            btn.title = 'Get help with this';
            btn.setAttribute('aria-label', 'Help with this feature');

            btn.addEventListener('click', (e) => {
                e.preventDefault();
                this.openHelp();
                this.showTopic(slug);
            });

            if (container) {
                container.appendChild(btn);
            }
            return btn;
        }

        setupSearchBar() {
            const searchInput = document.querySelector('.help-search-input');
            if (searchInput) {
                searchInput.addEventListener('keydown', (e) => {
                    if (e.key === 'Escape') {
                        this.closeHelp();
                    }
                });
            }
        }
    }

    window.helpSystem = new HelpSystem();

    window.showContextualHelp = (topic) => {
        const event = new CustomEvent('contextualHelp', { detail: { topic } });
        document.dispatchEvent(event);
    };
}());
