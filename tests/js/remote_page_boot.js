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
 * arrives, or a promise of either. `globals` adds to the browser (fakePush). */
function bootPage(hash, answer, globals) {
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
  const hold = (fn) => { // kept, and never fired but by a scenario
    timers.set(++lastTimer, fn);
    return lastTimer;
  };
  const win = { listeners: {} };
  let current = hash;
  /* The tab's history from the page's own load on: setting the hash pushes an entry, as a
   * browser does, and replace() takes the place of the one it is at. */
  const entries = [hash];
  let at = 0;
  const changed = () => setImmediate(() => { for (const fn of win.listeners.hashchange || []) fn({ type: "hashchange" }); });
  const go = (value, replace) => {
    const next = String(value).charAt(0) === "#" ? String(value) : "#" + value;
    if (next === current) return;
    current = next;
    if (replace) entries[at] = next;
    else entries.splice(++at, entries.length, next);
    changed();
  };
  const location = {
    protocol: "http:",
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
    toString: () => BASE + current,
  };
  const fetch = async (url, init) => {
    const where = String(url).split("?")[0];
    const body = typeof init.body === "string" ? JSON.parse(init.body) : null;
    requests.push({ method: init.method, path: where, body, query: String(url).split("?")[1] || "" });
    const reply = await answer(init.method, where, body);
    if (reply === "network") throw new TypeError("Failed to fetch");
    const text = JSON.stringify(reply.json);
    return { ok: reply.status >= 200 && reply.status < 300, status: reply.status, headers: { get: () => null }, text: async () => text };
  };
  Object.assign(win, {
    document: doc,
    location,
    navigator: {},
    sessionStorage: storage(),
    localStorage: storage(),
    fetch,
    WebSocket: function WebSocket() {
      return new FakeSocket(sockets);
    },
    crypto: globalThis.crypto,
    URL,
    URLSearchParams,
    getComputedStyle: (node) => (node.tagName === "PRE" ? { paddingLeft: PRE_PADDING, paddingRight: PRE_PADDING } : {}),
    setTimeout: hold,
    setInterval: hold,
    clearTimeout: (id) => timers.delete(id),
    clearInterval: (id) => timers.delete(id),
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    },
  });
  Object.assign(win, globals || {});
  win.window = win;
  const context = vm.createContext(win);
  vm.runInContext(SOURCE, context, { filename: "app.js" });

  const page = {
    sockets, location,
    run: (code) => vm.runInContext(code, context),
    main: () => page.run("UI.main"),
    toast: () => page.run("UI.toast.textContent"),
    /* Every socket the page opened and the machine has not answered yet, accepted. */
    acceptSockets() {
      for (const sock of sockets) if (sock.readyState === 0) sock.accept();
    },
    live: () => sockets[sockets.length - 1],
    sent: (where) => requests.filter((one) => one.method === "POST" && one.path === where).map((one) => one.body),
    requests,
    /* The names of the functions timers still hold, and one fired (and gone) by its name. */
    timers: () => Array.from(timers.values(), (fn) => fn.name).filter(Boolean).sort(),
    fireTimer(name) {
      for (const [id, fn] of Array.from(timers)) { // what it fires may set another: not this time
        if (fn.name !== name) continue;
        timers.delete(id);
        fn();
      }
    },
    /* The browser's Back: false once there is no entry of this page's before this one. */
    back() {
      if (at === 0) return false;
      current = entries[--at];
      changed();
      return true;
    },
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
 * `key` (bytes); what it is asked to do is written down in `log`. */
function fakePush(key) {
  const log = [];
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
    getRegistration: async () => registration, register: async () => registration, ready: Promise.resolve(registration), addEventListener() {},
  };
  const globals = {
    navigator: { serviceWorker, userAgent: "Mozilla/5.0 (Linux; Android 14)", platform: "Linux" },
    PushManager: function PushManager() {},
    Notification: { permission: "granted", requestPermission: async () => "granted" },
    isSecureContext: true,
    atob: (text) => Buffer.from(text, "base64").toString("binary"),
  };
  return { log, globals };
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
 * sheet says why in its own, and the next tap sends dismiss_dialog. */
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
  click(buttonNamed(page.run("UI.sheet"), "Press Esc (No) first"));
  await settle();
  return { said, dismissed: page.sent("api/agent/stop").map((body) => body.dismiss_dialog === true), toast: page.toast() };
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

/* The Board tab with a board frame on it, left for the Fleet tab and opened again, its read
 * held: what the tab shows meanwhile, the reads it made, and what it shows once answered. */
async function boardReopened() {
  const reads = [];
  const page = bootPage("#/p/" + PROJECT + "/board", signedIn({
    "GET api/board": () => (reads[reads.length] = deferred()).promise,
  }));
  await settle();
  page.acceptSockets();
  page.live().frame("board", { project: { id: PROJECT }, sessions: [], events: [note(1, "from before")] });
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
    if (frame) one.live().frame(tab, frame);
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
    afterAnswer: { whileAnswering, after: card.sent("api/send-keys").length },
  };
}

/* The pad's guards, which only the page keeps: ^C and ^D each ask first; a second Esc within
 * 1.5 s asks first (two open Claude Code's Rewind), and one 2 s after the last does not; a
 * second ^C the machine refuses double_press goes again, with confirm_exit, only once the
 * human says so. After each step: the sheet on screen and how many keys were sent. */
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
    return { reads, tap, result };
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
  return { twice: twice.result(), spliced: spliced.result() };
}

function transcriptPage(lines, cursor, more) {
  return { status: 200, json: { lines, cursor, more, stamps: {} } };
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

/* Waking and reconnecting (SPEC §6.4): a 4409 while the tab is hidden, then pageshow still
 * hidden, then the tab shown; each of visibilitychange, pageshow and online on a shown tab;
 * and what a new socket asks for after a wake (on the Board tab) and after a dropped
 * connection (on an agent's Live tab). */
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
  const from = page.requests.length;
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
  return { replaced, hiddenShow, shown, each, asks };
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
    lostKeyLongAgo: await lostKeyLongAgo(),
    lostThenSignedOut: await lostThenSignedOut(),
    lostRead: await lostRead(),
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
    keyNames: await keyNames(),
    liveScroll: await liveScroll(),
    padScroll: await padScroll(),
    boardOnItsTab: await boardOnItsTab(),
    boardReopened: await boardReopened(),
    transcriptTimes: await transcriptTimes(),
    readsAfterFrames: await readsAfterFrames(),
    backLeaves: await backLeaves(),
    lateAnswers: await lateAnswers(),
    sheetBeforeFleet: await sheetBeforeFleet(),
    keysInOrder: await keysInOrder(),
    padConfirms: await padConfirms(),
    staleAcrossAWake: await staleAcrossAWake(),
    transcriptSend: await transcriptSend(),
    sheetFocus: await sheetFocus(),
    transcriptColumns: await transcriptColumns(),
    transcriptLoads: await transcriptLoads(),
    buttonsInFlight: await buttonsInFlight(),
    stripCap: await stripCap(),
    spoken: await spoken(),
    writesReachTheirRoutes: await writesReachTheirRoutes(),
    wakes: await wakes(),
    dismissals: await dismissals(),
    afterLeaving: await afterLeaving(),
  };
  process.stdout.write(JSON.stringify(report) + "\n");
}

main().catch((error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error) + "\n");
  process.exitCode = 1;
});
