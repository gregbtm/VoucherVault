import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(__dirname, 'device-detect.js'), 'utf-8');

// device-detect.js computes window.VVDeviceDetect once, at load time, from
// whatever navigator looks like right then - so each scenario below sets up
// navigator first, then (re)loads the script, exactly as a real page load
// would compute it fresh from that device's navigator.
function loadDeviceDetect() {
  delete window.VVDeviceDetect;
  (0, eval)(source);
}

function setNavigator(overrides) {
  Object.defineProperty(window.navigator, 'userAgentData', {
    value: overrides.userAgentData,
    configurable: true,
  });
  Object.defineProperty(window.navigator, 'userAgent', {
    value: overrides.userAgent || '',
    configurable: true,
  });
  Object.defineProperty(window.navigator, 'platform', {
    value: overrides.platform || '',
    configurable: true,
  });
  Object.defineProperty(window.navigator, 'maxTouchPoints', {
    value: overrides.maxTouchPoints ?? 0,
    configurable: true,
  });
}

const originalDescriptors = {
  userAgentData: Object.getOwnPropertyDescriptor(window.navigator, 'userAgentData'),
  userAgent: Object.getOwnPropertyDescriptor(window.navigator, 'userAgent'),
  platform: Object.getOwnPropertyDescriptor(window.navigator, 'platform'),
  maxTouchPoints: Object.getOwnPropertyDescriptor(window.navigator, 'maxTouchPoints'),
};

afterEach(() => {
  Object.entries(originalDescriptors).forEach(([key, descriptor]) => {
    if (descriptor) Object.defineProperty(window.navigator, key, descriptor);
  });
});

describe('Client Hints branch (Chromium)', () => {
  it('reports Google Wallet compatible + not Apple on Android Chrome', () => {
    setNavigator({
      userAgentData: { platform: 'Android', brands: [{ brand: 'Google Chrome' }, { brand: 'Chromium' }] },
    });
    loadDeviceDetect();
    expect(window.VVDeviceDetect).toEqual({ isApple: false, isGoogleWalletCompatible: true });
  });

  it('reports Google Wallet compatible on desktop Chromium (Edge) even off Android', () => {
    setNavigator({
      userAgentData: { platform: 'Windows', brands: [{ brand: 'Microsoft Edge' }] },
    });
    loadDeviceDetect();
    expect(window.VVDeviceDetect).toEqual({ isApple: false, isGoogleWalletCompatible: true });
  });

  it('is not Google Wallet compatible when Client Hints reports a non-Chromium, non-Android brand', () => {
    setNavigator({
      userAgentData: { platform: 'Windows', brands: [{ brand: 'Not_A Brand' }] },
    });
    loadDeviceDetect();
    expect(window.VVDeviceDetect).toEqual({ isApple: false, isGoogleWalletCompatible: false });
  });
});

describe('User-Agent string fallback (no Client Hints)', () => {
  it('detects an iPhone Safari UA as Apple, not Google Wallet compatible', () => {
    setNavigator({
      userAgentData: undefined,
      userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
      platform: 'iPhone',
      maxTouchPoints: 5,
    });
    loadDeviceDetect();
    expect(window.VVDeviceDetect).toEqual({ isApple: true, isGoogleWalletCompatible: false });
  });

  it('detects iPadOS 13+ reporting as "Macintosh" via maxTouchPoints, as Apple', () => {
    setNavigator({
      userAgentData: undefined,
      userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
      platform: 'MacIntel',
      maxTouchPoints: 5,
    });
    loadDeviceDetect();
    expect(window.VVDeviceDetect).toEqual({ isApple: true, isGoogleWalletCompatible: false });
  });

  it('does not misdetect a real Mac (no touch points) as an iPad', () => {
    setNavigator({
      userAgentData: undefined,
      userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
      platform: 'MacIntel',
      maxTouchPoints: 0,
    });
    loadDeviceDetect();
    expect(window.VVDeviceDetect.isApple).toBe(true);
  });

  it('does not count CriOS (Chrome on iOS, still WebKit) as Chromium', () => {
    setNavigator({
      userAgentData: undefined,
      userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/119.0.0.0 Mobile/15E148 Safari/604.1',
      platform: 'iPhone',
      maxTouchPoints: 5,
    });
    loadDeviceDetect();
    expect(window.VVDeviceDetect.isGoogleWalletCompatible).toBe(false);
    // CriOS UAs also contain "Safari" but the isSafari check excludes CriOS,
    // so this correctly is NOT flagged as the real Safari/Apple browser either.
    expect(window.VVDeviceDetect.isApple).toBe(false);
  });

  it('detects a real Android Chrome UA as Google Wallet compatible, not Apple', () => {
    setNavigator({
      userAgentData: undefined,
      userAgent: 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36',
      platform: 'Linux armv8l',
      maxTouchPoints: 5,
    });
    loadDeviceDetect();
    expect(window.VVDeviceDetect).toEqual({ isApple: false, isGoogleWalletCompatible: true });
  });
});
