/**
 * Entry point: scene, camera, render loop, and the wiring from the wire
 * protocol to the ring.
 *
 * This is milestone M2 (plan §11) and deliberately contains no WebXR at all:
 * no `navigator.xr.requestSession`, no controllers, no voice. M2 exists because
 * every layout and data problem is an order of magnitude cheaper to solve on a
 * desktop than in a headset, so the ring is made to work here first and the
 * headset session (M3) is dropped in on top of a scene already known-good.
 *
 * Two feeds drive the same ring: a real websocket, and — under `#mock` — a
 * scripted in-memory feed, so the whole client is developable and demonstrable
 * before the server branch lands.
 */

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

import { MOCK_BACKGROUND, RING, AMBIENT, ambientFont } from './style.js';
import { Atlas } from './panel.js';
import { Ring } from './ring.js';
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

let renderer;
try {
  renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
} catch (err) {
  notice.hidden = false;
  notice.textContent = 'WebGL is unavailable in this browser, so the ring cannot render.';
  throw err;
}
// Above 2 the cost is real and the gain is not; the atlas is sampled ~1:1.
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;

const scene = new THREE.Scene();
// A neutral mid-grey stands in for passthrough. Never black: the plan is
// explicit that a palette evaluated in a dim room is a palette that fails over
// a lit one (§8/§13), and a black background would flatter these panels.
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
 * angular sizes in the headset. That is the pose to judge the 1.5° floor from.
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

addEventListener('resize', () => {
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
  // Note: no ring.layout() here. Resizing changes no data, and a redraw would
  // make `atlasRedraws` grow for a reason that has nothing to do with the feed.
});

/* ------------------------------------------------------------ render loop -- */

const frameStats = { frames: 0, lastMs: 0, maxMs: 0, longFrames: 0, fps: 0 };
const LONG_FRAME_MS = 20; // 50 fps — well inside the 72 Hz headset budget (§7)
let loggedLongFrames = 0;
let fpsWindowStart = performance.now();
let fpsWindowFrames = 0;

function tick(now) {
  const started = performance.now();

  controls.update();
  ring.update(now); // only alerting panels do anything
  renderer.render(scene, camera);

  const ms = performance.now() - started;
  frameStats.frames += 1;
  frameStats.lastMs = ms;
  fpsWindowFrames += 1;
  if (now - fpsWindowStart >= 1000) {
    frameStats.fps = Math.round((fpsWindowFrames * 1000) / (now - fpsWindowStart));
    fpsWindowStart = now;
    fpsWindowFrames = 0;
  }

  // Frame 1 compiles shaders and uploads the atlas; counting it as a dropped
  // frame would just train you to ignore the warning.
  if (frameStats.frames > 1) {
    frameStats.maxMs = Math.max(frameStats.maxMs, ms);
    if (ms > LONG_FRAME_MS) {
      frameStats.longFrames += 1;
      // Throttled: a genuinely slow machine would otherwise fill the console
      // and bury whatever you were actually reading.
      if (DEV && loggedLongFrames < 20) {
        loggedLongFrames += 1;
        console.warn(`[xr] long frame ${ms.toFixed(1)}ms (frame ${frameStats.frames})`);
        if (loggedLongFrames === 20) console.warn('[xr] further long-frame logs suppressed');
      }
    }
  }

  requestAnimationFrame(tick);
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

addEventListener('keydown', (event) => {
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  switch (event.key) {
    case 'n':
      flipRandomToNeedsYou();
      break;
    case 'r':
      ring.recenter();
      break;
    case '[':
      ring.rotate(-Math.PI / 18);
      break;
    case ']':
      ring.rotate(Math.PI / 18);
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

  // Every (re)connection: ask for ambient only. The focus-tier transcript
  // subscription is M4's, and asking for it now would stream text nothing reads.
  net.on('open', () => net.subscribe(null));

  // A snapshot is authoritative, always — on a reconnect the client rebuilds
  // from it rather than reconciling against whatever it was holding (§2.6).
  // `applySnapshot` replaces the session map wholesale, so the rebuild is the
  // ordinary path and there is no special reconnect branch to get wrong.
  net.on('snapshot', (msg) => ring.applySnapshot(msg.sessions ?? []));
  net.on('delta', (msg) => ring.applyDelta(msg));

  // Transcript and interim STT belong to the focus tier (M4) and the voice
  // pipeline (M6). Kept as the last frame of each so those tasks have a seam
  // to pick up, and so a server emitting them today is visibly not ignored.
  net.on('transcript', (msg) => {
    lastFrames.transcript = msg;
  });
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
  get net() {
    return net;
  },
  get atlas() {
    return atlas;
  },
  get atlasRedraws() {
    return atlas.redraws;
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
  mock: MOCK,
  flipRandomToNeedsYou,
  version: { three: THREE.REVISION, ambientFontPx: AMBIENT.fontPx },
};

loadFont().then(() => {
  if (MOCK) {
    chip.set('mock', 'scripted feed, no socket');
    startMockFeed();
  } else {
    startLiveFeed();
  }
  requestAnimationFrame(tick);
});
