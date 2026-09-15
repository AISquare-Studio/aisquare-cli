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
import { Hud, VoiceCapture } from './voice.js';

const params = hashParams();
const MOCK = params.has('mock');
/**
 * `#audio48` — refuse the 16 kHz AudioContext and make the worklet resample.
 *
 * On Chrome and the Quest browser the context comes up at 16 kHz for the
 * asking, which leaves the worklet's resampler as a pass-through and therefore
 * unexercised on every machine anyone tests on. This flag is how that path gets
 * driven deliberately rather than only on hardware nobody has.
 */
const FORCE_RESAMPLE = params.has('audio48');
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
  refreshSayField();
  // `focus.show` reset the strip; re-light the mic dot if this is the panel the
  // operator is currently talking to, so re-focusing it mid-utterance does not
  // drop the indicator until the next capture-state change.
  syncVoiceDot();
  return sessionId;
}

function closeFocus() {
  if (!focus.open) return;
  focus.hide();
  ring.setFocused(null);
  subscribe(null);
  refreshSayField();
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

/**
 * Push-to-talk (§10, M6).
 *
 * `talk` is the edge record M3 left; `voice` is the capture that hangs off it.
 * The division is worth keeping: `input.js` decides WHEN the operator wants to
 * speak from a trigger or a key, and knows nothing about microphones, while
 * `voice.js` owns the microphone and knows nothing about controllers.
 */
const talk = { active: false, since: 0, source: null };

const hud = new Hud(document.getElementById('toast'));

/**
 * How long a committed transcript stays on the panel before it clears.
 *
 * Long enough to read back what was heard — the operator's only check that ASR
 * got it right — and short enough that it is gone before the agent's reply
 * arrives on the same panel. The ack line that follows it has its own, longer
 * life: it says where the prompt went, which is worth reading after the words
 * themselves have stopped being interesting.
 */
const FINAL_TEXT_MS = 3000;
const ACK_MS = 6000;

/**
 * Voice-strip state, keyed by session id.
 *
 * The strip is one line on one panel, but the feedback on it belongs to the
 * SESSION the microphone was pointed at — not to whatever panel happens to be
 * focused when a late frame lands. So each session's timers and its queued
 * receipt are kept here and only ever DRAWN on the panel while that session is
 * the focused one. The old single-slot state drew A's receipt under B's title,
 * and lost a receipt into a panel the operator had already closed; keying by
 * session is what fixes both.
 */
const voiceState = new Map();
function vs(id) {
  let rec = voiceState.get(id);
  if (!rec) {
    rec = { speechTimer: null, noticeTimer: null, pendingNotice: null };
    voiceState.set(id, rec);
  }
  return rec;
}

/**
 * Utterances whose audio has gone out, oldest first, as `{ session, seq }`.
 *
 * `stt` and `error` frames carry no session id — the server has at most one open
 * utterance per socket — so the client matches them to the FRONT of this queue.
 * An utterance is pushed when its header reaches the wire (the `live`
 * transition) and shifted on its final or its error. Keyed by the `seq` this
 * client chose per press, so a late error for utterance N can be told from the
 * utterance N+1 the operator is already speaking, and does not tear it down.
 */
const utteranceQueue = [];
let lastLiveSeq = 0;

/** The session of the most recent capture: a fallback when the queue is empty,
 *  and what `__xr.voice.speakingAt` reports. */
let speakingAt = null;

/**
 * Put speech for `target` on the strip — or on the HUD when that session is not
 * the focused one — and schedule a final's removal.
 *
 * After `audioEnd` the server sends the final `stt` and then the `ack` a few
 * milliseconds later, so a receipt held behind the final (see `showNotice`) is
 * revealed only once the words have had their three seconds: the operator sees
 * WHAT was heard before WHERE it went, which is their one check that ASR got the
 * sentence right (acceptance criterion 1).
 */
function showSpeech(target, text, final) {
  if (target == null) return;
  const rec = vs(target);
  clearTimeout(rec.speechTimer);
  rec.speechTimer = null;
  const onPanel = target === focus.sessionId;
  // An empty final (silence) must not repaint an already-empty strip, so mark it
  // committed only when there is text — setVoice's compare then sees no change.
  const committed = final && Boolean(text);
  if (onPanel) {
    focus.setVoice({ speech: text, speechFinal: committed });
    // A fresh interim clears any notice sitting on the strip, so the live words
    // are what the operator sees while they are still talking (not a stale ack).
    if (!final && text) focus.setVoice({ notice: '', alert: false });
  } else if (committed) {
    hud.toast(`heard: ${text}`, FINAL_TEXT_MS);
  }
  if (committed) {
    rec.speechTimer = setTimeout(() => {
      rec.speechTimer = null;
      if (target === focus.sessionId) focus.setVoice({ speech: '', speechFinal: false });
      const queued = rec.pendingNotice;
      rec.pendingNotice = null;
      // Re-run through showNotice, not renderNotice: the target may be unfocused
      // now, in which case the receipt belongs on the HUD, and showNotice is what
      // decides panel-vs-HUD. The timer is cleared above, so it will not re-defer.
      if (queued) showNotice(target, queued.text, queued.opts);
    }, FINAL_TEXT_MS);
  }
}

/** Clear a dead half-sentence for `target`: an utterance that ended without a
 *  final (a socket drop, `stt_failed`, `audio_too_long`) leaves its interim on
 *  the strip forever otherwise. */
function clearSpeech(target) {
  if (target == null) return;
  const rec = vs(target);
  clearTimeout(rec.speechTimer);
  rec.speechTimer = null;
  if (target === focus.sessionId) focus.setVoice({ speech: '', speechFinal: false });
}

/**
 * The ack or error line for `target`.
 *
 * Deferred behind that session's final while it is still being read (queued as
 * `pendingNotice`). A problem also gets a HUD copy at once — so an `ok:false`
 * receipt is never discarded unseen the way the old single slot discarded it,
 * and so a message with no focused panel to draw on still reaches the operator.
 */
function showNotice(target, text, opts = {}) {
  const { ms = ACK_MS, hudToo = false } = opts;
  const onPanel = target != null && target === focus.sessionId;
  const rec = target != null ? vs(target) : null;
  const deferred = Boolean(text && rec && rec.speechTimer);
  if (deferred) {
    // The target's final is still being read. Hold the receipt behind it (the
    // final's timer replays it), so it does not clobber the words — including the
    // "heard:" HUD toast for an unfocused session. A PROBLEM still reaches the
    // HUD at once, because an ok:false receipt must never be discarded unseen —
    // and the replay is told so (`hudSent`), so the HUD copy goes up exactly once
    // rather than again when the timer fires, which would also restart its timer
    // and keep a stale message up for the sum of the two.
    rec.pendingNotice = { text, opts: { ...opts, hudToo: false, hudSent: hudToo } };
    if (hudToo) hud.toast(text, ms);
    return;
  }
  // Not deferred: show now — on the panel if that session is focused, else on
  // the HUD (also on the HUD for any problem), unless the HUD already has it.
  if (text && (hudToo || (!onPanel && !opts.hudSent))) hud.toast(text, ms);
  if (target == null) return;
  renderNotice(target, text, opts);
}

/** Draw the notice on the panel when `target` is focused (the HUD copy, if any,
 *  was already sent by `showNotice`), and time it out. */
function renderNotice(target, text, opts = {}) {
  const { alert = false, ms = ACK_MS } = opts;
  if (target !== focus.sessionId) return;
  const rec = vs(target);
  clearTimeout(rec.noticeTimer);
  rec.noticeTimer = null;
  focus.setVoice({ notice: text, alert });
  if (!text) return;
  rec.noticeTimer = setTimeout(() => {
    rec.noticeTimer = null;
    if (target === focus.sessionId) focus.setVoice({ notice: '', alert: false });
  }, ms);
}

/** Re-assert the mic dot after a focus change: `focus.show` resets the strip, so
 *  a panel re-focused while its own capture is live would otherwise lose the dot
 *  until the next capture-state change. */
function syncVoiceDot() {
  focus.setVoice({ live: voice.state.live && voice.state.session === focus.sessionId });
}

const voice = new VoiceCapture({
  // `net` is created later by `startLiveFeed`, and under `#mock` never at all.
  // A thunk rather than the value, so this binding is not captured as null.
  net: { get isOpen() { return Boolean(net?.isOpen); },
         audioStart: (...a) => Boolean(net?.audioStart(...a)),
         audioEnd: (...a) => Boolean(net?.audioEnd(...a)),
         sendBinary: (...a) => Boolean(net?.sendBinary(...a)) },
  hud,
  focusedSession: () => focus.sessionId,
  forceResample: FORCE_RESAMPLE,
  // The mic dot follows capture and nothing else — on at the press, off at the
  // release. No timer, no ramp: §8 allows one animated thing in this scene and
  // it is the alert bar.
  onChange: (state) => {
    if (state.capturing) {
      speakingAt = state.session;
      // A fresh utterance starts from a clean strip FOR ITS OWN SESSION: the
      // last one's words and receipt belong to a sentence that is over.
      const rec = vs(state.session);
      clearTimeout(rec.speechTimer);
      rec.speechTimer = null;
      clearTimeout(rec.noticeTimer);
      rec.noticeTimer = null;
      rec.pendingNotice = null;
      if (state.session === focus.sessionId) {
        focus.setVoice({ notice: '', alert: false, speech: '', speechFinal: false });
      }
    }
    // Enqueue the utterance once, when its header reaches the wire (`live`), so a
    // late final/error can be matched to the right session and seq even after the
    // focus and the current capture have moved on.
    if (state.live && state.seq !== lastLiveSeq) {
      lastLiveSeq = state.seq;
      utteranceQueue.push({ session: state.session, seq: state.seq });
    }
    // `state.live`, not `state.capturing`: the dot means audio is reaching the
    // server, which on a first press is later than the trigger going down by
    // however long the permission prompt was up. And only while the panel being
    // spoken to is the one in front of the operator.
    focus.setVoice({ live: state.live && state.session === focus.sessionId });
  },
  // Every capture-side failure voice.js detects. It has already put this on the
  // HUD; draw it on the focus PANEL too, because the HUD is not composited in an
  // immersive session without dom-overlay — the exact rule main.js already
  // applies to the server's stt_unavailable, now applied to mic-blocked, no mic,
  // not-connected, the disconnect, the worklet failure and the cap (§10). Routed
  // to the utterance's own session so it never lands under another panel's title.
  report: (message, { session } = {}) => {
    const target = session ?? speakingAt ?? focus.sessionId;
    renderNotice(target, message, { alert: true, ms: 6000 });
  },
});

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
  // Synchronous call, so the AudioContext inside it is still constructed within
  // this gesture — a Quest will not start one otherwise (§13, and voice.js).
  voice.press();
});
input.on('talkEnd', ({ source }) => {
  if (!talk.active) return;
  talk.active = false;
  voice.release();
  if (DEV) console.info(`[xr] talkEnd (${source}) after ${Math.round(performance.now() - talk.since)}ms`);
});
input.on('muteToggle', () => {
  // §9 maps X to "mute / unmute readout". There is no readout — text-to-speech
  // is item 2 on the §12 cut list — so this is deliberately inert. The event is
  // wired so the binding is provably live and the voice task has a seam.
  if (DEV) console.info('[xr] muteToggle (no readout to mute yet)');
});

