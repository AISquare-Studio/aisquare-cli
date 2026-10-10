/* The phone page booted in a fake browser: what it draws and sends when the
 * machine answers a certain way (SPEC §6.3, §6.4). Test-only; not packaged.
 *
 * remote_page_check.js runs the pure core. This runs the rest: each scenario
 * loads app.js afresh in its own vm context, whose globals are a browser just
 * big enough for the page. Elements keep their children, classes and
 * listeners; location's hash fires hashchange; fetch and WebSocket are answered
 * by the scenario; timers never fire on their own, so nothing waits on a clock,
 * but they are kept, and a scenario can fire one by its function's name.
 * Like the other harness it asserts nothing: it prints ONE JSON report, and
 * tests/test_remote_page.py asserts on it.
 *
 * usage: node tests/js/remote_page_boot.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const APP = path.join(__dirname, "..", "..", "src", "aisquare", "web", "remote", "app.js");
const SOURCE = fs.readFileSync(APP, "utf8");
const WORKER = fs.readFileSync(path.join(path.dirname(APP), "sw.js"), "utf8");
const CSS = fs.readFileSync(path.join(path.dirname(APP), "app.css"), "utf8");
/* The transcript box's padding, a side, as the stylesheet sets it. */
const PRE_PADDING = /pre\.pane, pre\.transcript \{[^}]*padding: (\d+)px;/.exec(CSS)[1] + "px";
const BASE = "http://127.0.0.1:8750/r/" + "t".repeat(32) + "/";
const PROJECT = "prj_x";
const NEEDS_ID = "ny_0123456789abcdef";
const PASSPHRASE = "amber birch cedar delta";
/* A 12 px monospace character's advance (0.6 em), as the page measures one. */
const CHAR_PX = 7.2;
/* A pre's clientWidth: its width inside the border, padding in. A scenario sets it. */
let preWidth = 0;

// --- a browser just big enough for the page -------------------------------------------------

class FakeNode {
  constructor(doc) {
    this.ownerDocument = doc;
    this.parentNode = null;
    this.childNodes = [];
  }

  get firstChild() {
    return this.childNodes[0] || null;
  }

  get nextSibling() {
    const siblings = this.parentNode ? this.parentNode.childNodes : [];
    return siblings[siblings.indexOf(this) + 1] || null;
  }

  get isConnected() {
    let node = this;
    while (node.parentNode) node = node.parentNode;
    return node === this.ownerDocument.documentElement;
  }

  get textContent() {
    return this.childNodes.map((child) => child.textContent).join("");
  }

  set textContent(value) {
    for (const child of this.childNodes) child.parentNode = null;
    this.childNodes = [];
    const text = String(value);
    if (text) this.appendChild(new FakeText(this.ownerDocument, text));
  }

  appendChild(child) {
    return this.insertBefore(child, null);
  }

  insertBefore(child, before) {
    if (child.parentNode) child.parentNode.removeChild(child);
    const at = before ? this.childNodes.indexOf(before) : -1;
    if (at < 0) this.childNodes.push(child);
    else this.childNodes.splice(at, 0, child);
    child.parentNode = this;
    return child;
  }

  removeChild(child) {
    const at = this.childNodes.indexOf(child);
    if (at < 0) throw new Error("removeChild: not a child");
    this.childNodes.splice(at, 1);
    child.parentNode = null;
    return child;
  }

  replaceChild(child, old) {
    if (child.parentNode) child.parentNode.removeChild(child);
    const at = this.childNodes.indexOf(old);
    if (at < 0) throw new Error("replaceChild: not a child");
    this.childNodes.splice(at, 1, child);
    old.parentNode = null;
    child.parentNode = this;
    return old;
  }

  append(...nodes) {
    for (const node of nodes) this.appendChild(typeof node === "string" ? new FakeText(this.ownerDocument, node) : node);
  }
}

class FakeText extends FakeNode {
  constructor(doc, text) {
    super(doc);
    this.data = text;
  }

  get textContent() {
    return this.data;
  }

  set textContent(value) {
    this.data = String(value);
  }
}

class FakeElement extends FakeNode {
  constructor(doc, tag) {
    super(doc);
    this.tagName = String(tag).toUpperCase();
    this.className = "";
    this.id = "";
    this.hidden = false;
    this.disabled = false;
    this.type = "";
    this.value = "";
    this.checked = false;
    this.listeners = {};
    this.attrs = {};
    this.style = { setProperty() {} };
  }

  get classList() {
    const node = this;
    const names = () => node.className.split(/\s+/).filter(Boolean);
    const set = (list) => { node.className = list.join(" "); };
    return {
      contains: (name) => names().indexOf(name) >= 0,
      add: (...more) => set(names().concat(more.filter((name) => names().indexOf(name) < 0))),
      remove: (...gone) => set(names().filter((name) => gone.indexOf(name) < 0)),
      toggle(name, force) {
        const on = force === undefined ? !this.contains(name) : !!force;
        if (on) this.add(name);
        else this.remove(name);
        return on;
      },
    };
  }

  /* Recorded, never judged: what an attribute may hold is remote_page_check.js's business. */
  setAttribute(name, value) {
    this.attrs[String(name)] = String(value);
  }

  addEventListener(type, fn) {
    (this.listeners[type] = this.listeners[type] || []).push(fn);
  }

  dispatch(type, extra) {
    const event = Object.assign({ type, target: this, preventDefault() {} }, extra);
    for (const fn of (this.listeners[type] || []).slice()) fn(event);
  }

  focus() {
    this.ownerDocument.activeElement = this;
    this.dispatch("focus");
  }

  blur() {
    if (this.ownerDocument.activeElement === this) this.ownerDocument.activeElement = this.ownerDocument.body;
    this.dispatch("blur");
  }

  contains(node) {
    for (let at = node; at; at = at.parentNode) if (at === this) return true;
    return false;
  }

  /* A layout of one kind: a monospace character is CHAR_PX wide, a pre is preWidth, and
   * nothing else has a size. */
  getBoundingClientRect() {
    return { width: this.classList.contains("measure") ? Array.from(this.textContent).length * CHAR_PX : 0 };
  }

  get clientWidth() {
    return this.tagName === "PRE" ? preWidth : 0;
  }

  /* "tag" or "tag.class.class": all the page ever asks for. */
  querySelectorAll(selector) {
    const [tag, ...classes] = selector.split(".");
    return descendants(this).filter((node) => node.tagName === tag.toUpperCase() && classes.every((name) => node.classList.contains(name)));
  }
}

function descendants(root) {
  const out = [];
  const walk = (node) => {
    for (const child of node.childNodes) {
      if (child instanceof FakeElement) out.push(child);
      walk(child);
    }
  };
  walk(root);
  return out;
}

class FakeSocket {
  constructor(sockets) {
    this.readyState = 0;
    this.listeners = {};
    this.sent = [];
    sockets.push(this);
  }

  addEventListener(type, fn) {
    (this.listeners[type] = this.listeners[type] || []).push(fn);
  }

  fire(type, extra) {
    for (const fn of (this.listeners[type] || []).slice()) fn(Object.assign({ type }, extra));
  }

  /* What the page told the machine on this socket, as parsed messages. */
  send(text) {
    this.sent.push(JSON.parse(text));
  }

  close(code) {
    if (this.readyState === 3) return;
    this.readyState = 3;
    setImmediate(() => this.fire("close", { code }));
  }

  /* The handshake succeeds: what the scenario says the machine did. */
  accept() {
    this.readyState = 1;
    this.fire("open");
  }

  frame(type, payload, extra) {
    this.fire("message", { data: JSON.stringify(Object.assign({ type, payload }, extra)) });
  }
}

function storage() {
  const items = new Map();
  return {
    getItem: (key) => (items.has(key) ? items.get(key) : null),
    setItem: (key, value) => { items.set(key, String(value)); },
    removeItem: (key) => { items.delete(key); },
  };
}

/* An answer the scenario holds back until it says so. */
function deferred() {
  let settle;
  const promise = new Promise((resolve) => { settle = resolve; });
  return { promise, settle };
}

/* Boot app.js at `hash`, the machine answering every request through `answer`:
 * (method, path, body) -> {status, json}, or "network" for a request that never
 * arrives, or a promise of either; {status, json, cut: true} is an answer whose
 * connection dropped halfway through its body, after its headers came. `globals` adds
 * to the browser (fakePush); `base` is the page's own URL, the machine's at http by
 * default. */
function bootPage(hash, answer, globals, base) {
  const address = base || BASE;
  const doc = {
    title: "",
    visibilityState: "visible",
    listeners: {},
    createElement: (tag) => new FakeElement(doc, tag),
    createTextNode: (text) => new FakeText(doc, String(text)),
    getElementById: (id) => descendants(doc.documentElement).find((node) => node.id === id) || null,
    querySelectorAll: (selector) => doc.documentElement.querySelectorAll(selector),
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    },
  };
  doc.documentElement = new FakeElement(doc, "html");
  doc.body = new FakeElement(doc, "body");
  doc.activeElement = doc.body;
  const app = new FakeElement(doc, "div");
  app.id = "app";
  doc.documentElement.appendChild(doc.body);
  doc.body.appendChild(app);

  const requests = [];
  const sockets = [];
  const timers = new Map();
  let lastTimer = 0;
  const hold = (fn, every) => { // kept, and never fired but by a scenario
    timers.set(++lastTimer, { fn, every });
    return lastTimer;
  };
  const win = { listeners: {} };
  let current = hash;
  /* The tab's history from the page's own load on: setting the hash pushes an entry, as a
   * browser does, and replace() takes the place of the one it is at; each fires popstate at
   * once, then hashchange. With `globals.history`, the page has pushState and back() too. */
  const entries = [hash];
  const states = [null];
  let at = 0;
  const changed = () => setImmediate(() => { for (const fn of win.listeners.hashchange || []) fn({ type: "hashchange" }); });
  const popped = () => { for (const fn of win.listeners.popstate || []) fn({ type: "popstate", state: states[at] }); };
  const go = (value, replace) => {
    const next = String(value).charAt(0) === "#" ? String(value) : "#" + value;
    if (next === current) return;
    current = next;
    if (!replace) at++;
    entries.splice(at, replace ? 1 : entries.length, next);
    states.splice(at, replace ? 1 : states.length, null);
    popped();
    changed();
  };
  const history = {
    pushState(state) {
      at++;
      entries.splice(at, entries.length, current);
      states.splice(at, states.length, state);
    },
    back: () => setImmediate(() => page.back()), // a traversal is queued, never done at once
  };
  const location = {
    protocol: new URL(address).protocol,
    get hash() {
      return current;
    },
    set hash(value) {
      go(value, false);
    },
    replace(url) {
      const value = String(url);
      go(value.slice(value.indexOf("#")), true);
    },
    toString: () => address + current,
  };
  const fetch = async (url, init) => {
    const where = String(url).split("?")[0];
    const body = typeof init.body === "string" ? JSON.parse(init.body) : null;
    const how = JSON.stringify({ credentials: init.credentials, cache: init.cache, headers: init.headers });
    requests.push({ method: init.method, path: where, body, query: String(url).split("?")[1] || "", how });
    const reply = await answer(init.method, where, body);
    if (reply === "network") throw new TypeError("Failed to fetch");
    const text = JSON.stringify(reply.json);
    const read = async () => {
      if (reply.cut) throw new TypeError("network error"); // as Chrome's ERR_CONTENT_LENGTH_MISMATCH
      return text;
    };
    return { ok: reply.status >= 200 && reply.status < 300, status: reply.status, headers: { get: () => null }, text: read };
  };
  Object.assign(win, {
    document: doc,
    location,
    navigator: {},
    sessionStorage: storage(),
    localStorage: storage(),
    fetch,
    WebSocket: function WebSocket(url) {
      const sock = new FakeSocket(sockets);
      sock.url = String(url);
      return sock;
    },
    crypto: globalThis.crypto,
    URL,
    URLSearchParams,
    getComputedStyle: (node) => (node.tagName === "PRE" ? { paddingLeft: PRE_PADDING, paddingRight: PRE_PADDING } : {}),
    setTimeout: (fn) => hold(fn, false),
    setInterval: (fn) => hold(fn, true),
    clearTimeout: (id) => timers.delete(id),
    clearInterval: (id) => timers.delete(id),
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    },
  });
  Object.assign(win, globals || {});
  if (win.history === true) win.history = history;
  win.window = win;
  const context = vm.createContext(win);
  vm.runInContext(SOURCE, context, { filename: "app.js" });

  const page = {
    sockets, location,
    run: (code) => vm.runInContext(code, context),
    main: () => page.run("UI.main"),
    /* What the newest toast says, and every toast line on screen. Timers never fire on their
     * own here, so a line stays until TOAST_LINES newer ones push it out. */
    toast: () => page.run("UI.toast.childNodes.length ? UI.toast.childNodes[UI.toast.childNodes.length - 1].textContent : ''"),
    toasts: () => page.run("UI.toast.classList.contains('show') ? UI.toast.childNodes.map((line) => line.textContent) : []"),
    /* Every socket the page opened and the machine has not answered yet, accepted. */
    acceptSockets() {
      for (const sock of sockets) if (sock.readyState === 0) sock.accept();
    },
    live: () => sockets[sockets.length - 1],
    sent: (where) => requests.filter((one) => one.method === "POST" && one.path === where).map((one) => one.body),
    requests,
    /* The names of the functions timeouts still hold (an interval is always held, so says
     * nothing), and one fired by its name: a timeout is gone once fired, an interval stays, as
     * a browser's do. */
    timers: () => Array.from(timers.values()).filter((timer) => !timer.every).map((timer) => timer.fn.name).filter(Boolean).sort(),
    fireTimer(name) {
      for (const [id, timer] of Array.from(timers)) { // what it fires may set another: not this time
        if (timer.fn.name !== name) continue;
        if (!timer.every) timers.delete(id);
        timer.fn();
      }
    },
    /* The browser's Back: false once there is no entry of this page's before this one. */
    back() {
      if (at === 0) return false;
      const was = current;
      current = entries[--at];
      popped();
      if (current !== was) changed();
      return true;
    },
    /* Where the tab is in its history, and how many entries it has. */
    history: () => ({ at, length: entries.length }),
  };
  return page;
}

/* Let the page's promises, the fake's hashchange events and socket closes run out. */
async function settle() {
  for (let round = 0; round < 20; round++) await new Promise((resolve) => setImmediate(resolve));
}

function find(root, test) {
  return descendants(root).find(test) || null;
}

function buttonNamed(root, text) {
  return find(root, (node) => node.tagName === "BUTTON" && node.textContent === text);
}

function click(control) {
  if (control && !control.disabled) control.dispatch("click");
}

function unlockForm(page) {
  const input = find(page.main(), (node) => node.tagName === "INPUT" && node.type === "password");
  return input ? { input, form: input.parentNode } : null;
}

/* The browser fires `type` at "document" or "window": a wake, as the phone sees one. */
function fire(page, target, type) {
  page.run("for (const fn of (" + target + ".listeners." + type + " || []).slice()) fn({ type: " + JSON.stringify(type) + " });");
}

/* What the page sent on `sock` with `kind` in it, in order. */
function asked(sock, kind) {
  return sock.sent.filter((message) => Object.prototype.hasOwnProperty.call(message, kind)).map((message) => message[kind]);
}

/* The text of each node, in order. */
function textsOf(nodes) {
  return nodes.map((node) => node.textContent);
}

// --- the machine's answers -------------------------------------------------------------------

const FLEET = {
  project: { id: PROJECT, root: "/tmp/x" },
  name: "x",
  agents: [{ agent: { id: "agt_1", label: "coder-1", role: "coder" }, state: "waiting", detail: null }],
};
const ITEM = {
  id: NEEDS_ID,
  kind: "question",
  project: { id: PROJECT, name: "x" },
  agent: "coder-1",
  agent_id: "agt_1",
  reason: "coder-1 asks which approach to take",
  since: "2026-10-07T10:12:03+00:00",
  detail: { questions: [{ question: "Which?", options: [{ label: "A" }, { label: "B" }] }] },
  answers: [{ label: "1. A", keys: ["1"] }, { label: "2. B", keys: ["2"] }],
  actions: ["answer", "open", "dismiss"],
};

/* A machine that knows this phone, allows writes, and has `extra` routes besides. */
function signedIn(extra) {
  return (method, where, body) => {
    const key = method + " " + where;
    if (extra && Object.prototype.hasOwnProperty.call(extra, key)) return extra[key](body);
    if (key === "GET api/remote") return { status: 200, json: { allow_write: true, auto_off_at: null, version: "test" } };
    if (key === "GET api/needs") return { status: 200, json: { items: [] } };
    if (key === "GET api/actions/recent") return { status: 200, json: { actions: [] } };
    if (key === "GET api/fleet") return { status: 200, json: FLEET };
    if (key === "GET api/board") return { status: 200, json: { project: { id: PROJECT }, sessions: [], events: [] } };
    return { status: 404, json: { error: "not_found", message: "no such route" } };
  };
}

/* A machine that has never seen this phone: everything but the unlock is a 401. */
function signedOut(onUnlock) {
  return (method, where, body) => {
    if (method === "POST" && where === "api/unlock") return onUnlock(body);
    return { status: 401, json: { error: "unauthorized", message: "unlock first" } };
  };
}

// --- the scenarios -----------------------------------------------------------------------------

/* No cookie, opened at a given hash: the bare link, and a reload while at #/unlock. */
async function openedSignedOut(hash) {
  const page = bootPage(hash, signedOut(() => ({ status: 401, json: { error: "unauthorized" } })));
  await settle();
  return { hash: page.location.hash, form: !!unlockForm(page), main: page.main().textContent };
}

/* The passphrase is right, but the next request is still signed out: the cookie was not kept. */
async function unlockNotKept() {
  const page = bootPage("#/p/" + PROJECT + "/board", signedOut(() => ({ status: 200, json: { ok: true, device: { id: "dev_0a1b2c3d" } } })));
  await settle();
  const { input, form } = unlockForm(page);
  input.value = PASSPHRASE;
  form.dispatch("submit");
  await settle();
  const after = unlockForm(page);
  return {
    hash: page.location.hash,
    form: !!after,
    typed: after ? after.input.value : null,
    said: after ? find(after.form, (node) => node.className === "status").textContent : null,
    remembered: page.run("sessionStorage.getItem('asq.after')"),
  };
}

/* The control: the cookie is kept, so the page goes where the lock interrupted it. */
async function unlockKept() {
  let unlocked = false;
  const page = bootPage("#/p/" + PROJECT + "/board", (method, where, body) => {
    if (method === "POST" && where === "api/unlock") {
      unlocked = true;
      return { status: 200, json: { ok: true, device: { id: "dev_0a1b2c3d" } } };
    }
    return unlocked ? signedIn()(method, where, body) : { status: 401, json: { error: "unauthorized" } };
  });
  await settle();
  const { input, form } = unlockForm(page);
  input.value = PASSPHRASE;
  form.dispatch("submit");
  await settle();
  return { hash: page.location.hash, form: !!unlockForm(page) };
}

/* The passphrase typed at a link the machine answers `status` for unlocking: 404 once a
 * new link was made (regenerate-password --new-link) or auto-off passed, as everything
 * under a token it no longer has is; 401 for a wrong passphrase, the control. */
async function unlockAnswered(status, json) {
  const page = bootPage("#/", signedOut(() => ({ status, json })));
  await settle();
  const { input, form } = unlockForm(page);
  input.value = PASSPHRASE;
  form.dispatch("submit");
  await settle();
  const after = unlockForm(page);
  const heading = find(page.main(), (node) => node.tagName === "H2");
  return {
    form: !!after,
    said: after ? find(after.form, (node) => node.className === "status").textContent : null,
    heading: heading ? heading.textContent : null,
    main: page.main().textContent,
  };
}

/* The frame the machine sends for a pane subscription on the socket open now, on its next
 * tick; no rows, so nothing scrolls. The Live tab's keys wait for it. */
function paneCame(page, label) {
  page.live().frame("pane", { rows: [], width: 80, height: 0 }, { agent: label || "coder-1", project: PROJECT });
}

/* The agent view, live, with its socket open and its pane in: where Send is. */
async function agentView(extra) {
  const page = bootPage("#/p/" + PROJECT + "/a/coder-1/live", signedIn(extra));
  await settle();
  page.acceptSockets();
  page.live().frame("remote", { allow_write: true, auto_off_at: null, version: "test" });
  paneCame(page);
  await settle();
  return page;
}

