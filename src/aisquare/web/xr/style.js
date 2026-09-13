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
  /** Push–pull limits (plan §9, right thumbstick Y). Closer than `minRadius`
   *  and the arc wraps past the edge of comfortable view; further than
   *  `maxRadius` and the 1.5° cap height stops being met by the atlas, which
   *  is sized for 1.6 m and does not re-render at a new distance. */
  minRadius: 1.15,
  maxRadius: 1.6,
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
 *   Consequence, measured rather than assumed — this is what M2 is for:
 *   1.5° is road-sign type. JetBrains Mono advances 0.6 em, so at 37 px a glyph
 *   is 22.2 px and the 304 px text column holds
 *        304 / 22.2 = 13.7 → THIRTEEN characters per line.
 *
 *   That is a hard physical consequence of the spec, not a tuning choice: the
 *   angular floor fixes the glyph height, the 0.55 m panel fixes the column
 *   width, and nothing in between is free. It also means a ≤ 6 word summary
 *   (plan §6) does NOT fit an ambient cell — §6's own example, "claimed tsk_01k4
 *   — wiring JWT", is 29 characters and truncates to 13. The two numbers were
 *   specified independently and they do not meet.
 *
 *   The client implements the spec as written and truncates with an ellipsis;
 *   which of the two numbers should move is a planning call, not this module's.
 *   The ambient tier stays glanceable either way — reading is the focus tier's
 *   job (M4), which is sized from FOCUS.minCapDeg below.
 */
export const AMBIENT = {
  minCapDeg: 1.5,
  capEmRatio: 0.73, // JetBrains Mono cap height / em
  fontPx: 37,
  lines: 3, // title, state, summary — never more (plan §5)
};

/**
 * Focus tier (plan §8 "focus tier ≥ 2.2°", §7 the `text-optimized` quad layer).
 *
 * This is the tier the manager's decision leans on: the ambient cell truncates
 * its summary with an ellipsis and THIS surface carries the full text. So the
 * arithmetic below is not a formality — a focus tier that cannot hold the
 * untruncated summary would leave the truncation with nowhere to land.
 *
 *   1. Required cap height in metres at the pull-forward distance:
 *        h_cap = 2 · d · tan(θ / 2)
 *              = 2 × 0.9 m × tan(1.1°)
 *              = 1.8 × 0.0192010 = 0.0345618 m   (34.56 mm)
 *
 *   2. Everything else follows from the em box, because the family is
 *      monospace and its metrics are fixed:
 *        em      = h_cap / 0.73      = 0.0473449 m
 *        advance = 0.6 em            = 0.0284069 m per character
 *
 *      Note what step 2 means: at a FIXED angular cap height, the number of
 *      characters that fit across a panel depends only on the panel's ANGULAR
 *      width. Moving the panel further away and scaling it up changes nothing.
 *      Resolution changes nothing either. The only free variable is how many
 *      degrees of the operator's view the surface is allowed to occupy.
 *
 *   3. Pick the texel density, which fixes the pixel sizes:
 *        D = 1280 px/m
 *        font_px = 0.0473449 × 1280 = 60.60 → 61 px  (rounded UP, never down)
 *        check:  61 × 0.73 = 44.53 px ÷ 1280 = 0.034789 m
 *                2 · atan(0.0173945 / 0.9) = 2.214°  ≥ 2.2°  ✓
 *        advance_px = 0.6 × 61 = 36.6 px
 *        line_px    = 1.35 × 61 ≈ 82 px
 *
 *   4. Size the surface from the character target. The target is the wire
 *      protocol's own summary cap: §6 allows 6 words, and its example
 *      "claimed tsk_01k4 — wiring JWT" is 29 characters, so a 6-word summary
 *      lands around 40. FORTY COLUMNS is therefore the floor at which this
 *      tier does the job the manager assigned it.
 *        text column = 40 × 36.6 = 1464 px, plus 100 px of bar and gutters
 *        → 1564 px, rounded up to a clean 1600 px = 1.25 m at 1280 px/m,
 *          which leaves 1500 px of column = 40 columns with 36 px to spare.
 *        1024 px = 0.80 m gives (1024 − 96 − 40) / 82 = 10.8 → 10 rows.
 *
 *   5. What that costs in field of view, stated plainly because it is the
 *      real price of the 2.2° floor:
 *        width  1.25 m at 0.9 m = 2 · atan(0.625 / 0.9) = 69.6°
 *        height 0.80 m at 0.9 m = 2 · atan(0.400 / 0.9) = 47.9°
 *      About two thirds of the Quest 3's ~110° horizontal field. That is a
 *      large slab, and it is deliberate: this tier only exists while the
 *      operator is deliberately reading one session, and it is dismissed with
 *      the same button that summoned it.
 *
 *   DEVIATION, called out for review: the task contract's `createQuadLayer`
 *   example passes `width: 0.55, height: 0.40` — the AMBIENT panel's
 *   dimensions. Carried over literally, at the mandatory 2.2° floor those give
 *        (0.55 × 1280 − 100) / 36.6 = 16 columns
 *        (0.40 × 1280 − 136) / 82   =  4 rows
 *   i.e. a 16 × 4 transcript, which cannot hold one untruncated summary line,
 *   let alone a transcript, and would fail plan §11/M4's "comfortably readable
 *   without leaning in". The two numbers were specified independently — the
 *   0.55 × 0.40 quad and the 2.2° floor — and they do not meet, in exactly the
 *   way §6's 6-word summary and §8's 1.5° floor did not meet. That collision
 *   was closed in favour of readability (board seq 293); this one is resolved
 *   the same way. The floor is honoured exactly, and the surface grew.
 *
 *   To ship the literal 0.55 × 0.40 instead, set `width`/`height` below — the
 *   pixel sizes and the column/row counts recompute from them, and the tier
 *   degrades to 16 × 4 rather than breaking.
 *
 *   Known and accepted: the surface is flat, so it foreshortens away from the
 *   axis — the far corner sits 1.166 m from the eye rather than 0.9 m, where
 *   the cap height reads 1.71°. The floor is met on axis and across the middle
 *   rows, which is where a transcript is actually read; the corner is where the
 *   oldest scrolled-off line sits. A cylinder layer would hold 2.2° across the
 *   full width, and is out of scope for M4.
 */
