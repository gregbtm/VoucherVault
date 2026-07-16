## [1.1.52](https://github.com/gregbtm/VoucherVault/compare/v1.1.51...v1.1.52) (2026-07-16)


### Bug Fixes

* **ci:** give the Changelog job full history so it can actually run ([c701be3](https://github.com/gregbtm/VoucherVault/commit/c701be391b9439d36026fb38a07a74e0c0139d29))

## Manually backfilled — 2026-07-16, v1.1.41 through v1.1.51

The Changelog job was broken for this entire window (root cause and
fix are in the v1.1.52 entry above) and generated nothing for any of
these releases. Backfilled by hand from the merged PRs since the job's
own tag-based baseline has since moved past this range and can't
regenerate it automatically. Automatic generation resumes from
v1.1.53 onward.

### Features

* "Next Up" widget - highlight the soonest item from a chosen wallet ([702c349](https://github.com/gregbtm/VoucherVault/commit/702c3493bcb79e18c572f435a5ebdc902fa086f1))
* expand Next Up widget - multi-wallet queue, mark-used, day-of reminder ([cadcb81](https://github.com/gregbtm/VoucherVault/commit/cadcb81908b90c329eabb05e7de641cd69ba4909))
* configurable analytics/duplicate-detection limits, consolidate logo.dev token ([840c09b](https://github.com/gregbtm/VoucherVault/commit/840c09bccefbccaa2dd0180c448571bc099b7788))
* live updates for already-issued Google Wallet passes ([4ce0139](https://github.com/gregbtm/VoucherVault/commit/4ce013915a1c1dad89ac37ef9c10c55f7eb7fff5))
* Firefly III balance sync recipe, zero-code via n8n ([3ccc13e](https://github.com/gregbtm/VoucherVault/commit/3ccc13e32e958e31b5a4a574fd43a9782e33adcc))
* daily digest mode for notification rules ([0427433](https://github.com/gregbtm/VoucherVault/commit/0427433aa61df4affa94a6b3e75ae8e301d4f86f))
* login brute-force lockout via django-axes ([0a1b848](https://github.com/gregbtm/VoucherVault/commit/0a1b8485facec172bddf392615874b0f74154c6d))

### Bug Fixes

* update preference-form and periodic-task tests for Next Up changes ([67bd4fa](https://github.com/gregbtm/VoucherVault/commit/67bd4fa3e095d963a0f20da8c40b2857f1050fa5))
* Dashboard "Expiring Soon" list now follows the configurable threshold ([69bc1e9](https://github.com/gregbtm/VoucherVault/commit/69bc1e98f67d0fff0e0c0ca7e90f5a96aca5196f))
* SiteConfigurationForm test payloads for new required fields ([bd31071](https://github.com/gregbtm/VoucherVault/commit/bd31071b150a6e526264f6064b64e24883595119))
* wallet filter reset getting stuck at zero results ([68ec7a7](https://github.com/gregbtm/VoucherVault/commit/68ec7a7d4fd84fb986e63245f1da72d225a8bf29))

### Code Refactoring

* simplify pass on the four features above - N+1 query consolidation, hoisted flag checks ([757d727](https://github.com/gregbtm/VoucherVault/commit/757d727132ed343aa39c66d851c14bd55404f238))

### Documentation

* Phase 72 changelog entry + feature count ([8de4ae9](https://github.com/gregbtm/VoucherVault/commit/8de4ae9b2929490efe076240f4a6f8a3c40349ae))
* docs sweep - fill README/wiki gaps left by the features above ([c892dee](https://github.com/gregbtm/VoucherVault/commit/c892dee535d79fa974eb909e7d5bfff06abd4b22))

## [1.1.51](https://github.com/gregbtm/VoucherVault/compare/v1.1.50...v1.1.51) (2026-07-16)

## [1.1.50](https://github.com/gregbtm/VoucherVault/compare/v1.1.49...v1.1.50) (2026-07-16)

## [1.1.49](https://github.com/gregbtm/VoucherVault/compare/v1.1.48...v1.1.49) (2026-07-16)

## [1.1.48](https://github.com/gregbtm/VoucherVault/compare/v1.1.47...v1.1.48) (2026-07-16)