async function typeAndSend(page, text) {
  const say = find(page.main(), (node) => node.tagName === "TEXTAREA" && node.className === "say");
  say.value = text;
  click(buttonNamed(page.main(), "Send"));
  await settle();
  return say;
}

function sendState(page) {
  const send = buttonNamed(page.main(), "Send");
  return { busy: send.classList.contains("busy"), disabled: send.disabled };
}

/* The first send-keys never reaches the machine; the socket looks healthy throughout. */
async function lostWrite() {
  let calls = 0;
  const page = await agentView({
    "POST api/send-keys": () => {
      calls += 1;
      return calls === 1 ? "network" : { status: 200, json: { agent: "coder-1", project: PROJECT, sent: true } };
    },
  });
  const say = await typeAndSend(page, "hello");
  const waiting = { sockets: page.sockets.length, firstClosed: page.sockets[0].readyState === 3, send: sendState(page) };
  page.acceptSockets();
  paneCame(page);
  await settle();
  return {
    waiting,
    bodies: page.sent("api/send-keys"),
    send: sendState(page),
    pending: page.run("S.pending.size"),
    offline: page.run("S.offline"),
    bannerHidden: page.run("UI.banner.hidden"),
    typed: say.value,
  };
}

/* The retry is lost as well; the ledger reports later that the first one did run. */
async function lostTwice() {
  const page = await agentView({ "POST api/send-keys": () => "network" });
  await typeAndSend(page, "hello");
  page.acceptSockets();
  paneCame(page);
  await settle();
  const bodies = page.sent("api/send-keys");
  const said = page.toast();
  const id = bodies.length ? bodies[0].request_id : null;
  const orphaned = page.run("S.orphans.has(" + JSON.stringify(id) + ")");
  page.live().frame("action", { actions: [{ request_id: id, endpoint: "send-keys", status: 200, body: { sent: true }, at: "2026-10-07T10:13:00+00:00" }] });
  await settle();
  return { bodies, said, orphaned, later: page.toast(), send: sendState(page) };
}

/* Results that arrive together. A tab reloaded with three writes in flight, and the ledger's
 * first read reports two of them (newest first: a Stop refused, then a Restart done); then,
 * in one tick of the socket, the frame that turns writes off and the third write's result.
 * The toast lines on screen after each; then after a fifth line. */
async function toastsTogether() {
  const now = Date.now();
  const kept = storage();
  kept.setItem("asq.pending", JSON.stringify([
    ["rq-restart", "Restart coder-1", now - 20000], ["rq-stop", "Stop coder-2", now - 10000], ["rq-tell", "Tell coder-3", now - 5000],
  ]));
  const at = "2026-10-07T10:13:00+00:00";
  const ledger = [
    { request_id: "rq-stop", endpoint: "agent/stop", status: 409, body: { error: "still_busy", message: "Escape was sent; coder-2 has not stopped yet" }, at },
    { request_id: "rq-restart", endpoint: "agent/restart", status: 200, body: { label: "coder-1" }, at },
  ];
  const page = bootPage("#/", signedIn({ "GET api/actions/recent": () => ({ status: 200, json: { actions: ledger } }) }), { sessionStorage: kept });
  await settle();
  const read = page.toasts();
  page.acceptSockets();
  await settle();
  page.live().frame("remote", { allow_write: false, auto_off_at: null, version: "test" });
  page.live().frame("action", { actions: [{ request_id: "rq-tell", endpoint: "agent/tell", status: 200, body: { delivered: true }, at }] });
  await settle();
  const frames = page.toasts();
  page.run("toast('A fifth line')");
  return { read, frames, capped: page.toasts(), newest: page.toast(), orphans: page.run("S.orphans.size") };
}

/* A send-keys the machine ran and answered 200, its connection dropping halfway through the
 * body, the socket healthy throughout; then the reconnect, and the retry answered whole. And
 * the feed's first read cut the same way. Where the page is, what it sent, and what it says. */
async function bodyCut() {
  let calls = 0;
  const page = await agentView({
    "POST api/send-keys": () => {
      calls += 1;
      return { status: 200, json: { agent: "coder-1", project: PROJECT, sent: true }, cut: calls === 1 };
    },
  });
  const say = await typeAndSend(page, "hello");
  const shown = buttonNamed(page.main(), "Send") ? sendState(page) : null; // the off screen has none
  const waiting = { off: page.run("S.off"), sockets: page.sockets.length, send: shown };
  page.acceptSockets();
  paneCame(page);
  await settle();
  const feed = bootPage("#/", signedIn({ "GET api/needs": () => ({ status: 200, json: { items: [] }, cut: true }) }));
  await settle();
  const said = find(feed.main(), (node) => node.className === "empty");
  return {
    write: {
      waiting, off: page.run("S.off"), bodies: page.sent("api/send-keys"), typed: say.value,
      pending: page.run("S.pending.size"), orphans: page.run("S.orphans.size"), toast: page.toast(),
    },
    read: { off: feed.run("S.off"), said: said ? said.textContent : null, sockets: feed.sockets.length },
  };
}

/* Send with nothing typed, ⏎ on as it is by default. */
async function emptySend() {
  const page = await agentView({ "POST api/send-keys": () => ({ status: 200, json: { sent: true } }) });
  const say = await typeAndSend(page, "");
  const enter = find(page.main(), (node) => node.tagName === "LABEL" && node.textContent === "⏎");
  return {
    bodies: page.sent("api/send-keys"), toast: page.toast(), enterOn: enter.firstChild.checked,
    keyHint: say.attrs.enterkeyhint || null,
  };
}

/* A pad key whose request never arrived, and a phone that is back an hour later. */
async function lostKeyLongAgo() {
  const page = await agentView({ "POST api/send-keys": () => "network" });
  click(buttonNamed(page.main(), "1"));
  await settle();
  page.run("Date.now = ((then) => () => then + 3600000)(Date.now());"); // the phone slept
  page.acceptSockets();
  await settle();
  const bodies = page.sent("api/send-keys");
  return {
    bodies,
    said: page.toast(),
    orphaned: page.run("S.orphans.has(" + JSON.stringify(bodies[0].request_id) + ")"),
    pending: page.run("S.pending.size"),
  };
}

/* A pad key whose request never arrived, then the device is revoked: the phone unlocks again. */
async function lostThenSignedOut() {
  const page = await agentView({
    "POST api/send-keys": () => "network",
    "POST api/unlock": () => ({ status: 200, json: { ok: true, device: { id: "dev_4e5f6a7b" } } }),
  });
  click(buttonNamed(page.main(), "1"));
  await settle();
  page.live().fire("close", { code: 4401 }); // the reconnect is refused: signed out
  await settle();
  const said = page.toast();
  const hash = page.location.hash;
  const { input, form } = unlockForm(page);
  input.value = PASSPHRASE;
  form.dispatch("submit");
  await settle();
  page.acceptSockets();
  await settle();
  return { said, hash, bodies: page.sent("api/send-keys"), pending: page.run("S.pending.size") };
}

/* A read lost while the socket is fine: the next frame says the machine is there. In
 * between, the browser says the phone itself went offline. */
async function lostRead() {
  const page = await agentView({ "GET api/projects": () => "network" });
  const banner = () => (page.run("UI.banner.hidden") ? "" : page.run("UI.banner").textContent);
  click(buttonNamed(page.run("UI.nav"), "Projects"));
  await settle();
  const lost = { offline: page.run("S.offline"), banner: banner(), said: page.main().textContent };
  page.run("navigator.onLine = false; for (const fn of window.listeners.offline) fn({ type: 'offline' });");
  const phone = banner();
  page.run("navigator.onLine = true;");
  page.live().frame("heartbeat", { needs_scanned_at: null });
  await settle();
  return { lost, phone, offline: page.run("S.offline"), bannerHidden: page.run("UI.banner.hidden") };
}

/* A Tell from the Actions menu whose request was lost, the phone offline so its new socket never
 * opens: the sheet while it waits, once its 15 s are up, after Escape, and the writes that went
 * once the phone is back; and a card's Reply, and a note on the Board tab, lost the same way,
 * while they wait. */
async function offlineSheet() {
  const page = await agentView({ "POST api/agent/tell": () => "network" });
  page.live().frame("fleet", FLEET);
  await settle();
  click(buttonNamed(page.main(), "Actions…"));
  click(buttonNamed(page.run("UI.sheet"), "Tell…"));
  find(page.run("UI.sheet"), (node) => node.tagName === "TEXTAREA").value = "carry on";
  click(buttonNamed(page.run("UI.sheet"), "Tell"));
  await settle();
  const sheet = (one) => {
    const wrap = one.run("UI.sheet");
    const close = buttonNamed(wrap, "Close");
    const said = find(wrap, (node) => node.className === "status");
    return { title: sheetTitle(one), busy: wrap.classList.contains("busy"), close: close ? close.disabled : null, said: said ? said.textContent : null };
  };
  const waiting = sheet(page);
  page.run("Date.now = ((then) => () => then + 16000)(Date.now());");
  page.fireTimer("tooLate");
  await settle();
  const late = sheet(page);
  page.run("for (const fn of document.listeners.keydown || []) fn({ type: 'keydown', key: 'Escape' });");
  const escaped = sheetTitle(page);
  page.acceptSockets();
  await settle();
  const question = Object.assign({}, ITEM, { kind: "board_question", detail: { text: "Which store?", author: "lead-1" }, answers: [], actions: ["reply"] });
  const feed = bootPage("#/", signedIn({ "GET api/needs": () => ({ status: 200, json: { items: [question] } }), "POST api/note": () => "network" }));
  await settle();
  feed.acceptSockets();
  await settle();
  click(buttonNamed(feed.main(), "Reply…"));
  find(feed.run("UI.sheet"), (node) => node.tagName === "TEXTAREA").value = "Postgres";
  click(buttonNamed(feed.run("UI.sheet"), "Post"));
  await settle();
  const board = bootPage("#/p/" + PROJECT + "/board", signedIn({ "POST api/note": () => "network" }));
  await settle();
  board.acceptSockets();
  await settle();
  find(board.main(), (node) => node.tagName === "TEXTAREA").value = "ship it";
  click(buttonNamed(board.main(), "Post"));
  await settle();
  const noting = find(board.main(), (node) => node.className === "status").textContent;
  return { waiting, late, escaped, told: page.sent("api/agent/tell").length, replying: sheet(feed), noting };
}

/* Two quick taps on a card's answers while the first is in flight. */
async function quickAnswerTwice() {
  const held = deferred();
  const page = bootPage("#/", signedIn({
    "GET api/needs": () => ({ status: 200, json: { items: [ITEM] } }),
    "POST api/needs/answer": () => held.promise,
  }));
  await settle();
  page.acceptSockets();
  await settle();
  const answers = () => page.main().querySelectorAll("button.qa");
  click(answers()[0]);
  await settle();
  const inFlight = answers().map((control) => control.disabled);
  click(answers()[0]);
  click(answers()[1]);
  await settle();
  const sentWhileHeld = page.sent("api/needs/answer").length;
  held.settle({ status: 200, json: { id: NEEDS_ID, sent: ["1"] } });
  await settle();
  return { inFlight, sentWhileHeld, after: answers().map((control) => control.disabled), toast: page.toast() };
}

/* A browser that can take pushes and already holds a subscription made against
 * `key` (bytes); what it is asked to do is written down in `log`, and what the page listens
 * for from its service worker in `heard`. */
function fakePush(key) {
  const log = [];
  const heard = {};
  const subscription = (name, bytes) => ({
    options: { applicationServerKey: Uint8Array.from(bytes).buffer },
    unsubscribe: async () => { log.push("unsubscribe " + name); return true; },
    toJSON: () => ({ endpoint: "https://fcm.googleapis.com/fcm/send/" + name, keys: { p256dh: "p", auth: "a" } }),
  });
  let current = subscription("old", key);
  const registration = {
    pushManager: {
      getSubscription: async () => current,
      subscribe: async (options) => {
        log.push("subscribe " + Buffer.from(options.applicationServerKey).toString("base64url"));
        current = subscription("new", options.applicationServerKey);
        return current;
      },
    },
  };
  const serviceWorker = {
    getRegistration: async () => registration, register: async () => registration, ready: Promise.resolve(registration),
    addEventListener: (type, fn) => { (heard[type] = heard[type] || []).push(fn); },
  };
  const globals = {
    navigator: { serviceWorker, userAgent: "Mozilla/5.0 (Linux; Android 14)", platform: "Linux" },
    PushManager: function PushManager() {},
    Notification: { permission: "granted", requestPermission: async () => "granted" },
    isSecureContext: true,
    atob: (text) => Buffer.from(text, "base64").toString("binary"),
  };
  return { log, heard, globals };
}

const KEY_NOW = Array.from({ length: 65 }, (unused, n) => (n * 7 + 4) % 256);
const KEY_BEFORE = Array.from({ length: 65 }, (unused, n) => (n * 11 + 4) % 256);

/* The machine's push answers, its VAPID key KEY_NOW; what was subscribed is collected. */
function pushRoutes(subscribed) {
  return {
    "GET api/push": () => ({ status: 200, json: { supported: true, vapid_public_key: Buffer.from(KEY_NOW).toString("base64url"), subscribed: false } }),
    "POST api/push/subscribe": (body) => {
      subscribed.push(body.endpoint);
      return { status: 201, json: { subscribed: true } };
    },
  };
}

/* Settings → Turn on, in a browser whose subscription was made against `held`. */
async function pushTurnedOn(held) {
  const push = fakePush(held);
  const subscribed = [];
  const page = bootPage("#/settings", signedIn(pushRoutes(subscribed)), push.globals);
  await settle();
  page.acceptSockets();
  await settle();
  const turnOn = buttonNamed(page.main(), "Turn on");
  click(turnOn);
  await settle();
  return { offered: !!turnOn, log: push.log, subscribed };
}

/* An unlock, in a browser whose subscription was made against `held`. */
async function pushAfterUnlock(held) {
  const push = fakePush(held);
  const subscribed = [];
  let unlocked = false;
  const routes = pushRoutes(subscribed);
  const page = bootPage("#/", (method, where, body) => {
    if (method === "POST" && where === "api/unlock") {
      unlocked = true;
      return { status: 200, json: { ok: true, device: { id: "dev_0a1b2c3d" } } };
    }
    return unlocked ? signedIn(routes)(method, where, body) : { status: 401, json: { error: "unauthorized" } };
  }, push.globals);
  await settle();
  const { input, form } = unlockForm(page);
  input.value = PASSPHRASE;
  form.dispatch("submit");
  await settle();
  return { log: push.log, subscribed };
}

/* Heartbeats whose last needs scan is a minute old, then two seconds old, on the feed. */
async function scansStopped() {
  const page = bootPage("#/", signedIn());
  await settle();
  page.acceptSockets();
  await settle();
  const beat = (scanned, ts) => page.live().frame("heartbeat", { needs_scanned_at: scanned }, { ts });
  beat("2026-10-07T10:00:00+00:00", "2026-10-07T10:01:00+00:00");
  await settle();
  const stopped = { said: page.main().textContent, greyed: page.main().querySelectorAll("div.data.behind").length };
  beat("2026-10-07T10:01:08+00:00", "2026-10-07T10:01:10+00:00");
  await settle();
  return { stopped, again: { said: page.main().textContent, greyed: page.main().querySelectorAll("div.data.behind").length } };
}

/* Nothing heard for longer than the stale limit, on the devices screen. */
async function staleDevices() {
  const page = bootPage("#/devices", signedIn({
    "GET api/devices": () => ({
      status: 200,
      json: [
        { id: "dev_0a1b2c3d", current: true, signed_in: true, ua: "this phone", last_seen: null },
        { id: "dev_4e5f6a7b", current: false, signed_in: true, ua: "another", last_seen: null },
      ],
    }),
  }));
  await settle();
  page.acceptSockets();
  await settle();
  page.run("S.lastFrameAt = Date.now() - 60000; checkStale();");
  return {
    stale: page.run("S.stale"),
    signOut: buttonNamed(page.main(), "Sign out").disabled,
    revoke: buttonNamed(page.main(), "Revoke").disabled,
  };
}

/* Interrupt & tell: the text was pasted at the prompt, but tmux could not press Enter. */
async function tellNotSent() {
  const how = "interrupted it with Escape, then pasted it at its prompt, but tmux could not press Enter (no pane) — press Enter on the pad to send it";
  const page = await agentView({
    "POST api/agent/tell": (body) => ({ status: 200, json: { label: "coder-1", delivered: false, how, mode: body.mode, project: PROJECT } }),
  });
  page.live().frame("fleet", FLEET);
  await settle();
  click(buttonNamed(page.main(), "Actions…"));
  click(buttonNamed(page.run("UI.sheet"), "Interrupt & tell…"));
  const text = find(page.run("UI.sheet"), (node) => node.tagName === "TEXTAREA");
  text.value = "commit it";
  click(buttonNamed(page.run("UI.sheet"), "Interrupt & tell"));
  await settle();
  const told = page.sent("api/agent/tell").map((body) => body.mode);
  return { told, toast: page.toast(), how };
}

/* The live pane's inverted cells, as the program shows its cursor, hides it (Claude Code
 * does), and as a machine that does not say whether it shows (an older one) draws it. */
async function paneCursor() {
  const page = await agentView();
  const cells = async (visible) => {
    const frame = { rows: ["> hello", "  Esc to cancel"], cursor: [7, 0], width: 80, height: 2 };
    if (visible !== undefined) frame.cursor_visible = visible;
    page.live().frame("pane", frame, { agent: "coder-1", project: PROJECT });
    await settle();
    return page.main().querySelectorAll("span.cur").length;
  };
  return { shown: await cells(true), hidden: await cells(false), unsaid: await cells(undefined) };
}

/* Stop, on an agent that shows a prompt: the machine refuses in its API's words, the
 * sheet says why in its own and adds the dismissal to what Stop will do, and the next tap
 * sends dismiss_dialog. */
async function stopAtAPrompt() {
  const refused = "coder-1 is showing a prompt; stopping would answer it — send dismiss_dialog: true to press Esc (No) first";
  const stopped = { agent: { id: "agt_1", label: "coder-1" }, claims_released: [], release_failed: null, project: PROJECT };
  const page = await agentView({
    "POST api/agent/stop": (body) => (body.dismiss_dialog === true
      ? { status: 200, json: stopped }
      : { status: 409, json: { error: "dialog_open", message: refused } }),
  });
  page.live().frame("fleet", FLEET);
  await settle();
  click(buttonNamed(page.main(), "Actions…"));
  click(buttonNamed(page.run("UI.sheet"), "Stop…"));
  click(buttonNamed(page.run("UI.sheet"), "Stop"));
  await settle();
  const said = find(page.run("UI.sheet"), (node) => node.className === "status").textContent;
  const lead = find(page.run("UI.sheet"), (node) => node.className === "lead").textContent;
  click(buttonNamed(page.run("UI.sheet"), "Press Esc (No) first"));
  await settle();
  return { said, lead, dismissed: page.sent("api/agent/stop").map((body) => body.dismiss_dialog === true), toast: page.toast() };
}

/* A pad key refused read_only: writes went off on the machine, and no remote frame has
 * said so yet. */
async function refusedReadOnly() {
  const page = await agentView({
    "POST api/send-keys": () => ({ status: 403, json: { error: "read_only", message: "writes are off" } }),
  });
  const pill = () => !page.run("UI.ro.hidden");
  const before = { send: sendState(page), pill: pill() };
  click(buttonNamed(page.main(), "1"));
  await settle();
  return {
    before,
    send: sendState(page),
    keys: page.main().querySelectorAll("button.w.key").map((key) => key.disabled),
    pill: pill(),
    readOnly: page.run("document.body.classList.contains('ro')"),
    sheet: page.run("UI.sheet").textContent,
  };
}

/* The Settings screen's line on this page while the machine's word changes under it: a
 * `remote` frame turning writes off and naming a version, one turning them on again, and a
 * pad key's 403 read_only answered after the human went on to Settings. */
