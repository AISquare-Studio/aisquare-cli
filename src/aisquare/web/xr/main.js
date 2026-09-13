/**
 * Entry point: scene, XR session, render loop, and the wiring from the wire
 * protocol and the input layer to the ring.
 *
 * M2 built this as a desktop-only scene on purpose — every layout and data
 * problem is an order of magnitude cheaper to solve on a desktop than in a
 * headset. M3–M5 drop the headset on top of a scene that was already known
 * good, and the desktop path is kept working rather than replaced: `#mock`
 * still runs with OrbitControls and no XR at all, which is what lets a tester
 * drive nearly every acceptance criterion from desktop Chrome.
 *
 * Two feeds drive the same ring: a real websocket, and — under `#mock` — a
 * scripted in-memory feed.
 */

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

import { MOCK_BACKGROUND, RING, AMBIENT, FOCUS, ambientFont } from './style.js';
import { Atlas } from './panel.js';
import { Ring } from './ring.js';
import { FocusPanel, poseInFrontOf } from './focus.js';
import { Input } from './input.js';
import { AlertAudio } from './audio.js';
import { ConnectionChip, Net, hashParams } from './net.js';

const params = hashParams();
const MOCK = params.has('mock');
const DEV = location.hostname === 'localhost' || location.hostname === '127.0.0.1' || params.has('debug');

/* ------------------------------------------------------------------ fonts -- */

/**
 * The atlas draws text into a canvas, and canvas text does not re-flow when a
 * webfont arrives later — it would silently bake the fallback stack into the
 * texture and stay that way until the next delta. So the font is resolved
 * *before* the first draw. This is also why `atlasRedraws` can be an exact
 * number: there is no "redraw once the font loaded" frame.
 */
async function loadFont() {
  if (!document.fonts) return;
  try {
    await Promise.all([
      document.fonts.load(ambientFont(400), 'AGmw0189'),
      document.fonts.load(ambientFont(600), 'AGmw0189'),
      document.fonts.load(`400 ${FOCUS.fontPx}px "JetBrains Mono"`, 'AGmw0189'),
    ]);
    await document.fonts.ready;
  } catch (err) {
    // A blocked CDN is not fatal: style.js ships a system-mono fallback stack
    // and the panels stay legible, just not in JetBrains Mono.
    console.warn('[xr] webfont unavailable, falling back to system mono', err);
  }
}

/* ------------------------------------------------------------------ scene -- */

const canvas = document.getElementById('scene');
const notice = document.getElementById('notice');
const enterButton = document.getElementById('enter-ar');

let renderer;
try {
  // alpha, and a zero clear alpha, so an immersive-ar frame composites over
  // passthrough instead of painting a background across the room (§7).
  renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
} catch (err) {
  notice.hidden = false;
  notice.textContent = 'WebGL is unavailable in this browser, so the ring cannot render.';
  throw err;
}
// Above 2 the cost is real and the gain is not; the atlas is sampled ~1:1.
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.xr.enabled = true;
// §7 anchors the ring to local-floor. This must be set before `setSession`.
renderer.xr.setReferenceSpaceType('local-floor');

const scene = new THREE.Scene();
// A neutral mid-grey stands in for passthrough on the desktop. Never black:
// the plan is explicit that a palette evaluated in a dim room is a palette that
// fails over a lit one (§8/§13), and a black background would flatter these
// panels. It is cleared entirely on entering AR — see `onSessionStart`.
scene.background = new THREE.Color(MOCK_BACKGROUND);

const camera = new THREE.PerspectiveCamera(60, innerWidth / innerHeight, 0.01, 50);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.minDistance = 0.05;
controls.maxDistance = 6;

/**
 * Two camera poses, because the desktop mock is asked to do two jobs that pull
 * in opposite directions.
 *
 * `inspect` orbits the ring's centre from slightly outside it, which is how you
 * judge *layout* — you can see the whole arc at once. But it also views the
 * panels from ~2.5 m when they are designed for 1.6 m, so every glyph is ~35%
 * smaller than the headset will render it. Judging type from this pose would
 * condemn type that is actually correct.
 *
 * `viewer` puts the eye exactly where the operator's head goes and looks at the
 * centre panel across the true 1.6 m radius, so angular sizes on screen are the
 * angular sizes in the headset. That is the pose to judge the 1.5° and 2.2°
 * floors from.
 */
