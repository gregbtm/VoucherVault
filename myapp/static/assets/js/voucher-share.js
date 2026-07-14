/**
 * Native OS/browser sharing for vouchers (Web Share API), with a
 * clipboard-copy fallback for desktop or unsupported browsers.
 *
 * When Site Settings' "smart share" is on (window.VV_SHARE_SMART_ENABLED),
 * tapping "Share via..." opens a small chooser between the classic
 * link-only share and a richer share (merchant, code, PIN, remaining
 * balance) backed by a public, no-login-required link - see
 * myapp/models.py::ItemPublicShare. When off, one tap shares exactly as
 * before, unchanged.
 */

function vvGetCSRFToken() {
  const input = document.querySelector('[name=csrfmiddlewaretoken]');
  return input ? input.value : '';
}

async function shareVoucher(shareData) {
  if (navigator.share && (!navigator.canShare || navigator.canShare(shareData))) {
    try {
      await navigator.share(shareData);
    } catch (err) {
      if (err.name !== 'AbortError') {
        console.error('Error sharing:', err);
      }
    }
  } else {
    // No files in the clipboard fallback - writeText only ever handles the
    // caption/link, and every caller composes a self-contained text (the
    // link is embedded in it, not left to a separate `.url` field) so this
    // is never missing anything a share-sheet share would have shown.
    try {
      await navigator.clipboard.writeText(shareData.text || '');
      alert('Voucher link copied to clipboard!');
    } catch (err) {
      console.error('Failed to copy to clipboard', err);
    }
  }
}

// Shared body for every "share details" flavour (text-only or with an
// attached logo image) - one consistently-formatted message instead of
// each call site hand-rolling its own \n joins, so a future field addition
// only needs to change one place.
function buildVoucherShareText(info) {
  const lines = [`${info.merchant} - ${info.name}`, '', `Code: ${info.code}`];
  if (info.pin) {
    lines.push(`PIN: ${info.pin}`);
  }
  if (info.balance !== null && info.balance !== undefined) {
    lines.push(`Remaining balance: ${info.balance} ${info.currency}`);
  }
  lines.push('', `View voucher: ${info.url}`);
  return lines.join('\n');
}

function shareClassic(btn) {
  shareVoucher({
    title: `${btn.dataset.merchant} Voucher`,
    text: `Here is my ${btn.dataset.title} for ${btn.dataset.merchant}.\n\n${btn.dataset.url}`,
  });
}

async function fetchPublicShareInfo(btn) {
  // Root cause of the original "Could not create a share link" bug: this
  // used to hand-build the URL as `/items/${itemId}/public-share/`, but
  // myapp.urls is wrapped in i18n_patterns (see myproject/urls.py), which
  // requires a locale prefix (/en/...). A POST to the unprefixed URL 404s,
  // LocaleMiddleware 302-redirects to the prefixed one, and browsers
  // silently downgrade POST to GET when following a 301/302 - so the
  // request that actually landed was a GET, which @require_POST correctly
  // rejected with 405. Using the server-rendered, already-prefixed URL
  // (data-public-share-url, built with {% url %} in the template) sidesteps
  // the whole redirect entirely.
  const response = await fetch(btn.dataset.publicShareUrl, {
    method: 'POST',
    headers: {
      'X-CSRFToken': vvGetCSRFToken(),
      'X-Requested-With': 'XMLHttpRequest',
    },
  });

  // Log enough to diagnose a failure from the console without guessing at
  // *why* the response wasn't usable (session expiry, CSRF failure, a
  // real server error, an OIDC re-auth flow, ...) - a previous attempt to
  // auto-redirect to /accounts/login/ on any non-JSON response turned out
  // to force a full OIDC re-authentication round-trip on every single
  // share tap, including ones with a perfectly valid session, so this
  // deliberately does NOT navigate the page on failure.
  const contentType = response.headers.get('content-type') || '';
  if (!response.ok || !contentType.includes('application/json')) {
    console.error(`public-share request failed: status=${response.status} content-type=${contentType}`);
    throw new Error(`public-share request failed: ${response.status}`);
  }
  return response.json();
}

async function shareViaLink(btn) {
  try {
    const info = await fetchPublicShareInfo(btn);
    shareVoucher({
      title: `${info.merchant} Voucher`,
      text: `Here is my ${info.name} for ${info.merchant}.\n\n${info.url}`,
    });
  } catch (err) {
    console.error('Could not create a public share link, falling back to the item page link', err);
    shareClassic(btn);
  }
}

async function shareViaDetails(btn) {
  try {
    const info = await fetchPublicShareInfo(btn);
    shareVoucher({
      title: `${info.merchant} Voucher`,
      text: buildVoucherShareText(info),
    });
  } catch (err) {
    console.error('Could not create a public share link', err);
    alert('Could not create a share link right now. Please try again.');
  }
}

// Fetches the merchant's logo through our own same-origin proxy (see
// item_share_logo in myapp/views.py - going straight to the third-party
// logo host from here risks a silent CORS failure) and turns it into a
// File the Web Share API can attach alongside the text, so e.g. an Uber
// gift card shares with Uber's own logo as the image instead of the
// generic VoucherVault ticket icon a link-preview card falls back to.
async function fetchLogoAsFile(info) {
  const response = await fetch(info.logo_image_url);
  if (!response.ok) return null;
  const blob = await response.blob();
  const extension = (blob.type.split('/')[1] || 'png').split('+')[0];
  const safeMerchant = (info.merchant || 'voucher').replace(/[^a-z0-9]+/gi, '-').toLowerCase();
  return new File([blob], `${safeMerchant}-logo.${extension}`, { type: blob.type });
}

