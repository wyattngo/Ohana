/**
 * Astronixa "OHANA" design tokens — frozen per DEC-OHANA-01 §U2.
 *
 * Source of truth: Figma file `JRoD28RIxiEfSEgVqDZLNJ` ("OHANA (Copy)"), page `system`
 * (node 0:1). This file is the SINGLE point of change (DEC-OHANA-01 override rule) — when
 * the Astronixa design team updates a token, re-pull it here via the Figma MCP
 * (`get_design_context` / `get_variable_defs`) and every screen picks it up through the
 * CSS custom properties below without a rewrite.
 *
 * Component reuse policy (DEC-OHANA-01 §U2): only PRIMITIVES from the Astronixa "OHANA"
 * system are reused here (colors, typography, button/toast/pill shapes). Feature
 * components (chat/contact/newsfeed/profile/meeting/...) belong to the Ohana Social
 * super-app and are NOT ported — Ohana AI Seller is a different product on the same
 * design language.
 *
 * GD0.5 scope note: this file LOCKS the token *shapes* (colors/typography/radii/gradient/
 * toast) so Phase P1 screens don't need to touch it — P1 fills in per-screen usage
 * (bottom-nav adaptation, icon overlap assessment) documented in DEC-OHANA-01 but not
 * frozen as code yet.
 */

export type ColorShade = 50 | 100 | 200 | 300 | 400 | 500 | 600 | 700 | 800 | 900;

export interface ColorFamily {
  50: string;
  100: string;
  200: string;
  300: string;
  400: string;
  500: string;
  600: string;
  700: string;
  800: string;
  900: string;
}

/** 6 color families × 10 shades (50–900), base shade = 500. */
export const colors = {
  primary: {
    50: "#f9f0ff",
    100: "#f6e8ff",
    200: "#e3bfff",
    300: "#cd97ff",
    400: "#b56eff",
    500: "#9744fb",
    600: "#752fd5",
    700: "#561eaf",
    800: "#3b1088",
    900: "#240762",
  },
  secondary: {
    50: "#f0f6ff",
    100: "#d6e6ff",
    200: "#adcaff",
    300: "#85abff",
    400: "#5c8aff",
    500: "#3366ff",
    600: "#2148d9",
    700: "#122fb3",
    800: "#071b8c",
    900: "#000c66",
  },
  tertiary: {
    50: "#fdf0ff",
    100: "#fdf0ff",
    200: "#f5ccff",
    300: "#eba3ff",
    400: "#dd7aff",
    500: "#ca50fb",
    600: "#a339d5",
    700: "#7e26af",
    800: "#5c1788",
    900: "#3e0c62",
  },
  accent: {
    50: "#ccfff9",
    100: "#a3fff8",
    200: "#7afffa",
    300: "#52fffe",
    400: "#29faff",
    500: "#00f0ff",
    600: "#00c5d9",
    700: "#009cb3",
    800: "#00768c",
    900: "#005266",
  },
  neutral: {
    50: "#beb5c1",
    100: "#b1a9b4",
    200: "#a49da7",
    300: "#98919b",
    400: "#8b858e",
    500: "#7c7481",
    600: "#554d5b",
    700: "#302a35",
    800: "#0d0b0e",
    900: "#000000",
  },
  greyscale: {
    50: "#f2f2f2",
    100: "#d2d2d2",
    200: "#b4b4b4",
    300: "#969696",
    400: "#797979",
    500: "#5e5e5e",
    600: "#444444",
    700: "#2c2c2c",
    800: "#151515",
    900: "#030303",
  },
} as const satisfies Record<string, ColorFamily>;

/** Deep purple, outside the standard 10-shade scale (Figma toast component) — treat as a
 * one-off "primary/850" custom value, not part of the `colors.primary` ladder. */
export const toastBackground = "#43038f";

/** Signature 3-stop CTA gradient (secondary/500 → primary/500 → tertiary/500), used on the
 * primary "Duyệt" (approve) action per DEC-OHANA-01 §U2 component-reuse table. */
export const ctaGradient =
  "linear-gradient(to left, #2e96fe 0.17%, #9744fb 55.75%, #ca50fb 100%)";

export const typography = {
  fontFamily: '"Inter", system-ui, -apple-system, sans-serif',
  weightRegular: 400,
  weightSemibold: 600,
  sizeLabel: "12px",
  sizeButton: "16px",
} as const;

export const radii = {
  /** Buttons, tabs — Figma component 4008:3157. */
  pill: "100px",
  /** Bottom-nav container — Figma component 4034:1270. */
  navContainer: "50px",
  /** Toast top corners only (drops from top) — Figma component 4018:7518. */
  toastTop: "20px",
  /** NOT SET in the Astronixa system — provisional default for seller-UI cards/rows,
   * subject to Wyatt tweak per DEC-OHANA-01 §U2. */
  card: "16px",
} as const;

export const spacing = {
  toastHeight: "80px",
  toastPadding: "30px",
  buttonPadding: "10px",
} as const;

/** Flattened `--ohana-*` CSS custom-property names, generated from the token objects above —
 * this is what screens should style against (`var(--ohana-color-primary-500)`), not the TS
 * objects directly, so a Figma re-pull only ever touches this one file. */
function buildCSSVariableText(): string {
  const lines: string[] = [];

  for (const [family, shades] of Object.entries(colors)) {
    for (const [shade, hex] of Object.entries(shades)) {
      lines.push(`  --ohana-color-${family}-${shade}: ${hex};`);
    }
  }

  lines.push(`  --ohana-toast-bg: ${toastBackground};`);
  lines.push(`  --ohana-cta-gradient: ${ctaGradient};`);
  lines.push(`  --ohana-font-family: ${typography.fontFamily};`);
  lines.push(`  --ohana-weight-regular: ${typography.weightRegular};`);
  lines.push(`  --ohana-weight-semibold: ${typography.weightSemibold};`);
  lines.push(`  --ohana-size-label: ${typography.sizeLabel};`);
  lines.push(`  --ohana-size-button: ${typography.sizeButton};`);
  lines.push(`  --ohana-radius-pill: ${radii.pill};`);
  lines.push(`  --ohana-radius-nav: ${radii.navContainer};`);
  lines.push(`  --ohana-radius-toast-top: ${radii.toastTop};`);
  lines.push(`  --ohana-radius-card: ${radii.card};`);
  lines.push(`  --ohana-toast-height: ${spacing.toastHeight};`);
  lines.push(`  --ohana-toast-padding: ${spacing.toastPadding};`);
  lines.push(`  --ohana-button-padding: ${spacing.buttonPadding};`);

  return lines.join("\n");
}

const OHANA_TOKENS_STYLE_ID = "ohana-tokens";

/** Injects the token CSS custom properties into `:root` via a single `<style>` tag. Call
 * once at app start (see `src/main.tsx`). Idempotent — safe to call more than once (e.g.
 * Vite HMR re-running module init) since it re-targets the same element by id. */
export function injectOhanaTokens(): void {
  if (typeof document === "undefined") return;

  let styleEl = document.getElementById(OHANA_TOKENS_STYLE_ID) as HTMLStyleElement | null;
  if (!styleEl) {
    styleEl = document.createElement("style");
    styleEl.id = OHANA_TOKENS_STYLE_ID;
    document.head.appendChild(styleEl);
  }
  styleEl.textContent = `:root {\n${buildCSSVariableText()}\n}`;
}