async function settingsFacts() {
  const facts = (page) => {
    const panel = page.main().querySelectorAll("section.panel").find((one) => one.firstChild.textContent === "This page");
    return panel.querySelectorAll("p.muted")[0].textContent;
  };
  const page = bootPage("#/settings", signedIn());
  await settle();
  page.acceptSockets();
  await settle();
  const before = facts(page);
  page.live().frame("remote", { allow_write: false, auto_off_at: null, version: "0.7.0" });
  await settle();
  const off = { line: facts(page), pill: !page.run("UI.ro.hidden") };
  page.live().frame("remote", { allow_write: true, auto_off_at: null, version: "0.7.0" });
  await settle();
  const on = facts(page);
  const answer = deferred();
  const late = await agentView({ "POST api/send-keys": () => answer.promise });
  click(buttonNamed(late.main(), "1"));
  await settle();
  late.location.hash = "#/settings";
  await settle();
  const left = facts(late);
  answer.settle({ status: 403, json: { error: "read_only", message: "writes are off" } });
  await settle();
  return { before, off, on, left, refused: facts(late) };
}

/* The live tab's scroll: after a pane that could not be read, after the first screen,
 * and after another screen once the human scrolled up to read. */
async function liveScroll() {
  const page = await agentView();
  page.run("UI.main.scrollHeight = 2400; UI.main.scrollTop = 0;");
  const pane = (rows, error) => page.live().frame("pane", { rows, cursor: [0, 39], width: 80, height: 40, error }, { agent: "coder-1", project: PROJECT });
  pane([], "can't find pane");
  await settle();
  const unread = page.run("UI.main.scrollTop");
  const rows = Array.from({ length: 40 }, (unused, n) => (n === 36 ? "❯ 1. Yes" : "line " + n));
  pane(rows);
  await settle();
  const first = page.run("UI.main.scrollTop");
  page.run("UI.main.scrollTop = 300;");
  pane(rows.map((row, n) => (n === 39 ? "a spinner moved" : row)));
  await settle();
  return { unread, first, later: page.run("UI.main.scrollTop") };
}

/* The key pad opened at the foot of the pane, and again once the human scrolled up to read.
 * Opening it grows the input bar; this fake has no layout, so the height stays put. */
async function padScroll() {
  const page = await agentView();
  const keys = () => click(buttonNamed(page.main(), "Keys"));
  page.run("UI.main.scrollHeight = 2400; UI.main.clientHeight = 600; UI.main.scrollTop = 1800;");
  keys();
  await settle();
  const atFoot = page.run("UI.main.scrollTop");
  keys();
  page.run("UI.main.scrollTop = 300;");
  keys();
  await settle();
  return { atFoot, reading: page.run("UI.main.scrollTop"), open: page.main().querySelectorAll("div.pad.open").length === 1 };
}

/* What a screen reader is given for each key of the pad, and for the ⏎ toggle beside Send. */
async function keyNames() {
  const page = await agentView();
  const keys = page.main().querySelectorAll("button.key").map((key) => [key.textContent, key.attrs["aria-label"] || null]);
  const toggle = find(page.main(), (node) => node.tagName === "INPUT" && node.type === "checkbox" && node.parentNode.textContent === "⏎");
  return { keys, enterToggle: toggle ? toggle.attrs["aria-label"] || null : null };
}

/* The board the socket asks for, screen by screen, and on the socket a wake opens. */
async function boardOnItsTab() {
  const page = bootPage("#/", signedIn());
  await settle();
  page.acceptSockets();
  await settle();
  const go = async (hash) => {
    page.run("pageGo(" + JSON.stringify(hash) + ")");
    await settle();
    return asked(page.sockets[0], "subscribe_board");
  };
  const steps = { feed: asked(page.sockets[0], "subscribe_board") };
  steps.fleet = await go("#/p/" + PROJECT + "/fleet");
  steps.board = await go("#/p/" + PROJECT + "/board");
  steps.agent = await go("#/p/" + PROJECT + "/a/coder-1/live");
  await go("#/p/" + PROJECT + "/board");
  fire(page, "document", "visibilitychange");
  page.acceptSockets();
  await settle();
  steps.woken = asked(page.live(), "subscribe_board");
  steps.sockets = page.sockets.length;
  return steps;
}

/* The fleet the socket asks for, screen by screen: none on the feed, its project's on a
 * project's tabs and on an agent's screen, asked once, false once they are left, and none
 * on the socket a wake opens there. */
async function fleetOnItsScreens() {
  const page = bootPage("#/", signedIn());
  await settle();
  page.acceptSockets();
  await settle();
  const fleet = () => asked(page.sockets[0], "subscribe_fleet");
  const go = async (hash) => {
    page.run("pageGo(" + JSON.stringify(hash) + ")");
    await settle();
    return fleet();
  };
  const steps = { feed: fleet() };
  steps.project = await go("#/p/" + PROJECT + "/fleet");
  await go("#/p/" + PROJECT + "/board");
  steps.agent = await go("#/p/" + PROJECT + "/a/coder-1/live");
  steps.left = await go("#/devices");
  fire(page, "document", "visibilitychange");
  page.acceptSockets();
  await settle();
  steps.woken = asked(page.live(), "subscribe_fleet");
  return steps;
}

/* The Board tab with a board frame on it, left for the Fleet tab and opened again, its read
 * held: what the tab shows meanwhile, the reads it made, and what it shows once answered. */
async function boardReopened() {
  const reads = [];
  const page = bootPage("#/p/" + PROJECT + "/board", signedIn({
    "GET api/board": () => (reads[reads.length] = deferred()).promise,
  }));
  await settle();
  page.acceptSockets();
  page.live().frame("board", { project: { id: PROJECT }, sessions: [], events: [note(1, "from before")] }, { project: PROJECT });
  await settle();
  reads[0].settle({ status: 200, json: { project: { id: PROJECT }, sessions: [], events: [note(1, "from before")] } });
  await settle();
  const shown = () => page.main().querySelectorAll("div.events")[0].childNodes.map((one) => {
    const text = find(one, (node) => node.className === "text");
    return text ? text.textContent : one.textContent;
  });
  const first = shown();
  page.run("pageGo('#/p/" + PROJECT + "/fleet')");
  await settle();
  page.run("pageGo('#/p/" + PROJECT + "/board')");
  await settle();
  const reopened = { shown: shown(), reads: reads.length };
  reads[reads.length - 1].settle({ status: 200, json: { project: { id: PROJECT }, sessions: [], events: [note(1, "from before"), note(2, "since")] } });
  await settle();
  return { first, reopened, answered: shown() };
}

/* The Board tab where every project's board is AISQUARE_TEAM_HUB's, whose own project id is
 * not the tab's: what it shows of a frame for another pid, and of a frame and of a read that
 * carry the hub's board. Then a board that could not be read: a refused read, a frame that
 * says why, and the board that came once it could be read. */
async function boardAnswers() {
  const hub = { project: { id: "prj_hub" }, sessions: [], events: [note(1, "on the hub")] };
  const shown = (page) => page.main().querySelectorAll("div.events")[0].childNodes.map((one) => {
    const text = find(one, (node) => node.className === "text");
    return text ? text.textContent : one.textContent;
  });
  const opened = async () => {
    const read = deferred();
    const page = bootPage("#/p/" + PROJECT + "/board", signedIn({ "GET api/board": () => read.promise }));
    await settle();
    page.acceptSockets();
    await settle();
    return { page, read };
  };
  const framed = await opened();
  framed.page.live().frame("board", Object.assign({}, hub, { events: [note(2, "prj_y's")] }), { project: "prj_y" });
  await settle();
  const other = shown(framed.page);
  framed.page.live().frame("board", hub, { project: PROJECT });
  await settle();
  const read = await opened();
  read.read.settle({ status: 200, json: hub });
  await settle();
  const failed = await opened();
  failed.read.settle({ status: 404, json: { error: "not_found", message: "no project matches 'prj_x'" } });
  await settle();
  const refused = shown(failed.page);
  failed.page.live().frame("board", hub, { project: PROJECT });
  await settle();
  const unread = await opened();
  unread.page.live().frame("board", { project: null, sessions: [], events: [], error: "the agent orchestrator is disabled" }, { project: PROJECT });
  await settle();
  unread.read.settle({ status: 503, json: { error: "unavailable", message: "the agent orchestrator is disabled" } });
  await settle();
  return {
    other, frame: shown(framed.page), read: shown(read.page),
    refused, readable: shown(failed.page), unread: shown(unread.page),
  };
}

/* A transcript read on a phone in UTC-7 from a machine that sends each turn's time as UTC:
 * the lines it draws. */
async function transcriptTimes() {
  const zone = process.env.TZ;
  process.env.TZ = "America/Los_Angeles";
  try {
    const page = bootPage("#/p/" + PROJECT + "/a/coder-1/transcript", signedIn({
      "GET api/transcript/coder-1": () => ({
        status: 200,
        json: {
          lines: ["\u001b[1;36m> you\u001b[0m", "  commit it", "", "\u001b[1;32m* claude\u001b[0m", "  done", ""],
          cursor: null, more: false, stamps: { 0: "2026-10-07T17:05:00+00:00", 3: "2026-10-07T17:06:00+00:00" },
        },
      }),
    }));
    await settle();
    return page.main().querySelectorAll("pre.transcript")[0].childNodes.map((line) => line.textContent);
  } finally {
    if (zone === undefined) delete process.env.TZ;
    else process.env.TZ = zone;
  }
}

/* The Fleet tab on a phone in UTC-7, with a row parked on a limit that lifts at 13:10 UTC,
 * 06:10 there, beside a waiting one: what each row's second line says. */
async function limitTimes() {
  const zone = process.env.TZ;
  process.env.TZ = "America/Los_Angeles";
  try {
    const parked = {
      agent: { id: "agt_2", label: "coder-2", role: "coder" }, state: "limited", detail: "limit resets in 3h 09m (13:10)",
      session: { limit_resets_at: "2026-10-07T13:10:00+00:00" },
    };
    const fleet = Object.assign({}, FLEET, { agents: [FLEET.agents[0], parked] });
    const page = bootPage("#/p/" + PROJECT + "/fleet", signedIn({ "GET api/fleet": () => ({ status: 200, json: fleet }) }));
    page.run("Date.now = () => Date.parse('2026-10-07T10:00:07+00:00');");
    await settle();
    return page.main().querySelectorAll("span.muted").map((line) => line.textContent);
  } finally {
    if (zone === undefined) delete process.env.TZ;
    else process.env.TZ = zone;
  }
}

/* The Board tab on a phone in UTC-7, from a machine in UTC+5:30 whose board's limited lines
 * say the reset by its own clock: coder-1 parked on a limit that lifts at 13:10 UTC (18:40
 * on the machine, 06:10 on the phone), an older limited line of coder-1's, one of coder-2's
 * (back at work), and a note that quotes such a line. Each line's text, newest first, as
 * GET api/board drew them, then as the board frame that follows does: a frame's sessions
 * are only their id, label and role (remote_server.remote_board_frame). */
async function boardLimitTimes() {
  const zone = process.env.TZ;
  process.env.TZ = "America/Los_Angeles";
  try {
    const switchIt = (label) => " — `aisquare fleet switch " + label + "` moves it to the account with the most headroom (or wait for the reset)";
    const limited = (seq, session, text) => ({ kind: "team.limited", ts: "2026-10-07T10:00:00+00:00", payload: { seq, text, session_id: session } });
    const board = {
      project: { id: PROJECT },
      sessions: [
        { id: "ses_1", label: "coder-1", role: "coder", state: "limited", limit_resets_at: "2026-10-07T13:10:00+00:00" },
        { id: "ses_2", label: "coder-2", role: "coder", state: "working", limit_resets_at: "2026-10-07T10:12:00+00:00" },
      ],
      events: [
        limited(1, "ses_1", "coder-1 hit its session limit · resets in 2d 4h (Fri 02:00)" + switchIt("coder-1")),
        limited(2, "ses_2", "coder-2 hit its session limit · resets in 12m" + switchIt("coder-2")),
        limited(3, "ses_1", "coder-1 hit its session limit · resets in 3h 10m (18:40)" + switchIt("coder-1")),
        note(4, "coder-1 said: hit its session limit · resets in 3h 10m (18:40)"),
      ],
    };
    const page = bootPage("#/p/" + PROJECT + "/board", signedIn({ "GET api/board": () => ({ status: 200, json: board }) }));
    page.run("Date.now = () => Date.parse('2026-10-07T10:00:07+00:00');");
    await settle();
    const lines = () => page.main().querySelectorAll("p.text").map((line) => line.textContent);
    const read = lines();
    const sessions = board.sessions.map(({ id, label, role }) => ({ id, label, role }));
    page.live().frame("board", Object.assign({}, board, { sessions, events: board.events.concat([note(5, "after")]) }), { project: PROJECT });
    await settle();
    return { read, framed: lines() };
  } finally {
    if (zone === undefined) delete process.env.TZ;
    else process.env.TZ = zone;
  }
}

const OLDER_REMOTE = { allow_write: false, auto_off_at: null, version: "test" };

function note(seq, text) {
  return { kind: "team.note", ts: "2026-10-07T10:00:00+00:00", payload: { seq, text, session_id: null } };
}

/* Reads the page makes while its socket sends the same kind, answered only after the socket's
 * frame, and older than it: the feed and the strip on a wake, the Board and Fleet tabs as they
 * open (one answered with a failure); and a board read no frame came before, the control. */
async function readsAfterFrames() {
  const held = {};
  let holding = false;
  const hold = (key, now) => () => (holding ? (held[key] = deferred()).promise : now);
  const page = bootPage("#/", signedIn({
    "GET api/needs": hold("needs", { status: 200, json: { items: [] } }),
    "GET api/remote": hold("remote", { status: 200, json: OLDER_REMOTE }),
  }));
  await settle();
  page.acceptSockets();
  await settle();
  holding = true;
  fire(page, "document", "visibilitychange");
  page.acceptSockets();
  page.live().frame("remote", { allow_write: true, auto_off_at: null, version: "test" });
  page.live().frame("needs_you", { items: [ITEM] });
  await settle();
  held.needs.settle({ status: 200, json: { items: [] } });
  held.remote.settle({ status: 200, json: OLDER_REMOTE });
  await settle();
  const wake = { cards: page.main().querySelectorAll("div.card").length, writable: page.run("writable()") };

  const opened = async (tab, answer, frame) => {
    const read = deferred();
    const one = bootPage("#/p/" + PROJECT + "/" + tab, signedIn({ ["GET api/" + tab]: () => read.promise }));
    await settle();
    one.acceptSockets();
    if (frame) one.live().frame(tab, frame, tab === "board" ? { project: PROJECT } : undefined);
    await settle();
    read.settle(answer);
    await settle();
    return one.main().querySelectorAll(tab === "board" ? "div.event" : "button.row").length;
  };
  const board = (events) => ({ project: { id: PROJECT }, sessions: [], events });
  const two = Object.assign({}, FLEET, { agents: FLEET.agents.concat({ agent: { id: "agt_2", label: "coder-2", role: "coder" }, state: "working" }) });
  return {
    wake,
    board: await opened("board", { status: 200, json: board([note(1, "first")]) }, board([note(1, "first"), note(2, "second")])),
    fleet: await opened("fleet", { status: 200, json: FLEET }, two),
    fleetFailed: await opened("fleet", { status: 503, json: { error: "unavailable", message: "tmux" } }, two),
    boardAlone: await opened("board", { status: 200, json: board([note(1, "first")]) }, null),
  };
}

/* The browser's Back, pressed until it leaves the page (six times at most), and where each
 * press landed: after an unlock, after a gone agent's tab sent the page to its fleet, after
 * the phone was signed out on a project's screen, and after a tab left at #/unlock was opened
 * again once signed in (another tab unlocked, or a reload). */
async function backLeaves() {
  const backs = async (page) => {
    const landed = [];
    for (let n = 0; n < 6; n++) {
      if (!page.back()) return { at: page.location.hash, landed, left: true };
      await settle();
      landed.push(page.location.hash);
    }
    return { at: page.location.hash, landed, left: false };
  };
  let unlocked = false;
  const locked = bootPage("", (method, where, body) => {
    if (method === "POST" && where === "api/unlock") {
      unlocked = true;
      return { status: 200, json: { ok: true, device: { id: "dev_0a1b2c3d" } } };
    }
    return unlocked ? signedIn()(method, where, body) : { status: 401, json: { error: "unauthorized" } };
  });
  await settle();
  const { input, form } = unlockForm(locked);
  input.value = PASSPHRASE;
  form.dispatch("submit");
  await settle();
  const afterUnlock = await backs(locked);
  const gone = bootPage("#/p/" + PROJECT + "/fleet", signedIn({
    "GET api/transcript/coder-1": () => ({ status: 404, json: { error: "no_such_agent", message: "no live agent 'coder-1'" } }),
  }));
  await settle();
  for (const tab of ["live", "transcript"]) {
    gone.run("pageGo('#/p/" + PROJECT + "/a/coder-1/" + tab + "')");
    await settle();
  }
  const afterGone = await backs(gone);
  const out = bootPage("#/", signedIn());
  await settle();
  out.acceptSockets();
  out.run("pageGo('#/p/" + PROJECT + "/fleet')");
  await settle();
  out.live().fire("close", { code: 4401 });
  await settle();
  const afterSignedOut = await backs(out);
  const reopened = bootPage("#/unlock", signedIn());
  await settle();
  return { afterUnlock, afterGone, afterSignedOut, unlockedAtUnlock: await backs(reopened) };
}

/* The title of the sheet on screen, or null. */
function sheetTitle(page) {
  if (!page.run("UI.sheet.classList.contains('open')")) return null;
  const title = find(page.run("UI.sheet"), (node) => node.tagName === "H2");
  return title ? title.textContent : null;
}

/* Answers that come after the human moved on. A second ^C to coder-1 is refused
 * double_press while coder-2's screen shows its own Ctrl-C sheet, on coder-1's own screen
 * once its Actions sheet is open, and once coder-2's screen shows with no sheet on it; a
 * restart answers after Back left its sheet and the human opened Tell and typed in it (done,
 * failed and stale), and once more with that Tell sent and out; a Reply, and a card's Tell
 * answered stale, once another is begun; and a Stop answered dialog_open once Back closed
 * its sheet. */
