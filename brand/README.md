# OpenStacks brand assets

Logo files for OpenStacks. Drop this folder into your repo (e.g. `assets/brand/` or `docs/brand/`).

## Layout

| Path | What it is |
|---|---|
| `svg/openstacks-logo.svg` | Horizontal lockup (icon + wordmark), for light backgrounds. **Primary logo.** |
| `svg/openstacks-logo-on-dark.svg` | Same, for dark backgrounds |
| `svg/openstacks-logo-stacked*.svg` | Icon above wordmark (light / on-dark) |
| `svg/openstacks-logo-mono-{black,white}.svg` | Single-color lockups (print, stamps, one-color use) |
| `svg/openstacks-icon.svg`, `-on-dark.svg`, `-mono-*.svg` | Icon only |
| `svg/openstacks-icon-small.svg` | Simplified icon (no faded back layer, heavier strokes) for 16-64 px |
| `svg/openstacks-app-icon.svg` | Rounded dark tile (store listings, docs site) |
| `svg/openstacks-app-icon-maskable.svg` | Full-bleed tile with safe padding (PWA / Android adaptive) |
| `png/` | Raster exports: icon 16-1024, app icon 512/1024, lockups at 800 and 2000 px wide, `apple-touch-icon-180.png`, `social-preview-1280x640.png` |
| `ico/favicon.ico` | 16/32/48 px favicon |

The wordmark is converted to outlines, so the SVGs need no font installed.

## Colors

| Role | Light bg | Dark bg |
|---|---|---|
| Phone (front) | `#3730A3` | `#6366F1` |
| Back layers | `#0D9488` | `#2DD4BF` |
| "Open" | `#1E1B4B` | `#FFFFFF` |
| "Stacks" | `#4F46E5` | `#A5B4FC` |
| App icon tile | | `#1E1B4B` |

## Type

Sora Bold (700), by Sora Foundry, licensed under the SIL Open Font License (`font-license/Sora-OFL.txt`). Use Sora for headings in docs/site to match.

## Usage

- Use the **small icon** below ~64 px and the favicon files at 16-48 px.
- Leave clear space around the logo of at least the width of the chevron.
- Don't recolor, stretch, or re-space the wordmark; use the mono files for one-color needs.

### Repo README (light/dark aware)

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/brand/svg/openstacks-logo-on-dark.svg">
  <img alt="OpenStacks" src="assets/brand/svg/openstacks-logo.svg" width="360">
</picture>
```

### Web

```html
<link rel="icon" href="/favicon.ico" sizes="48x48">
<link rel="icon" href="/openstacks-icon-small.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-touch-icon-180.png">
```

GitHub social preview: upload `png/social-preview-1280x640.png` under Settings > Social preview.

## Note

Wordmark outlines use Sora's default glyph spacing with -0.04em tracking and no pair kerning, so spacing can differ very slightly from text typed in the Sora font.