/* ------------------------------------------------------------ typed path -- */

/**
 * The typed field (§10: "Keep the typing path: repo names, branch names and
 * identifiers will be mangled by ASR").
 *
 * This is the ONLY caller of `net.prompt` in the client, and that is the
 * invariant that keeps voice from double-sending: the server turns a final
 * transcript into a prompt on its own, so the voice path must never produce
 * one. Typing is the one way a `{t:"prompt"}` frame leaves this page.
 *
 * Availability in the headset is decided by `dom-overlay`, which §13 warns is
 * optional like `layers` — so it is requested optionally, checked on
 * `sessionstart`, and the field is hidden rather than left as a control that
 * cannot be reached. On the desktop it is always there.
 */
const sayForm = document.getElementById('say');
const sayText = document.getElementById('say-text');
const saySend = document.getElementById('say-send');

/** Enabled when there is both a panel to talk to and a socket to talk on — but
 *  never disabled out from under an operator who is typing in it.
 *
 *  Disabling a focused element moves focus to <body>, and every keystroke after
 *  that reaches input.js's window shortcut map: a reconnect mid-sentence turned
 *  "fix the retry test" into recenter / push-to-talk / select-and-close. So a
 *  field (or its send button) that currently has focus stays enabled through a
 *  drop; a submit attempted while the socket is still down fails visibly (the
 *  submit handler shows "not connected"), and `net.on('open'/'status')` re-runs
 *  this the instant the socket is back. */