const POSES = {
  inspect: { eye: [0, RING.eyeHeight, -0.9], target: [0, RING.eyeHeight, 0] },
  viewer: { eye: [0, RING.eyeHeight, 0], target: [0, RING.eyeHeight, RING.radius] },
};

function setPose(name) {
  const pose = POSES[name] ?? POSES.inspect;
  camera.position.set(...pose.eye);
  controls.target.set(...pose.target);
  controls.update();
  return name;
}

setPose('inspect');

const atlas = new Atlas();
atlas.setAnisotropy(renderer.capabilities.getMaxAnisotropy());
const ring = new Ring(atlas);
scene.add(ring.group);

const focus = new FocusPanel({ renderer, scene });
const audio = new AlertAudio();

/**
 * The camera whose world matrix carries the CURRENT viewer pose.
 *
 * In an XR session that is `renderer.xr.getCamera()` — the array camera three.js
 * updates from the XRViewerPose each frame. The user-facing `camera` is also
 * synced, but only inside `render()`, so reading it during input or placement
 * would be a frame behind. Every re-anchor, every focus placement and the audio
 * listener all go through here so they agree on where the head is.
 */
const viewerCamera = () => (renderer.xr.isPresenting ? renderer.xr.getCamera() : camera);

addEventListener('resize', () => {
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
  // Note: no ring.layout() here. Resizing changes no data, and a redraw would
  // make `atlasRedraws` grow for a reason that has nothing to do with the feed.
});

/* ------------------------------------------------------------------ alerts -- */

const _alertAt = new THREE.Vector3();

/**
 * A session has just entered `needs_you`. §8 asks for the cue to come from the
 * panel's own position so the operator turns toward it — "the one thing this
 * medium does that a monitor cannot".
 */
ring.onAlert = (sessionId) => {
  const panel = ring.panelFor(sessionId);
  if (panel) panel.worldPosition(_alertAt);
  // A session on another page has no position in the room. Emitting at the ring
  // centre — which is the operator's own head — is the honest answer: it says
  // "something needs you" without inventing a direction to look in.
  else ring.group.getWorldPosition(_alertAt);
  audio.chimeAt(_alertAt);
  if (DEV) console.info(`[xr] alert: ${sessionId}${panel ? '' : ' (off-page)'}`);
};

/* ------------------------------------------------------------------- focus -- */

/** Transcript subscription, when there is a server to ask (§6: null = ambient). */
function subscribe(sessionId) {
  net?.subscribe(sessionId);
}

/**
 * Pull a session forward, or push it back.
 *
 * The ambient panel is hidden while its session is focused, so the session is
 * in exactly one place at a time and a second toggle visibly "returns the panel
 * to its ring slot".
 */
function toggleFocus(sessionId) {
  if (!sessionId) return null;

  if (focus.sessionId === sessionId) {
    closeFocus();
    return null;
  }

  const session = ring.sessions.get(sessionId);
  if (!session) return null;

  focus.show(session, poseInFrontOf(viewerCamera()));
  focus.ensureLayer(renderer.xr.getSession(), renderer.xr.getReferenceSpace());
  ring.setFocused(sessionId);
  subscribe(sessionId);
  if (MOCK) focus.setTranscript(mockTranscript(session));
  return sessionId;
}

function closeFocus() {
  if (!focus.open) return;
  focus.hide();
  ring.setFocused(null);
  subscribe(null);
}

/* ------------------------------------------------------------------- input -- */

const input = new Input({
  renderer,
  scene,
  viewer: viewerCamera,
  // The focused surface is hit-testable while it is up, so the pointer that
  // opened a panel can close it — it fills ~70° of view directly ahead, so it
  // is what the ray and the gaze fallback are on anyway. It stays in the list
  // even when a quad layer has taken over the drawing and the mesh is hidden:
  // three.js raycasts invisible objects, and `ring.panelMeshes` filters on
  // `visible` precisely so the ambient slot behind it does NOT answer.
  targets: () => (focus.open ? [...ring.panelMeshes, focus.mesh] : ring.panelMeshes),
});

