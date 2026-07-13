---
version: alpha
name: 哨响AI-design-analysis
description: "A dark, glass-morphism sports-analytics dashboard built on deep charcoal canvas (#0a0e0f) with three ambient radial gradients. The single chromatic system maps four semantic colors to football 1X2 outcomes: pitch-green (#22c55e) for home win, ember-amber (#f59e0b) for draw, frost-blue (#3b82f6) for away win, and danger-red (#ef4444) for alerts. Cards use ultra-low opacity white overlays (bg-white/[0.03]) with backdrop-blur-xl — hover states shift to pitch-green border glow. Titles use Titillium Web at font-black (900) with tightened tracking; body runs Inter; odds values use JetBrains Mono. Framer Motion spring animations drive score reveals and probability bar transitions. Sidebar collapses from 240px to 72px with a green-blue gradient logo mark."

colors:
  canvas: "#0a0e0f"
  surface-glass: "rgba(255,255,255,0.03)"
  surface-glass-hover: "rgba(255,255,255,0.06)"
  surface-sidebar: "rgba(10,14,15,0.80)"
  surface-topbar: "rgba(10,14,15,0.60)"
  surface-row-hover: "rgba(255,255,255,0.02)"
  surface-data-block: "rgba(255,255,255,0.04)"
  surface-button-rest: "rgba(255,255,255,0.06)"
  surface-button-hover: "rgba(255,255,255,0.10)"
  border-default: "rgba(255,255,255,0.05)"
  border-card: "rgba(255,255,255,0.06)"
  border-card-hover: "rgba(34,197,94,0.20)"
  border-input: "rgba(255,255,255,0.08)"
  border-input-focus: "rgba(34,197,94,0.40)"
  border-sidebar: "rgba(255,255,255,0.04)"
  ink: "rgba(255,255,255,0.90)"
  ink-primary: "rgba(255,255,255,0.70)"
  ink-secondary: "rgba(255,255,255,0.50)"
  ink-label: "rgba(255,255,255,0.40)"
  ink-muted: "rgba(255,255,255,0.30)"
  ink-disabled: "rgba(255,255,255,0.20)"
  ink-placeholder: "rgba(255,255,255,0.20)"
  ink-star-off: "rgba(255,255,255,0.10)"
  pitch-400: "#4ade80"
  pitch-500: "#22c55e"
  pitch-600: "#16a34a"
  pitch-glow: "rgba(34,197,94,0.50)"
  pitch-glow-soft: "rgba(34,197,94,0.08)"
  ember-400: "#fbbf24"
  ember-500: "#f59e0b"
  ember-600: "#d97706"
  ember-glow: "rgba(245,158,11,0.50)"
  frost-400: "#60a5fa"
  frost-500: "#3b82f6"
  frost-600: "#2563eb"
  frost-glow: "rgba(59,130,246,0.50)"
  danger-400: "#f87171"
  danger-500: "#ef4444"
  danger-600: "#dc2626"
  danger-glow: "rgba(239,68,68,0.50)"
  surface-skeleton: "rgba(255,255,255,0.05)"
  overlay-scrim: "rgba(0,0,0,0.40)"

typography:
  display-xl:
    fontFamily: "Titillium Web"
    fontSize: "60px"
    fontWeight: 900
    lineHeight: 1.0
    letterSpacing: "-0.02em"
  display-lg:
    fontFamily: "Titillium Web"
    fontSize: "40px"
    fontWeight: 900
    lineHeight: 1.1
    letterSpacing: "-0.02em"
  headline:
    fontFamily: "Titillium Web"
    fontSize: "24px"
    fontWeight: 900
    lineHeight: 1.2
    letterSpacing: "-0.01em"
  subhead:
    fontFamily: "Titillium Web"
    fontSize: "18px"
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: "0"
  stat-value:
    fontFamily: "Titillium Web"
    fontSize: "24px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.01em"
  body:
    fontFamily: Inter
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "0"
  body-sm:
    fontFamily: Inter
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "0"
  caption:
    fontFamily: Inter
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: "0"
  label:
    fontFamily: Inter
    fontSize: "12px"
    fontWeight: 500
    lineHeight: 1.2
    letterSpacing: "0.05em"
  eyebrow:
    fontFamily: Inter
    fontSize: "12px"
    fontWeight: 500
    lineHeight: 1.2
    letterSpacing: "0.05em"
  mono:
    fontFamily: "JetBrains Mono"
    fontSize: "14px"
    fontWeight: 500
    lineHeight: 1.5
    letterSpacing: "0"
  mono-sm:
    fontFamily: "JetBrains Mono"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: "0"
  button:
    fontFamily: Inter
    fontSize: "14px"
    fontWeight: 500
    lineHeight: 1.2
    letterSpacing: "0"

