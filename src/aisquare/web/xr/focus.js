/**
 * The focus tier: one session's transcript, pulled forward and made readable.
 *
 * This is the tier plan §11/M4 calls make-or-break, and the tier the manager's
 * ruling on the ambient summary depends on — the ring truncates with an
 * ellipsis, and the full text lives HERE. style.js carries the arithmetic that
 * sizes it (40 columns × 10 rows at a 2.2° cap height); this module draws into
 * it and gets it in front of the operator's eyes.
 *
 * Two presentation paths, same canvas and same pose:
 *
 *   1. An `XRQuadLayer` with `quality = "text-optimized"`. §7: this is what
 *      makes text readable, because the compositor samples the layer directly
 *      instead of resampling it through the projection layer's eye buffers.
 *   2. An in-scene textured plane, identical in size and resolution.
 *
 * Path 1 needs three separate things to be true — the session granted `layers`,
 * three.js is driving the session through a projection layer rather than an
 * `XRWebGLLayer` base layer, and `setRenderTargetTextures` exists to wrap the
 * compositor's texture. §13 is explicit that `layers` is optional and a hard
 * dependency breaks on any device without it, so every step is guarded and any
 * failure drops to path 2 permanently and says so once in the console.
 */

import * as THREE from 'three';
import {
  COLOR,
  FOCUS,
  HOVER,
  barColor,
  focusFont,
  rgba,
} from './style.js';

/** Wrap monospace text to a fixed column count, breaking over-long tokens. */
export function wrap(text, cols) {
  const out = [];
  for (const raw of String(text ?? '').split('\n')) {
    const line = raw.replace(/\t/g, '  ').trimEnd();
    if (!line.length) {
      out.push('');
      continue;
    }
    let current = '';
    for (const word of line.split(' ')) {
      let token = word;
      // A path or an id can be longer than the whole column; hard-break it
      // rather than letting it overflow the panel edge.
      while (token.length > cols) {
        if (current) {
          out.push(current);
          current = '';
        }
        out.push(token.slice(0, cols));
        token = token.slice(cols);
      }
      if (!current) current = token;
      else if (current.length + 1 + token.length <= cols) current += ` ${token}`;
      else {
        out.push(current);
        current = token;
      }
    }
    if (current) out.push(current);
  }
  return out;
}

/** Frames to keep looking for three.js's projection layer before concluding
 *  the session is on a base layer. ~1.4s at 72Hz, ~1.1s at 90Hz. */
const LAYER_ATTEMPT_BUDGET = 100;

export class FocusPanel {
  /**
   * @param {object} opts
   * @param {THREE.WebGLRenderer} opts.renderer
   * @param {THREE.Scene} opts.scene
   */
  constructor({ renderer, scene }) {
    this.renderer = renderer;
    this.scene = scene;

    this.session = null;
    /** Wrapped transcript lines, oldest first. Newest renders at the bottom. */
    this.lines = [];
    /** Lines scrolled back from the newest. 0 pins the view to the tail. */
    this.scrollOffset = 0;
    /** Highest `seq` applied, so a replayed transcript frame is not appended
     *  twice after a reconnect (§6 numbers them for exactly this reason). */
    this.seq = -1;

    /* ------------------------------------------------------------ canvas -- */

    this.canvas = document.createElement('canvas');
    this.canvas.width = FOCUS.pixelWidth;
    this.canvas.height = FOCUS.pixelHeight;
    this.ctx = this.canvas.getContext('2d');

    this.texture = new THREE.CanvasTexture(this.canvas);
    this.texture.colorSpace = THREE.SRGBColorSpace;
    this.texture.generateMipmaps = false;
    this.texture.minFilter = THREE.LinearFilter;
    this.texture.magFilter = THREE.LinearFilter;

    /** Grows only when the transcript actually changes — the focus tier's
     *  equivalent of the atlas invariant, and checkable the same way. */
    this.redraws = 0;
    this.dirty = true;

    /* ------------------------------------------------- in-scene fallback -- */

    this.mesh = new THREE.Mesh(
      new THREE.PlaneGeometry(FOCUS.width, FOCUS.height),
      new THREE.MeshBasicMaterial({ map: this.texture, transparent: true }),
    );
    this.mesh.name = 'focus';
    this.mesh.visible = false;
    this.mesh.renderOrder = 10; // in front of the ring, which it overlaps
    scene.add(this.mesh);

    /* --------------------------------------------------------- quad layer -- */

    this.layer = null;
    this.binding = null;
    this.layerTarget = null;
    /** Set once a layer attempt has failed for good, so it is not retried. */
    this.layersUnavailable = false;
    this.layerAttempts = 0;

    // A one-quad orthographic scene, used only to blit the canvas into the
    // compositor's texture. Not part of the main scene graph.
    this.blitScene = new THREE.Scene();
    this.blitCamera = new THREE.OrthographicCamera(-0.5, 0.5, 0.5, -0.5, 0, 1);
    this.blitScene.add(
      new THREE.Mesh(
        new THREE.PlaneGeometry(1, 1),
        new THREE.MeshBasicMaterial({ map: this.texture, transparent: true }),
      ),
    );

    this.draw();
  }