export const FOCUS = {
  minCapDeg: 2.2,
  /**
   * Denser than the ambient tier's 0.86α, and the reason is what sits behind
   * it rather than a change of mind about §8.
   *
   * An ambient panel has only passthrough behind it, and 0.86 over a room reads
   * as a solid object. The focus surface covers ~70° of view at 0.9 m, so what
   * is behind it is the RING — and at 0.86 the neighbouring panels' titles show
   * through the transcript as ghost text, competing with the thing the operator
   * pulled forward to read. §8 asks for panels "dark and opaque enough to hold
   * their own"; against a lit room that is 0.86, against another panel it is
   * this. Still short of 1.0, so the surface stays an object in a room.
   */
  surfaceAlpha: 0.97,
  capEmRatio: 0.73, // JetBrains Mono cap height / em — same face as the ambient tier
  distance: 0.9, // m in front of the viewer, at eye height
  width: 1.25, // m
  height: 0.8, // m
  density: 1280, // px/m
  fontPx: 61,
  advancePx: 36.6, // 0.6 em — monospace, so this is exact
  lineH: 82,
  titleH: 96, // title bar strip, incl. its bottom rule
  barW: 28, // 0.021875 m — the SAME physical bar width as the ambient cell
  padL: 38,
  padR: 34,
  padY: 20, // above the first transcript line and below the last
  /** Layer resolution. Derived, so a change to width/height carries through. */
  get pixelWidth() {
    return Math.round(this.width * this.density);
  },
  get pixelHeight() {
    return Math.round(this.height * this.density);
  },
  get textX() {
    return this.barW + this.padL;
  },
  get textW() {
    return this.pixelWidth - this.textX - this.padR;
  },
  /** Characters per transcript line — what the wrapper wraps to. */
  get cols() {
    return Math.max(1, Math.floor(this.textW / this.advancePx));
  },
  /** Transcript lines visible at once — what one scroll page is measured in. */
  get rows() {
    return Math.max(1, Math.floor((this.pixelHeight - this.titleH - 2 * this.padY) / this.lineH));
  },
};

/** Canvas font shorthand for the focus tier. */
export function focusFont(weight = 400) {
  return `${weight} ${FOCUS.fontPx}px ${FONT_FAMILY}`;
}

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
/**
 * Collapse / summon (plan §7, §11/M5). A transition, not an animation: it runs
 * only while the operator is holding the button that asked for it, and it ends.
 * §8 permits exactly this — "everything else is static or responds directly to
 * an operator action (focus pull-forward, collapse, ring rotation)".
 */
export const COLLAPSE = {
  ms: 250,
  /** Scale the ring shrinks to. Not 0: a zero-scale matrix is singular and
   *  three.js will warn when it tries to normalise the normal matrix. */
  minScale: 0.001,
};

/** Pointer hover highlight — an outline, drawn in `ink`. Deliberately NOT
 *  `alert`, which §8 reserves for `needs_you` and nothing else, ever. */
export const HOVER = { color: COLOR.ink, opacity: 0.9, inset: 0.004 };

export const ALERT_PULSE = {
  periodMs: 1800, // slow
  minOpacity: 0.0, // overlay alpha floor; the atlas bar underneath is already
  maxOpacity: 1.0, // alert at 50%, so the composite pulses 50% → 100%
  baseAlpha: 0.5, // alpha the atlas draws the alert bar at
};