/** The last push-to-talk edge. Voice capture itself is the M6 task's (§10). */
const talk = { active: false, since: 0, source: null };

input.on('hover', ({ targetId }) => ring.setHover(targetId));
input.on('rotate', ({ delta }) => ring.rotate(delta));
input.on('radius', ({ delta }) => ring.nudgeRadius(delta));
input.on('scroll', ({ delta }) => focus.scrollBy(delta));
input.on('nextAlert', () => {
  const panel = ring.nextAlert(viewerCamera());
  if (DEV) console.info(panel ? `[xr] nextAlert → ${panel.session.id}` : '[xr] nextAlert: nothing alerting');
});
// §9 gives the right trigger "select" and A "toggle focus on targeted panel".
// Both resolve to the same thing here: focus is what there is to do with a
// panel, and a trigger pull that did nothing would read as a broken pointer.
input.on('select', ({ targetId }) => toggleFocus(targetId));
input.on('focusToggle', ({ targetId }) => toggleFocus(targetId));
input.on('recenter', () => {
  ring.recenter(viewerCamera());
  if (focus.open) focus.place(poseInFrontOf(viewerCamera()));
});
input.on('collapse', () => {
  const collapsed = ring.toggleCollapse(viewerCamera());
  // Collapse puts the whole board away, focused panel included. Summon brings
  // back the ring only — re-focusing is a decision the operator makes at the
  // new location, not one restored from the old one.
  if (collapsed) closeFocus();
  if (DEV) console.info(`[xr] ring ${collapsed ? 'collapsed' : 'summoned'}`);
});
input.on('talkStart', ({ source }) => {
  audio.unlock(); // a key press is a gesture; take it
  talk.active = true;
  talk.since = performance.now();
  talk.source = source;
  if (DEV) console.info(`[xr] talkStart (${source}) — capture lands with the voice task (§10, M6)`);
});
input.on('talkEnd', ({ source }) => {
  if (!talk.active) return;
  talk.active = false;
  if (DEV) console.info(`[xr] talkEnd (${source}) after ${Math.round(performance.now() - talk.since)}ms`);
});
input.on('muteToggle', () => {
  // §9 maps X to "mute / unmute readout". There is no readout — text-to-speech
  // is item 2 on the §12 cut list — so this is deliberately inert. The event is
  // wired so the binding is provably live and the voice task has a seam.
  if (DEV) console.info('[xr] muteToggle (no readout to mute yet)');
});

/* --------------------------------------------------------------- AR entry -- */

const xrState = { supported: false, session: null, placed: false, targetHz: 72 };

/**
 * §13's trap, and the reason it is worth a paragraph of UI: on
 * `http://192.168.x.x:8748` `navigator.xr` is simply `undefined`, and the
 * failure looks like a broken page rather than a security policy. The fix is
 * one adb command, so the page says which one instead of showing a dead button.
 */
function explainNoXR() {
  const secure = window.isSecureContext;
  notice.hidden = false;
  notice.innerHTML =
    (secure
      ? 'No WebXR device here — this browser has no <code>navigator.xr</code>.\n' +
        'The ring below is the same scene the headset shows.\n\n'
      : '<code>navigator.xr</code> is undefined because <code>' +
        location.origin +
        '</code> is not a secure context.\n\n') +
    'To run this on the headset:\n' +
    '  tether over USB and run <code>adb reverse tcp:8748 tcp:8748</code>,\n' +
    '  then open <code>http://localhost:8748</code> — that IS a secure context and needs no certificates.\n\n' +
    'Untethered: add this origin under <code>chrome://flags</code> → "Insecure origins treated as secure".';
}

async function setupAR() {
  if (!navigator.xr) {
    explainNoXR();
    return;
  }
  try {
    xrState.supported = await navigator.xr.isSessionSupported('immersive-ar');
  } catch {
    xrState.supported = false;
  }
  if (!xrState.supported) {
    notice.hidden = false;
    notice.innerHTML =
      'This browser has <code>navigator.xr</code> but does not support <code>immersive-ar</code>.\n' +
      'The desktop ring below still runs; AR needs a passthrough headset, or\n' +
      'the Immersive Web Emulator extension in desktop Chrome.';
    return;
  }
  // The button appears ONLY once immersive-ar is known to be supported, so it
  // is never a control that does nothing when pressed.
  enterButton.hidden = false;
  enterButton.addEventListener('click', enterAR);
}

