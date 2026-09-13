/**
 * Ambient-tier panels and the shared canvas atlas behind them.
 *
 * The whole ring is ONE canvas texture and ONE material (plan §13: "One texture
 * per panel will tank the frame rate at ten panels. The shared atlas is not an
 * optimization to do later; it is the design."). Each panel is a quad whose UVs
 * point at its cell in that atlas.
 *
 * The atlas is redrawn only when a snapshot or delta arrives — never per frame.
 * That is why the alert pulse is a separate thin overlay quad with its own
 * opacity rather than a repaint: an animated repaint would upload a 1408 × 768
 * texture every frame and would make `__xr.atlasRedraws` climb while idle.
 *
 * The focus tier (plan §8, readable transcripts, `text-optimized` quad layer)
 * is M4's job and deliberately absent here.
 */

import * as THREE from 'three';
import {
  ALERT_PULSE,
  AMBIENT,
  ATLAS,
  CELL,
  COLOR,
  RING,
  SURFACE_ALPHA,
  ambientFont,
  barColor,
  rgba,
} from './style.js';

/** Metres per texel, and the bar width expressed in metres. */
const BAR_W_M = CELL.barW / ATLAS.density;

/** Trim `text` to fit `maxW` texels, with a tail ellipsis. */
function fitText(ctx, text, maxW) {
  const s = String(text ?? '');
  if (ctx.measureText(s).width <= maxW) return s;
  let lo = 0;
  let hi = s.length;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (ctx.measureText(`${s.slice(0, mid)}…`).width <= maxW) lo = mid;
    else hi = mid - 1;
  }
  return `${s.slice(0, lo)}…`;
}

/**
 * The state chip's text (plan §11/M7: "state chips, unread counts").
 *
 * The wire spells the three states in snake_case because Python wrote them
 * (`SessionState` in protocol.py); the panel spells them the way an operator
 * reads them. `needs_you` → "needs you" is the only one that differs, and it is
 * the one that matters most — an underscore in the middle of the alert state is
 * the sort of detail that makes a demo look unfinished.
 *
 * Anything unrecognised is passed through rather than blanked: a server that
 * grows a fourth state should show it, not show nothing.
 */
const STATE_LABEL = { working: 'working', waiting: 'waiting', needs_you: 'needs you', gone: 'gone' };

function stateLabel(session) {
  const state = String(session.state ?? '');
  return STATE_LABEL[state] ?? state.replace(/_/g, ' ');
}

/**
 * The unread badge: board events for this session since this connection last
 * subscribed to it (`Session.unread`, §6).
 *
 * Item 7 on the §12 cut list, kept because it is genuinely cheap — it is a
 * measureText and a fillText against a cell that is being drawn anyway — and
 * because it answers the one question the three ambient lines cannot: which
 * panel has been busy while you were looking somewhere else.
 *
 * Capped rather than truncated to a number that would not fit: a cell 352 px
 * wide has no room for "1284", and "99+" is both shorter and more useful than
 * a count nobody is going to read exactly.
 */
function unreadBadge(session) {
  const unread = Number(session.unread) || 0;
  if (unread <= 0) return '';
  return unread > 99 ? '99+' : String(unread);
}

/**
 * The shared atlas: one canvas, one texture, one material for the entire ring.
 */
export class Atlas {
  constructor() {
    this.canvas = document.createElement('canvas');
    this.canvas.width = ATLAS.width;
    this.canvas.height = ATLAS.height;
    this.ctx = this.canvas.getContext('2d');

    this.texture = new THREE.CanvasTexture(this.canvas);
    this.texture.colorSpace = THREE.SRGBColorSpace;
    // Sampled at roughly 1:1 from the ring radius, so mipmaps would only cost
    // an upload per redraw and blur the type.
    this.texture.generateMipmaps = false;
    this.texture.minFilter = THREE.LinearFilter;
    this.texture.magFilter = THREE.LinearFilter;

    this.material = new THREE.MeshBasicMaterial({
      map: this.texture,
      transparent: true, // the panel body is 0.86α over passthrough
      side: THREE.FrontSide,
    });

    /** Incremented once per draw() — one snapshot or delta, one redraw.
     *  Exposed through `window.__xr.atlasRedraws` for the ui-tester. */
    this.redraws = 0;
  }

