/**
 * Push-to-talk capture: microphone → 16 kHz mono PCM16LE → binary websocket.
 *
 * The operator holds the left trigger (or `t` on the desktop), speaks, and lets
 * go. What leaves this module is exactly what `services/xr/speech.py` is written
 * to eat: 20 ms frames of 16 kHz mono PCM16 little-endian, 320 samples — 640
 * bytes — each, as BINARY websocket messages between a `{t:"audio"}` header and
 * a `{t:"audioEnd"}` (plan §6, §10). Audio is never base64'd into JSON; that
 * would inflate a 32 kB/s stream by a third to travel down the same socket.
 *
 * ## The one thing this module must never do
 *
 * It must not send a `{t:"prompt"}` for a voice utterance. The server routes the
 * FINAL `stt` text as the prompt itself — `_close_utterance` transcribes and
 * calls `_route` with no round trip — so a client that also sent a prompt would
 * deliver the sentence twice, once as speech and once as text. The typed field
 * below is the only thing in this client that sends `prompt`, and it is reached
 * only by typing.
 *
 * ## Frame discipline
 *
 * Every buffer that goes out is an `Int16Array`'s, so its `byteLength` is even
 * by construction — an odd-length frame is half a sample and there is no way to
 * interpret the dangling byte. The server drops a trailing odd byte rather than
 * raising, but only after it has already mis-paired every byte after it: one
 * odd frame shifts the whole rest of the utterance by eight bits and turns
 * speech into noise. Nothing here can produce one: the framer writes into a
 * fixed `Int16Array(320)` and the tail flush is an `Int16Array.slice`.
 *
 * ## Two clocks, one cap
 *
 * The server drops an utterance past 60 s OR past 1.92 MB, whichever comes
 * first (`MAX_UTTERANCE_S` / `MAX_AUDIO_BYTES`). This module stops at
 * {@link MAX_UTTERANCE_MS} — 50 s — so the operator is told by their own client,
 * with the mic dot going out, rather than discovering it from an error frame
 * after the fact. At a fixed 32 kB/s one wall clock governs both caps: 50 s is
 * 1.6 MB, comfortably inside the byte cap too.
 *
 * ## Gestures
 *
 * A Quest will not start an `AudioContext` outside a user gesture, and a
 * context constructed before one can stay `suspended` for the life of the page.
 * So {@link VoiceCapture.press} constructs it SYNCHRONOUSLY, before its first
 * `await`, while the keydown or trigger edge that called it is still the
 * current task. Everything asynchronous — `getUserMedia`, `addModule` — happens
 * after, and only on the first press.
 */

/** The wire format. Mirrors `speech.SAMPLE_RATE` / `FRAME_MS`; changing either
 *  side alone breaks the other, which is why both name their numbers. */
const TARGET_RATE = 16_000;
const FRAME_MS = 20;
const FRAME_SAMPLES = (TARGET_RATE * FRAME_MS) / 1000; // 320 samples = 640 bytes

/**
 * Our own utterance cap, ten seconds inside the server's 60 s.
 *
 * The headroom is not superstition: it covers the drain of frames still in the
 * worklet's port queue plus the difference between a `performance.now()` here
 * and a `time.monotonic()` there. Being cut off by the far end produces an
 * `audio_too_long` error and a dropped sentence; stopping ourselves produces a
 * mic dot that goes out and a line saying why, which is the same information
 * delivered before the operator has finished wasting their breath.
 *
 * Set to 50 s rather than 55 s after review: `performance.now()` is wall time,
 * and on a loaded CPU the worklet's own clock can lag it — 55 s of `startedAt`
 * arithmetic here can already be 56–58 s of audio on the far side, close enough
 * to the server's 60 s that a slow machine loses the whole 45–54 s utterance to
 * `audio_too_long` instead of to our own graceful stop. Ten seconds of slack
 * buys back that margin and still leaves any realistic spoken command room.
 */
const MAX_UTTERANCE_MS = 50_000;

/** How long to wait for the worklet's flush before sending `audioEnd` anyway. */
const DRAIN_TIMEOUT_MS = 250;

