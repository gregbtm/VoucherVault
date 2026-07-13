/**
 * Shared device/browser detection for wallet-related UI: which of Apple
 * Wallet / Google Wallet the visiting device can actually use. iPadOS 13+
 * reports its UA as "Macintosh" but is touch-capable, hence the
 * maxTouchPoints check to still count it as iOS rather than a Mac.
 */
window.VVDeviceDetect = (function () {
  var ua = navigator.userAgent || '';
  var isIOSDevice = /iPhone|iPad|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  var isMac = /Macintosh/.test(ua) && navigator.maxTouchPoints <= 1;
  var isSafari = /Safari/.test(ua) && !/Chrome|CriOS|FxiOS|EdgiOS|OPiOS/.test(ua);
  var isApple = (isIOSDevice || isMac) && isSafari;

  var isAndroid = /Android/.test(ua);
  // CriOS/FxiOS etc. on iOS are just Safari's WebKit wearing a different
  // badge, so only count a "Chrome" UA as real Chromium off iOS.
  var isChromiumBrowser = !isIOSDevice && /Chrome|Chromium|Edg\//.test(ua);
  var isGoogleWalletCompatible = isAndroid || isChromiumBrowser;

  return {
    isApple: isApple,
    isGoogleWalletCompatible: isGoogleWalletCompatible,
  };
})();