async function shareViaImageAndDetails(btn) {
  let info;
  try {
    info = await fetchPublicShareInfo(btn);
  } catch (err) {
    console.error('Could not create a public share link', err);
    alert('Could not create a share link right now. Please try again.');
    return;
  }

  const title = `${info.merchant} Voucher`;
  const text = buildVoucherShareText(info);

  let file = null;
  try {
    file = await fetchLogoAsFile(info);
  } catch (err) {
    console.warn('Could not fetch merchant logo image, sharing text only', err);
  }

  // Only attempt the files+text share if the browser says it can actually
  // handle that combination - some Web Share implementations support text
  // shares and file shares individually but not together. If it can't (or
  // the share() call itself fails for a reason other than the user just
  // cancelling), fall through to the plain text share below rather than
  // leaving the user with nothing.
  if (file && navigator.share && navigator.canShare && navigator.canShare({ title, text, files: [file] })) {
    try {
      await navigator.share({ title, text, files: [file] });
      return;
    } catch (err) {
      if (err.name === 'AbortError') return;
      console.error('Error sharing with image, falling back to text-only', err);
    }
  }
  shareVoucher({ title, text });
}

let vvShareChooserEl = null;

function closeShareChooser() {
  if (vvShareChooserEl) {
    vvShareChooserEl.remove();
    vvShareChooserEl = null;
  }
  document.removeEventListener('click', handleOutsideChooserClick, true);
}

function handleOutsideChooserClick(event) {
  if (vvShareChooserEl && !vvShareChooserEl.contains(event.target)) {
    closeShareChooser();
  }
}

function openShareChooser(btn) {
  closeShareChooser();

  const menu = document.createElement('div');
  menu.className = 'vv-share-chooser';
  menu.innerHTML = `
    <button type="button" class="vv-share-option" data-action="link">
      <i class="bi bi-link-45deg"></i> Share link
    </button>
    <button type="button" class="vv-share-option" data-action="details">
      <i class="bi bi-card-text"></i> Share details (code, PIN, balance)
    </button>
    <button type="button" class="vv-share-option" data-action="image-details">
      <i class="bi bi-image"></i> Share details with logo image
    </button>
  `;
  document.body.appendChild(menu);
  vvShareChooserEl = menu;

  const rect = btn.getBoundingClientRect();
  menu.style.top = `${rect.bottom + window.scrollY + 6}px`;
  let left = rect.left + window.scrollX;
  const maxLeft = window.scrollX + document.documentElement.clientWidth - menu.offsetWidth - 8;
  if (left > maxLeft) left = Math.max(8, maxLeft);
  menu.style.left = `${left}px`;

  menu.querySelector('[data-action="link"]').addEventListener('click', () => {
    closeShareChooser();
    shareViaLink(btn);
  });
  menu.querySelector('[data-action="details"]').addEventListener('click', () => {
    closeShareChooser();
    shareViaDetails(btn);
  });
  menu.querySelector('[data-action="image-details"]').addEventListener('click', () => {
    closeShareChooser();
    shareViaImageAndDetails(btn);
  });

  setTimeout(() => document.addEventListener('click', handleOutsideChooserClick, true), 0);
}

function ensureShareChooserStyles() {
  if (document.getElementById('vv-share-chooser-styles')) return;
  const style = document.createElement('style');
  style.id = 'vv-share-chooser-styles';
  style.textContent = `
    .vv-share-chooser {
      position: absolute;
      z-index: 3000;
      background: #fff;
      color: #1b1f27;
      border: 1px solid rgba(127,127,127,0.3);
      border-radius: 10px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.18);
      padding: 6px;
      min-width: 240px;
    }
    .vv-share-option {
      display: flex;
      align-items: center;
      gap: 8px;
      width: 100%;
      min-height: 44px;
      text-align: left;
      background: none;
      border: none;
      padding: 10px 12px;
      border-radius: 8px;
      font-size: 0.9rem;
      cursor: pointer;
      color: inherit;
    }
    .vv-share-option:hover, .vv-share-option:focus {
      background: rgba(127,127,127,0.15);
    }
    body.dark-mode .vv-share-chooser {
      background: #21262d;
      color: #e0e6eb;
      border-color: rgba(255,255,255,0.1);
      box-shadow: 0 5px 30px 0 rgba(0, 0, 0, 0.5);
    }
    body.dark-mode .vv-share-option:hover, body.dark-mode .vv-share-option:focus {
      background: #30363d;
    }
  `;
  document.head.appendChild(style);
}

document.addEventListener('click', (event) => {
  const btn = event.target.closest('.share-voucher-btn');
  if (!btn) return;
  event.preventDefault();
  event.stopPropagation();

  if (!window.VV_SHARE_SMART_ENABLED || !btn.dataset.itemId) {
    shareClassic(btn);
    return;
  }

  ensureShareChooserStyles();
  openShareChooser(btn);
});