  get open() {
    return this.session !== null;
  }

  get sessionId() {
    return this.session?.id ?? null;
  }

  /* ---------------------------------------------------------------- data -- */

  /** Replace the transcript wholesale — a fresh focus, or the mock feed. */
  setTranscript(text) {
    this.lines = Array.isArray(text) ? text.flatMap((t) => wrap(t, FOCUS.cols)) : wrap(text, FOCUS.cols);
    this.scrollOffset = 0;
    this.dirty = true;
  }

  /** A `{t:"transcript"}` frame for the focused session (§6). */
  append({ text, seq } = {}) {
    if (typeof seq === 'number') {
      if (seq <= this.seq) return; // a replay after reconnect
      this.seq = seq;
    }
    const added = wrap(text, FOCUS.cols);
    if (!added.length) return;
    this.lines.push(...added);
    // Only follow the tail if the operator is already at it. Scrolling back to
    // read something and having it yanked away by an arriving line is the
    // single most irritating thing a log view can do.
    if (this.scrollOffset > 0) this.scrollOffset += added.length;
    this.clampScroll();
    this.dirty = true;
  }

  /** @param {number} delta lines; negative scrolls toward older output. */
  scrollBy(delta) {
    const before = this.scrollOffset;
    this.scrollOffset -= delta;
    this.clampScroll();
    if (this.scrollOffset !== before) this.dirty = true;
  }

  clampScroll() {
    const max = Math.max(0, this.lines.length - FOCUS.rows);
    this.scrollOffset = Math.min(max, Math.max(0, Math.round(this.scrollOffset)));
  }

  /** New session data for the panel already focused (state, title, unread). */
  setSession(session) {
    if (!session || session.id !== this.sessionId) return;
    this.session = session;
    this.dirty = true;
  }

  /* --------------------------------------------------------------- show -- */

  /**
   * Pull a session forward. `pose` is where to put it, already computed from
   * the viewer — see `poseInFrontOf` below.
   */
  show(session, pose) {
    const changed = session.id !== this.sessionId;
    this.session = session;
    // The pulled-forward surface IS the panel, so it answers to the same id.
    // Without this, a second press of A would target whatever slid in behind
    // the hidden ambient panel and focus that instead of dismissing this one.
    this.mesh.userData.sessionId = session.id;
    if (changed) {
      this.lines = [];
      this.scrollOffset = 0;
      this.seq = -1;
    }
    this.dirty = true;
    this.place(pose);
    this.mesh.visible = !this.layer;
  }

  hide() {
    this.session = null;
    this.lines = [];
    this.seq = -1;
    this.mesh.visible = false;
    this.mesh.userData.sessionId = null;
    if (this.layer) {
      // Leaving a stale layer in the render state would keep compositing a
      // panel the operator just dismissed.
      this.destroyLayer();
    }
  }

  /** Move the surface (and its layer, if any) to a pose. */
  place({ position, quaternion }) {
    this.mesh.position.copy(position);
    this.mesh.quaternion.copy(quaternion);
    if (this.layer && globalThis.XRRigidTransform) {
      try {
        this.layer.transform = new XRRigidTransform(
          { x: position.x, y: position.y, z: position.z },
          { x: quaternion.x, y: quaternion.y, z: quaternion.z, w: quaternion.w },
        );
      } catch (err) {
        console.warn('[xr] quad layer transform rejected', err);
      }
    }
  }

  /* --------------------------------------------------------------- layer -- */

