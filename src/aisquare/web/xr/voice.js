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
 * {@link MAX_UTTERANCE_MS} — 55 s — so the operator is told by their own client,
 * with the mic dot going out, rather than discovering it from an error frame
 * after the fact. At a fixed 32 kB/s one wall clock governs both caps: 55 s is
 * 1.76 MB, comfortably inside the byte cap too.
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
 * Our own utterance cap, five seconds inside the server's 60 s.
 *
 * The headroom is not superstition: it covers the drain of frames still in the
 * worklet's port queue plus the difference between a `performance.now()` here
 * and a `time.monotonic()` there. Being cut off by the far end produces an
 * `audio_too_long` error and a dropped sentence; stopping ourselves produces a
 * mic dot that goes out and a line saying why, which is the same information
 * delivered before the operator has finished wasting their breath.
 */
const MAX_UTTERANCE_MS = 55_000;

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
   */
  constructor({ net, hud, focusedSession, onChange = () => {}, forceResample = false }) {
    this.net = net;
    this.hud = hud;
    this.focusedSession = focusedSession;
    this.onChange = onChange;
    this.forceResample = forceResample;

    /** Held across the page's life once granted. */
    this.stream = null;
    this.ctx = null;
    this.source = null;
    this.node = null;
    this.moduleUrl = null;
    /** Set once permission has been refused, so every later press says so
     *  immediately instead of re-prompting a browser that will not ask again. */
    this.denied = false;

    /** True from the press until the release — what the mic dot follows. */
    this.capturing = false;
    /** The session this utterance was opened for. The focus may move mid-
     *  sentence; the audio still belongs to where it started. */
    this.session = null;
    this.seq = 0;
    this.bytes = 0;
    this.frames = 0;
    this.startedAt = 0;
    /** Bumped per press so a late `ensure()` can tell it has been superseded. */
    this.generation = 0;
    this.capTimer = null;
    this.drainTimer = null;
    /** True once the header is on the wire: only then may binary frames go. */
    this.headerSent = false;
  }

  get state() {
    return {
      capturing: this.capturing,
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

    const session = this.focusedSession();
    if (!session) {
      // §5: the transcript tier is one session at a time, so "talk" has no
      // referent without one. Told on the HUD because there is, by definition,
      // no focus panel to draw it on.
      this.hud.toast('focus a panel first — press f, or A on a controller', 2000);
      return;
    }
    if (!this.net?.isOpen) {
      this.hud.toast('not connected — nothing to talk to yet', 2000);
      return;
    }
    if (this.denied) {
      this.hud.pin(MIC_DENIED);
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
    this.source.connect(this.node); // frames start here, and not before

    this.capTimer = setTimeout(() => {
      // Our cap, not the server's. See MAX_UTTERANCE_MS.
      this.hud.toast(
        `that ran past ${Math.round(MAX_UTTERANCE_MS / 1000)}s and was sent as-is — ` +
          'let go of the trigger between sentences',
        5000,
      );
      this.release();
    }, MAX_UTTERANCE_MS);
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
   */
  abort(message) {
    const wasCapturing = this.capturing;
    this.capturing = false;
    this.session = null;
    this.headerSent = false;
    this.generation += 1;
    clearTimeout(this.capTimer);
    clearTimeout(this.drainTimer);
    this.capTimer = null;
    this.drainTimer = null;
    this.teardownGraph();
    if (wasCapturing && message) this.hud.toast(message, 4000);
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
   */
  closeAfterDrain(session) {
    const finish = () => {
      if (!this.drainTimer && !this.node) return; // already finished
      clearTimeout(this.drainTimer);
      this.drainTimer = null;
      this.headerSent = false; // the utterance is closed as this frame goes out
      this.teardownGraph();
      this.net.audioEnd(session);
    };

    // Stop the flow of new audio. The node stays connected to the port so the
    // flush can still cross it; `teardownGraph` drops the rest in `finish`.
    try {
      this.source?.disconnect(this.node);
    } catch {
      /* never connected — released before the graph was ready */
    }
    if (this.node) {
      this.node.port.onmessage = (event) => {
        if (typeof event.data === 'string') finish();
        else this.onFrame(event.data);
      };
      this.node.port.postMessage('stop');
    }
    this.drainTimer = setTimeout(finish, DRAIN_TIMEOUT_MS);
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
      try {
        if (this.forceResample) throw new Error('forced: #audio48');
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

  /** Mic + worklet module + node. Everything here is once-per-page. */
  async ensureGraph() {
    if (!(await this.ensureStream())) return false;
    await this.ensureModule();
    if (!this.node) {
      this.node = new AudioWorkletNode(this.ctx, 'pcm-framer', {
        numberOfInputs: 1,
        numberOfOutputs: 0,
        channelCount: 1,
        channelCountMode: 'explicit',
        channelInterpretation: 'speakers',
      });
      this.node.onprocessorerror = (err) => {
        console.error('[xr] the audio worklet failed', err);
        this.abort('the audio pipeline failed — say it again');
        this.node = null;
      };
    }
    this.node.port.onmessage = (event) => {
      if (typeof event.data !== 'string') this.onFrame(event.data);
    };
    if (!this.source) this.source = this.ctx.createMediaStreamSource(this.stream);
    return true;
  }

  async ensureStream() {
    if (this.stream) return true;
    const media = navigator.mediaDevices;
    if (!media?.getUserMedia) {
      // The usual cause is an insecure origin, which is also §13's first trap
      // and already explained by the page's own notice — so name it here too
      // rather than reporting a missing API the operator cannot install.
      this.hud.pin(
        'No microphone API on this origin. getUserMedia needs a secure context — ' +
          'use `adb reverse tcp:8748 tcp:8748` and open http://localhost:8748.',
      );
      return false;
    }
    try {
      this.stream = await media.getUserMedia(CONSTRAINTS);
    } catch (err) {
      this.failOpen(err);
      return false;
    }
    return true;
  }

  async ensureModule() {
    if (this.moduleUrl) return;
    const blob = new Blob([WORKLET_SOURCE], { type: 'text/javascript' });
    this.moduleUrl = URL.createObjectURL(blob);
    await this.ctx.audioWorklet.addModule(this.moduleUrl);
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
      this.denied = true;
      this.hud.pin(MIC_DENIED);
    } else if (name === 'NotFoundError' || name === 'OverconstrainedError') {
      this.hud.pin('No microphone was found, so push-to-talk has nothing to record.');
    } else {
      console.error('[xr] microphone unavailable', err);
      this.hud.toast(`Microphone unavailable: ${err?.message ?? err}`, 6000);
    }
    this.notify();
  }

  /** Drop the node's wiring. The stream and the context are kept for the page. */
  teardownGraph() {
    if (this.node) {
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
