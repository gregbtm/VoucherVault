## [1.1.126](https://github.com/gregbtm/VoucherVault/compare/v1.1.125...v1.1.126) (2026-07-24)


### Bug Fixes

* use PocketID's real /lc/ route for onboarding links ([c704ad3](https://github.com/gregbtm/VoucherVault/commit/c704ad399008690506a0e2d159ccaf56b51c0067))

## [1.1.125](https://github.com/gregbtm/VoucherVault/compare/v1.1.124...v1.1.125) (2026-07-24)


### Bug Fixes

* replace inline onclick handlers with CSP-safe event delegation ([5d9dbc2](https://github.com/gregbtm/VoucherVault/commit/5d9dbc2d0a2f8c027397b085f96300e70170cfff))

## [1.1.124](https://github.com/gregbtm/VoucherVault/compare/v1.1.123...v1.1.124) (2026-07-24)


### Features

* passkey registration status check for PocketID-provisioned invites ([5abd71f](https://github.com/gregbtm/VoucherVault/commit/5abd71f9c8c81229869e4939766f5c64046892d9))

## [1.1.123](https://github.com/gregbtm/VoucherVault/compare/v1.1.122...v1.1.123) (2026-07-24)


### Bug Fixes

* redirect OTA link to /profile so new users land on passkey setup page ([f9860bc](https://github.com/gregbtm/VoucherVault/commit/f9860bc5b6777e821d6b8a43ea3637bf9115840c))

## [1.1.122](https://github.com/gregbtm/VoucherVault/compare/v1.1.121...v1.1.122) (2026-07-24)


### Features

* surface OTA errors so admin knows when one-click link fails ([f65d1b6](https://github.com/gregbtm/VoucherVault/commit/f65d1b6393fdfd6603293a1408e60b2a4a9236fb)), closes [#6366f1](https://github.com/gregbtm/VoucherVault/issues/6366f1) [#6366f1](https://github.com/gregbtm/VoucherVault/issues/6366f1)


### Bug Fixes

* mock probe_ota in test_check_pocket_id_ok to match updated view ([bd115dc](https://github.com/gregbtm/VoucherVault/commit/bd115dcd83ab252b76d5a11da9fefefd7a3ce34c))
* send JSON body to PocketID OTA endpoint to resolve 500 error ([6bfd244](https://github.com/gregbtm/VoucherVault/commit/6bfd244992f8998d3e60bf69e45d82fe4f1718be))
* two-step invite flow — passkey setup before VoucherVault access ([f13d96c](https://github.com/gregbtm/VoucherVault/commit/f13d96c9ab1364c3039de173d1957c9062d895f2))