async function lateAnswers() {
  const keys = [];
  const page = await agentView({
    "POST api/send-keys": () => (keys[keys.length] = deferred()).promise,
  });
  for (const answer of [{ status: 200, json: { sent: true } }, null]) {
    click(buttonNamed(page.main(), "^C"));
    click(buttonNamed(page.run("UI.sheet"), "Send Ctrl-C"));
    await settle();
    if (answer) keys[keys.length - 1].settle(answer);
    await settle();
  }
  page.run("pageGo('#/p/" + PROJECT + "/a/coder-2/live')");
  await settle();
  paneCame(page, "coder-2");
  await settle();
  click(buttonNamed(page.main(), "^C"));
  keys[1].settle({ status: 409, json: { error: "double_press", message: "a second Ctrl-C within 3 s exits Claude Code — send confirm_exit: true" } });
  await settle();
  const doublePress = {
    sheet: sheetTitle(page), toast: page.toast(), exits: page.sent("api/send-keys").filter((body) => body.confirm_exit).length,
  };

  const restartThenTell = async (answer) => {
    const restart = deferred();
    const one = await agentView({
      "POST api/agent/restart": () => restart.promise,
      "GET api/transcript/coder-1": () => ({ status: 200, json: { lines: [], cursor: null, more: false, stamps: {} } }),
    });
    one.live().frame("fleet", FLEET);
    await settle();
    click(buttonNamed(one.main(), "Actions…"));
    click(buttonNamed(one.run("UI.sheet"), "Restart…"));
    click(buttonNamed(one.run("UI.sheet"), "Restart"));
    await settle();
    one.run("pageGo('#/p/" + PROJECT + "/a/coder-1/transcript')");
    await settle();
    click(buttonNamed(one.main(), "Actions…"));
    click(buttonNamed(one.run("UI.sheet"), "Tell…"));
    const text = find(one.run("UI.sheet"), (node) => node.tagName === "TEXTAREA");
    text.value = "carry on";
    restart.settle(answer);
    await settle();
    return { sheet: sheetTitle(one), typed: text.isConnected ? text.value : null, toast: one.toast() };
  };

  /* The restart answered while the Tell opened since is out: the Tell's sheet still waits on
   * its own answer, and neither Escape nor a tap beside it closes it. Then Back, Stop…, and
   * the Tell's answer, which leaves the Stop sheet where it is. */
  const restartUnderATell = async () => {
    const restart = deferred();
    const told = deferred();
    const one = await agentView({
      "POST api/agent/restart": () => restart.promise,
      "POST api/agent/tell": () => told.promise,
      "GET api/transcript/coder-1": () => transcriptPage([], null, false),
    });
    one.live().frame("fleet", FLEET);
    await settle();
    const menu = (item) => {
      click(buttonNamed(one.main(), "Actions…"));
      click(buttonNamed(one.run("UI.sheet"), item));
    };
    menu("Restart…");
    click(buttonNamed(one.run("UI.sheet"), "Restart"));
    await settle();
    one.run("pageGo('#/p/" + PROJECT + "/a/coder-1/transcript')");
    await settle();
    menu("Tell…");
    find(one.run("UI.sheet"), (node) => node.tagName === "TEXTAREA").value = "carry on";
    click(buttonNamed(one.run("UI.sheet"), "Tell"));
    await settle();
    restart.settle({ status: 200, json: { agent: { id: "agt_1", label: "coder-1" }, resumed: true, project: PROJECT } });
    await settle();
    const wrap = one.run("UI.sheet");
    const close = buttonNamed(wrap, "Close");
    const waiting = { busy: wrap.classList.contains("busy"), close: close ? close.disabled : null };
    one.run("for (const fn of document.listeners.keydown || []) fn({ type: 'keydown', key: 'Escape' });");
    wrap.dispatch("click");
    waiting.sheet = sheetTitle(one);
    one.run("pageGo('#/p/" + PROJECT + "/a/coder-1/live')");
    await settle();
    menu("Stop…");
    told.settle({ status: 200, json: { label: "coder-1", delivered: true, mode: "auto", project: PROJECT } });
    await settle();
    return { waiting, told: { sheet: sheetTitle(one), toast: one.toast() } };
  };

  /* A Reply posted on one question, then another question's card opened and a Reply begun
   * there: the first one's answer leaves the second's sheet, and what is typed in it, alone. */
  const replyUnderAReply = async () => {
    const posted = deferred();
    const first = Object.assign({}, ITEM, {
      kind: "board_question", detail: { text: "Which store?", author: "lead-1" }, answers: [], actions: ["reply"],
    });
    const second = Object.assign({}, first, { id: "ny_00000000000000b2", detail: { text: "Which port?", author: "lead-1" } });
    const page = bootPage("#/", signedIn({
      "GET api/needs": () => ({ status: 200, json: { items: [first, second] } }),
      "POST api/note": () => posted.promise,
      "POST api/needs/dismiss": () => ({ status: 200, json: { dismissed: true } }),
    }));
    await settle();
    page.acceptSockets();
    await settle();
    click(buttonNamed(page.main(), "Reply…"));
    find(page.run("UI.sheet"), (node) => node.tagName === "TEXTAREA").value = "Postgres";
    click(buttonNamed(page.run("UI.sheet"), "Post"));
    await settle();
    page.run("pageGo('#/n/" + second.id + "')");
    await settle();
    click(buttonNamed(page.main(), "Reply…"));
    const draft = find(page.run("UI.sheet"), (node) => node.tagName === "TEXTAREA");
    draft.value = "8080";
    posted.settle({ status: 200, json: { ok: true } });
    await settle();
    return {
      typed: draft.isConnected ? draft.value : null, toast: page.toast(),
      dismissed: page.sent("api/needs/dismiss").map((body) => body.id), at: page.location.hash,
    };
  };

  /* A Tell from one card answered stale (the card cleared meanwhile) once another card's Tell
   * is begun: that sheet, and what is typed in it, stays. */
  const staleUnderATell = async () => {
    const told = deferred();
    const asked = Object.assign({}, ITEM, { kind: "asked", detail: { text: "Shall I merge?" }, answers: [], actions: ["tell"] });
    const other = Object.assign({}, asked, { id: "ny_00000000000000b3", agent: "coder-2", agent_id: "agt_2" });
    const page = bootPage("#/n/" + asked.id, signedIn({
      "GET api/needs": () => ({ status: 200, json: { items: [asked, other] } }),
      "POST api/agent/tell": () => told.promise,
    }));
    await settle();
    page.acceptSockets();
    await settle();
    const tell = (words) => {
      click(buttonNamed(page.main(), "Tell…"));
      const text = find(page.run("UI.sheet"), (node) => node.tagName === "TEXTAREA");
      text.value = words;
      return text;
    };
    tell("yes, merge");
    click(buttonNamed(page.run("UI.sheet"), "Tell"));
    await settle();
    page.run("pageGo('#/n/" + other.id + "')");
    await settle();
    const draft = tell("not yet");
    told.settle({ status: 409, json: { error: "stale", message: "the item no longer needs you", current: null } });
    await settle();
    return { sheet: sheetTitle(page), typed: draft.isConnected ? draft.value : null, told: page.sent("api/agent/tell").length };
  };

  /* A Stop answered dialog_open after Back closed its sheet: what the toast says. */
  const promptAfterBack = async () => {
    const stopped = deferred();
    const one = await agentView({
      "POST api/agent/stop": () => stopped.promise,
      "GET api/transcript/coder-1": () => transcriptPage([], null, false),
    });
    one.live().frame("fleet", FLEET);
    await settle();
    click(buttonNamed(one.main(), "Actions…"));
    click(buttonNamed(one.run("UI.sheet"), "Stop…"));
    click(buttonNamed(one.run("UI.sheet"), "Stop"));
    await settle();
    one.run("pageGo('#/p/" + PROJECT + "/a/coder-1/transcript')");
    await settle();
    stopped.settle({ status: 409, json: { error: "dialog_open", message: "coder-1 is showing a prompt; send dismiss_dialog: true" } });
    await settle();
    return { sheet: sheetTitle(one), toast: one.toast() };
  };

  /* A second ^C to coder-1 refused double_press once the human moved on: `moveOn` opens
   * coder-1's Actions sheet, or coder-2's screen with no sheet on it. */
  const doubleLate = async (moveOn) => {
    const sent = [];
    const one = await agentView({ "POST api/send-keys": () => (sent[sent.length] = deferred()).promise });
    for (const answer of [{ status: 200, json: { sent: true } }, null]) {
      click(buttonNamed(one.main(), "^C"));
      click(buttonNamed(one.run("UI.sheet"), "Send Ctrl-C"));
      await settle();
      if (answer) sent[sent.length - 1].settle(answer);
      await settle();
    }
    await moveOn(one);
    sent[1].settle({ status: 409, json: { error: "double_press", message: "a second Ctrl-C within 3 s exits Claude Code — send confirm_exit: true" } });
    await settle();
    return { sheet: sheetTitle(one), toast: one.toast() };
  };
  const toActions = async (one) => click(buttonNamed(one.main(), "Actions…"));
  const toCoder2 = async (one) => {
    one.run("pageGo('#/p/" + PROJECT + "/a/coder-2/live')");
    await settle();
  };
  return {
    doublePress,
    restartDone: await restartThenTell({ status: 200, json: { agent: { id: "agt_1", label: "coder-1" }, resumed: true, project: PROJECT } }),
    restartFailed: await restartThenTell({ status: 503, json: { error: "fleet_unavailable", message: "tmux did not answer" } }),
    restartStale: await restartThenTell({ status: 409, json: { error: "stale", message: "coder-1 is not the agent this was" } }),
    restartUnderATell: await restartUnderATell(),
    replyUnderAReply: await replyUnderAReply(),
    staleUnderATell: await staleUnderATell(),
    promptAfterBack: await promptAfterBack(),
    doubleUnderASheet: await doubleLate(toActions),
    doubleElsewhere: await doubleLate(toCoder2),
    elsewhere: await lateElsewhere(),
  };
}

/* More answers that come after the human moved on: a card's Dismiss once another card is
 * open; a pad key refused read_only once a Tell sheet is open, typed in, and a Tell refused
 * read_only on its own sheet, the control; a transcript read that finds coder-1 gone once
 * coder-2's screen is open; and a pad key and a Tell to coder-1 answered "gone" once
 * coder-2's screen is open, and on coder-1's own screen, the control. Where the page is
 * after each, and for the last two whether Back then leaves the page. */
async function lateElsewhere() {
  const other = Object.assign({}, ITEM, { id: "ny_fedcba9876543210", agent: "coder-2" });
  const dismissed = deferred();
  const cards = bootPage("#/n/" + NEEDS_ID, signedIn({
    "GET api/needs": () => ({ status: 200, json: { items: [ITEM, other] } }),
    "POST api/needs/dismiss": () => dismissed.promise,
  }));
  await settle();
  cards.acceptSockets();
  await settle();
  click(buttonNamed(cards.main(), "Dismiss"));
  cards.run("pageGo('#/n/" + other.id + "')");
  await settle();
  dismissed.settle({ status: 200, json: { dismissed: true } });
  await settle();

  const key = deferred();
  const pad = await agentView({ "POST api/send-keys": () => key.promise });
  click(buttonNamed(pad.main(), "1"));
  click(buttonNamed(pad.main(), "Actions…"));
  click(buttonNamed(pad.run("UI.sheet"), "Tell…"));
  const text = find(pad.run("UI.sheet"), (node) => node.tagName === "TEXTAREA");
  text.value = "wait for me";
  key.settle({ status: 403, json: { error: "read_only", message: "writes are off" } });
  await settle();

  const own = await agentView({ "POST api/agent/tell": () => ({ status: 403, json: { error: "read_only", message: "writes are off" } }) });
  click(buttonNamed(own.main(), "Actions…"));
  click(buttonNamed(own.run("UI.sheet"), "Tell…"));
  find(own.run("UI.sheet"), (node) => node.tagName === "TEXTAREA").value = "go on";
  click(buttonNamed(own.run("UI.sheet"), "Tell"));
  await settle();

  const read = deferred();
  const gone = bootPage("#/p/" + PROJECT + "/a/coder-1/transcript", signedIn({ "GET api/transcript/coder-1": () => read.promise }));
  await settle();
  gone.run("pageGo('#/p/" + PROJECT + "/a/coder-2/live')");
  await settle();
  read.settle({ status: 404, json: { error: "no_such_agent", message: "no live agent 'coder-1'" } });
  await settle();

  const goneAfter = async (send, moveOn) => {
    const held = deferred();
    const one = await agentView({ "POST api/send-keys": () => held.promise, "POST api/agent/tell": () => held.promise });
    send(one);
    await settle();
    if (moveOn) {
      one.run("pageGo('#/p/" + PROJECT + "/a/coder-2/live')");
      await settle();
    }
    held.settle({ status: 404, json: { error: "no_such_agent", message: "no live agent 'coder-1'" } });
    await settle();
    return { at: one.location.hash, left: !one.back() };
  };
  const tapKey = (one) => click(buttonNamed(one.main(), "1"));
  const sendTell = (one) => {
    click(buttonNamed(one.main(), "Actions…"));
    click(buttonNamed(one.run("UI.sheet"), "Tell…"));
    find(one.run("UI.sheet"), (node) => node.tagName === "TEXTAREA").value = "go on";
    click(buttonNamed(one.run("UI.sheet"), "Tell"));
  };
  return {
    dismissedAt: cards.location.hash,
    readOnly: { sheet: sheetTitle(pad), typed: text.isConnected ? text.value : null, writable: pad.run("writable()") },
    readOnlyOwn: sheetTitle(own),
    goneAt: gone.location.hash,
    goneKey: { elsewhere: await goneAfter(tapKey, true), own: await goneAfter(tapKey, false) },
    goneTell: { elsewhere: await goneAfter(sendTell, true), own: await goneAfter(sendTell, false) },
  };
}

/* Stop… opened on an agent's screen before the fleet frame, tapped, then tapped again once
 * the fleet came; Restart… on a fleet that came without the agent; and a Tell opened before
 * the fleet frame and sent after it. */
async function sheetBeforeFleet() {
  const stopped = { agent: { id: "agt_1", label: "coder-1" }, claims_released: [], release_failed: null, project: PROJECT };
  const status = (page) => find(page.run("UI.sheet"), (node) => node.className === "status").textContent;
  const page = await agentView({ "POST api/agent/stop": () => ({ status: 200, json: stopped }) });
  click(buttonNamed(page.main(), "Actions…"));
  click(buttonNamed(page.run("UI.sheet"), "Stop…"));
  click(buttonNamed(page.run("UI.sheet"), "Stop"));
  await settle();
  const waiting = status(page);
  page.live().frame("fleet", FLEET);
  await settle();
  click(buttonNamed(page.run("UI.sheet"), "Stop"));
  await settle();
  const empty = await agentView();
  click(buttonNamed(empty.main(), "Actions…"));
  click(buttonNamed(empty.run("UI.sheet"), "Restart…"));
  empty.live().frame("fleet", Object.assign({}, FLEET, { agents: [] }));
  await settle();
  click(buttonNamed(empty.run("UI.sheet"), "Restart"));
  await settle();
  const tell = await agentView({
    "POST api/agent/tell": () => ({ status: 200, json: { label: "coder-1", delivered: true, mode: "auto", project: PROJECT } }),
  });
  click(buttonNamed(tell.main(), "Actions…"));
  click(buttonNamed(tell.run("UI.sheet"), "Tell…"));
  tell.live().frame("fleet", FLEET);
  await settle();
  find(tell.run("UI.sheet"), (node) => node.tagName === "TEXTAREA").value = "go on";
  click(buttonNamed(tell.run("UI.sheet"), "Tell"));
  await settle();
  return {
    waiting, stopped: page.sent("api/agent/stop").map((body) => body.agent_id), toast: page.toast(),
    absent: status(empty), restarts: empty.sent("api/agent/restart").length,
    told: tell.sent("api/agent/tell").map((body) => body.agent_id || null),
  };
}

/* Pad keys tapped faster than the machine answers, in the order they reached it: ↓ ↓ ⏎
 * with every answer held until the scenario gives it, and the keys marked sending once
 * tapped and after each answer; a ⏎ tapped behind a ↓ the machine refused, then one tapped
 * after; a ⏎ behind a ↓ answered only 16 s later; a ↓ the phone lost, a ⏎ tapped behind
 * it, and the reconnect that sends the ↓ again; and a ⏎ tapped while a card's quick
 * answer is typed. */
async function keysInOrder() {
  const held = async () => {
    const calls = [];
    const page = await agentView({
      "POST api/send-keys": (body) => {
        const answer = deferred();
        calls.push({ key: body.keys[0], answer });
        return answer.promise;
      },
    });
    const marked = () => page.main().querySelectorAll("button.key.sending").map((key) => key.textContent);
    return { page, calls, marked, tap: (name) => click(buttonNamed(page.main(), name)) };
  };
  const quick = await held();
  for (const name of ["↓", "↓", "⏎"]) quick.tap(name);
  await settle();
  const atOnce = quick.calls.length;
  const sending = [quick.marked()];
  for (let n = 0; n < quick.calls.length && n < 3; n++) {
    quick.calls[n].answer.settle({ status: 200, json: { sent: true } });
    await settle();
    sending.push(quick.marked());
  }
  const refused = await held();
  refused.tap("↓");
  refused.tap("⏎");
  await settle();
  refused.calls[0].answer.settle({ status: 409, json: { error: "busy", message: "keys are still being typed" } });
  await settle();
  const behind = { sent: refused.calls.length, toast: refused.page.toast(), marked: refused.marked() };
  refused.tap("⏎");
  await settle();
  const slow = await held();
  slow.tap("↓");
  slow.tap("⏎");
  await settle();
  slow.page.run("Date.now = ((then) => () => then + 16000)(Date.now());"); // past RETRY_WITHIN_MS
  slow.calls[0].answer.settle({ status: 200, json: { sent: true } });
  await settle();
  const waitedTooLong = { sent: slow.calls.map((call) => call.key), toast: slow.page.toast(), marked: slow.marked() };
  const reached = [];
  const lost = await agentView({
    "POST api/send-keys": (body) => {
      reached.push(body.keys[0]);
      return reached.length === 1 ? "network" : { status: 200, json: { sent: true } };
    },
  });
  click(buttonNamed(lost.main(), "↓"));
  click(buttonNamed(lost.main(), "⏎"));
  await settle();
  lost.acceptSockets();
  await settle();
  // A key that waited its turn 10 s and then was lost: its retry window counts from its
  // tap, not from when it could go, so 16 s after the tap it is not sent again.
  const turned = [];
  const slowFirst = deferred();
  const queued = await agentView({
    "POST api/send-keys": (body) => {
      turned.push(body.keys[0]);
      if (turned.length === 1) return slowFirst.promise;
      return turned.length === 2 ? "network" : { status: 200, json: { sent: true } };
    },
  });
  click(buttonNamed(queued.main(), "↓"));
  click(buttonNamed(queued.main(), "⏎"));
  await settle();
  queued.run("Date.now = ((then) => () => then + 10000)(Date.now());"); // ↓ answered 10 s on
  slowFirst.settle({ status: 200, json: { sent: true } });
  await settle();
  queued.run("Date.now = ((then) => () => then + 6000)(Date.now());"); // 16 s since ⏎ was tapped
  queued.acceptSockets();
  await settle();
  const lostAfterItsTurn = { sent: turned, toast: queued.toast() };
  const answered = deferred();
  const card = bootPage("#/", signedIn({
    "GET api/needs": () => ({ status: 200, json: { items: [ITEM] } }),
    "POST api/needs/answer": () => answered.promise,
    "POST api/send-keys": () => ({ status: 200, json: { sent: true } }),
  }));
  await settle();
  card.acceptSockets();
  await settle();
  click(card.main().querySelectorAll("button.qa")[0]);
  card.run("pageGo('#/p/" + PROJECT + "/a/coder-1/live')");
  await settle();
  paneCame(card);
  await settle();
  click(buttonNamed(card.main(), "⏎"));
  await settle();
  const whileAnswering = card.sent("api/send-keys").length;
  answered.settle({ status: 200, json: { id: NEEDS_ID, sent: ["1"] } });
  await settle();
  return {
    quick: { atOnce, order: quick.calls.map((call) => call.key), sending },
    refused: Object.assign(behind, { after: refused.calls.map((call) => call.key) }),
    waitedTooLong,
    lost: reached,
    lostAfterItsTurn,
    afterAnswer: { whileAnswering, after: card.sent("api/send-keys").length },
  };
}

/* The pad's guards, which only the page keeps: ^C and ^D each ask first; a second Esc within
 * 1.5 s asks first (two open Claude Code's Rewind), at once or 1.4 s after the last, and one
 * 2 s after the last does not; a second ^C the machine refuses double_press goes again, with
 * confirm_exit, only once the human says so. After each step: the sheet on screen and how
 * many keys were sent. */
async function padConfirms() {
  let ctrlC = 0;
  const page = await agentView({
    "POST api/send-keys": (body) => (body.keys[0] === "C-c" && body.confirm_exit !== true && ++ctrlC > 1
      ? { status: 409, json: { error: "double_press", message: "a second Ctrl-C within 3 s exits Claude Code" } }
      : { status: 200, json: { sent: true } }),
  });
  const steps = [];
  const sent = () => page.sent("api/send-keys");
  const act = async (where, name) => {
    click(buttonNamed(where === "sheet" ? page.run("UI.sheet") : page.main(), name));
    await settle();
    steps.push([name, sheetTitle(page), sent().length]);
  };
  await act("pad", "^C");
  await act("sheet", "Send Ctrl-C");
  await act("pad", "^D");
  await act("sheet", "Close");
  await act("pad", "^C");
  await act("sheet", "Send Ctrl-C");
  await act("sheet", "Send and exit");
  await act("pad", "Esc");
  page.run("Date.now = ((then) => () => then + 2000)(Date.now());");
  await act("pad", "Esc");
  await act("pad", "Esc");
  await act("sheet", "Send Esc");
  await act("pad", "Esc");
  page.run("Date.now = ((then) => () => then + 1400)(Date.now());");
  await act("pad", "Esc");
  await act("sheet", "Close");
  return { steps, keys: sent().map((body) => (body.confirm_exit === true ? body.keys.concat("confirm_exit") : body.keys)) };
}

/* The Transcript tab's input bar, writes on and the socket open: it watches no pane, so
 * nothing there waits for one. Send's state, and what ⏎ sends. */
async function transcriptSend() {
  const page = bootPage("#/p/" + PROJECT + "/a/coder-1/transcript", signedIn({
    "GET api/transcript/coder-1": () => transcriptPage([], null, false),
    "POST api/send-keys": () => ({ status: 200, json: { sent: true } }),
  }));
  await settle();
  page.acceptSockets();
  page.live().frame("remote", { allow_write: true, auto_off_at: null, version: "test" });
  await settle();
  const send = sendState(page);
  click(buttonNamed(page.main(), "⏎"));
  await settle();
  return { send, sent: page.sent("api/send-keys").map((body) => body.keys) };
}

