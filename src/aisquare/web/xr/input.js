/**
 * One input abstraction for the whole client (plan §9).
 *
 * Everything the operator can do arrives here as a SEMANTIC event — `select`,
 * `focusToggle`, `nextAlert`, `rotate`, `radius`, `scroll`, `recenter`,
 * `collapse`, `talkStart`, `talkEnd`, `muteToggle` — and nothing downstream
 * knows whether it came from a Touch controller, a key, or (later) a hand. That
 * is the point of the layer: §9 says "controllers and hands go through one
 * abstraction", and the desktop key bindings exist so the same events can be
 * driven without a headset at all.
 *
 * Targeting lives here too, because `select` and `focusToggle` are meaningless
 * without a target and splitting them apart would mean two modules agreeing on
 * which panel is under the pointer.
 *
 * NOT implemented, deliberately: right-grip grab-to-reposition. It is item 6 on
 * the §12 cut list and it is not trivial here — a dragged panel has to leave its
 * ring slot, survive the relayout that the next delta triggers, and acquire a
 * stored offset that collapse/summon and re-anchoring would then have to carry.
 * That is a data-model change for a feature already marked for cutting, so the
 * right grip is left unbound.
 */

import * as THREE from 'three';
import { RING } from './style.js';

/* ------------------------------------------------------------ button map -- */

/**
 * The `xr-standard` gamepad mapping, which is what the Quest Touch controllers
 * report. Indices are fixed by the WebXR Gamepads Module, not by the device.
 *
 * Note what is absent: the left MENU button. It is system-reserved (§9: "Left
 * menu button is system-reserved. Do not bind it.") and the runtime does not
 * even surface it as a gamepad button — binding it is impossible here, and
 * nothing below reaches for an index that could become it.
 */
const BUTTON = {
  trigger: 0,
  grip: 1, // "squeeze"
  // 2 is the touchpad, which Touch controllers do not have.
  stick: 3, // thumbstick press — deliberately unbound
  lower: 4, // A on the right controller, X on the left
  upper: 5, // B on the right controller, Y on the left
};

/** Thumbstick axes in the `xr-standard` mapping (0/1 are the absent touchpad). */
const AXIS = { x: 2, y: 3 };

/* -------------------------------------------------------------- tuning --- */

/**
 * Rates, in units per second at full stick deflection. These are interaction
 * tuning rather than design tokens, so they live here and not in style.js —
 * style.js owns what the operator SEES, this owns how fast it responds.
 */
const RATE = {
  rotate: 1.8, // rad/s ≈ 103°/s — a full 200° sweep takes about two seconds
  radius: 0.5, // m/s of push–pull
  scroll: 10, // transcript lines/s
};

/** Sticks rest slightly off-centre; below this a stick is at rest. */
const DEADZONE = 0.15;

/** §9: left grip HELD, not tapped, toggles collapse. */
const GRIP_HOLD_MS = 500;

/** Discrete keyboard steps, for a tester with no controller. */
const KEY_STEP = {
  rotate: (4 * Math.PI) / 180, // arrows — fine, because key repeat compounds it
  bracket: Math.PI / 18, // [ ] — 10°, the step M2 shipped, kept working
  scroll: 2, // lines per press
  radius: 0.05, // m
};

/** Ray length when it hits nothing. Long enough to read as a pointer. */
const RAY_MISS_M = 1.5;

/**
 * Rescale a stick axis so response starts at zero just past the deadzone,
 * instead of jumping to DEADZONE the moment the stick moves.
 */
function applyDeadzone(v) {
  const a = Math.abs(v);
  if (a < DEADZONE) return 0;
  return Math.sign(v) * ((a - DEADZONE) / (1 - DEADZONE));
}

/* ---------------------------------------------------------------- input -- */

