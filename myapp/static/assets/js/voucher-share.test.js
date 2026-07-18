import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { beforeEach, describe, expect, it } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(__dirname, 'voucher-share.js'), 'utf-8');

function loadVoucherShare() {
  (0, eval)(source);
}

beforeEach(() => {
  document.body.innerHTML = '';
  delete window.buildVoucherShareText;
});

describe('buildVoucherShareText', () => {
  const base = {
    merchant: 'Costa Coffee',
    name: 'Gift Card',
    code: 'ABC123',
    url: 'https://voucher.example/s/abc',
  };

  it('always includes the merchant/name header, code, and link', () => {
    loadVoucherShare();
    const text = buildVoucherShareText(base);
    expect(text).toContain('Costa Coffee - Gift Card');
    expect(text).toContain('Code: ABC123');
    expect(text).toContain('View voucher: https://voucher.example/s/abc');
  });

  it('omits the PIN line when there is no PIN', () => {
    loadVoucherShare();
    const text = buildVoucherShareText(base);
    expect(text).not.toContain('PIN:');
  });

  it('includes the PIN line when a PIN is present', () => {
    loadVoucherShare();
    const text = buildVoucherShareText({ ...base, pin: '4321' });
    expect(text).toContain('PIN: 4321');
  });

  it('omits the balance line when balance is null/undefined', () => {
    loadVoucherShare();
    expect(buildVoucherShareText({ ...base, balance: null })).not.toContain('Remaining balance');
    expect(buildVoucherShareText(base)).not.toContain('Remaining balance');
  });

  it('includes the balance line, with currency, when balance is present - even zero', () => {
    loadVoucherShare();
    const text = buildVoucherShareText({ ...base, balance: 0, currency: 'GBP' });
    // balance !== null && balance !== undefined - 0 is a real, sharable balance, not "no balance".
    expect(text).toContain('Remaining balance: 0 GBP');
  });
});