function refreshSayField() {
  const ready = Boolean(focus.sessionId) && (MOCK || Boolean(net?.isOpen));
  const typing = document.activeElement === sayText || document.activeElement === saySend;
  sayText.disabled = !ready && !typing;
  saySend.disabled = !ready && !typing;
  sayText.placeholder = focus.sessionId
    ? `prompt ${ring.sessions.get(focus.sessionId)?.title ?? focus.sessionId}`
    : 'focus a panel, then type to send it a prompt';
}

sayForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const text = sayText.value.trim();
  const session = focus.sessionId;
  if (!text || !session) return;
  if (MOCK) {
    hud.toast('#mock has no server to send to', 2000);
    return;
  }
  if (!net?.prompt(session, text)) {
    showNotice(session, 'not connected — the prompt was not sent', { alert: true, hudToo: true });
    return;
  }
  // Cleared optimistically: the ack that follows says where it landed, and a
  // field that still held the text would invite a second send of the same line.
  sayText.value = '';
});

/**
 * The whole form owns the keyboard while any of it has focus.
 *
 * `input.js` binds its whole key map on `window` in the bubble phase, so a
 * keystroke into this form would reach it too: typing "test" would fire
 * push-to-talk on the `t`, and Enter is bound to `select`, which toggles the
 * focus panel — so submitting a prompt would close or re-target the panel it was
 * aimed at. The listener is on the FORM, not just the text input, because Enter
 * pressed while the send BUTTON has keyboard focus (Tab to it, or a prior click)
 * bubbles from the button; a listener on the input alone never sees it, and the
 * window binding fired `select` before the button submitted — sending the prompt
 * to whatever panel the gaze tie then resolved to.
 *
 * Only KEYDOWN is stopped, not keyup. Push-to-talk ends on the window `keyup`
 * for `t`, and if the field swallowed that keyup a `t` still held when focus
 * moved into the field would latch the mic on to voice's cap. Stopping only
 * keydown means a typed `t` never STARTS push-to-talk (its keydown is caught
 * here) while a held `t`'s release still ENDS it (its keyup reaches window,
 * where `talkEnd` is a no-op unless a hold is actually active).
 *
 * The form still submits: propagation and default actions are different things,
 * and implicit form submission is the latter — only `preventDefault` would have
 * cancelled it, and this is not that.
 */