async function enterAR() {
  enterButton.disabled = true;
  try {
    // Unlocked here, inside the click, because a Quest will not start an
    // AudioContext outside a user gesture and a context created before one can
    // stay suspended for the life of the page.
    audio.unlock();

    const session = await navigator.xr.requestSession('immersive-ar', {
      requiredFeatures: ['local-floor'],
      optionalFeatures: ['layers', 'hand-tracking', 'anchors'],
    });
    await renderer.xr.setSession(session);
  } catch (err) {
    console.error('[xr] could not start immersive-ar', err);
    notice.hidden = false;
    notice.textContent = `Could not start the AR session: ${err?.message ?? err}`;
  } finally {
    enterButton.disabled = false;
  }
}

renderer.xr.addEventListener('sessionstart', () => {
  const session = renderer.xr.getSession();
  xrState.session = session;
  xrState.placed = false;
  xrState.targetHz = session.frameRate || 72;

  // Passthrough must show through. A scene background would paint over the
  // whole room, which is the single most common way an immersive-ar scene
  // comes out looking like VR.
  scene.background = null;
  renderer.setClearAlpha(0);
  controls.enabled = false;
  enterButton.hidden = true;

  session.addEventListener('frameratechange', () => {
    xrState.targetHz = session.frameRate || xrState.targetHz;
  });

  console.info(
    `[xr] immersive-ar started · features: ${[...(session.enabledFeatures ?? ['unreported'])].join(', ')} · ` +
      `${xrState.targetHz}Hz target`,
  );
});

renderer.xr.addEventListener('sessionend', () => {
  xrState.session = null;
  xrState.placed = false;
  scene.background = new THREE.Color(MOCK_BACKGROUND);
  renderer.setClearAlpha(1);
  controls.enabled = true;
  closeFocus();
  // The layer belongs to the session that just ended; a new session must build
  // a new one, and `layersUnavailable` from the old session must not stick.
  focus.destroyLayer();
  focus.layersUnavailable = false;
  if (xrState.supported) enterButton.hidden = false;
});

/* ------------------------------------------------------------ render loop -- */

const frameStats = { frames: 0, lastMs: 0, maxMs: 0, longFrames: 0, fps: 0, droppedFrames: 0 };
/** §7/M5: a frame is "dropped" when the interval to the previous one exceeds
 *  1.5× the target — 20.8 ms at 72 Hz. Measured on the interval, not on our own
 *  draw cost, because a frame the compositor missed is one we never saw. */
const DROP_FACTOR = 1.5;
let lastFrameAt = 0;
let windowStart = 0;
let windowFrames = 0;
let windowDropped = 0;
let windowWorst = 0;

const _listenerPos = new THREE.Vector3();
const _listenerFwd = new THREE.Vector3();
const _listenerUp = new THREE.Vector3();
const _listenerQuat = new THREE.Quaternion();

