/**
 * Shared prefers-reduced-motion check, computed once at load - three
 * separate scripts (animations.js, main.js, mobile-optimizations.js)
 * each ran their own `window.matchMedia('(prefers-reduced-motion: reduce)')`
 * query independently. Loaded before all three (see base.html) so they can
 * read this instead.
 */
window.VVPrefersReducedMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