/**
 * The AudioWorklet processor, as source.
 *
 * Inline and instantiated from a Blob URL rather than shipped as a file: the
 * client is served off `importlib.resources` out of a possibly-zipped wheel and
 * has no build step, so one fewer fetch that can 404 on a headset behind `adb
 * reverse` is worth the awkwardness of a template string. It is also the only
 * way to interpolate the frame geometry above into the processor instead of
 * writing 320 twice.
 *
 * Downmix then resample, in that order, because resampling per channel and
 * averaging afterwards costs twice the work for the same answer.
 *
 * The resampler is a box average over each output sample's span of input, which
 * for the common 3:1 case is a 3-tap FIR; the fractional accumulator makes
 * 44.1 kHz work too without a second code path. It is a weak anti-alias filter
 * rather than a good one, and the honest numbers are worth writing down —
 * measured against a full-scale tone at 48 kHz, it passes the speech band
 * essentially untouched (440 Hz at 0.999 of input, 3 kHz at 0.949, 7 kHz at
 * 0.739) and rejects 12 kHz — which naive decimation would fold onto 4 kHz at
 * FULL amplitude — to 0.33. About 9.5 dB, not the 40 a real low-pass would give.
 *
 * That is the right trade here for two reasons. This path runs only when the
 * browser REFUSES a 16 kHz AudioContext (see `openContext`), and when it obliges
 * the ratio is 1 and this code is a pass-through over a resample the browser
 * already did properly. And the signal reaching it has been through the AEC that
 * `echoCancellation: true` asks for, which is itself band-limited. A longer
 * window would buy another 9 dB in a path that mostly does not run, at the cost
 * of state in the one loop in this client that must never drop a sample.
 */
const WORKLET_SOURCE = `
const TARGET_RATE = ${TARGET_RATE};
const FRAME_SAMPLES = ${FRAME_SAMPLES};

class PcmFramer extends AudioWorkletProcessor {
  constructor() {
    super();
    // \`sampleRate\` is a global in worklet scope: the context's true rate.
    this.ratio = sampleRate / TARGET_RATE;
    this.phase = 0;
    this.acc = 0;
    this.accN = 0;
    this.held = 0;
    this.frame = new Int16Array(FRAME_SAMPLES);
    this.filled = 0;
    this.stopping = false;
    this.port.onmessage = (event) => {
      if (event.data === 'stop') this.stopping = true;
    };
  }

  /** One 16 kHz sample, clamped and written as PCM16. Posts a frame when full. */
  push(value) {
    // Written so NaN lands on 0 rather than falling through the comparisons.
    let s = value;
    if (!(s > -1)) s = s <= -1 ? -1 : 0;
    else if (s > 1) s = 1;
    this.frame[this.filled++] = s < 0 ? s * 0x8000 : s * 0x7fff;
    if (this.filled === FRAME_SAMPLES) {
      const out = new Int16Array(this.frame);
      this.filled = 0;
      this.port.postMessage(out.buffer, [out.buffer]);
    }
  }

  /** The partial frame, if any, then the marker the main thread waits for.
   *  \`slice\` of an Int16Array is still 2 bytes per sample: never odd. */
  flush() {
    if (this.filled > 0) {
      const tail = this.frame.slice(0, this.filled);
      this.filled = 0;
      this.port.postMessage(tail.buffer, [tail.buffer]);
    }
    this.port.postMessage('done');
  }

  process(inputs) {
    if (this.stopping) {
      this.flush();
      return false; // the node is finished; the graph may drop it
    }
    const input = inputs[0];
    if (!input || !input.length) return true; // disconnected, not ended
    const block = input[0];
    if (!block) return true;
    const channels = input.length;
    for (let i = 0; i < block.length; i++) {
      let x = block[i];
      if (channels > 1) {
        for (let c = 1; c < channels; c++) x += input[c][i];
        x /= channels;
      }
      this.acc += x;
      this.accN++;
      this.phase += 1;
      // \`while\`, not \`if\`: a context slower than 16 kHz owes more than one
      // output sample per input, and the held value makes that a zero-order
      // hold instead of a division by zero.
      while (this.phase >= this.ratio) {
        if (this.accN) {
          this.held = this.acc / this.accN;
          this.acc = 0;
          this.accN = 0;
        }
        this.push(this.held);
        this.phase -= this.ratio;
      }
    }
    return true;
  }
}

registerProcessor('pcm-framer', PcmFramer);
`;

/** What `getUserMedia` is asked for. §10 states the shape; the browser is free
 *  to ignore any of it, which is why the worklet resamples rather than trusts. */
const CONSTRAINTS = {
  audio: { channelCount: 1, sampleRate: TARGET_RATE, echoCancellation: true },
};

/**
 * The mic-permission message. Persistent, not a toast: it names a thing the
 * operator has to go and do, and a line that has scrolled away cannot be acted
 * on. The Quest half matters — the browser's permission prompt appears INSIDE
 * the immersive session, and an operator who dismissed it once will not find it
 * again by taking the headset off.
 */
