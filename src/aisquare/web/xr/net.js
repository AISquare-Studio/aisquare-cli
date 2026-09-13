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

  /** @param {'connected'|'reconnecting'|'auth failed'|'offline'|'mock'} state */
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
   * `transcript`, `stt`, `error`, `ack` (plan §6). Two pseudo-types are emitted
   * locally: `status` when the chip changes, and `open` on every (re)connection
   * after `hello` — the cue to treat the next snapshot as authoritative.
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
      if (this.closedByUs || this.authFailed) return;
      // 1008 is policy violation — how a server refuses a bad token.
      if (event.code === 1008) return this.failAuth(event.reason || 'token rejected');
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
    this.setStatus(navigator.onLine === false ? 'offline' : 'reconnecting');
    this.timer = setTimeout(() => {
      this.timer = null;
      this.connect();
    }, delay);
  }

  failAuth(detail) {
    this.authFailed = true;
    clearToken();
    clearTimeout(this.timer);
    this.timer = null;
    // Retrying a rejected token just hammers the server with the same rejection.
    this.setStatus('auth failed', detail);
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
      this.generation += 1;
      this.hub = msg.hub;
      this.protocol = msg.protocol;
      this.setStatus('connected');
      // Consumers rebuild on the snapshot that follows this.
      this.emit('open', { generation: this.generation, hello: msg });
    }

    if (msg.t === 'error') {
      console.warn(`[xr] server error ${msg.code}: ${msg.message}`);
      if (String(msg.code ?? '').startsWith('auth')) {
        this.failAuth(msg.message || msg.code);
      }
    }

    this.emit(msg.t, msg);
  }

  /* ----------------------------------------------------------------- send -- */

  send(message) {
    if (!this.isOpen && message.t !== 'auth') return false;
    if (this.socket?.readyState !== WebSocket.OPEN) return false;
    this.socket.send(JSON.stringify(message));
    return true;
  }

  /** `null` means ambient only — no transcript stream (plan §6). */
  subscribe(sessionId = null) {
    return this.send({ t: 'subscribe', session: sessionId ?? null });
  }

  prompt(sessionId, text) {
    return this.send({ t: 'prompt', session: sessionId, text });
  }
}