/* Send on the Transcript tab, which shows no pane: refused once as dialog_open, as the
 * machine refuses it while a prompt may be up, then sent. What each body said, the toast
 * and the box after the refusal, and the box after the send. */
async function transcriptSendGuarded() {
  let answer = { status: 409, json: { error: "dialog_open", message: "coder-1 is showing a prompt" } };
  const page = bootPage("#/p/" + PROJECT + "/a/coder-1/transcript", signedIn({
    "GET api/transcript/coder-1": () => transcriptPage([], null, false),
    "POST api/send-keys": () => answer,
  }));
  await settle();
  page.acceptSockets();
  page.live().frame("remote", { allow_write: true, auto_off_at: null, version: "test" });
  await settle();
  const say = await typeAndSend(page, "no - run the tests instead");
  const refused = { toast: page.toast(), typed: say.value };
  answer = { status: 200, json: { sent: true } };
  await typeAndSend(page, "run the tests");
  const bodies = page.sent("api/send-keys").map((body) => {
    const copy = Object.assign({}, body);
    delete copy.request_id;
    return copy;
  });
  return { bodies, refused, typed: say.value };
}

/* Keys and Send carry the agent_id of the screen they were typed at. On Live: a pad key at
 * one row's frame; a ^C asked on its sheet there and confirmed after a replacement's frame
 * came (still the first row's: the tap was at its screen); Send at the replacement's frame;
 * a key the machine refuses stale, and what the page says; a frame that could not be read,
 * at which the pad and Send wait (a tap there sends nothing) until a screen comes. On
 * Transcript: Send and the pad while the first page is out and once its read failed, each
 * held and a tap sending nothing ([Send, ⏎] disabled), then Send with the id of the page
 * Refresh read. Each body as [keys or text, agent_id]. */
async function pinnedKeys() {
  const ok = { status: 200, json: { sent: true } };
  const stale = { status: 409, json: { error: "stale", message: "'coder-1' is another agent now (agt_3) — nothing was sent", current: { agent_id: "agt_3" } } };
  let answer = ok;
  const live = await agentView({ "POST api/send-keys": () => answer });
  const pane = (payload) => live.live().frame("pane", payload, { agent: "coder-1", project: PROJECT });
  const held = () => [buttonNamed(live.main(), "Send").disabled, buttonNamed(live.main(), "3").disabled, live.run("document.body.classList.contains('held')")];
  pane({ rows: ["❯ 1. Yes"], cursor: [0, 0], width: 80, height: 1, agent_id: "agt_1" });
  await settle();
  click(buttonNamed(live.main(), "1"));
  await settle();
  click(buttonNamed(live.main(), "^C"));
  pane({ rows: ["❯ "], cursor: [2, 0], width: 80, height: 1, agent_id: "agt_2" });
  await settle();
  click(buttonNamed(live.run("UI.sheet"), "Send Ctrl-C"));
  await settle();
  await typeAndSend(live, "hello");
  answer = stale;
  click(buttonNamed(live.main(), "2"));
  await settle();
  const staleSaid = live.toast();
  answer = ok;
  pane({ rows: [], width: 0, height: 0, error: "no live agent 'coder-1' in prj_x" });
  await settle();
  const unread = held();
  click(buttonNamed(live.main(), "3"));
  await settle();
  pane({ rows: ["❯ "], cursor: [2, 0], width: 80, height: 1, agent_id: "agt_3" });
  await settle();
  const read = held();
  click(buttonNamed(live.main(), "4"));
  await settle();
  const said = (body) => [body.keys || body.text, body.agent_id === undefined ? null : body.agent_id];
  const reads = [];
  const transcript = bootPage("#/p/" + PROJECT + "/a/coder-1/transcript", signedIn({
    "GET api/transcript/coder-1": () => (reads[reads.length] = deferred()).promise,
    "POST api/send-keys": () => ok,
  }));
  await settle();
  transcript.acceptSockets();
  transcript.live().frame("remote", { allow_write: true, auto_off_at: null, version: "test" });
  await settle();
  const keys = () => ["Send", "⏎"].map((name) => buttonNamed(transcript.main(), name).disabled);
  const tapped = async (text) => {
    await typeAndSend(transcript, text);
    click(buttonNamed(transcript.main(), "⏎"));
    await settle();
  };
  const unpaged = keys();
  await tapped("before the page came");
  reads[0].settle({ status: 503, json: { error: "store_busy", message: "the store is busy" } });
  await settle();
  const failed = keys();
  await tapped("after the read failed");
  click(buttonNamed(transcript.main(), "Refresh"));
  await settle();
  reads[1].settle({ status: 200, json: { lines: ["done?"], cursor: null, more: false, stamps: {}, agent_id: "agt_1" } });
  await settle();
  const paged = keys();
  await typeAndSend(transcript, "yes");
  return {
    live: live.sent("api/send-keys").map(said),
    staleSaid,
    unread,
    read,
    transcriptHeld: { unpaged, failed, paged },
    transcript: transcript.sent("api/send-keys").map((body) => said(body).concat(body.dialog_guard === true)),
  };
}

/* Where focus goes: a card's Tell… opens a sheet whose message box takes it; the next feed
 * frame draws the card anew, its button with it, and then Close. */
async function sheetFocus() {
  const asked = Object.assign({}, ITEM, { kind: "asked", detail: { text: "Shall I merge?" }, answers: [], actions: ["tell"] });
  const page = bootPage("#/", signedIn({ "GET api/needs": () => ({ status: 200, json: { items: [asked] } }) }));
  await settle();
  page.acceptSockets();
  await settle();
  const tell = buttonNamed(page.main(), "Tell…");
  tell.focus();
  click(tell);
  const typing = page.run("document.activeElement").tagName;
  page.live().frame("needs_you", { items: [Object.assign({}, asked, { reason: "coder-1 asks again" })] });
  await settle();
  const redrawn = !tell.isConnected;
  click(buttonNamed(page.run("UI.sheet"), "Close"));
  return { typing, redrawn, closedOnto: page.run("document.activeElement === UI.main ? 'main' : document.activeElement.tagName") };
}

/* The focused element, as a screen reader follows it: its tag, its text, and whether it is on
 * the page, in the screen, or in the nav. */
function focusOf(page) {
  return page.run("(() => { const at = document.activeElement; return { tag: at.tagName, text: at.textContent, "
    + "connected: at.isConnected, main: UI.main.contains(at), nav: UI.nav.contains(at) }; })()");
}

/* A control focused and tapped, as a keyboard or a screen reader does, and where focus is then. */
async function tapFocused(page, control) {
  control.focus();
  click(control);
  await settle();
  return focusOf(page);
}

/* Where focus lands when the route changes: on the page's first screen, left where a page's load
 * leaves it; after a Projects row, then the Board tab; the bottom nav's Settings; a feed card's
 * Open; and on an agent's screen with a sheet open, Back. */
async function focusLands() {
  const projects = bootPage("#/projects", signedIn({ "GET api/projects": () => ({ status: 200, json: [{ id: PROJECT, name: "x", agents: {} }] }) }));
  await settle();
  projects.acceptSockets();
  await settle();
  const loaded = focusOf(projects);
  const row = await tapFocused(projects, find(projects.main(), (node) => node.tagName === "BUTTON" && node.className === "row"));
  const tab = await tapFocused(projects, buttonNamed(projects.main(), "Board"));
  const nav = await tapFocused(projects, buttonNamed(projects.run("UI.nav"), "Settings"));
  const feed = bootPage("#/", signedIn({ "GET api/needs": () => ({ status: 200, json: { items: [ITEM] } }) }));
  await settle();
  feed.acceptSockets();
  await settle();
  const open = await tapFocused(feed, buttonNamed(feed.main(), "Open"));
  const fleet = bootPage("#/p/" + PROJECT + "/fleet", signedIn());
  await settle();
  fleet.acceptSockets();
  await settle();
  await tapFocused(fleet, find(fleet.main(), (node) => node.tagName === "BUTTON" && node.className === "row"));
  await tapFocused(fleet, buttonNamed(fleet.main(), "Actions…"));
  fleet.back();
  await settle();
  return { loaded, row, tab, nav, open, back: focusOf(fleet) };
}

/* What was focused as it is drawn anew: a Fleet row by a fleet frame, a Projects row by the 15 s
 * poll, a card's Open by a needs frame that changed the card, a Revoke once it went through,
 * Reconnect here as the banner redraws, Settings' Turn on once its answer redrew the panel, and
 * the feed's notifications line once Hide took it away. */
async function focusKept() {
  const fleet = bootPage("#/p/" + PROJECT + "/fleet", signedIn());
  await settle();
  fleet.acceptSockets();
  await settle();
  find(fleet.main(), (node) => node.tagName === "BUTTON" && node.className === "row").focus();
  fleet.live().frame("fleet", Object.assign({}, FLEET, { agents: [Object.assign({}, FLEET.agents[0], { state: "working" })] }));
  await settle();
  const frame = focusOf(fleet);
  const projects = bootPage("#/projects", signedIn({ "GET api/projects": () => ({ status: 200, json: [{ id: PROJECT, name: "x", agents: {} }] }) }));
  await settle();
  projects.acceptSockets();
  await settle();
  find(projects.main(), (node) => node.tagName === "BUTTON" && node.className === "row").focus();
  projects.fireTimer("load");
  await settle();
  const poll = focusOf(projects);
  const changed = bootPage("#/", signedIn({ "GET api/needs": () => ({ status: 200, json: { items: [ITEM] } }) }));
  await settle();
  changed.acceptSockets();
  await settle();
  buttonNamed(changed.main(), "Open").focus();
  changed.live().frame("needs_you", { items: [Object.assign({}, ITEM, { reason: "coder-1 asks again" })] });
  await settle();
  const card = Object.assign(focusOf(changed), { redrawn: changed.main().textContent.indexOf("asks again") >= 0 });
  let devices = TWO_DEVICES;
  const revoking = bootPage("#/devices", signedIn({
    "GET api/devices": () => ({ status: 200, json: devices }),
    "DELETE api/devices/dev_4e5f6a7b": () => {
      devices = TWO_DEVICES.slice(0, 1);
      return { status: 200, json: { ok: true, id: "dev_4e5f6a7b", signed_out: false } };
    },
  }));
  await settle();
  revoking.acceptSockets();
  await settle();
  const revoked = await tapFocused(revoking, buttonNamed(revoking.main(), "Revoke"));
  revoking.live().fire("close", { code: 4409 });
  await settle();
  buttonNamed(revoking.run("UI.banner"), "Reconnect here").focus();
  fire(revoking, "window", "offline");
  const banner = focusOf(revoking);
  const settings = bootPage("#/settings", signedIn(pushRoutes([])), fakePush(KEY_NOW).globals);
  await settle();
  settings.acceptSockets();
  await settle();
  const toggled = await tapFocused(settings, buttonNamed(settings.main(), "Turn on"));
  const feedNotice = bootPage("#/", signedIn(pushRoutes([])), fakePush(KEY_NOW).globals);
  await settle();
  feedNotice.acceptSockets();
  await settle();
  const hidden = await tapFocused(feedNotice, buttonNamed(feedNotice.main(), "Hide"));
  return { frame, poll, card, revoked, banner, toggled, hidden };
}

/* A browser with CloseWatcher, as Chrome on Android 126 on is: Back goes to the newest active
 * watcher, if any, as a cancel the page may refuse (once, while the human has tapped since),
 * then a close; with none, it goes back in the tab's history. */
function closeWatchers() {
  const made = [];
  class CloseWatcher {
    constructor() {
      this.active = true;
      this.listeners = { cancel: [], close: [] };
      made.push(this);
    }

    addEventListener(type, fn) {
      this.listeners[type].push(fn);
    }

    destroy() {
      this.active = false;
    }
  }
  const back = (page, cancelable) => {
    const watcher = made.filter((one) => one.active).pop();
    if (!watcher) return page.back() ? "back" : "left";
    let refused = false;
    const cancel = { type: "cancel", cancelable, preventDefault: () => { refused = cancelable; } };
    for (const fn of watcher.listeners.cancel) if (cancelable) fn(cancel);
    if (refused) return "refused";
    watcher.active = false;
    for (const fn of watcher.listeners.close) fn({ type: "close" });
    return "closed";
  };
  return { made, back, globals: { CloseWatcher } };
}

/* Android's Back while a sheet is open: a card's Tell with words typed in it, on a feed opened
 * as the app's first screen; the same sheet shut with Close, then Back; a Tell whose answer is
 * held, Back twice; and on an agent's screen, Actions… then Stop… in its place, then Back. What
 * each Back did, the sheet left, where the page is, and the watchers still active. */
async function backOverASheet() {
  const asked = Object.assign({}, ITEM, { kind: "asked", detail: { text: "Shall I merge?" }, answers: [], actions: ["tell"] });
  const told = deferred();
  const watchers = closeWatchers();
  const page = bootPage("#/", signedIn({
    "GET api/needs": () => ({ status: 200, json: { items: [asked] } }),
    "POST api/agent/tell": () => told.promise,
  }), watchers.globals);
  await settle();
  page.acceptSockets();
  await settle();
  const active = () => watchers.made.filter((one) => one.active).length;
  const after = (did) => ({ did, sheet: sheetTitle(page), at: page.location.hash, cards: page.main().querySelectorAll("div.card").length, active: active() });
  click(buttonNamed(page.main(), "Tell…"));
  find(page.run("UI.sheet"), (node) => node.tagName === "TEXTAREA").value = "yes, merge";
  const typed = after(watchers.back(page, true));
  click(buttonNamed(page.main(), "Tell…"));
  click(buttonNamed(page.run("UI.sheet"), "Close"));
  const shut = active();
  click(buttonNamed(page.main(), "Tell…"));
  find(page.run("UI.sheet"), (node) => node.tagName === "TEXTAREA").value = "yes, merge";
  click(buttonNamed(page.run("UI.sheet"), "Tell"));
  await settle();
  const busy = [after(watchers.back(page, true)), after(watchers.back(page, false))];
  const agent = closeWatchers();
  const one = bootPage("#/p/" + PROJECT + "/a/coder-1/live", signedIn(), agent.globals);
  await settle();
  one.acceptSockets();
  await settle();
  click(buttonNamed(one.main(), "Actions…"));
  click(buttonNamed(one.run("UI.sheet"), "Stop…"));
  const replaced = { did: agent.back(one, true), sheet: sheetTitle(one), at: one.location.hash, made: agent.made.length };
  replaced.active = agent.made.filter((watcher) => watcher.active).length;
  return { typed, shut, busy, replaced };
}

/* The same without CloseWatcher (Safari; Firefox before 149), the sheet holding a history entry
 * of its own. On the feed, the tab's first entry: a card's Tell with words typed in it, then
 * Back; Tell again, then Close, once its Back landed; Tell again, then a route asked for as the
 * sheet closes; and on an agent's screen, Actions…, a notification's card while it is open,
 * then Back. Where history stands after each, the sheet, and the screen. */
async function backWithoutCloseWatcher() {
  const asked = Object.assign({}, ITEM, { kind: "asked", detail: { text: "Shall I merge?" }, answers: [], actions: ["tell"] });
  const page = bootPage("#/", signedIn({ "GET api/needs": () => ({ status: 200, json: { items: [asked] } }) }), { history: true });
  await settle();
  page.acceptSockets();
  await settle();
  const after = (one, did) => ({ did, sheet: sheetTitle(one), at: one.location.hash, cards: one.main().querySelectorAll("div.card").length, history: one.history() });
  click(buttonNamed(page.main(), "Tell…"));
  find(page.run("UI.sheet"), (node) => node.tagName === "TEXTAREA").value = "yes, merge";
  const opened = page.history();
  const did = page.back() ? "back" : "left";
  await settle();
  const typed = after(page, did);
  click(buttonNamed(page.main(), "Tell…"));
  click(buttonNamed(page.run("UI.sheet"), "Close"));
  await settle();
  const shut = after(page, "close");
  click(buttonNamed(page.main(), "Tell…"));
  page.run("closeSheet(); pageGo('#/projects');");
  await settle();
  const raced = after(page, "close, then go");
  const one = bootPage("#/p/" + PROJECT + "/a/coder-1/live", signedIn(), { history: true });
  await settle();
  one.acceptSockets();
  await settle();
  click(buttonNamed(one.main(), "Actions…"));
  one.run("pageGo('#/n/" + NEEDS_ID + "')");
  await settle();
  const led = after(one, "card");
  one.back();
  await settle();
  return { opened, typed, shut, raced, led, back: after(one, "back") };
}

/* The Live tab across a sleep, as [stale, Send disabled, pane greyed as held]: with its pane
 * in; after a minute with nothing heard; once a wake's socket opened and a second passed;
 * once that socket's first frame came, not the pane; and once the pane came. */
async function staleAcrossAWake() {
  const page = await agentView();
  const state = () => [page.run("S.stale"), buttonNamed(page.main(), "Send").disabled, page.run("document.body.classList.contains('held')")];
  const steps = [state()];
  page.run("S.lastFrameAt = Date.now() - 60000; checkStale();");
  steps.push(state());
  fire(page, "document", "visibilitychange");
  page.acceptSockets();
  await settle();
  page.run("checkStale();");
  steps.push(state());
  page.live().frame("remote", { allow_write: true, auto_off_at: null, version: "test" });
  await settle();
  steps.push(state());
  page.live().frame("pane", { rows: ["❯ 1. Yes"], cursor: [0, 0], width: 80, height: 1 }, { agent: "coder-1", project: PROJECT });
  await settle();
  steps.push(state());
  return steps;
}

/* The Live tab's keys between a socket lost and the next one's pane, its pane in before: a wake
 * whose new socket has not opened yet, a drop (1006) before the reconnect, and another tab taking
 * the socket (4409) until Reconnect here; in each, a tap on 1. Each gives [Send waits, 1 waits,
 * the pane greyed as held] then and once the next socket's pane came, and the keys sent. */
async function heldBetweenSockets() {
  const lost = async (how) => {
    const page = await agentView({ "POST api/send-keys": () => ({ status: 200, json: { sent: true } }) });
    const state = () => [buttonNamed(page.main(), "Send").disabled, buttonNamed(page.main(), "1").disabled, page.run("document.body.classList.contains('held')")];
    if (how === "wake") fire(page, "document", "visibilitychange");
    else page.live().fire("close", { code: how });
    await settle();
    const held = state();
    click(buttonNamed(page.main(), "1"));
    await settle();
    const sent = page.sent("api/send-keys").length;
    if (how === 1006) page.fireTimer("connect");
    if (how === 4409) click(buttonNamed(page.run("UI.banner"), "Reconnect here"));
    page.acceptSockets();
    paneCame(page);
    await settle();
    return { held, sent, after: state() };
  };
  return { wake: await lost("wake"), dropped: await lost(1006), taken: await lost(4409) };
}

/* A socket that died with no close (Wi-Fi gave way to cellular, a NAT forgot it), as each second's
 * tick finds it: on the Live tab, nothing heard for 30 s; the same second again; 26 s on, its
 * replacement silent too; then that one's pane. And a page whose socket another tab took (4409),
 * a minute stale. After each: the sockets opened, stale, and whether Send waits. */
async function silentSocket() {
  const page = await agentView();
  const later = (ms) => page.run("Date.now = ((then) => () => then + " + ms + ")(Date.now());");
  const state = () => ({ sockets: page.sockets.length, stale: page.run("S.stale"), send: buttonNamed(page.main(), "Send").disabled });
  const tick = async () => {
    page.fireTimer("onSecond");
    await settle();
    return state();
  };
  const steps = [state()];
  later(30000);
  steps.push(await tick(), await tick());
  later(26000);
  steps.push(await tick());
  page.acceptSockets();
  paneCame(page);
  steps.push(await tick());
  const taken = await agentView();
  taken.live().fire("close", { code: 4409 });
  await settle();
  taken.run("Date.now = ((then) => () => then + 60000)(Date.now());");
  taken.fireTimer("onSecond");
  await settle();
  return { steps, taken: { sockets: taken.sockets.length, stale: taken.run("S.stale"), state: taken.run("S.sockState") } };
}

/* A socket made once the page had gone stale, as each second's tick finds it: an unlock after a
 * minute at the lock (4401), Retry after a minute on the off screen (4410), and the backoff's
 * reconnect after a minute cut off (1006). After each: once the socket is made; a second on, its
 * handshake still out; and 26 s on, it still silent. Each step is the sockets opened, whether the
 * newest is still connecting, stale, and the reads made since the socket was. */