const MIC_DENIED =
  'Microphone blocked — voice is off. Grant it in the browser (the padlock in the ' +
  'address bar), then press to talk again. On Quest the prompt appears inside the ' +
  'session: answer it there without taking the headset off.';

/* -------------------------------------------------------------------- hud -- */

/**
 * The 2D message surface: transient toasts over a persistent line.
 *
 * Deliberately DOM rather than part of the scene, for the same reason the
 * connection chip is: it survives a WebGL context loss, and a tester reads it
 * without devtools. In an immersive session it is visible only when
 * `dom-overlay` was granted — so everything an operator in the headset must see
 * is ALSO drawn on the focus panel (see `FocusPanel.setVoice`). This surface
 * carries the two cases that have nowhere else to go: a message when no panel
 * is focused, and one that must outlive any single utterance.
 */
export class Hud {
  constructor(el) {
    this.el = el;
    this.pinned = '';
    this.transient = '';
    this.timer = null;
  }

  /** A message that clears itself. Does not disturb anything pinned under it. */
  toast(text, ms = 4000) {
    clearTimeout(this.timer);
    this.transient = String(text ?? '');
    this.timer = setTimeout(() => {
      this.transient = '';
      this.timer = null;
      this.render();
    }, ms);
    this.render();
  }

  /**
   * A message that stays until it is cleared.
   *
   * Clears any toast still showing. A pinned message names something the
   * operator has to go and do — the mic is blocked, speech is not installed —
   * and it is also the newer event; leaving a four-second toast about a dropped
   * utterance on top of it would hide the more important of the two behind the
   * less important, for as long as the timer had left to run.
   */
  pin(text) {
    clearTimeout(this.timer);
    this.timer = null;
    this.transient = '';
    this.pinned = String(text ?? '');
    this.render();
  }

  clear() {
    clearTimeout(this.timer);
    this.timer = null;
    this.transient = '';
    this.pinned = '';
    this.render();
  }

  /**
   * Drop only the pinned line, leaving any toast alone.
   *
   * A pinned message names something the operator had to go and do — the mic is
   * blocked, speech is not installed. Once that thing is done (the very next
   * press captures audio), the instruction is stale and must come down on its
   * own, or it sits over the scene contradicting a mic that is now live. `clear`
   * is too broad: it would also wipe a toast that has nothing to do with the
   * pin. So this clears the pin and nothing else.
   */
  unpin() {
    if (!this.pinned) return;
    this.pinned = '';
    this.render();
  }

  get text() {
    return this.transient || this.pinned;
  }

  render() {
    if (!this.el) return;
    const text = this.text;
    this.el.textContent = text;
    this.el.hidden = !text;
    this.el.dataset.pinned = this.transient ? 'false' : String(Boolean(this.pinned));
  }
}

/* ------------------------------------------------------------------ voice -- */

/**
 * One microphone, one AudioContext, one worklet node — for the life of the page.
 *
 * `getUserMedia` is called ONCE and the `MediaStream` is kept. Re-requesting per
 * utterance would re-prompt on some browsers, and on every browser it costs the
 * device-open latency at the front of the sentence, which is precisely where
 * §10 says the operator loses their first word.
 */