sayForm.addEventListener('keydown', (event) => event.stopPropagation());

/* --------------------------------------------------------------- AR entry -- */

const xrState = { supported: false, session: null, placed: false, targetHz: 72, domOverlay: false };

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
      // `dom-overlay` carries the typed field and the HUD into the session.
      // OPTIONAL, for the same reason §13 gives for `layers`: it is not
      // universal, and a hard dependency would refuse the session outright on a
      // device that simply lacks it — trading the whole demo for one control.
      optionalFeatures: ['layers', 'hand-tracking', 'anchors', 'dom-overlay'],
      // Only the in-session controls (the voice HUD and the typed field), not the
      // whole <body>: handing the page over floated #keys and #chip — desktop
      // chrome — over AR passthrough on a device that grants the overlay.
      domOverlay: { root: document.getElementById('xr-overlay') },
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

  // Whether the typed path exists in the headset is not a guess: ask the
  // session. Without the overlay the field is unreachable — invisible, and not
  // hit-testable by a controller ray, which only sees the 3D scene — so it is
  // hidden rather than left as a control that swallows presses. Voice still
  // works; this is the fallback §10 keeps for names ASR mangles, and on a
  // device without the overlay that fallback is the desktop tab.
  xrState.domOverlay = Boolean(session.enabledFeatures?.includes('dom-overlay'));
  sayForm.hidden = !xrState.domOverlay;

  console.info(
    `[xr] immersive-ar started · features: ${[...(session.enabledFeatures ?? ['unreported'])].join(', ')} · ` +
      `${xrState.targetHz}Hz target · typed field ${xrState.domOverlay ? 'available' : 'unavailable (no dom-overlay)'}`,
  );
});

