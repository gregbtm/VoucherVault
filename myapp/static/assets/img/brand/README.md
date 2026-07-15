# VoucherVault Plus+ Logo Package (Option 1B — Barcode V)

Geometric mark: five bars of ascending/descending height reading as a "V" — abstracted barcode nodding to redeem codes, with one indigo bar swapped for a warm accent to signal "Plus+".

## Files
- `mark-color.svg` — primary mark, transparent bg, #4154f1 + #ffcf6e accent bar. Use on light backgrounds and app UI.
- `mark-mono-white.svg` — all-white version for dark/OLED-black backgrounds.
- `mark-mono-black.svg` — all-black (#111318) version for light backgrounds, watermarks, print.
- `favicon.svg` — same geometry pre-optimized for 16–32px rendering (slightly thicker relative stroke). Use directly as `favicon.svg`, or rasterize to `favicon.ico` / `favicon-32.png`, `favicon-16.png`.
- `app-icon-light-bg.svg` / `app-icon-oled-black-bg.svg` — 512×512 PWA home-screen icons, rounded-square (iOS-style) container included. Light version uses a pale indigo tint background (#eef0ff); OLED version uses true black (#000000) per the app's dark mode. Export to PNG at 512, 192, 180 (apple-touch-icon), 32, 16.
- `lockup-dark-bg.svg` — horizontal mark + wordmark for README headers / in-app nav on dark surfaces.
- `lockup-light-bg.svg` — same lockup, recolored for light surfaces.

## Color tokens
- Indigo (primary): `#4154f1`
- Indigo (wordmark accent on dark): `#7b8bff`
- Accent bar (gold): `#ffcf6e` on dark / `#e0a300` on light (darkened for contrast on white)
- Text on dark: `#f2f4fc`
- Text on light: `#161a2e`
- Mono (dark bg): `#ffffff`
- Mono (light bg): `#111318`

## Usage / cropping rules
- **Square app icon (PWA/favicon):** use the mark alone, centered, on its own filled rounded-square container (see `app-icon-*` files for the exact padding ratio — bars occupy ~55% of the frame width, symmetric margins). Never place the wordmark in a square icon.
- **Favicon at 16–32px:** use `favicon.svg` as-is; don't reuse `mark-color.svg` at that size without re-checking stroke width, the thin bars can disappear below 24px.
- **README / app header lockup:** use `lockup-dark-bg.svg` or `lockup-light-bg.svg` directly — mark + wordmark are already spaced and baseline-aligned. Don't rebuild the lockup by placing `mark-color.svg` next to text by eye.
- **Dark mode / OLED true-black:** always swap to `mark-mono-white.svg` (or `lockup-dark-bg.svg`). Full-color mark on pure black is acceptable too since indigo has enough contrast, but white mono is safer for true-black OLED per the brand ask.
- **Monochrome contexts (watermarks, print, single-color stamps):** use `mark-mono-black.svg` or `mark-mono-white.svg` — never the color version desaturated on the fly.
- Minimum clear space around the mark: half the mark's own height on all sides.
- Do not recolor the accent bar to anything but the gold token — it's the one "Plus+" signal in an otherwise two-tone (indigo + neutral) mark.

## Where the derived assets live

This directory is the source of truth. Every raster/derived file the app
actually serves lives one level up in `../` and is generated from these
SVGs (via `cairosvg`, no manual editing):

| Generated file | Source | Notes |
|---|---|---|
| `../logo.svg` | `mark-color.svg` | Main in-app/README logo reference |
| `../logo-mono-white.svg` | `mark-mono-white.svg` | Dark-mode/OLED nav logo |
| `../logo.png` | `mark-color.svg` | Raster fallback, 1024×1024 |
| `../favicon.ico` | `favicon.svg` | Multi-size ICO (16/32/48) |
| `../apple-icon-180.png`, `../apple-touch-icon.png` | `app-icon-light-bg.svg` | 180×180 |
| `../manifest-icon-192.png`, `../manifest-icon-512.png` | `app-icon-light-bg.svg` | PWA icons, `purpose: any` |
| `../manifest-icon-192.maskable.png`, `../manifest-icon-512.maskable.png` | `app-icon-light-bg.svg`, corner radius stripped | PWA icons, `purpose: maskable` - full-bleed square since the OS applies its own mask shape |

If this logo ever changes again, regenerate all of the above from the new
SVGs rather than hand-editing the PNGs/ICO directly.