export class Input {
  /**
   * @param {object} opts
   * @param {THREE.WebGLRenderer} opts.renderer
   * @param {THREE.Scene} opts.scene           controllers are added here
   * @param {() => THREE.Camera} opts.viewer   the camera to target from; in XR
   *        this must be `renderer.xr.getCamera()`, whose pose is current
   * @param {() => THREE.Object3D[]} opts.targets  hit-testable panel meshes
   */
  constructor({ renderer, scene, viewer, targets }) {
    this.renderer = renderer;
    this.scene = scene;
    this.viewer = viewer;
    this.targets = targets;

    /** @type {Map<string, Set<Function>>} */
    this.handlers = new Map();

    this.raycaster = new THREE.Raycaster();
    this.raycaster.far = 8;

    /** Previous pressed state, keyed `${handedness}:${buttonIndex}`. */
    this.pressed = new Map();
    /** Left grip: when it went down, and whether this hold already fired. */
    this.grip = { downAt: 0, fired: false };

    /** The panel mesh under the pointer, or null. */
    this.target = null;

    /** Pointer-driven targeting on desktop only becomes active once the mouse
     *  has actually moved, so a tester who never touches it still gets gaze. */
    this.pointer = new THREE.Vector2();
    this.pointerMoved = false;

    this.controllers = [0, 1].map((i) => this.makeController(i));
    this.bindKeys();
    this.bindPointer();

    // Scratch vectors — allocating these per frame would be the only garbage
    // this client produces, at 72 per second.
    this._origin = new THREE.Vector3();
    this._dir = new THREE.Vector3();
    this._toPanel = new THREE.Vector3();
  }

  /* -------------------------------------------------------------- events -- */

  on(type, handler) {
    if (!this.handlers.has(type)) this.handlers.set(type, new Set());
    this.handlers.get(type).add(handler);
    return () => this.handlers.get(type)?.delete(handler);
  }

  emit(type, payload = {}) {
    for (const handler of this.handlers.get(type) ?? []) {
      // One throwing handler must not stop the input loop; a stuck input layer
      // in a headset means the operator cannot even collapse the ring.
      try {
        handler(payload);
      } catch (err) {
        console.error(`[xr] input handler for "${type}" threw`, err);
      }
    }
  }

  /** Events that act on whatever is targeted all carry the target with them. */
  emitTargeted(type) {
    this.emit(type, { targetId: this.targetId, target: this.target });
  }

  get targetId() {
    return this.target?.userData.sessionId ?? null;
  }

  /* --------------------------------------------------------- controllers -- */

