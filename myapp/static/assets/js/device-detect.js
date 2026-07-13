/**
 * Shared device/browser detection for wallet-related UI: which of Apple
 * Wallet / Google Wallet the visiting device can actually use.
 *
 * Prefers the User-Agent Client Hints API (navigator.userAgentData) when
 * available - it's a structured, non-freeform signal Chromium-based
 * browsers (Chrome, Edge, Samsung Internet, Opera, Brave, ...) expose on
 * Android and desktop, so there's no regex-guessing involved: it directly
 * reports the platform and the actual browser brand(s). Safari has never
 * implemented Client Hints, so its absence is itself a reliable signal
 * that we're looking at an Apple browser and should fall back to the
 * User-Agent string instead.
 *
 * The UA-string fallback below is only reached for Safari (iOS/iPadOS/
 * macOS) or any other non-Client-Hints browser - iPadOS 13+ reports its
 * UA as "Macintosh" but is touch-capable, hence the maxTouchPoints check
 * to still count it as iOS rather than a Mac.
 */
window.VVDeviceDetect = (function () {
  var uaData = navigator.userAgentData;

  if (uaData && Array.isArray(uaData.brands)) {
    var isChromiumBrand = uaData.brands.some(function (b) {
      return /Chromium|Google Chrome|Microsoft Edge/i.test(b.brand || '');
    });
    return {
      // Client Hints is a Chromium-only API - if it exists, we are
      // definitionally not looking at Safari/an Apple browser.
      isApple: false,
      isGoogleWalletCompatible: uaData.platform === 'Android' || isChromiumBrand,
    };
  }

  var ua = navigator.userAgent || '';
  var isIOSDevice = /iPhone|iPad|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  var isMac = /Macintosh/.test(ua) && navigator.maxTouchPoints <= 1;
  var isSafari = /Safari/.test(ua) && !/Chrome|CriOS|FxiOS|EdgiOS|OPiOS/.test(ua);
  var isApple = (isIOSDevice || isMac) && isSafari;

  var isAndroid = /Android/.test(ua);
  // CriOS/FxiOS etc. on iOS are just Safari's WebKit wearing a different
  // badge, so only count a "Chrome" UA as real Chromium off iOS.
  var isChromiumBrowser = !isIOSDevice && /Chrome|Chromium|Edg\//.test(ua);

  return {
    isApple: isApple,
    isGoogleWalletCompatible: isAndroid || isChromiumBrowser,
  };
})();
