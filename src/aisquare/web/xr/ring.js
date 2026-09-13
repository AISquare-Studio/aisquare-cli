/**
 * The ring: layout by angle, the mutation surface the wire protocol drives, and
 * (M5) where it sits in the room.
 *
 * Ordering is by session id, not by arrival, so a panel does not jump to a new
 * place in the arc when an unrelated session changes. Sorted ids read left →
 * right (see `ringAngles` in style.js).
 *
 * Placement is kept as an ANCHOR plus a SPIN rather than a single rotation:
 *
 *   anchor  where the operator was standing and which way they faced when the
 *           ring was last summoned or recentred
 *   spin    how far they have since rotated the arc with the thumbstick
 *
 * Splitting them is what makes §7's re-anchoring rule implementable — "on
 * collapse and re-summon, re-anchor to the current viewer pose rather than the
 * original floor anchor", because floor anchors drift when the guardian shifts.
 * The anchor is replaced wholesale on summon and recenter; the spin is the
 * operator's own adjustment and is reset with it.
 */

import * as THREE from 'three';
import { COLLAPSE, RING, ringAngles } from './style.js';
import { Panel } from './panel.js';
import { makeHoverOutline } from './focus.js';

/** A session the server has ended still arrives as state:"gone" (plan §6). */
const isLive = (session) => session && session.state !== 'gone';

/** Wrap to [0, 2π) — the form "which of these is next, going right" needs. */
const TWO_PI = Math.PI * 2;
const norm = (a) => ((a % TWO_PI) + TWO_PI) % TWO_PI;

/** Symmetric ease, so collapse and summon are the same gesture reversed. */
const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2);

export class Ring {
  /** @param {import('./panel.js').Atlas} atlas shared atlas for the whole ring */
  constructor(atlas) {
    this.atlas = atlas;

    this.group = new THREE.Group();
    this.group.name = 'ring';

    /** @type {Map<string, object>} id → Session */
    this.sessions = new Map();
    /** @type {Map<string, Panel>} id → live panel (visible page only) */
    this.panels = new Map();
    this.page = 0;

    /* ----------------------------------------------------------- placement -- */

    /** Replaced wholesale on summon and recenter — never incrementally nudged. */
    this.anchor = { x: 0, y: RING.eyeHeight, z: 0, yaw: 0 };
    /** The operator's own rotation on top of the anchor. */
    this.spin = 0;
    this.radius = RING.radius;
    this.applyTransform();

    /* ------------------------------------------------------------ collapse -- */

    this.collapsed = false;
    /** `{from, to, startedAt}` while a collapse or summon is in flight. */
    this.transition = null;

    /* --------------------------------------------------------------- hover -- */

    // One outline, re-parented to whichever panel is targeted. Twelve outlines
    // would be twelve materials, and the shared atlas exists precisely so the
    // ring holds one.
    this.hoverOutline = makeHoverOutline(RING.panelW, RING.panelH);
    this.hoverId = null;
    /** Session currently pulled out to the focus tier; its ambient panel hides. */
    this.focusedId = null;

    /* -------------------------------------------------------------- alerts -- */

    /** ids currently in `needs_you`, so a TRANSITION into it can be detected. */
    this.alertingIds = new Set();
    /** Set after the first apply: the state a client connects to is not a
     *  transition, and chiming once per already-blocked session the moment the
     *  headset goes on is noise, not information. */
    this.primed = false;
    /** @type {null | (id: string) => void} */
    this.onAlert = null;
  }

  /* ------------------------------------------------------------- queries -- */

  /** Live sessions, sorted by id for stable placement. */
  get ordered() {
    return [...this.sessions.values()].filter(isLive).sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  }

  get pages() {
    return Math.max(1, Math.ceil(this.ordered.length / RING.maxPanels));
  }

  /** The at-most-12 sessions this page shows (plan §7 caps live panels at 12). */
  get visible() {
    const start = this.page * RING.maxPanels;
    return this.ordered.slice(start, start + RING.maxPanels);
  }

  /** Hit-testable meshes for the pointer. A collapsed ring targets nothing, and
   *  neither does the panel currently pulled out to the focus tier. */
  get panelMeshes() {
    if (this.collapsed) return [];
    return [...this.panels.values()].map((panel) => panel.mesh).filter((mesh) => mesh.visible);
  }

  panelFor(sessionId) {
    return this.panels.get(sessionId) ?? null;
  }

  /* ------------------------------------------------------------ mutation -- */

  /**
   * A snapshot is always authoritative — on reconnect the client rebuilds from
   * it rather than trying to reconcile whatever it had (plan §2.6, fail-open).
   */
  applySnapshot(sessions) {
    this.sessions = new Map((sessions ?? []).map((s) => [s.id, s]));
    if (this.page >= this.pages) this.page = 0;
    this.layout();
    this.syncAlerts();
  }