rounded:
  sm: 6px
  md: 8px
  lg: 12px
  xl: 16px
  "2xl": 24px
  pill: 9999px

spacing:
  xs: 4px
  sm: 8px
  md: 12px
  lg: 16px
  xl: 24px
  section: 48px

components:
  glass-card:
    backgroundColor: "{colors.surface-glass}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.xl}"
    padding: 24px
  glass-card-hover:
    backgroundColor: "{colors.surface-glass-hover}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.xl}"
    padding: 24px
  button-primary:
    backgroundColor: "{colors.pitch-600}"
    textColor: "#ffffff"
    typography: "{typography.button}"
    rounded: "{rounded.lg}"
    padding: "8px 16px"
  button-primary-hover:
    backgroundColor: "{colors.pitch-500}"
    textColor: "#ffffff"
    typography: "{typography.button}"
    rounded: "{rounded.lg}"
  button-secondary:
    backgroundColor: "{colors.surface-button-rest}"
    textColor: "rgba(255,255,255,0.80)"
    typography: "{typography.button}"
    rounded: "{rounded.lg}"
    padding: "8px 16px"
  button-secondary-hover:
    backgroundColor: "{colors.surface-button-hover}"
    textColor: "rgba(255,255,255,0.80)"
    typography: "{typography.button}"
    rounded: "{rounded.lg}"
  button-ghost:
    backgroundColor: transparent
    textColor: "rgba(255,255,255,0.60)"
    typography: "{typography.button}"
    rounded: "{rounded.lg}"
    padding: "8px 16px"
  button-ghost-hover:
    backgroundColor: "{colors.surface-row-hover}"
    textColor: "rgba(255,255,255,0.80)"
    typography: "{typography.button}"
    rounded: "{rounded.lg}"
  text-input:
    backgroundColor: "{colors.surface-data-block}"
    textColor: "rgba(255,255,255,0.80)"
    typography: "{typography.body}"
    rounded: "{rounded.lg}"
    padding: "10px 16px"
  text-input-focus:
    backgroundColor: "{colors.surface-data-block}"
    textColor: "rgba(255,255,255,0.80)"
    typography: "{typography.body}"
    rounded: "{rounded.lg}"
    borderColor: "{colors.border-input-focus}"
  badge-green:
    backgroundColor: "rgba(34,197,94,0.20)"
    textColor: "{colors.pitch-400}"
    typography: "{typography.caption}"
    rounded: "{rounded.pill}"
    padding: "2px 10px"
  badge-amber:
    backgroundColor: "rgba(245,158,11,0.20)"
    textColor: "{colors.ember-400}"
    typography: "{typography.caption}"
    rounded: "{rounded.pill}"
    padding: "2px 10px"
  badge-blue:
    backgroundColor: "rgba(59,130,246,0.20)"
    textColor: "{colors.frost-400}"
    typography: "{typography.caption}"
    rounded: "{rounded.pill}"
    padding: "2px 10px"
  badge-red:
    backgroundColor: "rgba(239,68,68,0.20)"
    textColor: "{colors.danger-400}"
    typography: "{typography.caption}"
    rounded: "{rounded.pill}"
    padding: "2px 10px"
  status-dot-healthy:
    backgroundColor: "{colors.pitch-500}"
    textColor: null
    rounded: "{rounded.pill}"
    size: 8px
  status-dot-warning:
    backgroundColor: "{colors.ember-500}"
    textColor: null
    rounded: "{rounded.pill}"
    size: 8px
  status-dot-danger:
    backgroundColor: "{colors.danger-500}"
    textColor: null
    rounded: "{rounded.pill}"
    size: 8px
  probability-bar:
    backgroundColor: "rgba(255,255,255,0.05)"
    textColor: null
    rounded: "{rounded.pill}"
    height: 8px
  probability-fill-pitch:
    backgroundColor: "linear-gradient(to right, {colors.pitch-600}, {colors.pitch-400})"
    rounded: "{rounded.pill}"
  probability-fill-ember:
    backgroundColor: "linear-gradient(to right, {colors.ember-600}, {colors.ember-400})"
    rounded: "{rounded.pill}"
  probability-fill-frost:
    backgroundColor: "linear-gradient(to right, {colors.frost-600}, {colors.frost-400})"
    rounded: "{rounded.pill}"
  sidebar:
    backgroundColor: "{colors.surface-sidebar}"
    textColor: "{colors.ink}"
    typography: "{typography.body-sm}"
    rounded: 0
    width: 240px
  sidebar-collapsed:
    backgroundColor: "{colors.surface-sidebar}"
    textColor: "{colors.ink}"
    width: 72px
  sidebar-nav-active:
    backgroundColor: "rgba(34,197,94,0.10)"
    textColor: "{colors.pitch-400}"
    typography: "{typography.body-sm}"
    rounded: "{rounded.lg}"
  sidebar-nav-inactive:
    backgroundColor: transparent
    textColor: "rgba(255,255,255,0.40)"
    typography: "{typography.body-sm}"
    rounded: "{rounded.lg}"
  topbar:
    backgroundColor: "{colors.surface-topbar}"
    textColor: "{colors.ink}"
    typography: "{typography.body-sm}"
    height: 56px
  data-table-header:
    backgroundColor: transparent
    textColor: "{colors.ink-label}"
    typography: "{typography.eyebrow}"
    rounded: 0
    padding: "12px 16px"
  data-table-cell:
    backgroundColor: transparent
    textColor: "{colors.ink-primary}"
    typography: "{typography.body-sm}"
    rounded: 0
    padding: "12px 16px"
  data-table-row-hover:
    backgroundColor: "{colors.surface-row-hover}"
    textColor: "{colors.ink-primary}"
  skeleton:
    backgroundColor: "{colors.surface-skeleton}"
    textColor: null
    rounded: "{rounded.md}"
  divider:
    backgroundColor: transparent
    rounded: 0
    height: 1px
  nav-link:
    backgroundColor: transparent
    textColor: "rgba(255,255,255,0.50)"
    typography: "{typography.body-sm}"
    rounded: "{rounded.lg}"
    padding: "10px 16px"
  nav-link-hover:
    backgroundColor: "{colors.surface-row-hover}"
    textColor: "rgba(255,255,255,0.80)"
    typography: "{typography.body-sm}"
    rounded: "{rounded.lg}"
  gradient-text:
    textColor: "linear-gradient(to right, {colors.pitch-400}, {colors.frost-400})"
    typography: "{typography.display-xl}"