function tick(now, frame) {
  const started = performance.now();

  // The ring is anchored to local-floor on the first frame that actually has a
  // viewer pose — `sessionstart` fires before one exists.
  if (frame && !xrState.placed) {
    xrState.placed = true;
    ring.reanchor(viewerCamera());
  }
  // Asked every frame until it is settled one way or the other, because the
  // render state three.js sets is not observable on the frame it sets it.
  // Returns immediately once a layer exists or the fallback has been chosen.
  if (frame && !focus.layer && !focus.layersUnavailable) {
    focus.ensureLayer(renderer.xr.getSession(), renderer.xr.getReferenceSpace());
  }

  const dt = lastFrameAt ? Math.min(0.1, (now - lastFrameAt) / 1000) : 0;
  input.update(dt);
  if (!renderer.xr.isPresenting) controls.update();
  ring.update(now); // alerting panels and an in-flight collapse; nothing else

  // The audio listener rides the viewer pose. This is the only per-frame work
  // in the client besides rendering and the alert pulse (§8).
  if (audio.ctx) {
    const cam = viewerCamera();
    cam.getWorldPosition(_listenerPos);
    cam.getWorldDirection(_listenerFwd);
    cam.getWorldQuaternion(_listenerQuat);
    _listenerUp.set(0, 1, 0).applyQuaternion(_listenerQuat);
    audio.setListener(_listenerPos, _listenerFwd, _listenerUp);
  }

  // Repaint the focus canvas if it changed, and blit it into the quad layer.
  // Must precede the scene render: it binds its own render target.
  focus.update(frame);

  renderer.render(scene, camera);

  /* ---------------------------------------------------------- frame timing -- */

  const ms = performance.now() - started;
  frameStats.frames += 1;
  frameStats.lastMs = ms;
  windowFrames += 1;

  if (lastFrameAt) {
    const interval = now - lastFrameAt;
    const threshold = (DROP_FACTOR * 1000) / xrState.targetHz;
    // The first frames compile shaders and upload the atlas, and are skipped
    // ENTIRELY — including from `worst`. Counting them only in the worst-case
    // made the summary contradict itself: "0 over 20.8ms · worst 83.4ms".
    if (frameStats.frames > 2) {
      windowWorst = Math.max(windowWorst, interval);
      if (interval > threshold) {
        frameStats.droppedFrames += 1;
        frameStats.longFrames += 1;
        windowDropped += 1;
      }
    }
  }
  lastFrameAt = now;
  if (frameStats.frames > 1) frameStats.maxMs = Math.max(frameStats.maxMs, ms);

  if (!windowStart) windowStart = now;
  if (now - windowStart >= 5000) {
    const seconds = (now - windowStart) / 1000;
    frameStats.fps = Math.round(windowFrames / seconds);
    if (DEV) {
      const threshold = ((DROP_FACTOR * 1000) / xrState.targetHz).toFixed(1);
      console.info(
        `[xr] ${seconds.toFixed(0)}s · ${windowFrames} frames · ${frameStats.fps}fps · ` +
          `${windowDropped} over ${threshold}ms · worst ${windowWorst.toFixed(1)}ms · ` +
          `draw ${frameStats.lastMs.toFixed(1)}ms · atlas ${atlas.redraws} · focus ${focus.redraws}`,
      );
    }
    windowStart = now;
    windowFrames = 0;
    windowDropped = 0;
    windowWorst = 0;
  }
}

/* ----------------------------------------------------------------- alerts -- */

/**
 * Flip a live panel to `needs_you`. Bound to `n`, and the on-demand alert
 * trigger the demo runbook needs (§15) so the alert state can be shown without
 * waiting for a real agent to block on a question.
 *
 * Against a real server this is a *local* change: the next delta from the
 * server is authoritative and will undo it. That is the correct behaviour —
 * the client must never invent board state that outlives the server's view.
 */
function flipRandomToNeedsYou() {
  const candidates = ring.visible.filter((s) => s.state !== 'needs_you');
  if (!candidates.length) return null;
  const session = candidates[Math.floor(Math.random() * candidates.length)];
  ring.applyDelta({
    changed: [{ ...session, state: 'needs_you', summary: 'your call', unread: (session.unread ?? 0) + 1 }],
  });
  return session.id;
}

/**
 * Desktop-only keys. Everything semantic — focus, next alert, collapse,
 * recenter, rotate, scroll, push-to-talk — is owned by input.js so that one
 * map covers the controller and the keyboard. What is left here is the mock's
 * own scaffolding, which has no controller equivalent.
 */
addEventListener('keydown', (event) => {
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  // Any key is a user gesture, and the chime has to be audible the first time
  // `n` is pressed rather than the second.
  audio.unlock();
  switch (event.key) {
    case 'n':
      flipRandomToNeedsYou();
      break;
    case 'p':
      ring.nextPage();
      break;
    case 'v':
      setPose('viewer');
      break;
    case 'i':
      setPose('inspect');
      break;
    default:
  }
});

/* ------------------------------------------------------------- mock feed -- */

const iso = (offsetMs = 0) => new Date(Date.now() + offsetMs).toISOString();

/**
 * Three sessions, one per role, with the shapes the projector will emit
 * (plan §6). Ids sort planner → coder → runner, and the ring orders by id, so
 * the arc reads left → right in that order every single load.
 */