  /**
   * Try to promote the surface to a `text-optimized` quad layer. Called once
   * per session start, and again the first time a panel is focused inside a
   * session — whichever comes first. Returns true if the layer is live.
   */
  ensureLayer(session, referenceSpace) {
    if (this.layer) return true;
    if (this.layersUnavailable || !session || !referenceSpace) return false;
    this.layerAttempts += 1;

    const reason = (message) => {
      this.layersUnavailable = true;
      console.info(`[xr] quad layer unavailable (${message}); using an in-scene plane instead`);
      this.mesh.visible = this.open;
      return false;
    };

    // §13: check, never assume. `enabledFeatures` is the authoritative answer
    // to "did the session actually grant `layers`", not the optionalFeatures
    // list we asked with.
    if (typeof XRWebGLBinding === 'undefined') return reason('no XRWebGLBinding');
    if (session.enabledFeatures && !session.enabledFeatures.includes('layers')) {
      return reason('feature not granted');
    }
    // three.js only puts the session on the layers path when it can; if it fell
    // back to an XRWebGLLayer base layer then `layers` is empty and mixing the
    // two is illegal, so there is nothing to add a quad layer to.
    //
    // But an empty list does not mean that YET. `updateRenderState` is deferred
    // by spec — "the changes are applied at the beginning of the next animation
    // frame" — so three.js's own projection layer is invisible here for at
    // least one frame after the session starts. Giving up on the first frame
    // would silently cost the headset the entire text-optimized path, which is
    // the one thing this tier exists for. So: keep looking for about a second,
    // and only then call it.
    const existing = session.renderState.layers;
    if (!existing || !existing.length) {
      if (this.layerAttempts < LAYER_ATTEMPT_BUDGET) return false; // not yet — retried next frame
      return reason('session is on a base layer');
    }
    if (typeof this.renderer.setRenderTargetTextures !== 'function') {
      return reason('three.js build cannot wrap an external texture');
    }

    try {
      this.binding = new XRWebGLBinding(session, this.renderer.getContext());
      this.layer = this.binding.createQuadLayer({
        space: referenceSpace,
        viewPixelWidth: FOCUS.pixelWidth,
        viewPixelHeight: FOCUS.pixelHeight,
        width: FOCUS.width,
        height: FOCUS.height,
        layout: 'mono',
        // `quality` is an attribute of XRCompositionLayer rather than a member
        // of XRQuadLayerInit, so the init value is ignored by a spec-compliant
        // implementation and the assignment below is the one that takes. Both
        // are here: the init matches how §7 writes it, the assignment works.
        quality: 'text-optimized',
      });
      this.layer.quality = 'text-optimized';

      this.layerTarget = new THREE.WebGLRenderTarget(FOCUS.pixelWidth, FOCUS.pixelHeight);

      // Array order is back to front, so appending puts the focused transcript
      // over the projection layer — which is where a pulled-forward panel is.
      session.updateRenderState({ layers: [...existing, this.layer] });
    } catch (err) {
      this.destroyLayer();
      return reason(`creation failed: ${err?.message ?? err}`);
    }

    this.mesh.visible = false; // the layer replaces the plane, never doubles it
    console.info(
      `[xr] focus tier on an XRQuadLayer, quality="${this.layer.quality}", ` +
        `${FOCUS.pixelWidth}×${FOCUS.pixelHeight} over ${FOCUS.width}×${FOCUS.height}m`,
    );
    return true;
  }

  destroyLayer() {
    try {
      this.layer?.destroy?.();
    } catch {
      /* already gone with the session */
    }
    this.layer = null;
    this.binding = null;
    this.layerTarget?.dispose();
    this.layerTarget = null;
  }

  /**
   * Per frame. Repaints the canvas only when the transcript changed, then
   * blits it into the compositor's texture when a layer is driving the surface.
   */
  update(frame) {
    if (this.dirty) this.draw();
    if (!this.layer || !frame || !this.open) return;

    try {
      const sub = this.binding.getSubImage(this.layer, frame);
      const target = this.layerTarget;
      this.renderer.setRenderTargetTextures(target, sub.colorTexture);
      if (sub.viewport) {
        target.viewport.set(sub.viewport.x, sub.viewport.y, sub.viewport.width, sub.viewport.height);
      }
      // Save and restore rather than resetting to null: inside an XR session the
      // bound target is three.js's own projection-layer target, and handing it
      // back a null would drop the frame the ring is drawn into.
      const previous = this.renderer.getRenderTarget();
      this.renderer.setRenderTarget(target);
      this.renderer.render(this.blitScene, this.blitCamera);
      this.renderer.setRenderTarget(previous);
    } catch (err) {
      console.warn('[xr] quad layer blit failed; falling back to an in-scene plane', err);
      this.destroyLayer();
      this.layersUnavailable = true;
      this.mesh.visible = this.open;
    }
  }

  /* ---------------------------------------------------------------- draw -- */

