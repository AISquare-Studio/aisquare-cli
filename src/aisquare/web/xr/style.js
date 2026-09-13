/**
 * cliXR design tokens, ring geometry and angular type sizing.
 *
 * This module is the single source of truth (plan §8). No other file in the
 * client may hard-code a colour, a dimension or a font size — import it here so
 * the headset tiers added in M3–M5 inherit the same numbers.
 */

/* ---------------------------------------------------------------- colour -- */

/**
 * Panel surfaces are dark and near-opaque on purpose: passthrough washes out
 * light surfaces, so the panel has to hold its own against a lit room (plan §8).
 */
export const COLOR = {
  surface: '#12161C', // panel body — cool slate, not tinted black
  rule: '#2A323D', // panel edge, dividers
  ink: '#E8EDF2', // primary text
  inkDim: '#8B97A6', // secondary text, timestamps
  planner: '#F2B33D', // role bar
  coder: '#4FC3D9', // role bar
  runner: '#A78BDB', // role bar
  alert: '#FF5A4E', // RESERVED for state === 'needs_you'. Used nowhere else, ever.
};

/** Panel body alpha. Dark enough to read over a bright window. */
export const SURFACE_ALPHA = 0.86;

/** Neutral stand-in for passthrough on the desktop mock — never pure black, so
 *  the surface/background contrast we ship is the contrast we tested. */
export const MOCK_BACKGROUND = '#5A6068';

/** `colorKey` (plan §6) → role bar token. `role` is the fallback for a session
 *  whose colorKey the server has not set; anything unknown reads as inkDim. */
const ROLE_TOKENS = {
  planner: COLOR.planner,
  coder: COLOR.coder,
  runner: COLOR.runner,
};

/**
 * Resolve the left-edge bar colour for a session.
 * `needs_you` overrides the role entirely — that is the whole point of the token.
 */
export function barColor(session) {
  if (session.state === 'needs_you') return COLOR.alert;
  return ROLE_TOKENS[session.colorKey] ?? ROLE_TOKENS[session.role] ?? COLOR.inkDim;
}

/** Hex → {r,g,b} 0-255, for building rgba() fills on the atlas canvas. */
export function rgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
}