export class VoiceCapture {
  /**
   * @param {object} opts
   * @param {import('./net.js').Net} opts.net
   * @param {Hud} opts.hud
   * @param {() => string|null} opts.focusedSession  the session to talk at
   * @param {(state: object) => void} [opts.onChange] told whenever state moves
   * @param {boolean} [opts.forceResample] skip the 16 kHz context request, so
   *   the worklet's resampler runs on hardware that would not have needed it
   * @param {(message: string, meta?: object) => void} [opts.report] told about
   *   every capture-side failure the operator must see, with the session it
   *   belongs to. main.js draws it on the focus PANEL as well as the HUD,
   *   because the DOM HUD is not composited in an immersive session without
   *   `dom-overlay` — so a failure reported only to the HUD is silent in the
   *   headset. `{ session, pin }` say which panel and whether it was pinned.
   */
  constructor({ net, hud, focusedSession, onChange = () => {}, forceResample = false, report = () => {} }) {
    this.net = net;
    this.hud = hud;
    this.focusedSession = focusedSession;
    this.onChange = onChange;
    this.forceResample = forceResample;
    this.report = report;

    /** Held across the page's life once granted. */
    this.stream = null;
    this.ctx = null;
    this.source = null;
    this.node = null;
    this.moduleUrl = null;
    /** True only for a CONFIRMED block (the Permissions API reports `denied`),
     *  never for a merely dismissed prompt — a dismissal is a `NotAllowedError`
     *  too, and the browser will ask again on the next press, so latching here
     *  would make voice impossible to recover without a reload (which in the
     *  headset means leaving the session). Lifted by a permission change. */
    this.denied = false;
    /** The permission-status object we watch, so `denied` tracks reality even
     *  when the operator grants the mic from the browser's own UI. Resolved
     *  lazily and best-effort — the API is absent on some browsers. */
    this.micPermission = null;
    this.permissionWatched = false;

    /** In-flight setup, shared by concurrent presses so a re-press during the
     *  first press's permission prompt does not start a second getUserMedia or
     *  build a node before the worklet module is registered. A rejection clears
     *  the promise so the NEXT press retries rather than being wedged. */
    this.streamPromise = null;
    this.modulePromise = null;
    this.moduleReady = false;
    /** Set once a browser has refused a mic source at the 16 kHz context rate
     *  (Firefox before 148), so the context is rebuilt at the hardware rate and
     *  the worklet resamples. See ensureGraph. */
    this.avoid16k = false;

    /** True from the press until the release — what the mic dot follows. */
    this.capturing = false;
    /** The session this utterance was opened for. The focus may move mid-
     *  sentence; the audio still belongs to where it started. */
    this.session = null;
    /** The session a failure is reported against: `session` is nulled the moment
     *  an utterance ends, and a failure can surface after that. */
    this.utteranceSession = null;
    this.seq = 0;
    this.bytes = 0;
    this.frames = 0;
    this.startedAt = 0;
    /** Bumped per press so a late `ensure()` can tell it has been superseded. */
    this.generation = 0;
    this.capTimer = null;
    this.drainTimer = null;
    /** True while `closeAfterDrain` is waiting for the worklet's flush, with the
     *  session that drain will close. A re-press must not reuse the stopping
     *  node, so it force-finishes this drain first — see `press`. */
    this.draining = false;
    this.drainSession = null;
    /** True once the header is on the wire: only then may binary frames go. */
    this.headerSent = false;
  }

  get state() {
    return {
      capturing: this.capturing,
      /**
       * Audio is actually reaching the socket — which is NOT the same as the
       * trigger being down, and the mic dot follows this one.
       *
       * On the very first press the two differ by however long the operator
       * takes to answer the permission prompt. Lighting the dot at the press
       * would claim the microphone was live while the browser was still asking
       * for it, which is precisely the lie §10 introduces the indicator to
       * prevent: an operator who trusts a dot that is wrong speaks into a mic
       * that is not recording, and repeats themselves anyway. Every later press
       * reuses the stream and the two are the same instant.
       */
      live: this.capturing && this.headerSent,
      session: this.session,
      seq: this.seq,
      frames: this.frames,
      bytes: this.bytes,
      denied: this.denied,
      contextRate: this.ctx?.sampleRate ?? null,
      resampling: this.ctx ? this.ctx.sampleRate !== TARGET_RATE : null,
    };
  }

  notify() {
    this.onChange(this.state);
  }

  /* ------------------------------------------------------------- gesture -- */