renderer.xr.addEventListener('sessionend', () => {
  xrState.session = null;
  xrState.placed = false;
  // Out of the headset the overlay question does not arise: this is a web page.
  sayForm.hidden = false;
  // A trigger still held as the session ends must CLOSE the utterance, not abort
  // it. three.js disconnects the controllers BEFORE it fires 'sessionend', so
  // input.js has usually already emitted talkEnd and voice.release() has armed
  // the drain — calling abort here would cancel that drain and drop the finished
  // sentence with no audioEnd (the next press then makes the server discard the
  // orphan and reload whisper). So close a still-live capture, and leave a drain
  // already in flight to finish and send its audioEnd on the socket that is, for
  // a moment yet, still open.
  if (voice.capturing) voice.release();
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
  net.on('open', () => {
    // The server restarts transcript `seq` at 1 on every connection and every
    // subscribe, and the client never reset its high-water mark — so after any
    // drop, `append`'s `seq <= this.seq` replay guard silently discarded the new
    // stream's first N records and the panel froze while the chip read
    // "connected". Reset before re-subscribing; the server replays its backlog,
    // so the transcript rebuilds from the current tail.
    focus.resetSeq();
    net.subscribe(focus.sessionId);
    refreshSayField();
  });
  // Reconnecting, gone, or back: the field is only usable with a live socket.
  net.on('status', refreshSayField);

  // A snapshot is authoritative, always — on a reconnect the client rebuilds
  // from it rather than reconciling against whatever it was holding (§2.6).
  // `applySnapshot` replaces the session map wholesale, so the rebuild is the
  // ordinary path and there is no special reconnect branch to get wrong.
  net.on('snapshot', (msg) => {
    ring.applySnapshot(msg.sessions ?? []);
    // A focused session that is no longer on the board has ended: close the
    // panel rather than leave typing and push-to-talk aimed at a dead session
    // (the server would accept and ack a prompt for it, routing it to a role).
    if (focus.sessionId && !ring.sessions.has(focus.sessionId)) closeFocus();
    else focus.setSession(ring.sessions.get(focus.sessionId));
  });
  net.on('delta', (msg) => {
    ring.applyDelta(msg);
    if (focus.sessionId && msg.removed?.includes(focus.sessionId)) {
      // Same as above, on the transition: the focused session was removed.
      closeFocus();
      return;
    }
    focus.setSession(ring.sessions.get(focus.sessionId));
  });

  net.on('transcript', (msg) => {
    lastFrames.transcript = msg;
    // Frames for a session that is no longer focused are dropped, not buffered:
    // the subscription is already cancelled and the next focus starts clean.
    if (msg.session && msg.session === focus.sessionId) focus.append(msg);
  });

  /**
   * Speech, interim then final (§10).
   *
   * This is the feedback the plan is emphatic about: "without that feedback the
   * operator cannot tell whether the mic is live, and will repeat themselves".
   * It goes up the instant the first interim arrives — roughly a second into
   * the sentence, which is the server's `INTERIM_SECONDS` — and the final one
   * stays just long enough to read back what was heard.
   *
   * Note what does NOT happen here: no `prompt` is sent. The server routes the
   * final transcript as the prompt itself, so echoing one back would deliver
   * the operator's sentence to their agent twice.
   */
  net.on('stt', (msg) => {
    lastFrames.stt = msg;
    const text = String(msg.text ?? '');
    // The frame belongs to the oldest open utterance (FIFO on one socket).
    // `showSpeech` decides panel vs HUD from whether that session is focused, so
    // a final for A that arrives after the operator has focused B lands on the
    // HUD, not under B's title.
    const target = utteranceQueue[0]?.session ?? speakingAt;
    showSpeech(target, text, Boolean(msg.final));
    if (msg.final) utteranceQueue.shift();
  });

  /**
   * Where the prompt went (§6's one protocol addition).
   *
   * `detail` is the server's own words — "typed into its pane (it was waiting)"
   * or "filed as board note #293 to coder" — and it is repeated rather than
   * re-worded, because the difference between the two is the thing the operator
   * wants and any paraphrase here would drift from what `fleet.tell` did.
   */
  net.on('ack', (msg) => {
    lastFrames.ack = msg;
    const detail = String(msg.detail ?? '');
    const line = msg.ok ? `sent — ${detail}` : `not sent — ${detail || 'no detail'}`;
    // Routed to the ack's OWN session, and an `ok:false` receipt always gets a
    // HUD copy so it is never discarded unseen (fleet.tell returns ok:false
    // whenever the agent is not waiting — the common case, so this is not rare).
    const target = msg.session ?? speakingAt ?? focus.sessionId;
    showNotice(target, line, { alert: !msg.ok, hudToo: !msg.ok });
    if (msg.ok === false) console.warn(`[xr] ack: ${msg.for} rejected — ${detail || 'no detail'}`);
  });

  /**
   * A server error about something the client asked for (§6).
   *
   * EVERY code is made visible — a silent server error was how a typed prompt
   * lost to a store failure (`internal`), or a panel stuck forever on "waiting
   * for transcript…" (`no_transcript` for an MCP session with no transcript),
   * left the operator with nothing to read. The codes split three ways:
   *
   *  - `auth*` belongs to the connection chip (net.js), which already decides
   *    terminal-vs-transient; drawing it here too would double the message.
   *  - A VOICE-utterance error (`stt*`, `audio*`, `session_mismatch`) ends one
   *    utterance: match it to the front of the queue, clear that session's dead
   *    interim, and abort the LIVE capture only when the error is FOR it — a late
   *    error for utterance N must not tear down the N+1 the operator is speaking.
   *  - Anything else is a subscribe/prompt error: show it on the focused panel
   *    and the HUD.
   *
   * `message` is the server's own words, repeated rather than re-worded — it is
   * written for a human, and this client's paraphrase would only drift from it.
   */
  net.on('error', (msg) => {
    lastFrames.error = msg;
    const code = String(msg.code ?? '');
    const message = String(msg.message ?? code);
    if (code.startsWith('auth')) return; // the connection chip owns auth codes
    const isVoice =
      code.startsWith('stt') || code.startsWith('audio') || code === 'session_mismatch';
    if (isVoice) {
      const errored = utteranceQueue.shift() ?? { session: speakingAt ?? focus.sessionId, seq: -1 };
      // Abort the live capture only if this error is for it — otherwise a late
      // error for a released utterance would kill the one being spoken now.
      if (!voice.state.capturing || voice.state.seq === errored.seq) voice.abort(null);
      clearSpeech(errored.session);
      if (code === 'stt_unavailable') {
        // The full message carries the doctor's fix line, so it is PINNED on the
        // HUD where it can be read and acted on. A short, NEUTRAL pointer goes on
        // the panel (the HUD is not composited without dom-overlay) — neutral
        // because the cause varies (extra missing, unknown model, load failure)
        // and naming "not installed" was wrong for the last two.
        hud.pin(message);
        showNotice(errored.session, 'speech is unavailable on the host — see the message on screen', {
          alert: true,
          ms: 8000,
        });
      } else {
        showNotice(errored.session, message, { alert: true, ms: 8000, hudToo: true });
      }
    } else {
      showNotice(focus.sessionId, message, { alert: true, ms: 8000, hudToo: true });
    }
  });

  /**
   * The socket went away mid-sentence (§11/M7).
   *
   * The server forgets a burst whose connection closed, so there is no
   * transcript coming and nothing to wait for. Saying so is the whole point:
   * silence here is indistinguishable from a slow decode, and the operator
   * would stand there waiting for words that were discarded on the far end. The
   * old handler said so only through `voice.abort`, which toasts ONLY while still
   * capturing — so a drop during the drain, or during the server's decode after
   * release, was silent. Here the message is unconditional (when anything was in
   * flight), and each dead session's interim is cleared.
   */
  net.on('drop', () => {
    const wasActive =
      voice.state.capturing || voice.state.draining || utteranceQueue.length > 0;
    const targets = utteranceQueue.length
      ? utteranceQueue.map((u) => u.session)
      : speakingAt
        ? [speakingAt]
        : [];
    utteranceQueue.length = 0;
    voice.abort(null);
    for (const target of targets) clearSpeech(target);
    if (wasActive) {
      hud.toast('dropped — say it again', ACK_MS);
      const focused = focus.sessionId;
      if (focused && (targets.length === 0 || targets.includes(focused))) {
        renderNotice(focused, 'dropped — say it again', { alert: true, ms: ACK_MS });
      }
    }
    speakingAt = null;
    refreshSayField();
  });

  net.connect(true);
}

const lastFrames = { transcript: null, stt: null, ack: null, error: null };

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
  /**
   * The focus tier's equivalent. Grows only when the panel's contents change.
   *
   * Voice shares that canvas, so an interim `stt` frame necessarily redraws it.
   * `voiceRedraws` counts exactly those, so the pre-voice figure is still
   * recoverable as `focusRedraws - voiceRedraws`, and with the mic idle the two
   * behave precisely as they did before this milestone.
   */
  get focusRedraws() {
    return focus.redraws;
  },
  /** Of `focusRedraws`, the ones the voice strip caused. Two per utterance for
   *  the dot alone — on, then off — because it toggles and does not pulse. */
  get voiceRedraws() {
    return focus.voiceRedraws;
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
  /** Capture state: whether the mic is live, the frame and byte counters for
   *  the current burst, and whether the worklet is resampling or passing
   *  through. Every frame but the last is 640 bytes (20 ms); the last one of an
   *  utterance is the worklet's flush of a PARTIAL frame, so after a release
   *  `(frames - 1) * 640 < bytes <= frames * 640` — not `bytes / 640 === frames`,
   *  which only holds mid-hold, before the tail has crossed. (A measured burst:
   *  35 frames, 22272 bytes.) */
  get voice() {
    return { ...voice.state, speakingAt };
  },
  get hud() {
    return { text: hud.text, pinned: hud.pinned };
  },
  /** Present and reachable? In a session that is `dom-overlay`'s answer. */
  get typedPath() {
    return {
      present: !sayForm.hidden,
      enabled: !sayText.disabled,
      domOverlay: xrState.domOverlay,
    };
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