  draw() {
    const { ctx } = this;
    const w = FOCUS.pixelWidth;
    const h = FOCUS.pixelHeight;

    this.dirty = false;
    this.redraws += 1;
    this.texture.needsUpdate = true;

    ctx.clearRect(0, 0, w, h);
    if (!this.session) return;

    // Body. Same colour token as the ambient cell — the two tiers are the same
    // object at two distances — but denser, because this one has the ring
    // behind it rather than open room. See FOCUS.surfaceAlpha.
    ctx.fillStyle = rgba(COLOR.surface, FOCUS.surfaceAlpha);
    ctx.fillRect(0, 0, w, h);
    ctx.strokeStyle = COLOR.rule;
    ctx.lineWidth = 3;
    ctx.strokeRect(1.5, 1.5, w - 3, h - 3);

    // Full-height role bar, exactly as the ambient tier does it (§8). It is not
    // pulsed here: the pulse is the ring's job, and a 70°-wide flashing bar
    // 0.9 m from the operator's eyes is not the same design at all.
    ctx.fillStyle = barColor(this.session);
    ctx.fillRect(0, 0, FOCUS.barW, h);

    ctx.textBaseline = 'middle';
    const alerting = this.session.state === 'needs_you';

    // Title bar: title on the left, state (or the scroll position) on the right.
    const right = this.scrollOffset > 0 ? `▲${this.scrollOffset}` : String(this.session.state ?? '');
    ctx.font = focusFont(600);
    const rightW = ctx.measureText(right).width;
    ctx.fillStyle = this.scrollOffset > 0 ? COLOR.inkDim : alerting ? COLOR.alert : COLOR.ink;
    ctx.fillText(right, w - FOCUS.padR - rightW, FOCUS.titleH / 2);

    ctx.fillStyle = COLOR.ink;
    const titleW = w - FOCUS.textX - FOCUS.padR - rightW - FOCUS.advancePx * 2;
    ctx.fillText(this.clip(String(this.session.title ?? ''), titleW), FOCUS.textX, FOCUS.titleH / 2);

    ctx.fillStyle = COLOR.rule;
    ctx.fillRect(FOCUS.barW, FOCUS.titleH, w - FOCUS.barW, 3);

    // Transcript, newest at the bottom. The slice is taken from the end so the
    // tail is what shows when `scrollOffset` is 0.
    const end = this.lines.length - this.scrollOffset;
    const start = Math.max(0, end - FOCUS.rows);
    const page = this.lines.slice(start, Math.max(start, end));

    ctx.font = focusFont(400);
    ctx.fillStyle = COLOR.ink;
    const top = FOCUS.titleH + FOCUS.padY;
    page.forEach((line, i) => {
      ctx.fillText(line, FOCUS.textX, top + i * FOCUS.lineH + FOCUS.lineH / 2);
    });

    if (!this.lines.length) {
      ctx.fillStyle = COLOR.inkDim;
      ctx.fillText('waiting for transcript…', FOCUS.textX, top + FOCUS.lineH / 2);
    }
  }

  /** Trim to a pixel width with a tail ellipsis. Monospace, so this is exact. */
  clip(text, maxW) {
    const max = Math.floor(maxW / FOCUS.advancePx);
    if (max <= 1) return '';
    return text.length <= max ? text : `${text.slice(0, max - 1)}…`;
  }

  dispose() {
    this.destroyLayer();
    this.mesh.removeFromParent();
    this.mesh.geometry.dispose();
    this.mesh.material.dispose();
    this.texture.dispose();
  }
}

/* ----------------------------------------------------------------- pose --- */

const _forward = new THREE.Vector3();
const _euler = new THREE.Euler(0, 0, 0, 'YXZ');

/**
 * Where a focused panel goes: `distance` metres along the viewer's HORIZONTAL
 * heading, at the viewer's own eye height, turned to face straight back.
 *
 * Horizontal on purpose. Taking the full look direction would park the panel on
 * the floor whenever the operator happened to glance down as they pressed A,
 * and tilt it away from level.
 */
export function poseInFrontOf(camera, distance = FOCUS.distance) {
  const position = new THREE.Vector3();
  camera.getWorldPosition(position);
  camera.getWorldDirection(_forward);

  const heading = Math.atan2(_forward.x, _forward.z);
  position.x += Math.sin(heading) * distance;
  position.z += Math.cos(heading) * distance;

  // Facing the viewer means the plane's +Z normal points back down the heading,
  // which is the same `angle + π` rule the ring uses to face its centre.
  _euler.set(0, heading + Math.PI, 0);
  return { position, quaternion: new THREE.Quaternion().setFromEuler(_euler), heading };
}

/** Hover outline for the ambient ring — one shared mesh, re-parented to the
 *  targeted panel. Twelve per-panel outlines would be twelve more materials,
 *  and the whole point of the shared atlas is that the ring has exactly one. */
export function makeHoverOutline(width, height) {
  const x = width / 2 + HOVER.inset;
  const y = height / 2 + HOVER.inset;
  const geometry = new THREE.BufferGeometry().setFromPoints([
    new THREE.Vector3(-x, -y, 0),
    new THREE.Vector3(x, -y, 0),
    new THREE.Vector3(x, y, 0),
    new THREE.Vector3(-x, y, 0),
    new THREE.Vector3(-x, -y, 0),
  ]);
  const material = new THREE.LineBasicMaterial({
    color: new THREE.Color(HOVER.color),
    transparent: true,
    opacity: HOVER.opacity,
    depthTest: false,
  });
  const outline = new THREE.Line(geometry, material);
  outline.renderOrder = 5;
  outline.visible = false;
  return outline;
}
