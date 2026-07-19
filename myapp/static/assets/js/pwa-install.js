/**
 * PWA Install Prompt (Phase C)
 *
 * Captures the browser's beforeinstallprompt event and shows a dismissable
 * bottom banner when the app is not yet installed. The banner is suppressed
 * after the user dismisses it (stored in localStorage) or after a successful
 * install. Touch targets are ≥ 48 px tall for easy mobile tapping.
 */

(function () {
    'use strict';

    const DISMISS_KEY = 'pwa_install_dismissed';
    const DISMISS_DAYS = 30;
    let deferredPrompt = null;

    function isDismissed() {
        const ts = localStorage.getItem(DISMISS_KEY);
        if (!ts) return false;
        const age = (Date.now() - parseInt(ts, 10)) / 86400000;
        return age < DISMISS_DAYS;
    }

    function dismiss() {
        localStorage.setItem(DISMISS_KEY, String(Date.now()));
        const banner = document.getElementById('pwa-install-banner');
        if (banner) banner.remove();
    }

    function showBanner() {
        if (document.getElementById('pwa-install-banner')) return;

        const banner = document.createElement('div');
        banner.id = 'pwa-install-banner';
        banner.setAttribute('role', 'complementary');
        banner.setAttribute('aria-label', 'Install app');
        banner.style.cssText = [
            'position:fixed', 'bottom:0', 'left:0', 'right:0', 'z-index:9999',
            'background:linear-gradient(135deg,#1a1a2e,#16213e)',
            'color:#fff', 'padding:12px 16px',
            'display:flex', 'align-items:center', 'gap:12px',
            'box-shadow:0 -2px 12px rgba(0,0,0,.4)',
            'min-height:56px',
        ].join(';');

        banner.innerHTML = `
            <i class="bi bi-phone fs-4 flex-shrink-0" style="color:#00c9ff"></i>
            <span style="flex:1;font-size:.9rem">
                <strong>Add VoucherVault to your home screen</strong>
                <span style="opacity:.75"> for quick access.</span>
            </span>
            <button id="pwa-install-btn"
                style="background:linear-gradient(45deg,#6c63ff,#00c9ff);color:#fff;border:none;border-radius:6px;padding:10px 18px;font-weight:600;white-space:nowrap;min-height:44px;cursor:pointer;">
                Install
            </button>
            <button id="pwa-install-dismiss"
                style="background:transparent;border:none;color:rgba(255,255,255,.6);font-size:1.3rem;padding:8px;line-height:1;cursor:pointer;min-width:44px;min-height:44px;"
                aria-label="Dismiss">×</button>`;

        document.body.appendChild(banner);

        document.getElementById('pwa-install-btn').addEventListener('click', async () => {
            if (!deferredPrompt) return;
            deferredPrompt.prompt();
            const { outcome } = await deferredPrompt.userChoice;
            deferredPrompt = null;
            dismiss();
            console.log('[PWA] Install prompt outcome:', outcome);
        });

        document.getElementById('pwa-install-dismiss').addEventListener('click', dismiss);
    }

    window.addEventListener('beforeinstallprompt', (e) => {
        e.preventDefault();
        deferredPrompt = e;
        if (!isDismissed()) {
            // Small delay so the page content isn't immediately obscured
            setTimeout(showBanner, 2500);
        }
    });

    window.addEventListener('appinstalled', () => {
        deferredPrompt = null;
        dismiss();
        console.log('[PWA] App installed');
    });
}());