  /** Oblique panels at the ends of the arc are read at a shallow angle. */
  setAnisotropy(max) {
    this.texture.anisotropy = Math.min(8, max || 1);
  }

  /** Top-left texel of cell `index` in the 4 × 3 grid. */
  cellOrigin(index) {
    const col = index % ATLAS.cols;
    const row = Math.floor(index / ATLAS.cols);
    return { x: col * ATLAS.cellW, y: row * ATLAS.cellH, col, row };
  }

  /** UVs for cell `index`, in PlaneGeometry attribute order (TL, TR, BL, BR). */
  uvFor(index) {
    const { col, row } = this.cellOrigin(index);
    const u0 = (col * ATLAS.cellW) / ATLAS.width;
    const u1 = ((col + 1) * ATLAS.cellW) / ATLAS.width;
    // Canvas y grows downward, texture v grows upward.
    const vTop = 1 - (row * ATLAS.cellH) / ATLAS.height;
    const vBot = 1 - ((row + 1) * ATLAS.cellH) / ATLAS.height;
    return new Float32Array([u0, vTop, u1, vTop, u0, vBot, u1, vBot]);
  }

  /**
   * Redraw every visible cell and upload the texture once.
   * `sessions` is the ordered, already-paginated list the ring is showing.
   */
  draw(sessions) {
    const { ctx } = this;
    ctx.clearRect(0, 0, ATLAS.width, ATLAS.height);
    sessions.slice(0, RING.maxPanels).forEach((session, i) => this.drawCell(i, session));
    this.texture.needsUpdate = true;
    this.redraws += 1;
  }

  drawCell(index, session) {
    const { ctx } = this;
    const { x, y } = this.cellOrigin(index);
    const bar = barColor(session);
    const alerting = session.state === 'needs_you';

    ctx.save();
    ctx.translate(x, y);

    // Body. Never tinted by state — state lives on the bar (plan §8).
    ctx.fillStyle = rgba(COLOR.surface, SURFACE_ALPHA);
    ctx.fillRect(0, 0, ATLAS.cellW, ATLAS.cellH);

    // Edge rule.
    ctx.strokeStyle = COLOR.rule;
    ctx.lineWidth = 2;
    ctx.strokeRect(1, 1, ATLAS.cellW - 2, ATLAS.cellH - 2);

    // Full-height left edge bar. For needs_you it is drawn at half alpha and
    // the pulsing overlay quad supplies the other half.
    ctx.globalAlpha = alerting ? ALERT_PULSE.baseAlpha : 1;
    ctx.fillStyle = bar;
    ctx.fillRect(0, 0, CELL.barW, ATLAS.cellH);
    ctx.globalAlpha = 1;

    // The unread badge, top-right, in inkDim (§11/M7). Measured first because
    // the title shares the line with it and has to be trimmed to what is left —
    // a title long enough to run under the badge is the common case, not the
    // edge case, and overlapping glyphs read as corruption at ring distance.
    ctx.textBaseline = 'top';
    const badge = unreadBadge(session);
    let badgeW = 0;
    if (badge) {
      ctx.font = ambientFont(400);
      badgeW = ctx.measureText(badge).width;
      ctx.fillStyle = COLOR.inkDim;
      ctx.fillText(badge, ATLAS.cellW - CELL.padR - badgeW, CELL.textTop);
      badgeW += CELL.padR; // the gutter the title must also keep clear
    }

    // Three lines, never more: title, state, summary (plan §5).
    const lines = [
      { text: session.title, font: ambientFont(600), fill: COLOR.ink, reserve: badgeW },
      { text: stateLabel(session), font: ambientFont(400), fill: alerting ? COLOR.alert : COLOR.ink },
      { text: session.summary, font: ambientFont(400), fill: COLOR.inkDim },
    ];
    lines.forEach((line, i) => {
      ctx.font = line.font;
      ctx.fillStyle = line.fill;
      const width = CELL.textW - (line.reserve ?? 0);
      ctx.fillText(fitText(ctx, line.text, width), CELL.textX, CELL.textTop + i * CELL.lineH);
    });

    ctx.restore();
  }