  /**
   * `talkStart`. Returns a promise for tests; nothing awaits it in anger.
   *
   * The refusals come first and cost nothing — a press with no focused panel
   * must not open a microphone, and a press with no socket must not record
   * audio that has nowhere to go.
   */
  async press() {
    if (this.capturing) return;
    // A re-press while the previous utterance is still draining must NOT reuse
    // the stopping worklet node (its port handler is drain-aware and its 250 ms
    // timer would tear down the new utterance). Close the old drain now — its
    // `audioEnd` goes out before the new header, preserving order — and build a
    // fresh node below. The old utterance loses at most its trailing 20 ms, far
    // better than both sentences vanishing. This is the exact XR-runtime case
    // where the left source is removed and re-added mid-hold in one frame.
    if (this.draining) this.finishDrain();

    const session = this.focusedSession();
    if (!session) {
      // §5: the transcript tier is one session at a time, so "talk" has no
      // referent without one. Told on the HUD because there is, by definition,
      // no focus panel to draw it on.
      this.hud.toast('focus a panel first — press f, or A on a controller', 2000);
      return;
    }
    this.utteranceSession = session;
    if (this.denied) {
      // A CONFIRMED block only. `watchPermission` clears this the moment the
      // operator grants the mic, so this branch is not a life sentence.
      this.announce(MIC_DENIED, { pin: true });
      return;
    }
    if (!this.net?.isOpen) {
      this.announce('not connected — nothing to talk to yet');
      return;
    }

    // Synchronous, inside the gesture, before any await. See the module note.
    if (!this.openContext()) return;

    this.capturing = true;
    this.session = session;
    this.seq += 1;
    this.bytes = 0;
    this.frames = 0;
    this.headerSent = false;
    this.startedAt = performance.now();
    const generation = ++this.generation;
    this.notify();

    let ready = false;
    try {
      ready = await this.ensureGraph();
    } catch (err) {
      this.failOpen(err);
      return;
    }
    // The operator may have let go while the permission prompt was up, or the
    // socket may have dropped: either way this utterance is already over and
    // nothing may be sent for it.
    if (!ready || generation !== this.generation || !this.capturing) return;
    if (!this.net.isOpen) {
      this.abort('not connected — say it again');
      return;
    }

    // Header first, always. A binary frame that arrives before it is a "stray"
    // to the server and earns an error frame instead of a transcript.
    if (!this.net.audioStart(this.session, this.seq)) {
      this.abort('not connected — say it again');
      return;
    }
    this.headerSent = true;
    // The mic is live, so any stale mic-permission notice is now false and comes
    // down (the low-severity "pinned message never cleared" case).
    this.hud.unpin();
    this.source.connect(this.node); // frames start here, and not before
    this.notify(); // ...and the dot lights here, for the same reason

    this.capTimer = setTimeout(() => {
      // Our cap, not the server's. See MAX_UTTERANCE_MS.
      this.announce(
        `that ran past ${Math.round(MAX_UTTERANCE_MS / 1000)}s and was sent as-is — ` +
          'let go of the trigger between sentences',
      );
      this.release();
    }, MAX_UTTERANCE_MS);
  }

  /**
   * A failure the operator must see. Goes to the HUD here — the surface that
   * survives a WebGL context loss and that a desktop tester reads — AND through
   * `report`, which main.js also draws on the focus panel, because the HUD does
   * not exist in an immersive session without `dom-overlay`. Without the panel
   * copy, every capture-side failure would be silent in the headset (§10).
   */
  announce(message, { pin = false } = {}) {
    if (pin) this.hud.pin(message);
    else this.hud.toast(message, 5000);
    this.report(message, { session: this.utteranceSession, pin });
  }

  /** `talkEnd`: stop feeding, let the worklet flush, then close the utterance. */
  release() {
    if (!this.capturing) return;
    this.capturing = false;
    this.generation += 1;
    clearTimeout(this.capTimer);
    this.capTimer = null;
    this.notify();

    const session = this.session;
    this.session = null;

    if (!this.headerSent) {
      // Released before the graph was ready — nothing was ever sent, so there
      // is no utterance for the server to close.
      this.teardownGraph();
      return;
    }
    // `headerSent` deliberately stays true across the drain. The server's
    // utterance is open until `audioEnd` goes out, and the worklet's flush —
    // the last partial frame, which is the END OF THE FINAL WORD — arrives
    // during it. Clearing the flag here would make `onFrame` reject exactly
    // that frame as a stray, and the operator would lose the last syllable of
    // every sentence to a guard meant for a different mistake.
    this.closeAfterDrain(session);
  }

  /**
   * Stop without closing an utterance: the socket dropped, or the press could
   * not be completed. The server forgets a burst whose socket went away, so
   * there is nothing to tell it — only the operator needs telling.
   *
   * A message is shown if a capture was live OR a drain was in flight, because
   * in both states the operator is waiting for a transcript that is no longer
   * coming. It goes through `announce`, so it reaches the focus panel and not
   * only the HUD.
   */
  abort(message) {
    const active = this.capturing || this.draining;
    this.capturing = false;
    this.session = null;
    this.headerSent = false;
    this.draining = false;
    this.drainSession = null;
    this.generation += 1;
    clearTimeout(this.capTimer);
    clearTimeout(this.drainTimer);
    this.capTimer = null;
    this.drainTimer = null;
    this.teardownGraph();
    if (active && message) this.announce(message);
    this.notify();
  }

