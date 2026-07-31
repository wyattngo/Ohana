import { useId } from "react";
import "./Logo.css";

/**
 * Ohana logo — lockup 1a from the brand bundle (`ohana-brand/project/Ohana Brand
 * Identity.dc.html`, turn 1): roof/shelter mark + "Ohana" in Space Grotesk 600,
 * letter-spacing -0.5px, horizontal, tight.
 *
 * SCOPE — read before reaching for these colors elsewhere. The hexes below are hardcoded on
 * purpose and are NOT `--ohana-*` tokens. `lib/tokens.ts` is the frozen Astronixa/Figma
 * system (DEC-OHANA-01 §U2) and its purple ramp (primary/500 `#9744fb`) is a *different*
 * ramp from the brand bundle's (`#7C3AED`/`#4C1D95`). Both are legitimate; they just aren't
 * the same system. Wyatt scoped this work to "logo only, don't touch tokens" (2026-07-20), so
 * the bundle's violet lives inside this component's boundary and nowhere else. If you want
 * brand violet on a button, that is a palette decision — take it to a DEC, don't widen this
 * file's reach one usage at a time.
 *
 * Gradient ids are `useId()`-suffixed: SVG defs live in a global id namespace, so two
 * <Logo/>s on one page with static ids would make the second one silently paint with the
 * first one's gradient.
 */

interface LogoProps {
  /** Surface the logo sits on. `light` (default) = the app's grey shell; `dark` = the
   *  #0A0118 brand surface (splash, marketing). Picks the bundle's per-surface gradient. */
  tone?: "light" | "dark";
  /** Mark height in px. The wordmark scales with it (bundle ratio: 56px mark : 32px type). */
  size?: number;
  /** Mark only, no wordmark — for tight chrome (avatars, nav). */
  markOnly?: boolean;
}

const TONES = {
  light: { from: "#7C3AED", to: "#4C1D95", dot: "#6D28D9", dotOpacity: 0.8, text: "#1C1335" },
  dark: { from: "#8B5CF6", to: "#5B21B6", dot: "#F5F3FF", dotOpacity: 0.9, text: "#F5F3FF" },
} as const;

export function Logo({ tone = "light", size = 32, markOnly = false }: LogoProps) {
  const uid = useId();
  const roofId = `ohana-roof-${uid}`;
  const sparkId = `ohana-spark-${uid}`;
  const c = TONES[tone];

  return (
    <span className="ohana-logo" aria-label="Ohana" role="img">
      <svg
        viewBox="0 0 80 80"
        width={size}
        height={size}
        className="ohana-logo-mark"
        aria-hidden="true"
        focusable="false"
      >
        <defs>
          <linearGradient id={roofId} x1="0.5" y1="0" x2="0.5" y2="1">
            <stop offset="0%" stopColor={c.from} />
            <stop offset="100%" stopColor={c.to} />
          </linearGradient>
          <radialGradient id={sparkId} cx="0.5" cy="0.5" r="0.5">
            <stop offset="0%" stopColor={c.from} stopOpacity="0.5" />
            <stop offset="40%" stopColor={c.from} stopOpacity="0.15" />
            <stop offset="100%" stopColor={c.from} stopOpacity="0" />
          </radialGradient>
        </defs>
        <line
          x1="16"
          y1="56"
          x2="40"
          y2="24"
          stroke={`url(#${roofId})`}
          strokeWidth="10"
          strokeLinecap="round"
        />
        <line
          x1="40"
          y1="24"
          x2="64"
          y2="56"
          stroke={`url(#${roofId})`}
          strokeWidth="10"
          strokeLinecap="round"
        />
        <circle cx="40" cy="44" r="5" fill={`url(#${sparkId})`} />
        <circle cx="40" cy="44" r="2" fill={c.dot} opacity={c.dotOpacity} />
      </svg>
      {!markOnly && (
        <span
          className="ohana-logo-word"
          style={{ fontSize: size * (32 / 56), color: c.text }}
        >
          Ohana
        </span>
      )}
    </span>
  );
}