async function staleBeforeASocket() {
  const made = async (how) => {
    let signedOut = false;
    const page = bootPage("#/", (method, where, body) => {
      if (method === "POST" && where === "api/unlock") {
        signedOut = false;
        return { status: 200, json: { ok: true, device: { id: "dev_0a1b2c3d" } } };
      }
      return signedOut ? { status: 401, json: { error: "unauthorized" } } : signedIn()(method, where, body);
    });
    await settle();
    page.acceptSockets();
    page.live().frame("remote", { allow_write: true, auto_off_at: null, version: "test" });
    await settle();
    signedOut = how === 4401;
    if (how === 1006) page.live().readyState = 3;
    page.live().fire("close", { code: how });
    await settle();
    const later = (ms) => page.run("Date.now = ((then) => () => then + " + ms + ")(Date.now());");
    later(60000);
    page.fireTimer("onSecond");
    if (how === 4401) {
      const { input, form } = unlockForm(page);
      input.value = PASSPHRASE;
      form.dispatch("submit");
    } else if (how === 4410) click(buttonNamed(page.main(), "Retry"));
    else page.fireTimer("connect");
    await settle();
    const before = page.requests.length;
    const state = () => ({
      sockets: page.sockets.length, connecting: page.live().readyState === 0, stale: page.run("S.stale"), reads: page.requests.length - before,
    });
    const steps = [state()];
    for (const ms of [1000, 26000]) {
      later(ms);
      page.fireTimer("onSecond");
      await settle();
      steps.push(state());
    }
    return steps;
  };
  return { unlock: await made(4401), retry: await made(4410), reconnect: await made(1006) };
}

/* The backoff across a sign-in that ran out while the link was down: a drop, two handshakes the
 * machine was away for, a third it refused (its probe a 401, so the lock), then the passphrase,
 * and the first socket after it failing too. Each is how long the reconnect it set waits, in s
 * before the jitter. */
async function backoffAcrossAnUnlock() {
  let away = false;
  let signedOut = false;
  const page = bootPage("#/", (method, where, body) => {
    if (method === "POST" && where === "api/unlock") {
      signedOut = false;
      return { status: 200, json: { ok: true, device: { id: "dev_0a1b2c3d" } } };
    }
    if (away) return "network";
    return signedOut ? { status: 401, json: { error: "unauthorized" } } : signedIn()(method, where, body);
  });
  await settle();
  page.acceptSockets();
  page.live().frame("remote", { allow_write: true, auto_off_at: null, version: "test" });
  await settle();
  const waits = [];
  const fail = async () => {
    page.live().readyState = 3;
    page.live().fire("close", { code: 1006 });
    await settle();
    if (page.timers().includes("connect")) waits.push(page.run("BACKOFF_SECONDS[S.backoff - 1]"));
  };
  await fail();
  away = true;
  for (let i = 0; i < 2; i++) {
    page.fireTimer("connect");
    await fail();
  }
  away = false;
  signedOut = true;
  page.fireTimer("connect");
  await fail();
  const locked = page.run("S.locked");
  const { input, form } = unlockForm(page);
  input.value = PASSPHRASE;
  form.dispatch("submit");
  await settle();
  await fail();
  return { waits, locked };
}

/* The columns the Transcript asks the machine to wrap to, on 360, 390 and 412 px phones,
 * whose transcript box is 334, 364 and 386 px inside its border; and a 340 px box, exactly 45
 * columns inside its padding by clientWidth, which is whole pixels and may have rounded up. */
async function transcriptColumns() {
  const asked = {};
  for (const width of [334, 340, 364, 386]) {
    preWidth = width;
    const page = bootPage("#/p/" + PROJECT + "/a/coder-1/transcript", signedIn({
      "GET api/transcript/coder-1": () => ({ status: 200, json: { lines: [], cursor: null, more: false, stamps: {} } }),
    }));
    await settle();
    const read = page.requests.find((one) => one.path === "api/transcript/coder-1");
    asked[width] = Number(new URLSearchParams(read.query).get("width"));
  }
  preWidth = 0;
  return { asked, padding: parseFloat(PRE_PADDING), charPx: CHAR_PX };
}

/* The Transcript's reads, each answered when the scenario says: Load older tapped twice
 * while its read is out; and Load older, then Refresh, answered newest first. The reads
 * asked for (their before cursors), the lines drawn, and whether Load older shows. */
async function transcriptLoads() {
  const opened = async () => {
    const reads = [];
    const page = bootPage("#/p/" + PROJECT + "/a/coder-1/transcript", signedIn({
      "GET api/transcript/coder-1": () => (reads[reads.length] = deferred()).promise,
    }));
    await settle();
    reads[0].settle(transcriptPage(["t3", "t4"], "100", true));
    await settle();
    const tap = (name) => click(buttonNamed(page.main(), name));
    const result = () => ({
      asked: page.requests.filter((one) => one.path === "api/transcript/coder-1").map((one) => new URLSearchParams(one.query).get("before")),
      shown: page.main().querySelectorAll("pre.transcript")[0].childNodes.map((line) => line.textContent),
      older: !buttonNamed(page.main(), "Load older").hidden,
    });
    return { page, reads, tap, result };
  };
  const twice = await opened();
  twice.tap("Load older");
  twice.tap("Load older");
  await settle();
  for (const read of twice.reads.slice(1)) read.settle(transcriptPage(["t1", "t2"], null, false));
  await settle();
  const spliced = await opened();
  spliced.tap("Load older");
  spliced.tap("Refresh");
  await settle();
  spliced.reads[2].settle(transcriptPage(["t5", "t6"], "300", true));
  await settle();
  spliced.reads[1].settle(transcriptPage(["t1", "t2"], null, false));
  await settle();
  const last = await opened();
  buttonNamed(last.page.main(), "Load older").focus();
  last.tap("Load older");
  await settle();
  last.reads[1].settle(transcriptPage(["t1", "t2"], null, false));
  await settle();
  const focus = last.page.run("document.activeElement === UI.main ? 'main' : document.activeElement.textContent");
  return { twice: twice.result(), spliced: spliced.result(), lastFocus: focus };
}

function transcriptPage(lines, cursor, more) {
  return { status: 200, json: { lines, cursor, more, stamps: {}, agent_id: "agt_1" } };
}

/* The Transcript tab, its Load older answered stale_cursor (a /clear since its first page):
 * the reads it made, what it shows, and whether Load older shows. */
async function transcriptStale() {
  const reads = [];
  const page = bootPage("#/p/" + PROJECT + "/a/coder-1/transcript", signedIn({
    "GET api/transcript/coder-1": () => (reads[reads.length] = deferred()).promise,
  }));
  await settle();
  reads[0].settle(transcriptPage(["OLD 398", "OLD 399"], "ses_1:100", true));
  await settle();
  click(buttonNamed(page.main(), "Load older"));
  await settle();
  reads[1].settle({ status: 409, json: { error: "stale_cursor", message: "coder-1 is in another conversation since that page" } });
  await settle();
  if (reads[2]) reads[2].settle(transcriptPage(["NEW 0", "NEW 1"], null, false));
  await settle();
  return {
    asked: page.requests.filter((one) => one.path === "api/transcript/coder-1").map((one) => new URLSearchParams(one.query).get("before")),
    shown: page.main().querySelectorAll("pre.transcript")[0].childNodes.map((line) => line.textContent),
    older: !buttonNamed(page.main(), "Load older").hidden,
  };
}

const TWO_DEVICES = [
  { id: "dev_0a1b2c3d", current: true, signed_in: true, ua: "this phone", last_seen: null },
  { id: "dev_4e5f6a7b", current: false, signed_in: true, ua: "another", last_seen: null },
];

/* Extend 1 h and Revoke, each tapped twice before the machine answered the first: the
 * requests that went out, and whether the button waited meanwhile. */
async function buttonsInFlight() {
  const extended = deferred();
  const strip = bootPage("#/", signedIn({
    "GET api/remote": () => ({ status: 200, json: { allow_write: true, auto_off_at: "2026-10-07T11:00:00+00:00", version: "test" } }),
    "POST api/remote/extend": () => extended.promise,
  }));
  await settle();
  strip.acceptSockets();
  await settle();
  const extend = buttonNamed(strip.run("UI.top"), "Extend 1 h");
  click(extend);
  await settle();
  const extendWaited = extend.disabled;
  click(extend);
  extended.settle({ status: 200, json: { auto_off_at: "2026-10-07T12:00:00+00:00" } });
  await settle();
  const revoked = deferred();
  const devices = bootPage("#/devices", signedIn({
    "GET api/devices": () => ({ status: 200, json: TWO_DEVICES }),
    "DELETE api/devices/dev_4e5f6a7b": () => revoked.promise,
  }));
  await settle();
  devices.acceptSockets();
  await settle();
  const revoke = buttonNamed(devices.main(), "Revoke");
  click(revoke);
  await settle();
  const revokeWaited = revoke.disabled;
  click(revoke);
  revoked.settle({ status: 200, json: { ok: true, id: "dev_4e5f6a7b", signed_out: false } });
  await settle();
  return {
    extend: { sent: strip.sent("api/remote/extend").length, waited: extendWaited, after: extend.disabled },
    revoke: { sent: devices.requests.filter((one) => one.method === "DELETE").map((one) => one.path), waited: revokeWaited },
  };
}

/* A card the feed shows: `n` makes its id, `kind` and `agent` the rest. */
function card(n, kind, agent) {
  return {
    id: "ny_" + String(n).padStart(16, "0"), kind, project: { id: PROJECT, name: "x" }, agent,
    reason: agent + " waits on you", since: "2026-10-07T10:00:00+00:00", detail: {}, answers: [], actions: ["open"],
  };
}

/* The feed's pane strips over the socket: six plan cards, then three permission prompts
 * ranked above them. Replaying what the page sent, in order: the most panes the socket held
 * at once, and which it holds at the end. */
async function stripCap() {
  const page = bootPage("#/", signedIn());
  await settle();
  page.acceptSockets();
  const plans = [1, 2, 3, 4, 5, 6].map((n) => card(n, "plan", "planner-" + n));
  page.live().frame("needs_you", { items: plans });
  await settle();
  const prompts = [7, 8, 9].map((n) => card(n, "permission", "coder-" + n));
  page.live().frame("needs_you", { items: prompts.concat(plans) });
  await settle();
  const held = new Set();
  let most = 0;
  for (const message of page.live().sent) {
    if (typeof message.subscribe === "string") held.add(message.subscribe);
    if (typeof message.unsubscribe === "string") held.delete(message.unsubscribe);
    most = Math.max(most, held.size);
  }
  return { most, held: Array.from(held).sort() };
}

/* What a screen reader is told on the agent view: the connection dot, live and then stale;
 * each tab and each bottom nav button; the Actions… sheet (the dialog, the page behind it,
 * where focus went), then Stop… in its place, then Escape; and the Keys and More toggles
 * before and after a tap. */
async function spoken() {
  const page = await agentView();
  const sheet = () => page.run("UI.sheet").childNodes[0];
  const behind = () => page.run("[UI.top, UI.banner, UI.main, UI.nav].map((part) => part.inert === true)");
  const dot = () => page.run("UI.dot").attrs["aria-label"] || null;
  const live = dot();
  page.run("S.lastFrameAt = Date.now() - 60000; checkStale();");
  const dots = [live, dot()];
  page.live().frame("heartbeat", { needs_scanned_at: null });
  const tabs = page.main().querySelectorAll("div.tabs")[0].childNodes.map((tab) => [tab.textContent, tab.attrs["aria-selected"] || null]);
  const nav = page.run("[UI.navNeeds, UI.navProjects, UI.navDevices, UI.navSettings]").map((tab) => tab.attrs["aria-current"] || null);
  const actions = buttonNamed(page.main(), "Actions…");
  actions.focus();
  click(actions);
  const opened = {
    dialog: ["role", "aria-modal", "aria-labelledby"].map((name) => sheet().attrs[name] || null),
    named: (find(sheet(), (node) => node.id && node.id === sheet().attrs["aria-labelledby"]) || { textContent: null }).textContent,
    behind: behind(),
    focusIn: sheet().contains(page.run("document.activeElement")),
  };
  click(buttonNamed(page.run("UI.sheet"), "Stop…"));
  const replaced = { named: sheetTitle(page), behind: behind(), focusIn: sheet().contains(page.run("document.activeElement")) };
  page.run("for (const fn of document.listeners.keydown || []) fn({ type: 'keydown', key: 'Escape' });");
  const escaped = { open: page.run("UI.sheet.classList.contains('open')"), behind: behind(), focusBack: page.run("document.activeElement") === actions };
  const toggles = () => ["Keys", "More"].map((name) => buttonNamed(page.main(), name).attrs["aria-expanded"] || null);
  const shut = toggles();
  click(buttonNamed(page.main(), "Keys"));
  click(buttonNamed(page.main(), "More"));
  return { dots, tabs, nav, opened, replaced, escaped, toggles: [shut, toggles()] };
}

/* The writes no other scenario sends, as the machine received them: Post on the Board tab,
 * Reply on a board question, and Restart and Switch from an agent's Actions menu; and which
 * names writePath refuses, a listed one the control. */
async function writesReachTheirRoutes() {
  const writes = (page) => page.requests.filter((one) => one.method !== "GET").map((one) => one.method + " " + one.path);
  const ok = () => ({ status: 200, json: { ok: true } });
  const board = bootPage("#/p/" + PROJECT + "/board", signedIn({ "POST api/note": ok }));
  await settle();
  board.acceptSockets();
  await settle();
  find(board.main(), (node) => node.tagName === "TEXTAREA").value = "shipping now";
  click(buttonNamed(board.main(), "Post"));
  await settle();
  const question = Object.assign({}, ITEM, {
    kind: "board_question", detail: { text: "Which store?", author: "lead-1" }, answers: [], actions: ["reply", "dismiss"],
  });
  const feed = bootPage("#/", signedIn({
    "GET api/needs": () => ({ status: 200, json: { items: [question] } }), "POST api/note": ok, "POST api/needs/dismiss": ok,
  }));
  await settle();
  feed.acceptSockets();
  await settle();
  click(buttonNamed(feed.main(), "Reply…"));
  find(feed.run("UI.sheet"), (node) => node.tagName === "TEXTAREA").value = "Postgres";
  click(buttonNamed(feed.run("UI.sheet"), "Post"));
  await settle();
  const agent = await agentView({ "POST api/agent/restart": ok, "POST api/agent/switch": ok });
  agent.live().frame("fleet", FLEET);
  await settle();
  for (const [item, go] of [["Restart…", "Restart"], ["Switch account…", "Switch account"]]) {
    click(buttonNamed(agent.main(), "Actions…"));
    click(buttonNamed(agent.run("UI.sheet"), item));
    click(buttonNamed(agent.run("UI.sheet"), go));
    await settle();
  }
  const refused = {};
  for (const name of ["agents/stop", "notes", "agent/stop"]) {
    try {
      agent.run("writePath(" + JSON.stringify(name) + ")");
      refused[name] = false;
    } catch (error) {
      refused[name] = true;
    }
  }
  return { board: writes(board), reply: writes(feed), agent: writes(agent), refused };
}

/* Waking and reconnecting (SPEC §6.4): a 4409 while the tab is hidden, then pageshow and
 * online (a network flap reaches every tab) still hidden, then the tab shown; each of
 * visibilitychange, pageshow and online on a shown tab; and what a new socket asks for after
 * a wake (on the Board tab) and after a dropped connection (on an agent's Live tab). */
async function wakes() {
  const reads = (page, from) => page.requests.slice(from).filter((one) => one.method === "GET").map((one) => one.path).sort();
  const page = bootPage("#/", signedIn());
  await settle();
  page.acceptSockets();
  await settle();
  page.run("document.visibilityState = 'hidden'");
  page.live().fire("close", { code: 4409 });
  await settle();
  const banner = page.run("UI.banner.hidden") ? "" : page.run("UI.banner").textContent;
  const replaced = { sockets: page.sockets.length, state: page.run("S.sockState"), banner, timers: page.timers() };
  fire(page, "window", "pageshow");
  await settle();
  const hiddenShow = page.sockets.length;
  let from = page.requests.length;
  fire(page, "window", "online");
  await settle();
  const hiddenOnline = { sockets: page.sockets.length, state: page.run("S.sockState"), reads: reads(page, from) };
  from = page.requests.length;
  page.run("document.visibilityState = 'visible'");
  fire(page, "document", "visibilitychange");
  await settle();
  const shown = { sockets: page.sockets.length, reads: reads(page, from) };
  const each = {};
  for (const [target, type] of [["document", "visibilitychange"], ["window", "pageshow"], ["window", "online"]]) {
    const one = bootPage("#/", signedIn());
    await settle();
    one.acceptSockets();
    await settle();
    const at = one.requests.length;
    fire(one, target, type);
    await settle();
    each[type] = { oldClosed: one.sockets[0].readyState === 3, sockets: one.sockets.length, reads: reads(one, at) };
  }
  const asks = {};
  for (const [hash, how] of [["#/p/" + PROJECT + "/board", "wake"], ["#/p/" + PROJECT + "/a/coder-1/live", "drop"]]) {
    const one = bootPage(hash, signedIn());
    await settle();
    one.acceptSockets();
    await settle();
    if (how === "wake") fire(one, "document", "visibilitychange");
    else {
      one.live().fire("close", { code: 1006 });
      await settle();
      one.fireTimer("connect");
    }
    one.acceptSockets();
    await settle();
    asks[how] = { sockets: one.sockets.length, sent: one.live().sent.map((message) => Object.keys(message).filter((key) => key !== "project").map((key) => key + " " + message[key]).join()) };
  }
  return { replaced, hiddenShow, hiddenOnline, shown, each, asks };
}

/* Cards dismissed: by hand (answered 200, and 404 for one already gone); after a Tell from
 * an asked card that the machine typed in, and after one it did not; and after a Reply on a
 * board question. The dismissals sent, the cards left, and the needs_id each Tell carried. */
async function dismissals() {
  const asked = Object.assign({}, ITEM, { kind: "asked", detail: { text: "Shall I merge?" }, answers: [], actions: ["tell", "dismiss"] });
  const question = Object.assign({}, ITEM, {
    id: "ny_00000000000000b2", kind: "board_question", detail: { text: "Which store?", author: "lead-1" }, answers: [], actions: ["reply"],
  });
  const done = () => ({ status: 200, json: { dismissed: true } });
  const feed = async (items, routes) => {
    const page = bootPage("#/", signedIn(Object.assign({ "GET api/needs": () => ({ status: 200, json: { items } }) }, routes)));
    await settle();
    page.acceptSockets();
    await settle();
    return page;
  };
  const result = (page) => ({ sent: page.sent("api/needs/dismiss"), cards: page.main().querySelectorAll("div.card").length });
  const byHand = async (answer) => {
    const page = await feed([ITEM], { "POST api/needs/dismiss": answer });
    click(buttonNamed(page.main(), "Dismiss"));
    await settle();
    return result(page);
  };
  const tell = async (delivered) => {
    const page = await feed([asked], {
      "POST api/agent/tell": (body) => ({ status: 200, json: { label: "coder-1", delivered, mode: body.mode, project: PROJECT } }),
      "POST api/needs/dismiss": done,
    });
    click(buttonNamed(page.main(), "Tell…"));
    find(page.run("UI.sheet"), (node) => node.tagName === "TEXTAREA").value = "yes, merge";
    click(buttonNamed(page.run("UI.sheet"), "Tell"));
    await settle();
    return Object.assign(result(page), { told: page.sent("api/agent/tell").map((body) => body.needs_id) });
  };
  const reply = await feed([question], { "POST api/note": () => ({ status: 200, json: { ok: true } }), "POST api/needs/dismiss": done });
  click(buttonNamed(reply.main(), "Reply…"));
  find(reply.run("UI.sheet"), (node) => node.tagName === "TEXTAREA").value = "Postgres";
  click(buttonNamed(reply.run("UI.sheet"), "Post"));
  await settle();
  return {
    byHand: await byHand(done), gone: await byHand(() => ({ status: 404, json: { error: "not_found", message: "no such item" } })),
    delivered: await tell(true), notDelivered: await tell(false), reply: result(reply),
  };
}