  /**
   * Disconnect the mic, wait for the worklet's tail, then send `audioEnd`.
   *
   * The wait is what keeps the last word. Frames cross from the worklet on a
   * port queue; `audioEnd` crosses on the websocket. Sending it the instant the
   * trigger came up would race the frames still in that queue and clip the end
   * of every sentence — so the worklet is asked to flush and answers `done`,
   * and because a port queue is FIFO, every frame it ever posted is already in
   * hand by then. The timer is the answer to a worklet that never replies (a
   * suspended context, a node the graph already dropped): the utterance still
   * closes, just without its last 20 ms.
   *
   * `draining`/`drainSession` are instance state, not closure state, so a
   * re-press (`press`) or a session-end can force the drain to complete from
   * outside — `finishDrain` is idempotent and the single place `audioEnd` for a
   * released utterance is sent.
   */
  closeAfterDrain(session) {
    this.draining = true;
    this.drainSession = session;

    // Stop the flow of new audio. The node stays connected to the port so the
    // flush can still cross it; `teardownGraph` drops the rest in `finishDrain`.
    try {
      this.source?.disconnect(this.node);
    } catch {
      /* never connected — released before the graph was ready */
    }
    if (this.node) {
      this.node.port.onmessage = (event) => {
        if (typeof event.data === 'string') this.finishDrain();
        else this.onFrame(event.data);
      };
      this.node.port.postMessage('stop');
      this.drainTimer = setTimeout(() => this.finishDrain(), DRAIN_TIMEOUT_MS);
    } else {
      // Nothing to flush — released before the graph was ready. Close at once.
      this.finishDrain();
    }
  }

  /** Close the drained utterance: send its `audioEnd` and tear the node down.
   *  Idempotent, so the worklet's `done`, the timeout, and a re-press can all
   *  call it and only the first does the work. */
  finishDrain() {
    if (!this.draining) return;
    this.draining = false;
    clearTimeout(this.drainTimer);
    this.drainTimer = null;
    const session = this.drainSession;
    this.drainSession = null;
    this.headerSent = false; // the utterance is closed as this frame goes out
    this.teardownGraph();
    this.net.audioEnd(session);
  }

  /* --------------------------------------------------------------- frames -- */

  /** One 20 ms frame out of the worklet, straight onto the socket as binary. */
  onFrame(buffer) {
    if (!this.headerSent || !(buffer instanceof ArrayBuffer)) return;
    // Even by construction — the worklet only ever transfers an Int16Array's
    // buffer. Checked anyway, because the cost of being wrong is not a dropped
    // frame but a transcriber fed half a sample and mis-aligned for the rest of
    // the utterance.
    if (buffer.byteLength === 0 || buffer.byteLength % 2 !== 0) {
      console.warn(`[xr] refusing a ${buffer.byteLength}-byte audio frame (must be even)`);
      return;
    }
    if (this.net.sendBinary(buffer)) {
      this.bytes += buffer.byteLength;
      this.frames += 1;
    }
  }

  /* ---------------------------------------------------------------- graph -- */

  /**
   * The AudioContext, constructed inside the gesture. Returns false if this
   * browser has no Web Audio at all, which is not survivable for voice but is
   * survivable for the ring.
   */
  openContext() {
    const Ctor = globalThis.AudioContext ?? globalThis.webkitAudioContext;
    if (!Ctor) {
      this.hud.pin('This browser has no Web Audio, so push-to-talk cannot run here.');
      return false;
    }
    if (!this.ctx) {
      // Ask for a 16 kHz context: when the browser obliges, its own resampler
      // does the work and the worklet's box filter becomes a pass-through.
      // Chrome and the Quest browser both honour this; anything that refuses
      // throws here and gets the hardware rate with the worklet resampling.
      // `avoid16k` is set after a browser (Firefox before 148) accepts the 16 kHz
      // context but then refuses to connect a mic source to it — see ensureGraph.
      try {
        if (this.forceResample || this.avoid16k) throw new Error('forced: hardware rate');
        this.ctx = new Ctor({ sampleRate: TARGET_RATE });
      } catch {
        try {
          this.ctx = new Ctor();
        } catch (err) {
          this.hud.pin(`Web Audio would not start: ${err?.message ?? err}`);
          return false;
        }
      }
    }
    // A context can come up suspended even from inside a gesture; resuming is
    // also a gesture-scoped call, so it belongs here rather than after an await.
    if (this.ctx.state === 'suspended') this.ctx.resume().catch(() => {});
    return true;
  }