const MOCK_SESSIONS = [
  {
    id: 'ses_01k4a1planner',
    role: 'planner',
    title: 'auth refactor',
    state: 'working',
    summary: 'drafting plan',
    taskId: 'tsk_01k4aa10',
    colorKey: 'planner',
    lastActivityAt: iso(),
    unread: 0,
  },
  {
    id: 'ses_01k4b2coder',
    role: 'coder',
    title: 'token store',
    state: 'working',
    summary: 'claimed tsk_01k4 — wiring JWT', // plan §6's own example: 29 chars, truncates to 13
    taskId: 'tsk_01k4bb20',
    colorKey: 'coder',
    lastActivityAt: iso(),
    unread: 2,
  },
  {
    id: 'ses_01k4c3runner',
    role: 'runner',
    title: 'make check',
    state: 'waiting',
    summary: '214 passed',
    taskId: 'tsk_01k4cc30',
    colorKey: 'runner',
    lastActivityAt: iso(),
    unread: 0,
  },
];

/**
 * Forty lines of plausible session output, so the focus tier can be judged
 * without a server. Deliberately includes lines longer than the 40-column
 * measure and a bare path longer than the whole column, because wrapping and
 * hard-breaking are exactly what would go unnoticed on tidy sample text.
 */
function mockTranscript(session) {
  const lines = [
    `$ aisquare task claim ${session.taskId}`,
    `✓ claimed: ${session.taskId} [doing]`,
    '',
    `> ${session.summary}`,
    'reading src/aisquare/auth/tokens.py',
    'reading src/aisquare/auth/__init__.py',
    'src/aisquare/web/xr/verylongpathsegmentthatcannotbebrokenonaspace.js',
    '',
    'The token store currently writes through to disk on every refresh, which is',
    'why the second worker sees a stale value.',
    '',
    '$ rg -n "refresh_token" src | head',
    'src/aisquare/auth/tokens.py:41:def refresh_token(...)',
    'src/aisquare/auth/tokens.py:88:    return refresh_token(',
    'src/aisquare/auth/store.py:12:REFRESH_KEY = "refresh"',
    '',
    'editing src/aisquare/auth/store.py',
    '  + a single writer, guarded by the existing lock',
    '  - the per-call open()/close() pair',
    '',
    '$ make check',
    'ruff format --check src tests ... 270 files',
    'ruff check src tests ......... All checks passed!',
    'mypy ........................ Success: no issues',
    'pytest ...................... 214 passed in 18.3s',
    '',
    '✓ make check green',
    '',
    '$ git commit -m "fix(auth): one writer for the token store"',
    '[feat/token-store 4f1c8ab] fix(auth): one writer',
    ' 2 files changed, 31 insertions(+), 18 deletions(-)',
    '',
    'Pushed and opened PR #181.',
    '',
    'One open question before I mark this for review: the lock is per-process,',
    'so two aisquare processes on the same machine still race. Worth fixing now,',
    'or is single-process the documented contract?',
    '',
    `state: ${session.state}`,
    '',
  ];
  return lines;
}

/**
 * The scripted feed: one snapshot and three deltas, on the schedule the
 * acceptance criteria name. Four frames in, four atlas redraws out — and then
 * the count must sit still, which is the cheapest possible proof that nothing
 * is repainting the texture per frame.
 */
function startMockFeed() {
  const timers = [];
  ring.applySnapshot(MOCK_SESSIONS);

  timers.push(
    setTimeout(() => {
      // A coder session blocks on a question: the only state allowed to animate.
      const coder = ring.sessions.get('ses_01k4b2coder');
      ring.applyDelta({
        changed: [{ ...coder, state: 'needs_you', summary: 'approve diff?', unread: 5, lastActivityAt: iso() }],
      });
    }, 5000),
  );

  timers.push(
    setTimeout(() => {
      // The runner finished and exited. Removals arrive by id (plan §6).
      ring.applyDelta({ removed: ['ses_01k4c3runner'] });
    }, 10000),
  );

  timers.push(
    setTimeout(() => {
      // A new session starts. Its id sorts last, so it joins the right-hand end.
      ring.applyDelta({
        changed: [
          {
            id: 'ses_01k4d4coder',
            role: 'coder',
            title: 'xr client',
            state: 'working',
            summary: 'ring is up',
            taskId: 'tsk_01k4dd40',
            colorKey: 'coder',
            lastActivityAt: iso(),
            unread: 0,
          },
        ],
      });
    }, 12000),
  );

  return () => timers.forEach(clearTimeout);
}

