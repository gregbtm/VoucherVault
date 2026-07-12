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
    try {
      await navigator.clipboard.writeText(`${shareData.text} ${shareData.url}`);
      alert('Voucher link copied to clipboard!');
    } catch (err) {
      console.error('Failed to copy to clipboard', err);
    }
  }
}

function shareClassic(btn) {
  shareVoucher({
    title: `${btn.dataset.merchant} Voucher`,
    text: `Here is my ${btn.dataset.title} for ${btn.dataset.merchant}.`,
    url: btn.dataset.url,
  });
}

async function fetchPublicShareInfo(itemId) {
  const response = await fetch(`/items/${itemId}/public-share/`, {
    method: 'POST',
    headers: {
      'X-CSRFToken': vvGetCSRFToken(),
      'X-Requested-With': 'XMLHttpRequest',
    },
  });
  if (!response.ok) {
    throw new Error(`public-share request failed: ${response.status}`);
  }
  return response.json();
}

async function shareViaLink(btn) {
  try {
    const info = await fetchPublicShareInfo(btn.dataset.itemId);
    shareVoucher({
      title: `${info.merchant} Voucher`,
      text: `Here is my ${info.name} for ${info.merchant}.`,
      url: info.url,
    });
  } catch (err) {
    console.error('Could not create a public share link, falling back to the item page link', err);
    shareClassic(btn);
  }
}

async function shareViaDetails(btn) {
  try {
    const info = await fetchPublicShareInfo(btn.dataset.itemId);
    let text = `${info.merchant} - ${info.name}\nCode: ${info.code}`;
    if (info.pin) {
      text += `\nPIN: ${info.pin}`;
    }
    if (info.balance !== null && info.balance !== undefined) {
      text += `\nRemaining balance: ${info.balance} ${info.currency}`;
    }
    shareVoucher({
      title: `${info.merchant} Voucher`,
      text,
      url: info.url,
    });
  } catch (err) {
    console.error('Could not create a public share link', err);
    alert('Could not create a share link right now. Please try again.');
  }
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
