/**
 * Transport: the token, the websocket, message dispatch and reconnect.
 *
 * Everything here is fail-open (plan §2.6). The socket dropping is an expected
 * condition, not an error: the client backs off, re-authenticates, and rebuilds
 * the ring from whatever snapshot it is handed. No state survives a reconnect,
 * which is precisely why nothing can drift out of sync across one.
 *
 * This module knows the wire protocol (plan §6) and nothing about three.js.
 */

/* ------------------------------------------------------------------ token -- */

const TOKEN_KEY = 'aisquare.xr.token';

/** The hash carries both the token and the dev flags: `#mock`, `#token=…`. */
export function hashParams() {
  return new URLSearchParams(location.hash.replace(/^#/, ''));
}

/**
 * Resolve the token, once, and get it out of the URL.
 *
 * `aisquare xr` prints `http://localhost:8748/#token=…`, and the plan is blunt
 * about the reason this matters: typing URLs in the Quest browser is miserable,
 * so the flow has to survive being bookmarked (§13). A bookmark saved from the
 * address bar would capture the fragment and pin a token that later rotates —
 * so the token moves to localStorage on first visit and the fragment is
 * rewritten without it. Bookmark whatever you like; the next visit still auths.
 *
 * A token in the hash always wins over the stored one: that is how you recover
 * after the server restarts with a new token.
 */
export function resolveToken() {
  const params = hashParams();
  const fromHash = params.get('token');

  if (fromHash) {
    try {
      localStorage.setItem(TOKEN_KEY, fromHash);
    } catch {
      // Private mode or blocked storage: the token still works for this visit.
    }
    params.delete('token');
    const rest = params.toString().replace(/=(?=&|$)/g, ''); // `mock=` → `mock`
    // replaceState, not assignment: no reload, and no history entry that would
    // put the token back in the URL when the operator hits Back.
    history.replaceState(null, '', `${location.pathname}${location.search}${rest ? `#${rest}` : ''}`);
    return fromHash;
  }

  try {
    return localStorage.getItem(TOKEN_KEY) ?? '';
  } catch {
    return '';
  }
}

/** Forget a token the server has rejected, so a fresh `#token=…` link works. */
export function clearToken() {
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* nothing to forget */
  }
}

/* ------------------------------------------------------------------- chip -- */

/**
 * The connection chip. A tester has to be able to read the transport state off
 * the screen without opening devtools, so it is DOM — deliberately not part of
 * the 3D scene, which is why it survives a WebGL context loss too.
 */
export class ConnectionChip {
  constructor(el) {
    this.el = el;
    this.set('offline');
  }

  /** @param {'connected'|'reconnecting'|'auth failed'|'server gone'|'offline'|'mock'} state */
  set(state, detail = '') {
    this.state = state;
    if (!this.el) return;
    this.el.textContent = detail ? `${state} — ${detail}` : state;
    this.el.dataset.state = state.replace(/ /g, '-');
  }
}

/* ---------------------------------------------------------------- backoff -- */

const BACKOFF_MIN_MS = 500;
const BACKOFF_MAX_MS = 8000;

/**
 * Attempts after which the chip stops saying "reconnecting" and says "server
 * gone" instead (plan §11/M7).
 *
 * It does NOT stop retrying — this client is fail-open and the server coming
 * back must not need a headset reload, which is the whole M7 verification. It
 * is a change of wording, and the wording is the point: five failures in a row
 * with the backoff already at its ceiling is no longer "a blip", and an
 * operator watching a chip that has said "reconnecting" for two minutes has no
 * way to tell a slow reconnect from a server they forgot to start.
 */
const GONE_AFTER_ATTEMPTS = 5;

/* -------------------------------------------------------------------- net -- */

/**
 * One websocket to `/ws`, with dispatch and reconnect.
 *
 * Usage:
 *   const net = new Net({ chip });
 *   net.on('snapshot', (m) => ring.applySnapshot(m.sessions));
 *   net.connect();
 */
export class Net {
  /**
   * @param {object}   opts
   * @param {ConnectionChip} [opts.chip]
   * @param {string}   [opts.url]   override for tests; defaults to ws(s)://<host>/ws
   * @param {string}   [opts.token]
   */
  constructor({ chip = null, url = null, token = null } = {}) {
    this.chip = chip;
    this.token = token ?? resolveToken();
    this.url = url ?? `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;

    /** @type {Map<string, Set<Function>>} message type → handlers */
    this.handlers = new Map();
    this.socket = null;
    this.attempt = 0;
    this.timer = null;
    /** Bumped per successful connection. A snapshot from generation N is only
     *  meaningful for the ring built in generation N. */
    this.generation = 0;
    this.authFailed = false;
    this.closedByUs = false;
    /** The reason behind a TRANSIENT auth refusal (`auth_timeout`,
     *  `auth_invalid`), kept so the chip can say it: the server closes 4408
     *  right after the frame and `scheduleReconnect` would otherwise overwrite
     *  the only explanation with a bare "reconnecting — attempt N". Cleared by
     *  the next `hello`. */
    this.transientAuth = '';

    // A machine that has gone offline will not connect; say so rather than
    // burning the backoff ladder against a dead adapter.
    addEventListener('offline', () => {
      if (!this.authFailed) this.setStatus('offline');
    });
    addEventListener('online', () => {
      if (!this.authFailed && !this.isOpen) this.connect(true);
    });
  }

  get isOpen() {
    return this.socket?.readyState === WebSocket.OPEN;
  }

  /* ---------------------------------------------------------- dispatch --- */

  /**
   * Subscribe to a server message type: `hello`, `snapshot`, `delta`,
   * `transcript`, `stt`, `error`, `ack` (plan §6). Three pseudo-types are emitted
   * locally: `status` when the chip changes, `open` on every (re)connection
   * after `hello` — the cue to treat the next snapshot as authoritative — and
   * `drop` when a socket closes, which is the cue to abandon anything that was
   * in flight on it.
   */
  on(type, handler) {
    if (!this.handlers.has(type)) this.handlers.set(type, new Set());
    this.handlers.get(type).add(handler);
    return () => this.handlers.get(type)?.delete(handler);
  }

  emit(type, payload) {
    for (const handler of this.handlers.get(type) ?? []) {
      // One bad handler must not take down the socket loop.
      try {
        handler(payload);
      } catch (err) {
        console.error(`[xr] handler for "${type}" threw`, err);
      }
    }
  }

  setStatus(state, detail = '') {
    this.chip?.set(state, detail);
    this.emit('status', { state, detail });
  }

  /* ------------------------------------------------------------ lifecycle -- */

  connect(immediate = false) {
    clearTimeout(this.timer);
    this.timer = null;
    this.closedByUs = false;

    if (this.authFailed) return;
    if (!this.token) {
      this.setStatus('auth failed', 'no token — open the URL printed by `aisquare xr`');
      return;
    }
    if (this.socket && this.socket.readyState <= WebSocket.OPEN) return;
    if (immediate) this.attempt = 0;

    let socket;
    try {
      socket = new WebSocket(this.url);
    } catch (err) {
      console.warn('[xr] websocket construction failed', err);
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;
    socket.binaryType = 'arraybuffer';

    socket.addEventListener('open', () => {
      // The first frame is always auth (plan §6). Nothing else may precede it.
      this.send({ t: 'auth', token: this.token });
    });

    socket.addEventListener('message', (event) => this.receive(event));

    socket.addEventListener('close', (event) => {
      if (socket !== this.socket) return; // a stale socket losing a race
      this.socket = null;
      // Before anything else: whatever was mid-flight on this socket is gone.
      // A push-to-talk burst in particular has audio the server has already
      // forgotten, and the operator has to be told to say it again rather than
      // waiting on a transcript that is never coming (§11/M7).
      this.emit('drop', { code: event.code, reason: event.reason });
      if (this.closedByUs || this.authFailed) return;
      // Every close here is transient and reconnects with backoff. The one
      // terminal case — a rejected token — is driven by the `auth_failed` ERROR
      // FRAME the server sends just before it closes (see `receive`), never by
      // the close code alone. The codes the server uses, none terminal by
      // itself: 4401 after `auth_failed` (the frame already ended us above);
      // 4408 after `auth_timeout` (silence) or `auth_invalid` (a malformed or
      // non-auth first frame) — retry: true in the published contract; 1013
      // after `board_unavailable`; 1012 for a service restart; and the ordinary
      // 1001/1006 of a socket that simply went away. Keying off the frame keeps
      // a stalled reconnect retrying instead of deleting a still-valid token.
      this.scheduleReconnect();
    });

    socket.addEventListener('error', () => {
      // `close` always follows; reconnect is scheduled there so it happens once.
      if (socket === this.socket && !this.isOpen) this.setStatus('reconnecting');
    });
  }

  /**
   * Exponential backoff, 0.5s → 8s. Jittered: when the server comes back, every
   * headset and tab that was watching it would otherwise redial in lockstep.
   */
  scheduleReconnect() {
    if (this.timer || this.authFailed || this.closedByUs) return;
    const base = Math.min(BACKOFF_MIN_MS * 2 ** this.attempt, BACKOFF_MAX_MS);
    const delay = base * (0.75 + Math.random() * 0.5);
    this.attempt += 1;
    // A transient auth refusal names its reason on the chip, or the operator
    // sees a rising attempt count with no way to tell a stalled adb reverse from
    // a server they forgot to start.
    const why = this.transientAuth ? ` — ${this.transientAuth}` : '';
    if (navigator.onLine === false) {
      this.setStatus('offline');
    } else if (this.attempt > GONE_AFTER_ATTEMPTS) {
      // Still retrying on the same ladder — only the wording has given up.
      this.setStatus('server gone', `retrying · attempt ${this.attempt}${why}`);
    } else {
      // The attempt count is what makes this chip testable: "reconnecting" that
      // never changes is indistinguishable from a frozen client, and the M7
      // criterion is a RISING count (§11/M7).
      this.setStatus('reconnecting', `attempt ${this.attempt}${why}`);
    }
    this.timer = setTimeout(() => {
      this.timer = null;
      this.connect();
    }, delay);
  }

  /**
   * A token the server will not accept. Terminal, and the only terminal state
   * here: retrying a rejected token just hammers the server with the same
   * rejection, and no amount of backoff turns a stale token into a good one.
   *
   * The detail carries §13's recovery, because it is not guessable. The token
   * rotates when the server restarts, the stored copy is now the wrong one, and
   * the fix is to open the URL `aisquare xr` printed — which is also why
   * `clearToken` runs first, so that URL's `#token=…` is not shadowed by the
   * dead one in localStorage on the next load.
   */
  failAuth(detail) {
    this.authFailed = true;
    clearToken();
    clearTimeout(this.timer);
    this.timer = null;
    this.setStatus(
      'auth failed',
      `${detail} — open the URL \`aisquare xr\` printed (the token rotates with the server)`,
    );
  }

  /** Stop for good — used by the mock feed, which owns the scene instead. */
  close() {
    this.closedByUs = true;
    clearTimeout(this.timer);
    this.timer = null;
    this.socket?.close();
    this.socket = null;
  }

  /* -------------------------------------------------------------- receive -- */

  receive(event) {
    // Binary frames are the voice channel's return path (plan §6/§10, M6).
    // Nothing here consumes them yet; dropping them silently is correct.
    if (typeof event.data !== 'string') return;

    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      console.warn('[xr] non-JSON text frame dropped');
      return;
    }
    if (!msg || typeof msg.t !== 'string') return;

    if (msg.t === 'hello') {
      this.attempt = 0;
      this.transientAuth = '';
      this.generation += 1;
      this.hub = msg.hub;
      this.protocol = msg.protocol;
      this.setStatus('connected');
      // Consumers rebuild on the snapshot that follows this.
      this.emit('open', { generation: this.generation, hello: msg });
    }

    if (msg.t === 'error') {
      console.warn(`[xr] server error ${msg.code}: ${msg.message}`);
      // ONLY a genuinely rejected token is terminal. `auth_failed` is the code
      // the server reserves for that; `auth_timeout` (and any other transport
      // stall) must stay transient and keep the stored token, because the token
      // was never the problem — the frame simply did not arrive in time, and the
      // next connection may well succeed with the same one. The published
      // `closeCodes` contract says as much: 4401 is do-not-retry only for a
      // rejected token, and the error frame is how the client tells which 4401
      // this is. Everything else falls through to the app's own `error` handler.
      if (msg.code === 'auth_failed') {
        this.failAuth(msg.message || msg.code);
      } else if (String(msg.code ?? '').startsWith('auth')) {
        // `auth_timeout` / `auth_invalid`: transient, and the reason rides the
        // chip through the 4408 close that follows (see scheduleReconnect).
        this.transientAuth = msg.message || msg.code;
      }
    }

    this.emit(msg.t, msg);
  }

  /* ----------------------------------------------------------------- send -- */

  /**
   * Drops the frame and reports false if the socket is not open, rather than
   * queueing it. A prompt composed against a board that has since been rebuilt
   * is worse than a prompt that visibly did not send: the caller can retry,
   * and after a reconnect the snapshot may have moved the session out from
   * under it. The `auth` frame is sent from the `open` handler, so it is
   * subject to the same check and needs no exemption.
   */
  send(message) {
    if (!this.isOpen) return false;
    this.socket.send(JSON.stringify(message));
    return true;
  }

  /**
   * One binary frame — 16 kHz mono PCM16LE audio, and nothing else ever.
   *
   * Separate from `send` because it must not be JSON.stringify'd, and because
   * the failure mode is different: a dropped audio frame is 20 ms of a sentence
   * and the right response is to carry on, whereas a dropped `prompt` is the
   * whole message and the caller has to know. So this reports false the same
   * way, but `VoiceCapture` reads it as a byte counter rather than an error.
   *
   * `bufferedAmount` is not checked. At 32 kB/s over loopback or `adb reverse`
   * the socket is never the bottleneck, and dropping frames to protect a buffer
   * that is not filling would punch holes in the audio for no reason.
   */
  sendBinary(buffer) {
    if (!this.isOpen) return false;
    this.socket.send(buffer);
    return true;
  }

  /** `null` means ambient only — no transcript stream (plan §6).
   *
   *  The server now requires a non-empty id (`^[A-Za-z0-9_:-]+$`) wherever a
   *  session is named and answers anything else with `bad_message`. This client
   *  only ever names ids it received from the server, so an empty string can
   *  reach here only through a bug — and the honest reading of "subscribe to
   *  nothing" is ambient, so `''` is sent as `null` rather than refused. */
  subscribe(sessionId = null) {
    return this.send({ t: 'subscribe', session: sessionId || null });
  }

  /** A frame that NAMES a session cannot be sent without one: the server would
   *  refuse it as `bad_message` and the caller would learn nothing it could act
   *  on. `false` here is the same answer as a closed socket — nothing was sent. */
  prompt(sessionId, text) {
    if (!sessionId) return false;
    return this.send({ t: 'prompt', session: sessionId, text });
  }

  /**
   * Open a push-to-talk burst. Binary frames follow until `audioEnd` (§6).
   *
   * Every binary frame before this header is a "stray" to the server, which
   * answers the first one with an error and stays quiet for the rest — so the
   * caller must not start the worklet until this has returned true.
   */
  audioStart(sessionId, seq = 0) {
    if (!sessionId) return false;
    return this.send({ t: 'audio', session: sessionId, seq });
  }

  /**
   * Close the burst: the server transcribes what it buffered and ROUTES THE
   * FINAL TEXT AS THE PROMPT ITSELF. There is deliberately no `prompt` call
   * anywhere on the voice path — sending one here would deliver the operator's
   * sentence twice.
   */
  audioEnd(sessionId) {
    if (!sessionId) return false;
    return this.send({ t: 'audioEnd', session: sessionId });
  }
}