  /** Mic + worklet module + node. Everything here is once-per-page, except when
   *  the mic is re-requested (its track ended) or the context is rebuilt at the
   *  hardware rate (a browser that refused a 16 kHz source). */
  async ensureGraph() {
    if (!(await this.ensureStream())) return false;
    await this.ensureModule();
    if (!this.node) this.buildNode();
    if (!this.source) {
      try {
        this.source = this.ctx.createMediaStreamSource(this.stream);
      } catch (err) {
        // Firefox before 148 accepts `new AudioContext({sampleRate: 16000})` but
        // then refuses to connect a mic source running at the hardware rate to
        // it (NotSupportedError, "different sample-rate ... not supported"). The
        // 16 kHz try/catch in openContext cannot see this — the context built
        // fine. Rebuild at the hardware rate and let the worklet resample, once.
        const mismatch =
          err?.name === 'NotSupportedError' || /sample-rate|sample rate/i.test(err?.message ?? '');
        if (!this.avoid16k && this.ctx?.sampleRate === TARGET_RATE && mismatch) {
          this.avoid16k = true;
          this.resetContext();
          if (!this.openContext()) return false;
          await this.ensureModule();
          this.buildNode();
          this.source = this.ctx.createMediaStreamSource(this.stream);
        } else {
          throw err;
        }
      }
    }
    return true;
  }

  /** Build the worklet node and wire its frame handler. */
  buildNode() {
    this.node = new AudioWorkletNode(this.ctx, 'pcm-framer', {
      numberOfInputs: 1,
      numberOfOutputs: 0,
      channelCount: 1,
      channelCountMode: 'explicit',
      channelInterpretation: 'speakers',
    });
    this.node.onprocessorerror = (err) => {
      console.error('[xr] the audio worklet failed', err);
      // abort tears the node down; do not null it separately or the drain path
      // loses the reference it needs to stop the processor.
      this.abort('the audio pipeline failed — say it again');
    };
    this.node.port.onmessage = (event) => {
      if (typeof event.data !== 'string') this.onFrame(event.data);
    };
  }

  /** True only when a cached stream is still usable — a track that has ended
   *  (mic unplugged, permission revoked) reports `ended` and must be replaced,
   *  or every press lights the dot and streams silence. */
  streamLive() {
    return Boolean(this.stream) && this.stream.getTracks().every((t) => t.readyState === 'live');
  }

  async ensureStream() {
    if (this.streamLive()) return true;
    if (this.stream) this.dropStream(); // a dead cached stream: re-request below
    const media = navigator.mediaDevices;
    if (!media?.getUserMedia) {
      // The usual cause is an insecure origin, which is also §13's first trap
      // and already explained by the page's own notice — so name it here too
      // rather than reporting a missing API the operator cannot install. The
      // port is read from the page, not hard-coded, so the advice is right when
      // the server is not on 8748.
      const host = globalThis.location?.host ?? 'localhost:8748';
      const port = globalThis.location?.port || '8748';
      this.announce(
        'No microphone API on this origin. getUserMedia needs a secure context — ' +
          `use \`adb reverse tcp:${port} tcp:${port}\` and open http://${host}.`,
        { pin: true },
      );
      return false;
    }
    // Share one getUserMedia across concurrent presses: a re-press while the
    // first press's permission prompt is still up must not open a second stream
    // and orphan the first. A rejection clears the promise so the next press
    // retries rather than being wedged on a stale failure.
    if (!this.streamPromise) {
      this.streamPromise = media
        .getUserMedia(CONSTRAINTS)
        .then((stream) => {
          this.attachStream(stream);
          return true;
        })
        .catch((err) => {
          this.failOpen(err);
          return false;
        })
        .finally(() => {
          this.streamPromise = null;
        });
    }
    return this.streamPromise;
  }

  /** Adopt a fresh mic stream and watch its tracks for the device going away. */
  attachStream(stream) {
    this.stream = stream;
    for (const track of stream.getTracks()) {
      track.addEventListener('ended', () => this.onTrackEnded());
    }
  }

  /** A mic track ended mid-page (unplugged, or permission revoked). Drop the
   *  cached stream so the next press re-requests it, and if we were mid-sentence
   *  say so — a lit dot over a dead mic is the "talking into nothing" §10 warns
   *  the operator against. */
  onTrackEnded() {
    this.dropStream();
    if (this.capturing || this.draining) this.abort('microphone disconnected — say it again');
    else this.notify();
  }

  /** Stop and forget the cached stream and the source bound to it. */
  dropStream() {
    this.stream?.getTracks().forEach((track) => {
      try {
        track.stop();
      } catch {
        /* already stopped */
      }
    });
    this.stream = null;
    // The source is bound to the now-dead stream; a new stream needs a new one.
    this.source = null;
  }

