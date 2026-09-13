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
  voiceFont,
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

    /**
     * Canvas redraws. Grows only when something on the panel actually changed
     * — never per frame — which is the focus tier's half of the atlas
     * invariant and is checkable the same way.
     *
     * Voice shares this canvas, so an interim `stt` frame necessarily redraws
     * it: there is one texture and the speech line is on it. That would blur
     * the invariant, so voice-caused redraws are ALSO counted on their own and
     * the transcript figure stays recoverable as `redraws - voiceRedraws`.
     * With the mic idle the two move exactly as they did before voice existed.
     */
    this.redraws = 0;
    /** Of `redraws`, the ones caused by the voice strip rather than by data. */
    this.voiceRedraws = 0;
    this._dirty = false;
    this._dataDirty = false;
    this.dirty = true;

    /**
     * The voice strip's contents (§10). Timers live in main.js — this object
     * is a rendering input, and a panel that expired its own text would be a
     * second place where "how long does a final transcript stay up" is decided.
     */
    this.voice = { live: false, speech: '', speechFinal: false, notice: '', alert: false };

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

  /**
   * Needs a redraw. Assigning true through this setter also records that DATA
   * asked for it — which is what keeps `redraws - voiceRedraws` meaning exactly
   * what `redraws` meant before voice shared the canvas.
   *
   * A setter rather than a rename so that every existing `this.dirty = true` in
   * this file keeps working and keeps being attributed correctly; `setVoice` is
   * the one writer that goes to `_dirty` directly, because a voice update must
   * not claim a redraw the transcript had already earned.
   */
  get dirty() {
    return this._dirty;
  }

  set dirty(value) {
    this._dirty = Boolean(value);
    if (value) this._dataDirty = true;
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

  /**
   * Update the voice strip. Only the keys passed are changed.
   *
   * Returns true if anything actually moved. The comparison is the load-bearing
   * part, not a micro-optimisation: interim `stt` frames repeat their prefix as
   * the decoder firms it up, and a panel that redrew on every identical string
   * would upload a 1600 × 1024 texture for a no-op — the exact failure the
   * atlas invariant exists to catch, arriving through the other tier.
   *
   * @param {object}  patch
   * @param {boolean} [patch.live]         mic dot on/off. On/off only: §8 allows
   *   exactly one animated thing in the scene and it is the alert bar.
   * @param {string}  [patch.speech]       interim or final transcript
   * @param {boolean} [patch.speechFinal]  render it as committed, not provisional
   * @param {string}  [patch.notice]       the ack line, or an error
   * @param {boolean} [patch.alert]        this notice is a problem. Does NOT
   *   colour it red — see `drawVoice` — but it is what routes the message to
   *   the HUD as well, where the scene's palette rules do not apply.
   */
  setVoice(patch = {}) {
    let changed = false;
    for (const key of ['live', 'speech', 'speechFinal', 'notice', 'alert']) {
      if (!(key in patch)) continue;
      const value = key === 'speech' || key === 'notice' ? String(patch[key] ?? '') : Boolean(patch[key]);
      if (this.voice[key] === value) continue;
      this.voice[key] = value;
      changed = true;
    }
    if (!changed) return false;
    this._dirty = true; // deliberately not through the setter — see `dirty`
    return true;
  }

  /** Everything voice put on the panel, gone. Used when focus moves. */
  clearVoice() {
    return this.setVoice({ live: false, speech: '', speechFinal: false, notice: '', alert: false });
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
      // A different session: the previous one's speech feedback is not about
      // this panel and must not appear under its title.
      this.voice = { live: false, speech: '', speechFinal: false, notice: '', alert: false };
    }
    this.dirty = true;
    this.place(pose);
    this.mesh.visible = !this.layer;
  }

  hide() {
    this.session = null;
    this.lines = [];
    this.seq = -1;
    // Leaving a final transcript or an ack on the strip would put the last
    // session's words under the next session's title.
    this.voice = { live: false, speech: '', speechFinal: false, notice: '', alert: false };
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

    this._dirty = false;
    this.redraws += 1;
    if (!this._dataDirty) this.voiceRedraws += 1;
    this._dataDirty = false;
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

    this.drawVoice();
  }

  /**
   * The voice strip along the foot of the panel: the mic dot, then one line.
   *
   * This is what §10 asks for in as many words — "show interim ASR text on the
   * focused panel as it arrives; without that feedback the operator cannot tell
   * whether the mic is live, and will repeat themselves". Both halves of that
   * are here, and both are on the PANEL rather than the HUD, because in an
   * immersive session the DOM overlay may not exist at all.
   */
  drawVoice() {
    const { ctx } = this;
    // `alert` is deliberately not read here — it routes to the HUD, it does not
    // colour anything in the scene. See the fillStyle below.
    const { live, speech, speechFinal, notice } = this.voice;
    if (!live && !speech && !notice) return;

    const midY = FOCUS.voiceTop + FOCUS.voiceH / 2;

    // A hairline, so the strip reads as chrome rather than as a transcript line
    // that has come adrift from the ones above it.
    ctx.fillStyle = COLOR.rule;
    ctx.fillRect(FOCUS.barW, FOCUS.voiceTop, FOCUS.pixelWidth - FOCUS.barW - FOCUS.padR, 2);

    let x = FOCUS.textX;

    // The mic-live dot. A filled circle in ink, drawn or not drawn — there is
    // no opacity ramp and no timer behind it. §8 allows exactly one animated
    // thing in this scene and it is the alert bar; a "breathing" mic indicator
    // would be a second, and would also make `voiceRedraws` climb while the
    // operator simply held the trigger.
    if (live) {
      ctx.beginPath();
      ctx.arc(x + FOCUS.dotR, midY, FOCUS.dotR, 0, Math.PI * 2);
      ctx.fillStyle = COLOR.ink;
      ctx.fill();
    }
    x += FOCUS.dotR * 4; // the gutter is held whether or not the dot is lit, so
    // text does not jump sideways when the trigger comes up.

    const text = notice || speech;
    if (!text) return;

    // Provisional speech is dim and light; anything committed — a final
    // transcript, an ack, an error — is ink and heavy. Two steps, legible at
    // 0.9 m without reading the words.
    //
    // NOT `alert`, even for an error, and the temptation is worth naming: §8
    // reserves that red for `state === 'needs_you'` and the token's own comment
    // says "used nowhere else, ever". A red line on a focus panel is the exact
    // signal an operator scans the ring for, and spending it on "speech is not
    // installed" would make the one colour that means "an agent is blocked on
    // you" mean two things. `makeHoverOutline` refused it on the same grounds.
    // The error's own words carry the alarm here, and the HUD — 2D chrome,
    // outside the scene and outside §8 — is where its red edge lives.
    ctx.font = voiceFont(notice || speechFinal ? 600 : 400);
    ctx.fillStyle = notice || speechFinal ? COLOR.ink : COLOR.inkDim;

    const max = FOCUS.voiceCols;
    // Clipped from the FRONT for live speech: the words that matter while you
    // are still talking are the ones you just said. A notice is a whole short
    // sentence, so it clips from the back like every other label here.
    const shown =
      text.length <= max
        ? text
        : notice
          ? `${text.slice(0, max - 1)}…`
          : `…${text.slice(text.length - (max - 1))}`;
    ctx.fillText(shown, x, midY);
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