/** Hex + alpha → canvas rgba() string. */
export function rgba(hex, alpha) {
  const { r, g, b } = rgb(hex);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/* -------------------------------------------------------------- geometry -- */

const DEG = Math.PI / 180;

/**
 * Ring geometry (plan §7). A 200° arc in front of the operator rather than a
 * full circle: full circles force neck rotation past comfort and hide a third
 * of the board behind you.
 */
export const RING = {
  radius: 1.6, // m
  panelW: 0.55, // m
  panelH: 0.4, // m
  arcDeg: 200,
  eyeHeight: 1.6, // m — ring centre sits at seated/standing eye height
  maxPanels: 12, // live-updating panels; beyond this the ring paginates
};

/**
 * Angular width one panel occupies at the ring radius:
 *   2 · atan((panelW / 2) / radius) = 2 · atan(0.275 / 1.6) = 19.51°
 * The comfortable pitch adds a 1.5° gutter so neighbours never touch.
 */
export const PANEL_ANGLE_DEG = 2 * Math.atan(RING.panelW / 2 / RING.radius) / DEG;
export const PITCH_DEG = PANEL_ANGLE_DEG + 1.5; // ≈ 21.01°

/**
 * Angles for `n` panels, centred on the forward (+Z) axis.
 *
 * Panels keep the comfortable pitch until they would overflow the 200° arc,
 * then compress to fit it — "fill the arc; scroll the surplus" (plan §7).
 * Ten panels span 189° at the comfortable pitch; at the 12-panel cap the pitch
 * compresses to 18.2° and neighbours overlap by ~1.3°, which is the visual cost
 * of the cap and the reason pagination exists above it.
 *
 * Index 0 gets the largest positive angle. For a +Z-forward viewer with +Y up,
 * screen-right is −X, so the largest positive angle is the LEFT-most panel:
 * sessions sorted by id therefore read left → right.
 */
export function ringAngles(n) {
  if (n <= 0) return [];
  if (n === 1) return [0];
  const pitch = Math.min(PITCH_DEG, RING.arcDeg / (n - 1));
  const mid = (n - 1) / 2;
  return Array.from({ length: n }, (_, i) => (mid - i) * pitch * DEG);
}

/* ------------------------------------------------------------ type sizing -- */

/**
 * Angular type sizing — plan §8: "ambient tier cap height ≥ 1.5° at 1.6 m …
 * Compute the texture resolution backwards from that."
 *
 *   1. Required cap height in metres at the ring radius:
 *        h_cap = 2 · r · tan(θ / 2)
 *              = 2 × 1.6 m × tan(0.75°)
 *              = 3.2 × 0.0130896 = 0.04189 m   (41.9 mm)
 *
 *   2. Choose the atlas cell height, which fixes the texel density. A 0.40 m
 *      tall panel drawn into a 256 px cell gives
 *        density = 256 px / 0.40 m = 640 px/m
 *
 *   3. Cap height in texels:
 *        h_cap_px = 0.04189 m × 640 px/m = 26.81 px
 *
 *   4. JetBrains Mono has a cap height of 730/1000 em, so
 *        font_px = 26.81 / 0.73 = 36.7 → 37 px  (rounded UP: 1.51°, still ≥ 1.5°)
 *
 *   5. Cell width follows from the same density, keeping texels square:
 *        0.55 m × 640 px/m = 352 px
 *
 *   A 4 × 3 grid of those cells holds the whole 12-panel ring in ONE
 *   1408 × 768 texture — one texture, one material (plan §13: the shared atlas
 *   is the design, not an optimisation to do later).
 *
 *   Consequence worth knowing before you read a panel: 1.5° is road-sign type.
 *   At 37 px the mono advance is ~22 px, so a cell fits ~13 characters per line.
 *   The ambient tier is glanceable, not readable — reading is the focus tier's
 *   job (M4), and that is why the server sends a ≤ 6 word summary, not text.
 */
export const AMBIENT = {
  minCapDeg: 1.5,
  capEmRatio: 0.73, // JetBrains Mono cap height / em
  fontPx: 37,
  lines: 3, // title, state, summary — never more (plan §5)
};

/** Focus tier floor (plan §8). Its surface is sized in M4; the floor lives here
 *  so both tiers derive from one place. */
export const FOCUS = { minCapDeg: 2.2 };

/** The atlas: cell size, grid and the resulting canvas. */
export const ATLAS = {
  cellW: 352, // 0.55 m × 640 px/m
  cellH: 256, // fixes the 640 px/m density above
  cols: 4,
  rows: 3, // 4 × 3 = RING.maxPanels
  get width() {
    return this.cellW * this.cols;
  },
  get height() {
    return this.cellH * this.rows;
  },
  /** px per metre — the single density every cell dimension derives from. */
  get density() {
    return this.cellH / RING.panelH;
  },
};

/** Cell interior, in texels at 640 px/m. */
export const CELL = {
  barW: 14, // 0.022 m — full-height left edge bar
  padL: 18, // gutter between bar and text
  padR: 16,
  /** Baselines use textBaseline='top'; the block is centred in the cell. */
  lineH: 56,
  get textX() {
    return this.barW + this.padL;
  },
  get textW() {
    return ATLAS.cellW - this.textX - this.padR;
  },
  get textTop() {
    return (ATLAS.cellH - (AMBIENT.lines * this.lineH - (this.lineH - AMBIENT.fontPx))) / 2;
  },
};

/** One monospace family from CDN, with a system-mono fallback stack (plan §8). */
export const FONT_FAMILY =
  '"JetBrains Mono", ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace';

/** Canvas font shorthand for an ambient line. */
export function ambientFont(weight = 400) {
  return `${weight} ${AMBIENT.fontPx}px ${FONT_FAMILY}`;
}

/* ----------------------------------------------------------------- motion -- */

/**
 * Exactly one animated thing in the scene: the alert bar (plan §8). No ambient
 * drift, no breathing, no idle pulses. Everything else is static or responds
 * directly to an operator action.
 */
export const ALERT_PULSE = {
  periodMs: 1800, // slow
  minOpacity: 0.0, // overlay alpha floor; the atlas bar underneath is already
  maxOpacity: 1.0, // alert at 50%, so the composite pulses 50% → 100%
  baseAlpha: 0.5, // alpha the atlas draws the alert bar at
};