  /** `{ changed: [Session], removed: ["<id>"] }` (plan §6). */
  applyDelta({ changed = [], removed = [] } = {}) {
    for (const id of removed) this.sessions.delete(id);
    for (const session of changed) this.sessions.set(session.id, session);
    if (this.page >= this.pages) this.page = this.pages - 1;
    this.layout();
    this.syncAlerts();
  }

  /**
   * Fire `onAlert` for sessions that have just ENTERED `needs_you`.
   *
   * Tracked across every live session rather than the visible page, because a
   * session blocking is a fact about the board, not about which page you are
   * looking at — and because a page turn would otherwise re-fire every alert on
   * the page you turned to.
   */
  syncAlerts() {
    const now = new Set(this.ordered.filter((s) => s.state === 'needs_you').map((s) => s.id));
    if (this.primed) {
      for (const id of now) if (!this.alertingIds.has(id)) this.onAlert?.(id);
    }
    this.alertingIds = now;
    this.primed = true;
  }

  setPage(page) {
    const next = Math.max(0, Math.min(page, this.pages - 1));
    if (next === this.page) return;
    this.page = next;
    this.layout();
  }

  nextPage() {
    this.setPage((this.page + 1) % this.pages);
  }

  clear() {
    for (const panel of this.panels.values()) panel.dispose();
    this.panels.clear();
    this.sessions.clear();
    this.layout();
  }

  /* -------------------------------------------------------------- layout -- */

  /**
   * Reconcile meshes with the visible set and redraw the atlas exactly once.
   * Every path that changes what the ring SHOWS funnels through here, which is
   * what keeps `atlasRedraws` equal to the number of frames received.
   *
   * Paths that only change where the ring IS — rotate, push–pull, collapse,
   * re-anchor — must never call this. They go through `place()`, which moves
   * meshes and touches no texture.
   */
  layout() {
    const visible = this.visible;
    const keep = new Set(visible.map((s) => s.id));

    for (const [id, panel] of this.panels) {
      if (!keep.has(id)) {
        panel.dispose();
        this.panels.delete(id);
      }
    }

    visible.forEach((session, i) => {
      let panel = this.panels.get(session.id);
      if (!panel) {
        panel = new Panel(this.atlas, session, i);
        this.panels.set(session.id, panel);
        this.group.add(panel.mesh);
      } else {
        panel.setSession(session);
        panel.setCell(i);
      }
    });

    this.place();
    // A panel re-created by this relayout comes back visible and without the
    // outline; both flags are re-applied here rather than waiting for the next
    // pointer move or focus toggle.
    this.setFocused(this.focusedId);
    this.setHover(this.hoverId);

    // One texture upload per snapshot or delta — never per frame.
    this.atlas.draw(visible);
  }

  /** Position the panels on the arc. Deliberately does NOT touch the atlas. */
  place() {
    const visible = this.visible;
    const angles = ringAngles(visible.length);
    visible.forEach((session, i) => this.panels.get(session.id)?.setAngle(angles[i], this.radius));
  }

  /* ------------------------------------------------------------ placement -- */

  applyTransform() {
    this.group.position.set(this.anchor.x, this.anchor.y, this.anchor.z);
    this.group.rotation.y = this.anchor.yaw + this.spin;
  }

  /**
   * Re-frame the ring around where the viewer is NOW: their position, their
   * eye height, and their heading. This is §7's re-anchoring, and §13's answer
   * to guardian drift — nothing here consults where the ring used to be.
   *
   * @param {THREE.Camera} camera in XR this must be `renderer.xr.getCamera()`,
   *   whose world matrix carries the current XRViewerPose.
   */
  reanchor(camera) {
    const position = camera.getWorldPosition(new THREE.Vector3());
    const forward = camera.getWorldDirection(new THREE.Vector3());
    this.anchor.x = position.x;
    this.anchor.y = position.y; // the operator's eye height, not a fixed 1.6 m
    this.anchor.z = position.z;
    // Horizontal heading only. Taking the full look vector would tip the whole
    // arc toward the floor whenever the summon happened mid-glance.
    this.anchor.yaw = Math.atan2(forward.x, forward.z);
    this.spin = 0;
    this.applyTransform();
  }

  /** Rotate the whole ring about the operator (plan §9: right thumbstick X). */
  rotate(deltaRadians) {
    this.spin += deltaRadians;
    this.applyTransform();
  }

  /**
   * Push–pull (plan §9: right thumbstick Y). Clamped in style.js — past the
   * far limit the atlas, which is rasterised for 1.6 m, stops meeting the 1.5°
   * cap-height floor, and no amount of pushing re-renders it.
   */
  setRadius(radius) {
    const next = Math.min(RING.maxRadius, Math.max(RING.minRadius, radius));
    if (next === this.radius) return this.radius;
    this.radius = next;
    this.place(); // moves meshes only — no atlas redraw
    return this.radius;
  }

  nudgeRadius(delta) {
    return this.setRadius(this.radius + delta);
  }

  /**
   * Re-frame without collapsing (plan §9: Y). Same re-anchoring as summon, so
   * "recenter" and "collapse then summon" put the ring in the same place.
   */
  recenter(camera) {
    if (camera) this.reanchor(camera);
    else {
      this.spin = 0;
      this.applyTransform();
    }
  }