  dispose() {
    this.texture.dispose();
    this.material.dispose();
  }
}

/**
 * One ambient panel: a quad into the shared atlas, plus the alert overlay.
 */
export class Panel {
  /**
   * @param {Atlas} atlas shared atlas — supplies the one material
   * @param {object} session wire-protocol Session (plan §6)
   * @param {number} index cell index, also the ring slot
   */
  constructor(atlas, session, index) {
    this.atlas = atlas;
    this.session = session;

    this.geometry = new THREE.PlaneGeometry(RING.panelW, RING.panelH);
    this.geometry.setAttribute('uv', new THREE.BufferAttribute(atlas.uvFor(index), 2));

    // The shared material: every panel in the ring points at the same one.
    this.mesh = new THREE.Mesh(this.geometry, atlas.material);
    this.mesh.name = `panel:${session.id}`;
    this.mesh.userData.sessionId = session.id;

    // Alert overlay — the only animated object in the scene. Its own material
    // because its opacity is per-panel; 2 mm proud of the body to avoid
    // z-fighting, and it renders after the body because transparent objects
    // are drawn far → near.
    this.alertMaterial = new THREE.MeshBasicMaterial({
      color: new THREE.Color(COLOR.alert),
      transparent: true,
      opacity: 0,
      depthWrite: false,
    });
    this.alertMesh = new THREE.Mesh(new THREE.PlaneGeometry(BAR_W_M, RING.panelH), this.alertMaterial);
    this.alertMesh.position.set(-RING.panelW / 2 + BAR_W_M / 2, 0, 0.002);
    this.alertMesh.visible = false;
    this.mesh.add(this.alertMesh);

    this.setSession(session);
  }

  get alerting() {
    return this.session.state === 'needs_you';
  }

  /** Point this panel at a different atlas cell (used when the ring relayouts). */
  setCell(index) {
    const uv = this.geometry.getAttribute('uv');
    uv.copyArray(this.atlas.uvFor(index));
    uv.needsUpdate = true;
  }

  /** Adopt new session data. Cell contents are redrawn by Atlas.draw(). */
  setSession(session) {
    this.session = session;
    this.alertMesh.visible = this.alerting;
    if (!this.alerting) this.alertMaterial.opacity = 0;
  }

  /**
   * Place on the ring: angle measured from +Z, panel faces the centre.
   *
   * `radius` is a parameter rather than `RING.radius` because push–pull (§9,
   * right thumbstick Y) moves the whole arc. The angle is kept because M5's
   * jump-to-next-alert has to ask each panel where it sits.
   */
  setAngle(angle, radius = RING.radius) {
    this.angle = angle;
    this.radius = radius;
    this.mesh.position.set(radius * Math.sin(angle), 0, radius * Math.cos(angle));
    // Facing the centre means the plane's +Z normal points inward: a + π.
    this.mesh.rotation.y = angle + Math.PI;
  }

  /** World position of the panel centre — where its alert chime is emitted
   *  from (§8: the cue is positioned at the panel, not at the operator). */
  worldPosition(out) {
    return this.mesh.getWorldPosition(out);
  }

  /** Per-frame, and only for alerting panels: a slow pulse, nothing else. */
  pulse(elapsedMs) {
    const phase = (elapsedMs % ALERT_PULSE.periodMs) / ALERT_PULSE.periodMs;
    const wave = 0.5 - 0.5 * Math.cos(phase * Math.PI * 2);
    this.alertMaterial.opacity =
      ALERT_PULSE.minOpacity + wave * (ALERT_PULSE.maxOpacity - ALERT_PULSE.minOpacity);
  }

  dispose() {
    this.mesh.removeFromParent();
    this.geometry.dispose();
    this.alertMesh.geometry.dispose();
    this.alertMaterial.dispose();
    // The atlas material is shared and outlives the panel — never disposed here.
  }
}
