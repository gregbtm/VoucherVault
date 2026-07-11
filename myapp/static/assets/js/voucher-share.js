/**
 * Native OS/browser sharing for vouchers (Web Share API), with a
 * clipboard-copy fallback for desktop or unsupported browsers.
 */
async function shareVoucher(voucherTitle, merchantName, voucherUrl) {
  const shareData = {
    title: `${merchantName} Voucher`,
    text: `Here is my ${voucherTitle} for ${merchantName}.`,
    url: voucherUrl,
  };

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

document.addEventListener('click', (event) => {
  const btn = event.target.closest('.share-voucher-btn');
  if (!btn) return;
  event.preventDefault();
  shareVoucher(btn.dataset.title, btn.dataset.merchant, btn.dataset.url);
});
