/**
 * Suggests marking the current item "Used" when the phone is tilted
 * forward and held there briefly - the motion of presenting a barcode to
 * a reader (train barriers, a till scanner). Opt-in
 * (UserPreference.tilt_scan_detection_enabled) and only ever suggests: it
 * never marks an item used on its own, only shows a dismissible banner
 * with an explicit "Mark Used" button.
 *
 * iOS requires an explicit user gesture to grant motion-sensor access
 * (DeviceOrientationEvent.requestPermission()) - this starts inert behind
 * a small "Enable tilt detection" button on iOS, and attaches immediately
 * everywhere else, where no such gate exists.
 */
function vvInitTiltScanDetect(config) {
  config = config || {};
  var TILT_THRESHOLD_BETA = config.tiltThresholdBeta != null ? config.tiltThresholdBeta : 25;
  var HOLD_MS = config.holdMs != null ? config.holdMs : 350;
  var COOLDOWN_MS = config.cooldownMs != null ? config.cooldownMs : 15000;

  var banner = document.getElementById('tilt-scan-banner');
  if (!banner) return;
  var enableBtn = document.getElementById('tilt-scan-enable-btn');
  var markUsedBtn = document.getElementById('tilt-scan-mark-used-btn');
  var dismissBtn = document.getElementById('tilt-scan-dismiss-btn');

  var belowSince = null;
  var lastTriggerAt = 0;
  var listening = false;
  var holdTimer = null;

  function showBanner() {
    banner.hidden = false;
  }
  function hideBanner() {
    banner.hidden = true;
  }

  function maybeTrigger() {
    holdTimer = null;
    if (belowSince === null) return;
    var now = Date.now();
    if (now - lastTriggerAt < COOLDOWN_MS) return;
    lastTriggerAt = now;
    belowSince = null;
    showBanner();
  }

  // event.beta is the phone's front-to-back tilt in degrees: ~0 when flat
  // face-up on a table, ~90 when held upright facing the user in a normal
  // reading position. Tilting the top of the phone forward and down - to
  // present its screen to a reader - moves beta back down toward (and
  // often past) 0. A single threshold crossing held for HOLD_MS avoids
  // firing on every brief wobble while just holding the phone normally.
  // The hold is confirmed with a timer (not just by waiting for the next
  // event) since device orientation events can be throttled or gapped,
  // especially in low-power/background states.
  function handleOrientation(event) {
    if (event.beta === null || event.beta === undefined) return;
    var now = Date.now();
    if (now - lastTriggerAt < COOLDOWN_MS) return;

    if (event.beta < TILT_THRESHOLD_BETA) {
      if (belowSince === null) {
        belowSince = now;
        if (holdTimer) clearTimeout(holdTimer);
        holdTimer = setTimeout(maybeTrigger, HOLD_MS);
      }
    } else {
      belowSince = null;
      if (holdTimer) {
        clearTimeout(holdTimer);
        holdTimer = null;
      }
    }
  }

  function attach() {
    if (listening) return;
    listening = true;
    window.addEventListener('deviceorientation', handleOrientation);
  }

  function needsExplicitPermission() {
    return typeof DeviceOrientationEvent !== 'undefined' &&
      typeof DeviceOrientationEvent.requestPermission === 'function';
  }

  function requestPermissionAndAttach() {
    DeviceOrientationEvent.requestPermission().then(function (state) {
      if (state === 'granted') {
        attach();
        if (enableBtn) enableBtn.hidden = true;
      }
    }).catch(function () {});
  }

  if (needsExplicitPermission()) {
    if (enableBtn) {
      enableBtn.hidden = false;
      enableBtn.addEventListener('click', requestPermissionAndAttach);
    }
  } else {
    if (enableBtn) enableBtn.hidden = true;
    attach();
  }

  if (markUsedBtn) {
    markUsedBtn.addEventListener('click', function () {
      hideBanner();
      var csrfInput = document.querySelector('[name=csrfmiddlewaretoken]');
      fetch(config.toggleUrl, {
        method: 'POST',
        headers: {
          'X-Requested-With': 'XMLHttpRequest',
          'X-CSRFToken': csrfInput ? csrfInput.value : '',
        },
      })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (data) {
          if (data && data.success) window.location.reload();
        })
        .catch(function () {});
    });
  }
  if (dismissBtn) {
    dismissBtn.addEventListener('click', hideBanner);
  }
}
