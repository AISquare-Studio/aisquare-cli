/**
 * The ring: layout by angle, and the mutation surface the wire protocol drives.
 *
 * Ordering is by session id, not by arrival, so a panel does not jump to a new
 * place in the arc when an unrelated session changes. Sorted ids read left →
 * right (see `ringAngles` in style.js).
 *
 * Collapse / summon and pose re-anchoring belong to M5 and are not here.
 */

import * as THREE from 'three';
import { RING, ringAngles } from './style.js';
import { Panel } from './panel.js';

/** A session the server has ended still arrives as state:"gone" (plan §6). */
const isLive = (session) => session && session.state !== 'gone';

export class Ring {
  /** @param {import('./panel.js').Atlas} atlas shared atlas for the whole ring */
  constructor(atlas) {
    this.atlas = atlas;

    this.group = new THREE.Group();
    this.group.name = 'ring';
    // The ring hangs at eye height; panels sit at local y = 0.
    this.group.position.set(0, RING.eyeHeight, 0);

    /** @type {Map<string, object>} id → Session */
    this.sessions = new Map();
    /** @type {Map<string, Panel>} id → live panel (visible page only) */
    this.panels = new Map();
    this.page = 0;
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

  /* ------------------------------------------------------------ mutation -- */

  /**
   * A snapshot is always authoritative — on reconnect the client rebuilds from
   * it rather than trying to reconcile whatever it had (plan §2.6, fail-open).
   */
  applySnapshot(sessions) {
    this.sessions = new Map((sessions ?? []).map((s) => [s.id, s]));
    if (this.page >= this.pages) this.page = 0;
    this.layout();
  }

  /** `{ changed: [Session], removed: ["<id>"] }` (plan §6). */
  applyDelta({ changed = [], removed = [] } = {}) {
    for (const id of removed) this.sessions.delete(id);
    for (const session of changed) this.sessions.set(session.id, session);
    if (this.page >= this.pages) this.page = this.pages - 1;
    this.layout();
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
   * Every path that changes what the ring shows funnels through here, which is
   * what keeps `atlasRedraws` equal to the number of frames received.
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

    const angles = ringAngles(visible.length);
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
      panel.setAngle(angles[i]);
    });

    // One texture upload per snapshot or delta — never per frame.
    this.atlas.draw(visible);
  }

  /* ------------------------------------------------------------- motion --- */

  /** Rotate the whole ring about the operator (plan §9: right thumbstick X). */
  rotate(deltaRadians) {
    this.group.rotation.y += deltaRadians;
  }

  /** Bring the arc back to centre on the forward axis. */
  recenter() {
    this.group.rotation.y = 0;
  }

  /** Per frame. Touches only alerting panels; a quiet ring animates nothing. */
  update(elapsedMs) {
    for (const panel of this.panels.values()) {
      if (panel.alerting) panel.pulse(elapsedMs);
    }
  }

  /** Panels currently in the needs_you state, in ring order — M5's jump target. */
  get alerting() {
    return this.visible.filter((s) => s.state === 'needs_you').map((s) => this.panels.get(s.id));
  }
}