  makeController(index) {
    const controller = this.renderer.xr.getController(index);

    // A thin line down −Z: the WebXR target ray. LineBasicMaterial ignores
    // `linewidth` on every WebGL backend, so this is a hairline by construction,
    // which is what §9's "raycast pointer" wants — a pointer, not a beam.
    const geometry = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(0, 0, 0),
      new THREE.Vector3(0, 0, -1),
    ]);
    const material = new THREE.LineBasicMaterial({ transparent: true, opacity: 0.55 });
    const ray = new THREE.Line(geometry, material);
    ray.scale.z = RAY_MISS_M;
    ray.visible = false;
    controller.add(ray);
    controller.userData.ray = ray;

    controller.addEventListener('connected', (event) => {
      controller.userData.source = event.data;
      // Only the right hand points. A visible ray on the left would invite
      // aiming with a controller whose trigger is push-to-talk.
      ray.visible = event.data.handedness === 'right';
    });
    controller.addEventListener('disconnected', (event) => {
      // Handedness comes off the event, not off `userData`: clearing the source
      // first would have made this release nothing, and a controller that goes
      // flat mid-hold would leave push-to-talk latched on for good.
      const handedness = event.data?.handedness ?? controller.userData.source?.handedness;
      controller.userData.source = null;
      ray.visible = false;
      this.releaseHand(handedness);
    });

    this.scene.add(controller);
    return controller;
  }

  /** Live input sources, so targeting can tell "no controller" from "idle". */
  get sources() {
    return this.controllers.map((c) => c.userData.source).filter(Boolean);
  }

  get rightController() {
    return this.controllers.find((c) => c.userData.source?.handedness === 'right') ?? null;
  }

  /** A controller that vanished mid-hold must not leave talk or grip latched. */
  releaseHand(handedness) {
    if (handedness !== 'left') return;
    if (this.pressed.get('left:0')) this.emit('talkEnd', { source: 'controller' });
    this.grip.downAt = 0;
    this.grip.fired = false;
    for (const key of [...this.pressed.keys()]) {
      if (key.startsWith('left:')) this.pressed.set(key, false);
    }
  }

  /** True on the frame a button goes down; tracks the release too. */
  edge(handedness, gamepad, index) {
    const key = `${handedness}:${index}`;
    const now = gamepad.buttons[index]?.pressed === true;
    const was = this.pressed.get(key) === true;
    this.pressed.set(key, now);
    return { down: now && !was, up: !now && was, held: now };
  }

  axis(gamepad, index) {
    return applyDeadzone(gamepad.axes[index] ?? 0);
  }

  /* --------------------------------------------------------------- poll --- */

  /**
   * Poll controllers and update targeting. Called once per rendered frame.
   * @param {number} dt seconds since the previous frame
   */
  update(dt) {
    for (const controller of this.controllers) {
      const source = controller.userData.source;
      const gamepad = source?.gamepad;
      if (!gamepad) continue;
      if (source.handedness === 'right') this.pollRight(gamepad, dt);
      else if (source.handedness === 'left') this.pollLeft(gamepad, dt);
    }
    this.updateTarget();
  }

  /** §9: right thumbstick X rotates, Y push–pulls; trigger selects; A/B. */
  pollRight(gamepad, dt) {
    const x = this.axis(gamepad, AXIS.x);
    // Stick forward reports −1; pushing the ring away should read as forward.
    const y = -this.axis(gamepad, AXIS.y);
    if (x) this.emit('rotate', { delta: -x * RATE.rotate * dt });
    if (y) this.emit('radius', { delta: y * RATE.radius * dt });

    if (this.edge('right', gamepad, BUTTON.trigger).down) this.emitTargeted('select');
    if (this.edge('right', gamepad, BUTTON.lower).down) this.emitTargeted('focusToggle'); // A
    if (this.edge('right', gamepad, BUTTON.upper).down) this.emit('nextAlert'); // B

    // The right grip stays unbound — see the module header.
  }

  /** §9: left thumbstick Y scrolls; trigger is push-to-talk; X/Y; grip held. */
  pollLeft(gamepad, dt) {
    // Stick forward (−1) should scroll toward older lines, i.e. up the log.
    const y = this.axis(gamepad, AXIS.y);
    if (y) this.emit('scroll', { delta: y * RATE.scroll * dt });

    // Push-to-talk emits events only. Capture is the voice task's (§10, M6).
    const trigger = this.edge('left', gamepad, BUTTON.trigger);
    if (trigger.down) this.emit('talkStart', { source: 'controller' });
    if (trigger.up) this.emit('talkEnd', { source: 'controller' });

    if (this.edge('left', gamepad, BUTTON.lower).down) this.emit('muteToggle'); // X
    if (this.edge('left', gamepad, BUTTON.upper).down) this.emit('recenter'); // Y

    // Left grip HELD for 0.5s toggles collapse. It fires once per hold, on the
    // frame the threshold is crossed, so the ring does not flap while held.
    const grip = this.edge('left', gamepad, BUTTON.grip);
    if (grip.down) {
      this.grip.downAt = performance.now();
      this.grip.fired = false;
    } else if (grip.held && !this.grip.fired && performance.now() - this.grip.downAt >= GRIP_HOLD_MS) {
      this.grip.fired = true;
      this.emit('collapse');
    } else if (grip.up) {
      this.grip.fired = false;
    }
  }

  /* ------------------------------------------------------------ targeting -- */

  /**
   * Resolve what the operator is pointing at, in this order:
   *
   *   1. The right controller's target ray — a deliberate point, so a miss is
   *      a miss and nothing is targeted.
   *   2. The mouse, on desktop, once it has moved.
   *   3. Gaze. §9's fallback: with no controller, "the panel nearest the view
   *      ray counts as targeted" — nearest, not hit, so looking roughly at the
   *      board is enough and a tester always has something to press `f` on.
   */
  updateTarget() {
    const camera = this.viewer();
    const panels = this.targets();
    const right = this.rightController;

    let hit = null;
    let gaze = false;

    if (right && right.userData.source) {
      right.getWorldPosition(this._origin);
      right.getWorldDirection(this._dir).negate(); // the ray runs down −Z
      this.raycaster.set(this._origin, this._dir);
      hit = this.raycaster.intersectObjects(panels, false)[0] ?? null;
      // Stop the ray at what it touches: the length IS the depth cue.
      right.userData.ray.scale.z = hit ? hit.distance : RAY_MISS_M;
    } else if (this.pointerMoved && !this.renderer.xr.isPresenting) {
      this.raycaster.setFromCamera(this.pointer, camera);
      hit = this.raycaster.intersectObjects(panels, false)[0] ?? null;
      gaze = !hit; // mouse off the board falls through to gaze, below
    } else {
      gaze = true;
    }

    let target = hit?.object ?? null;
    if (!target && gaze) target = this.nearestToViewRay(camera, panels);

    if (target !== this.target) {
      this.target = target;
      this.emit('hover', { targetId: this.targetId, target });
    }
  }

  /** Smallest angle between the view ray and the direction to a panel. */
  nearestToViewRay(camera, panels) {
    if (!panels.length) return null;
    camera.getWorldPosition(this._origin);
    camera.getWorldDirection(this._dir);

    let best = null;
    let bestDot = -Infinity;
    for (const panel of panels) {
      panel.getWorldPosition(this._toPanel).sub(this._origin);
      const len = this._toPanel.length();
      if (len < 1e-4) continue;
      const dot = this._toPanel.divideScalar(len).dot(this._dir);
      if (dot > bestDot) {
        bestDot = dot;
        best = panel;
      }
    }
    // Behind the viewer is not "nearest the view ray", it is out of sight.
    return bestDot > 0.2 ? best : null;
  }

  /* -------------------------------------------------------------- desktop -- */

  bindPointer() {
    addEventListener('pointermove', (event) => {
      this.pointer.set((event.clientX / innerWidth) * 2 - 1, -(event.clientY / innerHeight) * 2 + 1);
      this.pointerMoved = true;
    });
  }

  /**
   * The same semantic events from the keyboard, so every acceptance criterion
   * that does not need a headset can be driven in desktop Chrome.
   */
  bindKeys() {
    const ARROWS = new Set(['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown']);

    // Push-to-talk keys, compared case-insensitively so a hold begun or released
    // with Shift down (which delivers 'T', not 't') is still push-to-talk. A
    // keyup that does not match latches the mic on until voice's 55 s cap sends
    // the whole capture as a prompt — the failure this set exists to prevent.
    const isTalkKey = (event) => event.key === 't' || event.key === 'T';

    addEventListener('keydown', (event) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (isTalkKey(event)) {
        // `repeat` is the autorepeat that fires while a key is held down;
        // push-to-talk must start once, not sixty times.
        if (!event.repeat) this.emit('talkStart', { source: 'key' });
        return;
      }
      // Arrows scroll the document by default, which would fight the overlay.
      if (ARROWS.has(event.key)) event.preventDefault();

      switch (event.key) {
        case 'f':
          this.emitTargeted('focusToggle');
          break;
        case 'Enter':
          this.emitTargeted('select');
          break;
        case 'b':
          this.emit('nextAlert');
          break;
        case 'c':
          this.emit('collapse');
          break;
        case 'r':
          this.emit('recenter');
          break;
        case 'm':
          this.emit('muteToggle');
          break;
        case 'ArrowLeft':
          this.emit('rotate', { delta: KEY_STEP.rotate });
          break;
        case 'ArrowRight':
          this.emit('rotate', { delta: -KEY_STEP.rotate });
          break;
        case '[':
          this.emit('rotate', { delta: -KEY_STEP.bracket });
          break;
        case ']':
          this.emit('rotate', { delta: KEY_STEP.bracket });
          break;
        case 'ArrowUp':
          this.emit('scroll', { delta: -KEY_STEP.scroll });
          break;
        case 'ArrowDown':
          this.emit('scroll', { delta: KEY_STEP.scroll });
          break;
        case '-':
          this.emit('radius', { delta: -KEY_STEP.radius });
          break;
        case '=':
          this.emit('radius', { delta: KEY_STEP.radius });
          break;
        default:
      }
    });

    addEventListener('keyup', (event) => {
      if (isTalkKey(event)) this.emit('talkEnd', { source: 'key' });
    });

    // A key held while the tab loses focus never delivers its keyup, which
    // would latch push-to-talk on for the rest of the session.
    addEventListener('blur', () => this.emit('talkEnd', { source: 'blur' }));
  }
}

/** Push–pull clamp, shared by the stick and the keyboard path. */
export function clampRadius(radius) {
  return Math.min(RING.maxRadius, Math.max(RING.minRadius, radius));
}