/* ------------------------------------------------------------------- live -- */

const chip = new ConnectionChip(document.getElementById('chip'));
let net = null;

function startLiveFeed() {
  net = new Net({ chip });

  // Every (re)connection: re-assert whatever the operator is looking at. A
  // focused panel must survive a reconnect, or walking away from the desk for
  // the length of a backoff would silently stop its transcript.
  net.on('open', () => net.subscribe(focus.sessionId));

  // A snapshot is authoritative, always — on a reconnect the client rebuilds
  // from it rather than reconciling against whatever it was holding (§2.6).
  // `applySnapshot` replaces the session map wholesale, so the rebuild is the
  // ordinary path and there is no special reconnect branch to get wrong.
  net.on('snapshot', (msg) => {
    ring.applySnapshot(msg.sessions ?? []);
    focus.setSession(ring.sessions.get(focus.sessionId));
  });
  net.on('delta', (msg) => {
    ring.applyDelta(msg);
    focus.setSession(ring.sessions.get(focus.sessionId));
  });

  net.on('transcript', (msg) => {
    lastFrames.transcript = msg;
    // Frames for a session that is no longer focused are dropped, not buffered:
    // the subscription is already cancelled and the next focus starts clean.
    if (msg.session && msg.session === focus.sessionId) focus.append(msg);
  });

  // Interim STT belongs to the voice pipeline (M6, §10). Kept as the last
  // untouched frame so that task has a seam, and so a server emitting it today
  // is visibly not ignored.
  net.on('stt', (msg) => {
    lastFrames.stt = msg;
  });
  net.on('ack', (msg) => {
    lastFrames.ack = msg;
    if (msg.ok === false) console.warn(`[xr] ack: ${msg.for} rejected — ${msg.detail ?? 'no detail'}`);
  });

  net.connect(true);
}

const lastFrames = { transcript: null, stt: null, ack: null };

/* ------------------------------------------------------------------- boot -- */

/** The handle the ui-tester reads. Getters, not snapshots: a frozen copy of
 *  `atlasRedraws` would read 0 forever and prove nothing. */
window.__xr = {
  scene,
  ring,
  camera,
  renderer,
  controls,
  input,
  audio,
  get focus() {
    return focus;
  },
  get net() {
    return net;
  },
  get atlas() {
    return atlas;
  },
  /** MUST grow only on snapshot/delta. Never per frame, never on rotate,
   *  push–pull, collapse, focus or resize. */
  get atlasRedraws() {
    return atlas.redraws;
  },
  /** The focus tier's equivalent: grows only when the transcript changes. */
  get focusRedraws() {
    return focus.redraws;
  },
  get droppedFrames() {
    return frameStats.droppedFrames;
  },
  get frameStats() {
    return frameStats;
  },
  get lastFrames() {
    return lastFrames;
  },
  get sessions() {
    return ring.ordered;
  },
  get xr() {
    return { ...xrState, presenting: renderer.xr.isPresenting, quadLayer: Boolean(focus.layer) };
  },
  get talk() {
    return { ...talk };
  },
  mock: MOCK,
  flipRandomToNeedsYou,
  toggleFocus,
  enterAR,
  version: { three: THREE.REVISION, ambientFontPx: AMBIENT.fontPx, focusFontPx: FOCUS.fontPx },
  geometry: {
    focus: {
      metres: [FOCUS.width, FOCUS.height],
      pixels: [FOCUS.pixelWidth, FOCUS.pixelHeight],
      cols: FOCUS.cols,
      rows: FOCUS.rows,
      capDeg:
        (2 * Math.atan((FOCUS.fontPx * FOCUS.capEmRatio) / FOCUS.density / 2 / FOCUS.distance) * 180) /
        Math.PI,
    },
  },
};

loadFont().then(() => {
  setupAR();
  if (MOCK) {
    chip.set('mock', 'scripted feed, no socket');
    startMockFeed();
  } else {
    startLiveFeed();
  }
  // setAnimationLoop, not requestAnimationFrame: in an XR session the frames
  // come from the device, and rAF would keep running the desktop clock.
  renderer.setAnimationLoop(tick);
});