/* A Reply on a board question to the agent that asked, as the machine builds one for a coder
 * the fleet runs (agent and agent_id are its row), the tell typed in and filed as a note; and
 * a Reply to the manager. The sheet's title, what was sent, the toast and the dismissals. */
async function crewReplies() {
  const crew = Object.assign({}, ITEM, {
    id: "ny_00000000000000c1", kind: "board_question", detail: { text: "Take T-4 or T-5?", author: "coder-1" }, answers: [], actions: ["reply", "dismiss"],
  });
  const manager = Object.assign({}, crew, { id: "ny_00000000000000c2", agent: "manager", agent_id: "agt_m", detail: { text: "Ship it?", author: "manager" } });
  const ok = () => ({ status: 200, json: { ok: true } });
  const strip = (body) => Object.fromEntries(Object.entries(body).filter(([key]) => key !== "request_id"));
  const reply = async (item, told, go) => {
    const page = bootPage("#/", signedIn({
      "GET api/needs": () => ({ status: 200, json: { items: [item] } }), "POST api/agent/tell": told, "POST api/note": ok, "POST api/needs/dismiss": ok,
    }));
    await settle();
    page.acceptSockets();
    await settle();
    click(buttonNamed(page.main(), "Reply…"));
    const title = sheetTitle(page);
    find(page.run("UI.sheet"), (node) => node.tagName === "TEXTAREA").value = "Take T-4.";
    click(buttonNamed(page.run("UI.sheet"), go));
    await settle();
    return {
      title, told: page.sent("api/agent/tell").map(strip), noted: page.sent("api/note").map(strip),
      toast: page.toast(), dismissed: page.sent("api/needs/dismiss").map((body) => body.id),
    };
  };
  const answered = (delivered, how) => () => ({ status: 200, json: { label: "coder-1", delivered, how, mode: "auto", project: PROJECT } });
  return {
    typed: await reply(crew, answered(true, "typed into its pane (it was waiting)"), "Tell"),
    filed: await reply(crew, answered(false, "it is working — filed as board note #12 to coder-1"), "Tell"),
    manager: await reply(manager, answered(true, "typed into its pane (it was waiting)"), "Post"),
  };
}

/* Answers that come after the human left the screen that asked: a note posted on the Board
 * tab, then the tab left; a transcript read, then the Live tab opened; and Back after a hash
 * typed in by hand that is no route. The toast, where the page scrolled, and where Back went. */
async function afterLeaving() {
  const posted = deferred();
  const board = bootPage("#/p/" + PROJECT + "/board", signedIn({ "POST api/note": () => posted.promise }));
  await settle();
  board.acceptSockets();
  await settle();
  find(board.main(), (node) => node.tagName === "TEXTAREA").value = "shipping now";
  click(buttonNamed(board.main(), "Post"));
  board.run("pageGo('#/')");
  await settle();
  posted.settle({ status: 200, json: { ok: true } });
  await settle();
  const read = deferred();
  const transcript = bootPage("#/p/" + PROJECT + "/a/coder-1/transcript", signedIn({ "GET api/transcript/coder-1": () => read.promise }));
  await settle();
  transcript.run("UI.main.scrollHeight = 2400; UI.main.scrollTop = 0;");
  transcript.run("pageGo('#/p/" + PROJECT + "/a/coder-1/live')");
  await settle();
  read.settle(transcriptPage(["t1", "t2"], null, false));
  await settle();
  const typed = bootPage("#/", signedIn());
  await settle();
  typed.location.hash = "#/no/such/route";
  await settle();
  const landed = [];
  while (typed.back() && landed.length < 6) {
    await settle();
    landed.push(typed.location.hash);
  }
  return { note: board.toast(), scrolled: transcript.run("UI.main.scrollTop"), back: { landed, left: landed.length < 6 } };
}

/* What the page says for each refusal and failure (SPEC §6.4), as failText words it; a 413
 * names the most the box takes. */
async function refusalSentences() {
  const page = bootPage("#/", signedIn());
  await settle();
  const said = (res, max) => {
    const full = Object.assign({ ok: false, status: 0, data: null, error: "", message: "", retryAfter: 0, network: false, notJson: false }, res);
    return page.run("failText(" + JSON.stringify(full) + ", " + JSON.stringify(max || null) + ")");
  };
  return {
    badOrigin: said({ status: 403, error: "bad_origin", message: "this origin may not write" }),
    readOnly: said({ status: 403, error: "read_only", message: "writes are off" }),
    gone: said({ status: 404, error: "no_such_agent", message: "no live agent 'coder-1'" }),
    busy: said({ status: 409, error: "busy", message: "another action on coder-1 is still running" }),
    inProgress: said({ status: 409, error: "in_progress" }),
    other: said({ status: 409, error: "not_agent", message: "coder-1's pane is not running the agent — nothing was sent" }),
    tooLong: said({ status: 413, error: "too_large", message: "the body is too large" }, 8000),
    tooMany: said({ status: 429, error: "rate_limited", retryAfter: 30 }),
    unavailable: said({ status: 503, error: "fleet_unavailable", message: "tmux did not answer" }),
    unavailableBare: said({ status: 503, error: "unavailable" }),
    unwritable: said({
      status: 503, error: "remote_state_unwritable",
      message: "the machine could not save that: its ~/.aisquare/remote.json would not write (a full disk, or a home it may not write) — nothing was changed; fix that on the machine, then try again",
    }),
    notJson: said({ status: 200, notJson: true }),
  };
}

/* The machine's 503 sentence for a revoke it holds on the running Remote but could not save
 * (remote.json would not write), as remote_server.REVOKE_UNSAVED words it for `id`. */
function unsavedRevoke(id) {
  return {
    status: 503,
    json: {
      error: "remote_state_unwritable",
      message: "revoked on the running Remote, but the machine's ~/.aisquare/remote.json would not write (a full disk, or a home " +
        "it may not write): once it can, run  aisquare remote revoke " + id + "  on the machine, as until it is saved a change " +
        "to that file from a shell, or a Remote turned on again, would take the device back",
    },
  };
}

/* 503s whose reason the machine gives: Revoke of another device that held but was not saved,
 * then this device's own Sign out the same way; a revoke answered 404 (another tab revoked it
 * first); and the Tasks tab of a machine with Team off. What each says, the device rows after,
 * how often the list was read, and where the page went. */
async function reasonsGiven() {
  let listed = TWO_DEVICES;
  const devices = bootPage("#/devices", signedIn({
    "GET api/devices": () => ({ status: 200, json: listed }),
    "DELETE api/devices/dev_4e5f6a7b": () => {
      listed = TWO_DEVICES.slice(0, 1);
      return unsavedRevoke("dev_4e5f6a7b");
    },
    "DELETE api/devices/dev_0a1b2c3d": () => unsavedRevoke("dev_0a1b2c3d"),
  }));
  await settle();
  devices.acceptSockets();
  await settle();
  const rows = () => textsOf(devices.main().querySelectorAll("span.name"));
  const reads = () => devices.requests.filter((one) => one.method === "GET" && one.path === "api/devices").length;
  click(buttonNamed(devices.main(), "Revoke"));
  await settle();
  const revoked = { toast: devices.toast(), rows: rows(), reads: reads() };
  click(buttonNamed(devices.main(), "Sign out"));
  await settle();
  const signedOut = { toast: devices.toast(), hash: devices.location.hash };
  let gone = TWO_DEVICES;
  const twice = bootPage("#/devices", signedIn({
    "GET api/devices": () => ({ status: 200, json: gone }),
    "DELETE api/devices/dev_4e5f6a7b": () => {
      gone = TWO_DEVICES.slice(0, 1);
      return { status: 404, json: { error: "not_found", message: "no such device" } };
    },
  }));
  await settle();
  twice.acceptSockets();
  await settle();
  click(buttonNamed(twice.main(), "Revoke"));
  await settle();
  const tasks = bootPage("#/p/" + PROJECT + "/tasks", signedIn({
    "GET api/tasks": () => ({ status: 503, json: { error: "unavailable", message: "the agent orchestrator is disabled (AISQUARE_TEAM=0)" } }),
  }));
  await settle();
  return {
    revoked, signedOut,
    revokedElsewhere: { toast: twice.toast(), rows: textsOf(twice.main().querySelectorAll("span.name")) },
    tasks: textsOf(tasks.main().querySelectorAll("div.data")[0].childNodes),
  };
}

/* The socket closed 4404 (the link changed) and 4410 (Remote went off) once open; a handshake
 * that failed before open, the machine then answering its probe 404, and answering it; and a
 * socket that dropped once open, the control: no probe. What the page shows, the probes it
 * made, and the timers it holds by name. */
async function socketCloses() {
  const remote = { status: 200, json: { allow_write: true, auto_off_at: null, version: "test" } };
  const closed = async (code, opened, probe) => {
    let reads = 0;
    const page = bootPage("#/", signedIn({ "GET api/remote": () => (++reads > 1 && probe ? probe : remote) }));
    await settle();
    if (opened) page.acceptSockets();
    page.live().fire("close", { code });
    await settle();
    const heading = find(page.main(), (node) => node.tagName === "H2");
    return { shown: heading ? heading.textContent : null, probes: reads - 1, timers: page.timers() };
  };
  return {
    link: await closed(4404, true),
    off: await closed(4410, true),
    probedGone: await closed(1006, false, { status: 404, json: { error: "not_found", message: "no such link" } }),
    probedHere: await closed(1006, false, remote),
    dropped: await closed(1006, true),
  };
}

/* Off screens a wake or a tapped notification meets (SPEC §6.4 "Waking up"): Remote went off
 * (4410), then came back under the same link, as a fleet UI started again turns it on, and the
 * phone woke; a page opened while no Remote answered (404), handed a notification's card by its
 * worker once Remote was back; and a link the machine refused (4404), woken. What each shows,
 * whether it is still off, and how often it asked api/remote. */
async function offAndBack() {
  const card = "#/n/" + NEEDS_ID + "/p/" + PROJECT + "/a/coder-1";
  const heading = (page) => {
    const title = find(page.main(), (node) => node.tagName === "H2");
    return title ? title.textContent : null;
  };
  const machine = () => {
    const state = { on: true, reads: 0 };
    state.routes = {
      "GET api/remote": () => {
        state.reads += 1;
        return state.on ? { status: 200, json: { allow_write: true, auto_off_at: null, version: "test" } }
          : { status: 404, json: { error: "not_found", message: "no such link" } };
      },
      "GET api/needs": () => ({ status: 200, json: { items: [ITEM] } }),
    };
    return state;
  };
  const shown = (page, state) => ({
    off: page.run("S.off"), heading: heading(page), cards: page.main().querySelectorAll("div.card").length, reads: state.reads,
  });
  const woke = machine();
  const woken = bootPage("#/", signedIn(woke.routes));
  await settle();
  woken.acceptSockets();
  woken.live().fire("close", { code: 4410 });
  await settle();
  const wentOff = shown(woken, woke);
  woken.run("S.lastWake = 0;");
  fire(woken, "document", "visibilitychange");
  await settle();
  woken.acceptSockets();
  await settle();
  const wokeUp = Object.assign(shown(woken, woke), { sockets: woken.sockets.length, open: woken.live().readyState === 1 });
  const push = fakePush(KEY_NOW);
  const tap = machine();
  tap.on = false;
  const scope = "https://x.ngrok-free.app/r/" + "t".repeat(32) + "/";
  const tapped = bootPage("#/settings", signedIn(Object.assign(pushRoutes([]), tap.routes)), push.globals, scope);
  await settle();
  const gone = shown(tapped, tap);
  tap.on = true;
  for (const fn of push.heard.message || []) fn({ data: { type: "open", hash: card } });
  await settle();
  const landed = Object.assign(shown(tapped, tap), { hash: tapped.location.hash });
  const link = machine();
  const refused = bootPage("#/", signedIn(link.routes));
  await settle();
  refused.acceptSockets();
  refused.live().fire("close", { code: 4404 });
  await settle();
  refused.run("S.lastWake = 0;");
  fire(refused, "document", "visibilitychange");
  await settle();
  return { wentOff, wokeUp, gone, landed, link: shown(refused, link) };
}

/* The strip at the top, writes off, an auto-off set and a card in the feed, before and after the
 * socket closed 4410 (Remote went off), 4404 (the link changed) and 4401 (signed out), and a
 * handshake whose probe the machine answered 404: whether the timer, Extend and the READ-ONLY
 * pill show, what the dot says, and the tab's title. And the banner of a page whose socket
 * another tab took (4409), before and after it had to unlock. */
async function offStrip() {
  const remote = { status: 200, json: { allow_write: false, auto_off_at: new Date(Date.now() + 30 * 60000).toISOString(), version: "test" } };
  const strip = (page) => page.run("({ off: !UI.off.hidden, extend: !UI.extend.hidden, readOnly: !UI.ro.hidden, dot: UI.dot.attrs['aria-label'], title: document.title })");
  const closed = async (code, opened, probe) => {
    let reads = 0;
    const page = bootPage("#/", signedIn({
      "GET api/remote": () => (++reads > 1 && probe ? probe : remote),
      "GET api/needs": () => ({ status: 200, json: { items: [ITEM] } }),
    }));
    await settle();
    if (opened) page.acceptSockets();
    const before = strip(page);
    page.live().fire("close", { code });
    await settle();
    return { before, after: strip(page), at: page.location.hash };
  };
  const taken = bootPage("#/", signedIn());
  await settle();
  taken.acceptSockets();
  taken.live().fire("close", { code: 4409 });
  await settle();
  const said = !taken.run("UI.banner.hidden");
  taken.run("toUnlock()");
  await settle();
  return {
    off: await closed(4410, true),
    link: await closed(4404, true),
    signedOut: await closed(4401, true),
    probedGone: await closed(1006, false, { status: 404, json: { error: "not_found", message: "no such link" } }),
    takenThenLocked: { said, after: !taken.run("UI.banner.hidden"), at: taken.location.hash },
  };
}

/* The strip at the top and the bottom nav (SPEC §6.3): an auto-off 10 minutes away with writes
 * on and two cards in the feed; then a remote frame with the auto-off two hours away and
 * writes off; then the READ-ONLY pill tapped. */
async function statusStrip() {
  const at = (minutes) => new Date(Date.now() + minutes * 60000).toISOString();
  const page = bootPage("#/", signedIn({
    "GET api/remote": () => ({ status: 200, json: { allow_write: true, auto_off_at: at(10), version: "test" } }),
    "GET api/needs": () => ({ status: 200, json: { items: [ITEM, Object.assign({}, ITEM, { id: "ny_00000000000000d5" })] } }),
  }));
  await settle();
  page.acceptSockets();
  await settle();
  const strip = () => page.run("({ off: UI.off.textContent, soon: UI.off.classList.contains('soon'), extend: !UI.extend.hidden, readOnly: !UI.ro.hidden, needs: UI.badge.textContent })");
  const writesOn = strip();
  page.live().frame("remote", { allow_write: false, auto_off_at: at(120), version: "test" });
  await settle();
  const writesOff = Object.assign(strip(), { toast: page.toast() });
  click(page.run("UI.ro"));
  return { writesOn, writesOff, tapped: sheetTitle(page) };
}

/* The screens that list (SPEC §6.3): the feed with nothing in it; the Projects screen and a
 * project's Fleet tab with two cards for coder-1; the Tasks and Memory tabs; a card that
 * cleared, opened from its push link, then Back to the feed; an agent's empty transcript; and
 * its Card tab. */
async function screensListed() {
  const open = async (hash, routes) => {
    const page = bootPage(hash, signedIn(routes));
    await settle();
    page.acceptSockets();
    await settle();
    return page;
  };
  const twoCards = { "GET api/needs": () => ({ status: 200, json: { items: [ITEM, Object.assign({}, ITEM, { id: "ny_00000000000000d5" })] } }) };
  const feed = await open("#/");
  const projects = await open("#/projects", Object.assign({
    "GET api/projects": () => ({ status: 200, json: [{ id: PROJECT, name: "x", agents: { working: 1, waiting: 1 } }] }),
  }, twoCards));
  const fleet = await open("#/p/" + PROJECT + "/fleet", twoCards);
  const tasks = await open("#/p/" + PROJECT + "/tasks", {
    "GET api/tasks": () => ({
      status: 200,
      json: [
        { title: "write the docs", status: "todo" }, { title: "fix the bug", status: "doing" }, { title: "ship it", status: "done" },
        { title: "look it over", status: "review", role: "reviewer", claimed_by: "ses_1" },
      ],
    }),
  });
  const memory = await open("#/p/" + PROJECT + "/memory", {
    "GET api/memory": () => ({
      status: 200,
      json: [
        { text: "kept", pool: "project", tags: ["db"], updated_at: null },
        { text: "deleted", pool: "project", tags: [], updated_at: null, deleted_at: "2026-10-07T10:00:00+00:00" },
      ],
    }),
  });
  const cleared = await open("#/n/" + NEEDS_ID + "/p/" + PROJECT + "/a/coder-1");
  const clearedSaid = textsOf(cleared.main().querySelectorAll("div.data")[0].childNodes);
  click(buttonNamed(cleared.main(), "Back to the feed"));
  await settle();
  const transcript = await open("#/p/" + PROJECT + "/a/coder-1/transcript", { "GET api/transcript/coder-1": () => transcriptPage([], null, false) });
  const card = await open("#/p/" + PROJECT + "/a/coder-1/card", {
    "GET api/explainability/coder-1": () => ({ status: 200, json: { available: true, model: "claude-x", tokens_in: 1200, tokens_out: 300 } }),
  });
  return {
    feed: find(feed.main(), (node) => node.className === "empty").textContent,
    projects: textsOf(projects.main().querySelectorAll("span.badge")),
    fleet: textsOf(fleet.main().querySelectorAll("span.badge")),
    tasks: tasks.main().querySelectorAll("div.data")[0].childNodes.map((node) => (node.tagName === "H3" ? "# " + node.textContent : textsOf(node.childNodes).join(" | "))),
    memory: textsOf(memory.main().querySelectorAll("p.text")),
    cleared: { said: clearedSaid, back: cleared.location.hash },
    transcript: textsOf(transcript.main().querySelectorAll("pre.transcript")[0].childNodes),
    card: card.main().querySelectorAll("pre.mono")[0].textContent,
  };
}

/* The card screen, a push link's target, as its card clears, comes back (its pane printed, or a
 * scan failed) before the cleared view's fleet read answered, and then clears and comes back
 * twice more, each read answered at once. What the screen holds after each step, its card as
 * "card". */
async function cardFlicker() {
  const prompt = Object.assign({}, ITEM, {
    kind: "permission", detail: { tool: "Bash", input: { command: "rm -rf build" } }, answers: [{ label: "1", keys: ["1"] }],
  });
  const reads = [];
  const page = bootPage("#/n/" + NEEDS_ID + "/p/" + PROJECT + "/a/coder-1", signedIn({
    "GET api/needs": () => ({ status: 200, json: { items: [prompt] } }),
    "GET api/fleet": () => (reads[reads.length] = deferred()).promise,
  }));
  await settle();
  page.acceptSockets();
  await settle();
  const shown = () => page.main().querySelectorAll("div.data")[0].childNodes.map((node) => (node.classList.contains("card") ? "card" : node.textContent));
  const feed = async (items, answer) => {
    page.live().frame("needs_you", { items });
    await settle();
    if (answer) for (const read of reads) read.settle({ status: 200, json: FLEET });
    await settle();
    return shown();
  };
  const steps = { first: await feed([]) };
  await feed([prompt]);
  for (const read of reads) read.settle({ status: 200, json: FLEET }); // answered under the card
  await settle();
  steps.readLate = shown();
  return Object.assign(steps, { cleared: await feed([], true), back: await feed([prompt]), again: await feed([], true) });
}

/* Reads refused while the screen that made them stays open: a Fleet tab whose project the machine
 * no longer has (404), then a needs frame, a heartbeat saying the scans fell behind, another
 * project's fleet frame, and a wake; and the Projects screen answered 503, then two more polls
 * answered the same, and a needs frame. What the screen shows after each. */