---

## Overview

哨响AI (`哨响AI | Football Prediction System`) is a sports-analytics dashboard purpose-built for football match prediction. The design language reads as a **sports-tech war room** — dark glass panels, ambient pitch-stadium lighting, and a fiercely consistent 1X2 (Home / Draw / Away) semantic color system.

The canvas (`{colors.canvas}` #0a0e0f) is a deep charcoal with a subtle cyan-green undertone — not pure black. Three radial ambient glows (green left, blue top-right, amber bottom) create depth without gradients on cards. Every content surface is glass: `backdrop-blur-xl` with `bg-white/[0.03]` over a 24px black drop-shadow. Hovering a glass card fades in a pitch-green border tint — the sole chromatic accent on neutral surfaces.

The color system maps **directly to football outcomes** — `{colors.pitch-400}` for home win, `{colors.ember-400}` for draw, `{colors.frost-400}` for away win, `{colors.danger-400}` for alerts/errors. This 1X2 mapping is enforced in every badge, probability bar, prediction tag, and score display. Status dots carry a 50% opacity glow (`shadow-[0_0_8px_...]`) that pulses via CSS animation.

Typography splits across three families — **Titillium Web** at font-black (900) for scores and page titles, **Inter** for body and labels, **JetBrains Mono** for odds numbers. Score predictions use `{typography.display-xl}` at 60px with font-weight 900 — the largest text in the system, signaling it as the most critical data point.

Motion is integral. Framer Motion drives sidebar collapse (240px ↔ 72px), page transitions (fade + slide-up), probability bar animations (width 0 → target), spring-animated score reveals, and slide-in list items with staggered delays. The active sidebar indicator uses a `layoutId` magic-motion pill that glides between items.

**Key characteristics:**
- **Dark glass-morphism sports dashboard.** Every surface is `backdrop-blur-xl` over `{colors.surface-glass}`.
- **Four-color semantic system** (1X2 + alerts) used sparingly — badges, probability bars, dot indicators.
- **Titillium Web font-black for scores.** The system reserves weight 900 exclusively for prediction numbers and page titles.
- Three radial ambient glows on body for atmosphere; no gradients on individual cards.
- Cards round at `{rounded.xl}` (16px); buttons at `{rounded.lg}` (12px); status dots and badges at `{rounded.pill}`.
- **Framer Motion on every interactive element** — spring reveals, staggered list entries, elastic number transitions.
- Odds data uses JetBrains Mono; body uses Inter; titles use Titillium Web.
- Single-mode: dark theme only. `<html>` sets `class="dark"`.

## Design Philosophy

The dashboard serves a **data-first audience** (analysts, model engineers, prediction consumers). The UI's job is to surface probability, risk, and confidence — not to decorate. Glass cards fade into the background; color only arrives when it carries meaning (a home-win prediction, a draw alert, a system anomaly). The result is a UI that recedes during deep analytical work yet delivers instantaneous visual scanning of match predictions via the 1X2 color-mapping.

## Colors

### Canvas & Surfaces

- **Canvas** (`{colors.canvas}`): Deep charcoal-cyan page background. Source of the sport-tech atmosphere.
- **Glass Card** (`{colors.surface-glass}`): Default content container — `bg-white/[0.03]`, `backdrop-blur-xl`, `rounded-2xl`.
- **Glass Card Hover** (`{colors.surface-glass-hover}`): Lift to `bg-white/[0.06]` with `{colors.border-card-hover}` green tint.
- **Sidebar Surface** (`{colors.surface-sidebar}`): Semi-transparent with `backdrop-blur-2xl` — sits above canvas.
- **Topbar Surface** (`{colors.surface-topbar}`): Lighter transparency with `backdrop-blur-xl`.
- **Row Hover** (`{colors.surface-row-hover}`): Table row and nav item hover — `bg-white/[0.02]`.
- **Data Block** (`{colors.surface-data-block}`): Stat tiles, input fields, metric blocks — `bg-white/[0.04]`.
- **Button Rest** (`{colors.surface-button-rest}`): Secondary and ghost button default fill.
- **Button Hover** (`{colors.surface-button-hover}`): Button lift state.
- **Skeleton** (`{colors.surface-skeleton}`): Loading placeholder `bg-white/[0.05]` with `animate-pulse`.

### Semantic Colors (1X2 System)

| Outcome | Color | Hex | Badge | Probability Bar | Dot |
|---------|-------|-----|-------|-----------------|-----|
| **Home Win (H)** | pitch | `{colors.pitch-500}` | `green` | `pitch` gradient | `healthy` |
| **Draw (D)** | ember | `{colors.ember-500}` | `amber` | `ember` gradient | `warning` |
| **Away Win (A)** | frost | `{colors.frost-500}` | `blue` | `frost` gradient | — |
| **Alert/Error** | danger | `{colors.danger-500}` | `red` | — | `danger` |

### Text Ink Scale

- **Ink** (`{colors.ink}`): All headings — `text-white/90`.
- **Ink Primary** (`{colors.ink-primary}`): Body text, table cells — `text-white/70`.
- **Ink Secondary** (`{colors.ink-secondary}`): Supplementary text — `text-white/50`.
- **Ink Label** (`{colors.ink-label}`): Table headers, stat labels, sidebar inactive — `text-white/40`.
- **Ink Muted** (`{colors.ink-muted}`): Tooltips, hints — `text-white/30`.
- **Ink Disabled** (`{colors.ink-disabled}`): Disabled controls — `text-white/20`.
- **Ink Star Off** (`{colors.ink-star-off}`): Unlit confidence stars — `text-white/10`.

### Glow Effects

- **pitch-glow**: `shadow-[0_0_8px_rgba(34,197,94,0.5)]` — green dot pulsing.
- **ember-glow**: `shadow-[0_0_8px_rgba(245,158,11,0.5)]` — amber dot pulsing.
- **danger-glow**: `shadow-[0_0_8px_rgba(239,68,68,0.5)]` — red dot pulsing.
- **gradient-text**: `from-pitch-400 to-frost-400` clip — used on sidebar logo, hero numbers.

## Typography

### Font Families

- **Titillium Web** (display): Carries page titles, score predictions, stat values. Only weight 900 is used for impact; weight 700 for stat labels; weight 600 for subheadings. Fallback: `sans-serif`.
- **Inter** (body): All body text, labels, buttons, captions. Weights 300–700 available. Fallback: `sans-serif`.
- **JetBrains Mono** (mono): Odds values, data tables with numeric content, technical metadata. Weights 400, 500, 600. Fallback: `monospace`.

### Hierarchy

| Token | Size | Weight | Line Height | Letter Spacing | Use |
|---|---|---|---|---|---|
| `{typography.display-xl}` | 60px | 900 | 1.0 | -0.02em | Score prediction (largest element) |
| `{typography.display-lg}` | 40px | 900 | 1.1 | -0.02em | VS display in hero banner |
| `{typography.headline}` | 24px | 900 | 1.2 | -0.01em | Page titles in sidebar + pages |
| `{typography.subhead}` | 18px | 600 | 1.3 | 0 | Section headings, card titles |
| `{typography.stat-value}` | 24px | 700 | 1.2 | -0.01em | Stat cards, metric numbers |
| `{typography.body}` | 14px | 400 | 1.5 | 0 | Default body, table cells |
| `{typography.body-sm}` | 13px | 400 | 1.5 | 0 | Compact body, nav items |
| `{typography.caption}` | 12px | 400 | 1.4 | 0 | Badges, meta, auxiliary info |
| `{typography.label}` | 12px | 500 | 1.2 | 0.05em | Table headers, uppercase stat labels |
| `{typography.eyebrow}` | 12px | 500 | 1.2 | 0.05em | Section eyebrows (positive tracking) |
| `{typography.mono}` | 14px | 500 | 1.5 | 0 | Odds values, probability numbers |
| `{typography.mono-sm}` | 12px | 400 | 1.4 | 0 | Compact odds in badges |
| `{typography.button}` | 14px | 500 | 1.2 | 0 | All button labels |

### Principles

- Font-black 900 exclusively for scores and page titles — never for body or labels.
- Positive tracking (+0.05em) on `{typography.label}` and `{typography.eyebrow}` separates taxonomy from data.
- Monospace font used only for odds, probability, and numeric data — not for general labels.
- Three-family split is invisible to users; the runtime experience reads as one voice.

## Layout

### Container & Sidebar

- Sidebar: fixed left, 240px (expanded) or 72px (collapsed). z-50. Backdrop-blur-2xl.
- Topbar: fixed top, 56px height. Backdrop-blur-xl.
- Content area: marginLeft adjusted by sidebar width, animated via Framer Motion.
- Page padding: `p-6` (24px) on `<main>`.
- Max content width: fluid — no hard max-width. Cards fill available space in grid.

### Grid

- Card grids: 3-up at desktop, 2-up at tablet, 1-up at mobile.
- Page layout: `space-y-6` (24px) vertical rhythm between sections.
- Stat row: `grid grid-cols-4 gap-4` at desktop.

### Spacing Scale

| Token | Value | Use |
|---|---|---|
| `{spacing.xs}` | 4px | Icon gaps, dot spacing |
| `{spacing.sm}` | 8px | Button internal padding, small gaps |
| `{spacing.md}` | 12px | Card component gaps |
| `{spacing.lg}` | 16px | Card padding, grid gaps |
| `{spacing.xl}` | 24px | Page section gaps, card internal padding |
| `{spacing.section}` | 48px | Major section separation |

## Depth & Elevation

哨响AI carries depth through **backdrop-blur** + **opacity lift** — no box-shadows (except the subtle card shadow).

| Level | Treatment | Use |
|---|---|---|
| 0 (canvas) | `{colors.canvas}` flat background with three radial glows | Page body |
| 1 (glass) | `{colors.surface-glass}` + `backdrop-blur-xl` | Default cards, panels |
| 2 (glass hover) | `{colors.surface-glass-hover}` + pitch border tint | Hovered cards |
| 3 (sidebar) | `{colors.surface-sidebar}` + `backdrop-blur-2xl` | Fixed sidebar |
| 4 (topbar) | `{colors.surface-topbar}` + `backdrop-blur-xl` | Fixed header |
| 5 (modal overlay) | `{colors.overlay-scrim}` | Modal backdrops |

## Shapes

### Border Radius

| Token | Value | Use |
|---|---|---|
| `{rounded.sm}` | 6px | Small chips, inline tags |
| `{rounded.md}` | 8px | Skeleton loaders, compact elements |
| `{rounded.lg}` | 12px | All buttons, form inputs, nav items |
| `{rounded.xl}` | 16px | All cards, panels, containers |
| `{rounded.2xl}` | 24px | Hero banners, large panels |
| `{rounded.pill}` | 9999px | Badges, status dots, probability bars, avatars |

### Iconography

All icons are inline SVG — no icon library dependency. The sidebar logo is a custom SVG circle-check with green-to-blue linear gradient. Navigation icons use single-color strokes at `currentColor`. Score "VS" separators use Titillium Web font-black at 40-60px.

## Components

### Buttons

**button-primary** — Pitch-green CTA. The only chromatic button in the system.
- Background `{colors.pitch-600}`, text white, rounded `{rounded.lg}`, padding 8px 16px.
- Hover: `{colors.pitch-500}`.
- Active: `scale-95` press animation.
- Used for: predict actions, submit, primary page actions.

**button-secondary** — Neutral glass button.
- Background `{colors.surface-button-rest}`, text `{colors.ink-primary}`, border `{colors.border-input}`.
- Hover: `{colors.surface-button-hover}`.

**button-ghost** — Text-only minimal button.
- Transparent background, text `{colors.ink-secondary}`.
- Hover: `{colors.surface-row-hover}` background, text `{colors.ink-primary}`.

### Glass Card

The atomic content container. Every data panel, stat tile, and prediction card uses this pattern:
- Background `{colors.surface-glass}`, backdrop-blur-xl, border `{colors.border-card}`, rounded `{rounded.xl}`, padding 24px.
- Shadow: `0 4px 24px rgba(0,0,0,0.2)`.
- Hover: background lift to `{colors.surface-glass-hover}`, border shifts to `{colors.border-card-hover}`, shadow gains green tint `0 8px 32px rgba(34,197,94,0.1)`.

### Badge

Four variants mapping to the 1X2 system:
- **badge-green**: `bg-pitch-500/20 text-pitch-400 border border-pitch-500/20` — home win tags.
- **badge-amber**: `bg-ember-500/20 text-ember-400 border border-ember-500/20` — draw tags.
- **badge-blue**: `bg-frost-500/20 text-frost-400 border border-frost-500/20` — away win tags.
- **badge-red**: `bg-danger-500/20 text-danger-400 border border-danger-500/20` — alert/risk tags.

All use `{rounded.pill}`, `text-xs font-medium`, `px-2.5 py-0.5`.

### Probability Bar

Horizontal animated bar showing 1X2 probability split:
- Container: `h-2 rounded-full overflow-hidden bg-white/5`.
- Fill segments: each segment uses a gradient (`from-pitch-600 to-pitch-400` / `from-ember-600 to-ember-400` / `from-frost-600 to-frost-400`).
- Width animates from 0 to target via `transition-all duration-700 ease-out`.

### Status Dot

Three variants with glow effects:
- **status-dot-healthy**: 8px circle, `bg-pitch-500`, `shadow-[0_0_8px_rgba(34,197,94,0.5)]`.
- **status-dot-warning**: 8px circle, `bg-ember-500`, `shadow-[0_0_8px_rgba(245,158,11,0.5)]`.
- **status-dot-danger**: 8px circle, `bg-danger-500`, `shadow-[0_0_8px_rgba(239,68,68,0.5)]`.

### Sidebar Navigation

Fixed left panel with five route entries:
- **sidebar**: 240px width, `{colors.surface-sidebar}`, `backdrop-blur-2xl`, border-right `{colors.border-sidebar}`.
- **sidebar-collapsed**: 72px width — icons only, centered.
- **sidebar-nav-active**: `bg-pitch-500/10 text-pitch-400 border border-pitch-500/20`.
- **sidebar-nav-inactive**: `text-white/40 hover:text-white/70 hover:bg-white/[0.03]`.
- Active indicator: 4px × 20px vertical pill at left edge, `bg-pitch-400`, positioned via Framer Motion `layoutId="activeIndicator"` for smooth glide between items.
- Logo: 28×28 SVG circle-check with `linearGradient #22c55e → #3b82f6`.

### Topbar

56px fixed header bar:
- Background `{colors.surface-topbar}`, `backdrop-blur-xl`, border-bottom `{colors.border-sidebar}`.
- Left: status dot + label ("系统正常") + divider + API rate counter.
- Right: bell icon with red unread count badge + user avatar circle with green-blue gradient border.

### Data Table

- Header: `{typography.eyebrow}`, text `{colors.ink-label}`, `px-4 py-3 border-b border-white/[0.04]`.
- Cell: `{typography.body-sm}`, text `{colors.ink-primary}`, `px-4 py-3 border-t border-white/[0.04]`.
- Row hover: `{colors.surface-row-hover}`.
- Odds values in cells use JetBrains Mono.

### Text Input

- Default: `{colors.surface-data-block}`, border `{colors.border-input}`, rounded `{rounded.lg}`, padding 10px 16px.
- Focus: border `{colors.border-input-focus}`, ring `focus:ring-1 focus:ring-pitch-500/20`.
- Placeholder: `{colors.ink-placeholder}`.

### Star Rating

5-star confidence display:
- Active stars: `text-ember-400` with `fill="currentColor"`.
- Inactive stars: `{colors.ink-star-off}`.
- Sizes: sm (12px, `w-3 h-3`), md (16px, `w-4 h-4`).
- Spacing: `gap-0.5`.

### Skeleton

Loading placeholder:
- `{colors.surface-skeleton}` background, `{rounded.md}`, `animate-pulse`.

### Divider

Section separator:
- `h-px bg-gradient-to-r from-transparent via-white/[0.06] to-transparent`.

## Do's and Don'ts

### Do

- Map every prediction outcome to the 1X2 color system — green for H, amber for D, blue for A.
- Use `backdrop-blur-xl` on every card surface — glass is the core material.
- Reserve Titillium Web font-black (900) for scores, page titles, and stat values only.
- Use JetBrains Mono exclusively for odds, probability numbers, and data tables.
- Apply Framer Motion spring animations to score reveals and probability bar transitions.
- Use `layoutId` for smooth animated transitions between UI states (sidebar active indicator, tab switchers).
- Round cards to `{rounded.xl}` (16px) and buttons to `{rounded.lg}` (12px) — never mix these.
- Keep the canvas pure dark — use the three radial ambient glows, not gradient backgrounds on cards.
- Right-align numeric data in table cells; left-align labels and text.

### Don't

- Don't ship a light theme — the system is dark-mode only.
- Don't use pitch-green as a section background or decorative fill — it's reserved for home-win badges and the primary CTA.
- Don't add a second chromatic accent (purple, pink, teal) to the marketing or dashboard surface.
- Don't use box-shadows for elevation lift — depth comes from opacity and blur.
- Don't pill-round buttons — buttons stay at `{rounded.lg}` 12px.
- Don't use `#000000` pure black — always use `{colors.canvas}` #0a0e0f.
- Don't mix multiple colors on a single card — one semantic color per element.
- Don't animate static content — reserve motion for data transitions and state changes.

## Responsive Behavior

### Breakpoints

| Name | Width | Key Changes |
|---|---|---|
| Desktop | 1280px | Full sidebar (240px), 3-up card grids |
| Tablet | 1024px | Sidebar collapses to 72px by default, 2-up grids |
| Mobile-Lg | 768px | Sidebar auto-collapse, 1-up cards, table → card list |
| Mobile | 480px | Single column, score text scales down, touch targets enlarge |

### Touch Targets

- All buttons: min 40px tap height.
- Table rows: min 44px tap target on touch views.
- Sidebar nav items: 44px height for tappability.

### Collapsing Strategy

- **Sidebar**: auto-collapses to 72px below 1024px. Toggle button available at all sizes.
- **Card grids**: 3 → 2 → 1 column.
- **Tables**: convert to stacked card layout below 768px.
- **Score display**: `{typography.display-xl}` 60px scales toward `{typography.display-lg}` 40px on mobile.

## Motion & Animation

### Framer Motion Conventions

- **Page entry**: `initial={{ opacity: 0, y: 10 }} → animate={{ opacity: 1, y: 0 }}`, duration 300ms.
- **Staggered list items**: increment `transition.delay` by 0.1s per item.
- **Probability bars**: `initial={{ width: 0 }} → animate={{ width: target }}`, duration 700ms, ease "easeOut".
- **Score numbers**: spring animation `type: "spring", stiffness: 200, damping: 20`.
- **Sidebar toggle**: content marginLeft animated via `layout`, duration 300ms, ease `[0.25, 0.1, 0.25, 1]`.
- **Active indicator**: `layoutId` handles position interpolation automatically.
- **List presence**: use `AnimatePresence` for enter/exit animations on alerts and notifications.

### CSS Animations

- **fadeIn**: opacity 0 → 1.
- **slideUp**: opacity 0 + translateY(10px) → opacity 1 + translateY(0).
- **Combined**: `.animate-in` runs both simultaneously over 500ms.
- **Staggered**: `.animate-in-delay-1/2/3/4` adds 100ms increments.

## Agent Prompt Guide

### Quick Color Reference

```
Canvas:     #0a0e0f (deep charcoal-cyan)
Glass Card: bg-white/[0.03] backdrop-blur-xl
Pitch Green: #22c55e (home win / primary CTA)
Ember Amber: #f59e0b (draw / warning)
Frost Blue:  #3b82f6 (away win / info)
Danger Red:  #ef4444 (alert / error)
```

### Ready-to-Use Prompts

**"Build a match prediction card in 哨响AI style."**
→ Glass card with rounded-2xl, 24px padding. Top: team names in Inter 14px. Center: score prediction in Titillium Web 60px font-black. Bottom: probability bar with pitch/ember/frost gradient segments. Home-win badge in pitch-green pill.

**"Add a data table with odds values."**
→ Header row: Inter 12px uppercase tracking-wider text-white/40. Cells: Inter 14px text-white/70. Odds column: JetBrains Mono 14px text-right. Row hover: bg-white/[0.02]. Border between rows: border-t border-white/[0.04].

**"Create a sidebar nav item."**
→ w-full px-4 py-2.5 rounded-xl. Inactive: text-white/40. Active: bg-pitch-500/10 text-pitch-400 border border-pitch-500/20. Active indicator: 4px × 20px green pill on left edge.

## Iteration Guide

1. Before adding a new component, check if it maps to one of the four semantic colors (H/D/A/alert).
2. Every visible surface outside the canvas body must use `backdrop-blur-xl` and an ultra-low white opacity fill.
3. Default body typography to `{typography.body}` at weight 400.
4. Odds values always use JetBrains Mono — never Inter or Titillium Web.
5. Add new variants as separate component entries in the front matter.
6. Treat pitch-green as scarce: prediction badges, primary CTA, status dots, focus rings.
7. Motion is data-driven: animate on value change, not on page load.
8. Run `npx @google/design.md lint DESIGN.md` after edits.

## Known Gaps

- Light mode is not supported and not planned.
- All icons are inline SVGs — no icon library is used, and no icon sprite is maintained.
- The sidebar logo gradient is custom SVG — it does not use a CSS gradient.
- Form validation error states exist but are not consistently applied across all input instances.
- The system originally shipped with a v6.0 label; the sidebar and HTML title still reference this. The prediction engine has been upgraded to v7.0, but frontend labels may still show v6.0.
- The frontend uses Zustand for state and React Query for data fetching — no Redux or Context API patterns.
- **CS-EV 不可用（数据缺口，E5 P0-9）**：全库无跨庄波胆(CS)赔率源 → 波胆期望值(EV)不可计算。价值层 `correct_score_value` 在缺 `score_odds` 时诚实降级为概率扫描(decision="SCAN", edge_available=False)，绝不伪称 edge。详见文末《CS-EV 数据缺口与外接源计划》。

---

## CS-EV 数据缺口与外接源计划（E5 P0-9 · 2026-07-11）

### 现状（已核实）
- `football_data.db` 的 `odds` 表仅有 home/draw/away 三列，无 CS 列。
- `betting_markets` 仅 564 行，且为截图 OCR 派生，非跨书对齐数据 → 不构成可计算 EV 的 CS 赔率。
- `odds_db/schema.json` 定义了 `odds_cs` 字段，但仅 16/25 单场含该字段且未入 SQL。

### 铁律约束（footballAI v7 共识）
- 1X2 有效市场无超越赔率 edge；唯一真实 edge = 跨庄/跨市场不平衡(soft line 价差)。
- 子市场(波胆/OU/AH)的真实 edge 只来自跨庄最优价(best_odds)与跨庄共识隐含概率之差。
- **无跨庄 CS 赔率 feed → 波胆 EV 在数学上不可算**，任何"波胆 value"在当前数据下都是伪信号。

### 外接源计划（待用户激活）
1. **Betfair Exchange**（已具备 `betfair_client` 接入能力）→ 扩展
   `odds_cs_bookmaker(match_id, book, score, odds, volume, ts)` 表，加唯一约束
   `UNIQUE(match_id, book, score)`。
2. **The Odds API** 跨庄 CS 端点（用户已配 `THE_ODDS_API_KEY`，500 次/月免费额度）→ 补全 `data_collector` 采集器。
3. 接入后：`correct_score_value` 的 `score_odds` 由该 feed 填充，波胆 EV 方可计算；
   未接入前保持诚实降级(SCAN)。

### 上游依赖
- 同 RLM/Steam 真信号：需外接 bet-split 源（`bet_split_source.py` 已兼容 `THE_ODDS_API_KEY`/`THEODDS_API_KEY` 双拼写，待用户填 key 到 `.env` 重启即活）。