  /* ------------------------------------------------------- collapse/summon -- */

  get scale() {
    return this.group.scale.x;
  }

  /** Collapse to a point at the ring's centre, over COLLAPSE.ms. */
  collapse() {
    if (this.collapsed) return;
    this.collapsed = true;
    this.transition = { from: this.scale, to: COLLAPSE.minScale, startedAt: performance.now() };
  }

  /**
   * Re-form the ring around the CURRENT viewer pose — §7's whole point. The
   * re-anchor happens before the tween so the arc grows outward from where the
   * operator is standing now, not from wherever they left it.
   */
  summon(camera) {
    if (camera) this.reanchor(camera);
    this.collapsed = false;
    this.group.visible = true;
    this.transition = { from: this.scale, to: 1, startedAt: performance.now() };
  }

  toggleCollapse(camera) {
    if (this.collapsed) this.summon(camera);
    else this.collapse();
    return this.collapsed;
  }

  /* --------------------------------------------------------------- hover -- */

  /** Move the shared outline onto a panel, by session id. */
  setHover(sessionId) {
    this.hoverId = sessionId ?? null;
    const panel = sessionId ? this.panels.get(sessionId) : null;
    // `!panel.mesh.visible` is the focused session: its ambient slot is empty,
    // and an outline around nothing reads as a rendering bug.
    if (!panel || this.collapsed || !panel.mesh.visible) {
      this.hoverOutline.visible = false;
      this.hoverOutline.removeFromParent();
      return;
    }
    if (this.hoverOutline.parent !== panel.mesh) panel.mesh.add(this.hoverOutline);
    this.hoverOutline.position.set(0, 0, 0.003);
    this.hoverOutline.visible = true;
  }

  /* --------------------------------------------------------------- focus -- */

  /**
   * Hide the ambient panel whose session is currently pulled forward, so the
   * session is in exactly one place at a time and un-focusing visibly returns
   * it to its ring slot. The cell it owns in the atlas is untouched — this is a
   * `visible` flag, not a relayout, so it costs no texture upload.
   */
  setFocused(sessionId) {
    this.focusedId = sessionId ?? null;
    for (const [id, panel] of this.panels) panel.mesh.visible = id !== this.focusedId;
    if (this.focusedId && this.hoverId === this.focusedId) this.setHover(null);
  }

  /* ------------------------------------------------------------ next alert -- */

  /**
   * Rotate the ring so the next `needs_you` panel sits directly in front of the
   * viewer (plan §9: B).
   *
   * "Next" walks RIGHTWARD from the current heading, which is the direction the
   * arc reads in — `ringAngles` gives index 0 the largest angle, and a larger
   * angle is further left. Repeated presses therefore sweep the alerts in the
   * same order the eye would.
   *
   * @returns {Panel|null} the panel now in front, or null if nothing is alerting
   */
  nextAlert(camera) {
    const alerting = [...this.panels.values()].filter((panel) => panel.alerting);
    if (!alerting.length) return null;

    const forward = camera.getWorldDirection(new THREE.Vector3());
    const heading = Math.atan2(forward.x, forward.z);
    const base = this.anchor.yaw + this.spin;

    const EPS = 1e-3;
    let best = null;
    let bestDistance = Infinity;
    for (const panel of alerting) {
      // How far right of the heading this panel sits, as a positive sweep.
      const distance = norm(heading - (base + panel.angle));
      if (distance < EPS || distance > TWO_PI - EPS) continue; // already in front
      if (distance < bestDistance) {
        bestDistance = distance;
        best = panel;
      }
    }
    // Only one alert, and it is already centred: re-centre it rather than doing
    // nothing, so B always ends with an alert in front of you.
    if (!best) best = alerting[0];

    this.spin = heading - this.anchor.yaw - best.angle;
    this.applyTransform();
    return best;
  }

  /* ------------------------------------------------------------- motion --- */

  /**
   * Per frame. Touches only alerting panels and an in-flight collapse; a quiet,
   * settled ring does nothing at all here (§8: the alert bar is the only thing
   * permitted to animate on its own).
   */
  update(elapsedMs) {
    if (this.transition) {
      const { from, to, startedAt } = this.transition;
      const t = Math.min(1, (elapsedMs - startedAt) / COLLAPSE.ms);
      const scale = from + (to - from) * ease(t);
      this.group.scale.setScalar(scale);
      if (t >= 1) {
        this.transition = null;
        // A collapsed ring is hidden outright: a point-sized group still costs
        // a draw call per panel and can still swallow a pointer ray.
        if (this.collapsed) {
          this.group.visible = false;
          this.setHover(null);
        }
      }
    }

    for (const panel of this.panels.values()) {
      if (panel.alerting) panel.pulse(elapsedMs);
    }
  }

  /** Panels currently in the needs_you state, in ring order. */
  get alerting() {
    return this.visible.filter((s) => s.state === 'needs_you').map((s) => this.panels.get(s.id));
  }
}
