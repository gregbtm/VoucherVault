(function () {
  var AUTO_DISMISS_MS = 5000;
  function autoDismiss(el) {
    if (!el || el.dataset.autoDismissArmed) return;
    el.dataset.autoDismissArmed = '1';
    setTimeout(function () {
      if (!el.isConnected) return;
      var bsAlert = window.bootstrap && window.bootstrap.Alert ? window.bootstrap.Alert.getOrCreateInstance(el) : null;
      if (bsAlert) bsAlert.close(); else el.remove();
    }, AUTO_DISMISS_MS);
  }
  // Toasts already rendered by Django's messages framework on page load.
  document.querySelectorAll('#toast-stack .toast-item').forEach(autoDismiss);

  // Shared helper for JS-driven flows (autosave, update check, ...) to
  // show a toast without a page navigation - same markup/styling as a
  // server-rendered Django message.
  window.showToast = function (text, tag) {
    tag = tag || 'info';
    var icon = { success: '✅', error: '❌', danger: '❌', warning: '⚠️', info: 'ℹ️' }[tag] || 'ℹ️';
    var el = document.createElement('div');
    el.className = 'alert alert-' + tag + ' alert-dismissible shadow-sm rounded-3 p-3 mb-0 toast-item';
    el.setAttribute('role', 'alert');
    el.innerHTML = '<strong class="me-2">' + icon + '</strong>' + text +
      '<button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>';
    document.getElementById('toast-stack').appendChild(el);
    autoDismiss(el);
    return el;
  };

  // Shared helper for any button that fires a fetch() and needs to show
  // it's working - swaps in a small spinner ahead of the button's own
  // label/icon and disables it, then restores the original content
  // exactly as it was. Idempotent: calling with loading=true twice in a
  // row (or before the first call ever resolves) won't lose the real
  // original content behind the spinner.
  window.setButtonLoading = function (btn, loading) {
    if (!btn) return;
    if (loading) {
      if (btn.dataset.loadingOriginalHtml === undefined) {
        btn.dataset.loadingOriginalHtml = btn.innerHTML;
      }
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span>' + btn.dataset.loadingOriginalHtml;
    } else {
      btn.disabled = false;
      if (btn.dataset.loadingOriginalHtml !== undefined) {
        btn.innerHTML = btn.dataset.loadingOriginalHtml;
        delete btn.dataset.loadingOriginalHtml;
      }
    }
  };
})();