async function failuresKept() {
  const fleet = bootPage("#/p/prj_gone/fleet", signedIn({
    "GET api/fleet": () => ({ status: 404, json: { error: "not_found", message: "no project matches 'prj_gone'" } }),
  }));
  await settle();
  fleet.acceptSockets();
  await settle();
  const tab = () => textsOf(fleet.main().querySelectorAll("div.data")[0].childNodes);
  const steps = [tab()];
  fleet.live().frame("needs_you", { items: [] });
  await settle();
  steps.push(tab());
  fleet.live().frame("heartbeat", { needs_scanned_at: "2026-10-07T10:00:00+00:00" }, { ts: "2026-10-07T10:01:00+00:00" });
  fleet.live().frame("fleet", FLEET);
  await settle();
  steps.push(tab());
  fire(fleet, "document", "visibilitychange");
  fleet.acceptSockets();
  await settle();
  steps.push(tab());
  const projects = bootPage("#/projects", signedIn({
    "GET api/projects": () => ({ status: 503, json: { error: "unavailable", message: "tmux did not answer" } }),
  }));
  await settle();
  projects.acceptSockets();
  await settle();
  const listed = () => textsOf(projects.main().querySelectorAll("div.data")[0].childNodes);
  const polls = [listed()];
  for (let n = 0; n < 2; n++) {
    projects.fireTimer("load");
    await settle();
    polls.push(listed());
  }
  projects.live().frame("needs_you", { items: [] });
  await settle();
  polls.push(listed());
  return { fleet: steps, projects: polls, reads: projects.requests.filter((one) => one.path === "api/projects").length };
}

/* The feed and a card screen whose read of the feed the machine refused (503), with no feed frame
 * yet; then the feed once a frame brings it. What each shows. */
async function needsRefused() {
  const refused = { "GET api/needs": () => ({ status: 503, json: { error: "unavailable", message: "the scan failed" } }) };
  const feed = bootPage("#/", signedIn(refused));
  await settle();
  feed.acceptSockets();
  await settle();
  const said = () => find(feed.main(), (node) => node.className === "empty");
  const steps = [[said().hidden ? "" : said().textContent, feed.main().querySelectorAll("div.card").length]];
  feed.live().frame("needs_you", { items: [ITEM] });
  await settle();
  steps.push([said().hidden ? "" : said().textContent, feed.main().querySelectorAll("div.card").length]);
  const card = bootPage("#/n/" + NEEDS_ID, signedIn(refused));
  await settle();
  return { feed: steps, card: textsOf(card.main().querySelectorAll("div.data")[0].childNodes) };
}

/* The Tasks tab of a board whose tasks were all dropped, and of one with a dropped task among
 * the rest: what it shows under its tabs. */
async function droppedTasks() {
  const shown = async (json) => {
    const page = bootPage("#/p/" + PROJECT + "/tasks", signedIn({ "GET api/tasks": () => ({ status: 200, json }) }));
    await settle();
    return textsOf(page.main().querySelectorAll("div.data")[0].childNodes);
  };
  return {
    all: await shown([{ title: "migrate the db", status: "dropped" }, { title: "old spike", status: "dropped" }]),
    some: await shown([{ title: "ship it", status: "done" }, { title: "old spike", status: "dropped" }]),
  };
}

/* Notifications where the page offers them (SPEC §6.3): the feed of a device the machine
 * sends nothing to yet; Settings with them on, and Send test answered not_subscribed; and
 * Settings in Safari on an iPhone, the page not on its Home Screen. */
async function pushScreens() {
  const feed = bootPage("#/", signedIn(pushRoutes([])), fakePush(KEY_NOW).globals);
  await settle();
  const on = bootPage("#/settings", signedIn({
    "GET api/push": () => ({ status: 200, json: { supported: true, vapid_public_key: Buffer.from(KEY_NOW).toString("base64url"), subscribed: true } }),
    "POST api/push/test": () => ({ status: 404, json: { error: "not_subscribed", message: "no subscription for this device" } }),
  }), fakePush(KEY_NOW).globals);
  await settle();
  on.acceptSockets();
  await settle();
  const said = find(on.main(), (node) => node.className === "muted").textContent;
  click(buttonNamed(on.main(), "Send test"));
  await settle();
  const iphone = bootPage("#/settings", signedIn(), {
    navigator: { userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Safari", platform: "iPhone", standalone: false },
  });
  await settle();
  const banner = find(feed.main(), (node) => node.tagName === "DIV" && node.className === "notice-line");
  return {
    banner: banner ? textsOf(banner.childNodes) : null,
    on: { said, test: on.toast() },
    iphone: find(iphone.main(), (node) => node.className === "muted").textContent,
  };
}

/* sw.js run as a worker of its scope, with the windows open in its browser at `open`: what it
 * did, in order, for a push and for each notification tap, and what it posted to each window. */
function workerAt(scope, open) {
  const did = [];
  const listeners = {};
  const windows = open.map((url) => ({ url, posted: [] }));
  const self = {
    location: { origin: new URL(scope).origin },
    registration: { scope, showNotification: async (title, options) => { did.push(["show", title, options.tag]); } },
    clients: {
      matchAll: async () => windows.map((one) => ({
        url: one.url,
        focus: async () => { did.push(["focus", one.url]); },
        postMessage: (message) => { did.push(["post", one.url, message]); one.posted.push(message); },
      })),
      openWindow: async (url) => { did.push(["open", url]); },
      claim: async () => undefined,
    },
    skipWaiting: () => undefined,
    addEventListener: (type, fn) => { listeners[type] = fn; },
  };
  vm.runInContext(WORKER, vm.createContext({ self, URL }), { filename: "sw.js" });
  const fire = async (type, event) => {
    let waited = Promise.resolve();
    listeners[type](Object.assign({ waitUntil: (promise) => { waited = promise; } }, event));
    await waited;
  };
  const tap = (url) => fire("notificationclick", { notification: { close: () => did.push(["close"]), data: { url } } });
  return { did, windows, fire, tap };
}

/* A notification tapped (SPEC §6.5): with the page open on Settings, the worker's message
 * handed to the page; with no page open; with the link on another ngrok address (the domain
 * changed) and the old page open; with a link no worker may open; and a push, shown. */
async function notificationTap() {
  const card = "#/n/" + NEEDS_ID + "/p/" + PROJECT + "/a/coder-1";
  const scope = "https://x.ngrok-free.app/r/" + "t".repeat(32) + "/";
  const open = workerAt(scope, [scope + "#/settings"]);
  await open.tap(scope + card);
  const push = fakePush(KEY_NOW);
  const page = bootPage("#/settings", signedIn(pushRoutes([])), push.globals, scope);
  await settle();
  page.acceptSockets();
  await settle();
  for (const message of open.windows[0].posted) for (const fn of push.heard.message || []) fn({ data: message });
  await settle();
  const none = workerAt(scope, []);
  await none.tap(scope + card);
  const moved = workerAt(scope, [scope + "#/"]);
  await moved.tap("https://y.ngrok-free.app/r/" + "t".repeat(32) + "/" + card);
  const forged = workerAt(scope, [scope + "#/settings"]);
  await forged.tap("https://evil.example/r/t/" + card);
  const shown = workerAt(scope, []);
  await shown.fire("push", { data: { json: () => ({ title: "x: coder-1 needs you", body: "coder-1 asks", tag: "asq-needs", url: scope + card }) } });
  return { open: open.did, landed: page.location.hash, none: none.did, moved: moved.did, forged: forged.did, shown: shown.did };
}

/* The socket the page opens, on the machine itself under http and through ngrok under https;
 * and how every request the page made on the way asked (credentials, cache, headers). */
async function socketUrls() {
  const asked = new Set();
  const at = async (base) => {
    const page = bootPage("#/", signedIn(), null, base);
    await settle();
    for (const one of page.requests) asked.add(one.how);
    return page.sockets.map((sock) => sock.url);
  };
  const urls = { http: await at(BASE), https: await at("https://x.ngrok-free.app/r/" + "t".repeat(32) + "/") };
  return Object.assign(urls, { asked: Array.from(asked, (one) => JSON.parse(one)) });
}

/* What each write carries (SPEC §6.3), as the machine received it, its request_id left out:
 * Send with ⏎ unticked; a note from the Board tab; a Reply on a board question; a card's
 * Tell; a usage limit card's Switch account; and a Tell refused agent_busy, then sent again
 * from its sheet. And an agent at its usage limit: its Actions menu, in order. */
async function writeBodies() {
  const ok = () => ({ status: 200, json: { ok: true } });
  const bodies = (page, where) => page.sent(where).map((one) => {
    const copy = Object.assign({}, one);
    delete copy.request_id;
    return copy;
  });
  const send = await agentView({ "POST api/send-keys": ok });
  find(send.main(), (node) => node.tagName === "INPUT" && node.parentNode.textContent === "⏎").checked = false;
  await typeAndSend(send, "hi");
  const board = bootPage("#/p/" + PROJECT + "/board", signedIn({ "POST api/note": ok }));
  await settle();
  board.acceptSockets();
  await settle();
  find(board.main(), (node) => node.tagName === "TEXTAREA").value = "shipping now";
  find(board.main(), (node) => node.tagName === "SELECT").value = "decision";
  find(board.main(), (node) => node.tagName === "INPUT").value = "lead-1";
  click(buttonNamed(board.main(), "Post"));
  await settle();
  const feed = async (item, routes) => {
    const page = bootPage("#/", signedIn(Object.assign({ "GET api/needs": () => ({ status: 200, json: { items: [item] } }) }, routes)));
    await settle();
    page.acceptSockets();
    await settle();
    return page;
  };
  const question = Object.assign({}, ITEM, { kind: "board_question", detail: { text: "Which store?", author: "lead-1" }, answers: [], actions: ["reply"] });
  const reply = await feed(question, { "POST api/note": ok, "POST api/needs/dismiss": ok });
  click(buttonNamed(reply.main(), "Reply…"));
  find(reply.run("UI.sheet"), (node) => node.tagName === "TEXTAREA").value = "Postgres";
  click(buttonNamed(reply.run("UI.sheet"), "Post"));
  await settle();
  const asked = Object.assign({}, ITEM, { kind: "asked", detail: { text: "Shall I merge?" }, answers: [], actions: ["tell"] });
  const tell = await feed(asked, { "POST api/agent/tell": () => ({ status: 200, json: { label: "coder-1", delivered: false, mode: "prompt", project: PROJECT } }) });
  click(buttonNamed(tell.main(), "Tell…"));
  find(tell.run("UI.sheet"), (node) => node.tagName === "TEXTAREA").value = "yes, merge";
  click(buttonNamed(tell.run("UI.sheet"), "Tell"));
  await settle();
  const limited = Object.assign({}, ITEM, { kind: "limited", detail: {}, answers: [], actions: ["switch"] });
  const switched = await feed(limited, { "POST api/agent/switch": ok });
  click(buttonNamed(switched.main(), "Switch account…"));
  click(buttonNamed(switched.run("UI.sheet"), "Switch account"));
  await settle();
  const busy = await agentView({
    "POST api/agent/tell": (body) => (body.mode === "interrupt"
      ? { status: 200, json: { label: "coder-1", delivered: true, mode: "interrupt", project: PROJECT } }
      : { status: 409, json: { error: "agent_busy", message: "coder-1 is working: interrupt it to tell it now" } }),
  });
  busy.live().frame("fleet", FLEET);
  await settle();
  click(buttonNamed(busy.main(), "Actions…"));
  click(buttonNamed(busy.run("UI.sheet"), "Tell…"));
  find(busy.run("UI.sheet"), (node) => node.tagName === "TEXTAREA").value = "stop and commit";
  click(buttonNamed(busy.run("UI.sheet"), "Tell"));
  await settle();
  const offered = buttonNamed(busy.run("UI.sheet"), "Interrupt & tell");
  click(offered);
  await settle();
  const menu = await agentView();
  menu.live().frame("fleet", Object.assign({}, FLEET, { agents: [Object.assign({}, FLEET.agents[0], { state: "limited" })] }));
  await settle();
  click(buttonNamed(menu.main(), "Actions…"));
  return {
    send: bodies(send, "api/send-keys"),
    note: bodies(board, "api/note"),
    reply: bodies(reply, "api/note"),
    tell: bodies(tell, "api/agent/tell"),
    switched: bodies(switched, "api/agent/switch"),
    busy: { offered: !!offered, modes: busy.sent("api/agent/tell").map((body) => body.mode) },
    limitedMenu: textsOf(menu.run("UI.sheet").querySelectorAll("button.row")),
  };
}

/* The key pad and the phone's keyboard never share the screen (SPEC §6.3): the box focused,
 * then Keys tapped, then the box focused again. After each: whether the pad is open, and
 * whether the box has the focus. */
async function padOrKeyboard() {
  const page = await agentView();
  const say = find(page.main(), (node) => node.tagName === "TEXTAREA" && node.className === "say");
  const state = () => [page.main().querySelectorAll("div.pad.open").length === 1, page.run("document.activeElement") === say];
  say.focus();
  const steps = [state()];
  click(buttonNamed(page.main(), "Keys"));
  steps.push(state());
  say.focus();
  steps.push(state());
  return steps;
}

/* Cards the machine answers 409 stale (SPEC §6.3, §6.4): a quick answer, the feed's read that
 * follows still listing the card (the machine has not scanned since), then the note's 6 s up
 * and a frame that lists the card still; a Tell from an asked card, nothing waiting on coder-1
 * now; and a crashed card's Stop. What the feed shows, and the reads of api/needs that
 * followed the quick answer; and the Tell's sheet, kept with what was typed, sent again. */
async function staleCards() {
  const feed = async (item, routes) => {
    const page = bootPage("#/", signedIn(Object.assign({ "GET api/needs": () => ({ status: 200, json: { items: [item] } }) }, routes)));
    await settle();
    page.acceptSockets();
    await settle();
    return page;
  };
  const shown = (page) => page.main().querySelectorAll("div.card").map((card) => (card.classList.contains("gone")
    ? card.textContent : "card: " + find(card, (node) => node.className === "reason").textContent));
  const reads = (page) => page.requests.filter((one) => one.method === "GET" && one.path === "api/needs").length;
  const stale = (current) => () => ({ status: 409, json: { error: "stale", message: "coder-1 no longer shows that question", current } });
  const now = [Object.assign({}, ITEM, { id: "ny_00000000000000e6", kind: "permission", reason: "coder-1 asks to run a command" })];
  const answered = await feed(ITEM, { "POST api/needs/answer": stale(now) });
  const before = reads(answered);
  click(answered.main().querySelectorAll("button.qa")[0]);
  await settle();
  const answer = { shown: shown(answered), reads: reads(answered) - before };
  answered.run("Date.now = ((then) => () => then + 7000)(Date.now());");
  answered.live().frame("needs_you", { items: [ITEM] });
  await settle();
  answer.later = shown(answered);
  const asked = Object.assign({}, ITEM, { kind: "asked", detail: { text: "Shall I merge?" }, answers: [], actions: ["tell"] });
  let tells = 0;
  const typed = { status: 200, json: { label: "coder-1", delivered: true, how: "typed", mode: "prompt", project: PROJECT } };
  const told = await feed(asked, { "POST api/agent/tell": () => (++tells > 1 ? typed : stale([])()) });
  const box = () => find(told.run("UI.sheet"), (node) => node.tagName === "TEXTAREA");
  click(buttonNamed(told.main(), "Tell…"));
  box().value = "yes, merge, but squash the commits first";
  click(buttonNamed(told.run("UI.sheet"), "Tell"));
  await settle();
  const said = find(told.run("UI.sheet"), (node) => node.className === "status");
  const tell = { shown: shown(told), sheet: sheetTitle(told), typed: box() && box().value, said: said && said.textContent };
  if (box()) click(buttonNamed(told.run("UI.sheet"), "Tell"));
  await settle();
  tell.sent = told.requests.filter((one) => one.path === "api/agent/tell").map((one) => [one.body.text, one.body.needs_id || null, one.body.agent_id || null]);
  tell.after = sheetTitle(told);
  const crashed = Object.assign({}, ITEM, { kind: "crashed", detail: {}, answers: [], actions: ["stop"] });
  const stopped = await feed(crashed, { "POST api/agent/stop": stale([]) });
  click(buttonNamed(stopped.main(), "Stop…"));
  click(buttonNamed(stopped.run("UI.sheet"), "Stop"));
  await settle();
  return {
    answer,
    tell,
    stop: { shown: shown(stopped), sheet: sheetTitle(stopped) },
  };
}

async function main() {
  const report = {
    bareLink: await openedSignedOut(""),
    reloadAtUnlock: await openedSignedOut("#/unlock"),
    unlockNotKept: await unlockNotKept(),
    unlockKept: await unlockKept(),
    unlockMoved: await unlockAnswered(404, { error: "not_found" }),
    unlockWrong: await unlockAnswered(401, { error: "wrong_password", message: "wrong password" }),
    lostWrite: await lostWrite(),
    lostTwice: await lostTwice(),
    toastsTogether: await toastsTogether(),
    bodyCut: await bodyCut(),
    lostKeyLongAgo: await lostKeyLongAgo(),
    lostThenSignedOut: await lostThenSignedOut(),
    lostRead: await lostRead(),
    offlineSheet: await offlineSheet(),
    emptySend: await emptySend(),
    scansStopped: await scansStopped(),
    pushKeyChanged: await pushTurnedOn(KEY_BEFORE),
    pushKeyKept: await pushTurnedOn(KEY_NOW),
    pushKeyChangedAtUnlock: await pushAfterUnlock(KEY_BEFORE),
    pushKeyKeptAtUnlock: await pushAfterUnlock(KEY_NOW),
    keyNow: Buffer.from(KEY_NOW).toString("base64url"),
    quickAnswerTwice: await quickAnswerTwice(),
    staleDevices: await staleDevices(),
    tellNotSent: await tellNotSent(),
    paneCursor: await paneCursor(),
    stopAtAPrompt: await stopAtAPrompt(),
    refusedReadOnly: await refusedReadOnly(),
    settingsFacts: await settingsFacts(),
    keyNames: await keyNames(),
    liveScroll: await liveScroll(),
    padScroll: await padScroll(),
    boardOnItsTab: await boardOnItsTab(),
    fleetOnItsScreens: await fleetOnItsScreens(),
    boardReopened: await boardReopened(),
    boardAnswers: await boardAnswers(),
    transcriptTimes: await transcriptTimes(),
    limitTimes: await limitTimes(),
    boardLimitTimes: await boardLimitTimes(),
    readsAfterFrames: await readsAfterFrames(),
    backLeaves: await backLeaves(),
    lateAnswers: await lateAnswers(),
    sheetBeforeFleet: await sheetBeforeFleet(),
    keysInOrder: await keysInOrder(),
    padConfirms: await padConfirms(),
    staleAcrossAWake: await staleAcrossAWake(),
    heldBetweenSockets: await heldBetweenSockets(),
    silentSocket: await silentSocket(),
    staleBeforeASocket: await staleBeforeASocket(),
    backoffAcrossAnUnlock: await backoffAcrossAnUnlock(),
    transcriptSend: await transcriptSend(),
    transcriptSendGuarded: await transcriptSendGuarded(),
    pinnedKeys: await pinnedKeys(),
    sheetFocus: await sheetFocus(),
    focusLands: await focusLands(),
    focusKept: await focusKept(),
    backOverASheet: await backOverASheet(),
    backWithoutCloseWatcher: await backWithoutCloseWatcher(),
    transcriptColumns: await transcriptColumns(),
    transcriptLoads: await transcriptLoads(),
    transcriptStale: await transcriptStale(),
    buttonsInFlight: await buttonsInFlight(),
    stripCap: await stripCap(),
    spoken: await spoken(),
    writesReachTheirRoutes: await writesReachTheirRoutes(),
    wakes: await wakes(),
    dismissals: await dismissals(),
    crewReplies: await crewReplies(),
    afterLeaving: await afterLeaving(),
    refusalSentences: await refusalSentences(),
    reasonsGiven: await reasonsGiven(),
    socketCloses: await socketCloses(),
    offAndBack: await offAndBack(),
    unlockWait: await unlockAnswered(429, { error: "rate_limited", message: "too many tries" }),
    statusStrip: await statusStrip(),
    offStrip: await offStrip(),
    screensListed: await screensListed(),
    cardFlicker: await cardFlicker(),
    failuresKept: await failuresKept(),
    needsRefused: await needsRefused(),
    droppedTasks: await droppedTasks(),
    pushScreens: await pushScreens(),
    notificationTap: await notificationTap(),
    socketUrls: await socketUrls(),
    writeBodies: await writeBodies(),
    padOrKeyboard: await padOrKeyboard(),
    staleCards: await staleCards(),
  };
  process.stdout.write(JSON.stringify(report) + "\n");
}

main().catch((error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error) + "\n");
  process.exitCode = 1;
});