  async ensureModule() {
    if (this.moduleReady) return;
    // One in-flight load, shared by concurrent callers: `moduleUrl` used to be
    // set before addModule resolved, so a second press during the first load
    // skipped it and built a node before `pcm-framer` was registered. A rejected
    // load clears the promise so a later press retries instead of being stuck.
    if (!this.modulePromise) {
      const blob = new Blob([WORKLET_SOURCE], { type: 'text/javascript' });
      const url = URL.createObjectURL(blob);
      this.modulePromise = this.ctx.audioWorklet
        .addModule(url)
        .then(() => {
          this.moduleUrl = url;
          this.moduleReady = true;
        })
        .catch((err) => {
          URL.revokeObjectURL(url);
          this.modulePromise = null;
          throw err;
        });
    }
    await this.modulePromise;
  }

  /** Drop the context and everything registered against it, for a rebuild at a
   *  different sample rate. The stream is kept — it is rate-independent. */
  resetContext() {
    if (this.node) {
      // Silence the discarded node's port before dropping it: closing the context
      // below drops the node itself, but a flush already queued on its port would
      // otherwise reach `onFrame` and inject a stray tail into the new utterance.
      this.node.port.onmessage = null;
    }
    this.node = null;
    this.source = null;
    this.moduleReady = false;
    this.modulePromise = null;
    this.moduleUrl = null;
    try {
      this.ctx?.close();
    } catch {
      /* already closing */
    }
    this.ctx = null;
  }

  /** A failure that ends this utterance and says why, without killing voice. */
  failOpen(err) {
    const name = err?.name ?? '';
    this.capturing = false;
    this.session = null;
    this.headerSent = false;
    clearTimeout(this.capTimer);
    this.capTimer = null;

    if (name === 'NotAllowedError' || name === 'SecurityError') {
      // A NotAllowedError covers BOTH a hard block and a merely dismissed
      // prompt, and Chrome would re-prompt after a dismissal. So do not latch
      // `denied` on the error alone: pin the advice, and ask the Permissions API
      // whether it is a real block. A dismissal leaves `denied` false, so the
      // next press prompts again — which is exactly what MIC_DENIED's own words
      // ("press to talk again") tell the operator to do, and what a page reload
      // used to be the only way to reach.
      this.announce(MIC_DENIED, { pin: true });
      this.confirmBlock();
    } else if (name === 'NotFoundError' || name === 'OverconstrainedError') {
      this.announce('No microphone was found, so push-to-talk has nothing to record.', { pin: true });
    } else {
      console.error('[xr] microphone unavailable', err);
      this.announce(`Microphone unavailable: ${err?.message ?? err}`);
    }
    this.notify();
  }

  /** Ask the Permissions API whether the mic is genuinely blocked (state
   *  `denied`) rather than merely dismissed, and start watching it. Best-effort:
   *  the API is absent on some browsers, where every dismissal simply re-prompts
   *  on the next press — the safe direction. */
  confirmBlock() {
    const perms = globalThis.navigator?.permissions;
    if (!perms?.query) return;
    perms
      .query({ name: 'microphone' })
      .then((status) => {
        this.denied = status.state === 'denied';
        if (this.denied) this.notify();
        this.watchPermission(status);
      })
      .catch(() => {});
  }

  /** Track the mic permission so a grant from the browser's own UI (the padlock)
   *  lifts the block without a page reload — which in the headset would mean
   *  leaving the immersive session. */
  watchPermission(status) {
    if (this.permissionWatched || !status) return;
    this.permissionWatched = true;
    this.micPermission = status;
    const onChange = () => {
      this.denied = status.state === 'denied';
      if (!this.denied) this.hud.unpin();
      this.notify();
    };
    if (status.addEventListener) status.addEventListener('change', onChange);
    else status.onchange = onChange;
  }

  /** Drop the node's wiring. The stream and the context are kept for the page. */
  teardownGraph() {
    if (this.node) {
      // Tell the processor to stop so it returns false and the graph drops it.
      // Without this, an aborted utterance leaks one live AudioWorklet processor
      // (`process` keeps returning true) for the life of the page.
      try {
        this.node.port.postMessage('stop');
      } catch {
        /* no port */
      }
      try {
        this.source?.disconnect(this.node);
      } catch {
        /* already disconnected */
      }
      this.node.port.onmessage = null;
      // A processor that returned false is finished: the next utterance builds
      // a new node against the module that is already registered, which costs
      // an object rather than a fetch.
      this.node = null;
    }
  }

  /** Page teardown. Not part of the demo path; here so the module is honest. */
  dispose() {
    this.abort(null);
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
    this.source = null;
    if (this.moduleUrl) URL.revokeObjectURL(this.moduleUrl);
    this.moduleUrl = null;
    this.ctx?.close().catch(() => {});
    this.ctx = null;
  }
}

export { FRAME_SAMPLES, MAX_UTTERANCE_MS, TARGET_RATE };
