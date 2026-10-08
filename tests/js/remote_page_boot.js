/* The phone page booted in a fake browser: what it draws and sends when the
 * machine answers a certain way (SPEC §6.3, §6.4). Test-only; not packaged.
 *
 * remote_page_check.js runs the pure core. This runs the rest: each scenario
 * loads app.js afresh in its own vm context, whose globals are a browser just
 * big enough for the page. Elements keep their children, classes and
 * listeners; location's hash fires hashchange; fetch and WebSocket are answered
 * by the scenario; timers never fire on their own, so nothing waits on a clock.
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
const BASE = "http://127.0.0.1:8750/r/" + "t".repeat(32) + "/";
const PROJECT = "prj_x";
const NEEDS_ID = "ny_0123456789abcdef";
const PASSPHRASE = "amber birch cedar delta";

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
    this.dispatch("focus");
  }

  blur() {
    this.dispatch("blur");
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
    sockets.push(this);
  }

  addEventListener(type, fn) {
    (this.listeners[type] = this.listeners[type] || []).push(fn);
  }

  fire(type, extra) {
    for (const fn of (this.listeners[type] || []).slice()) fn(Object.assign({ type }, extra));
  }

  send() {}

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
  const app = new FakeElement(doc, "div");
  app.id = "app";
  doc.documentElement.appendChild(doc.body);
  doc.body.appendChild(app);

  const requests = [];
  const sockets = [];
  let timers = 0;
  const hold = () => ++timers; // a timer is an id and nothing more: none ever fires
  const win = { listeners: {} };
  let current = hash;
  const location = {
    protocol: "http:",
    get hash() {
      return current;
    },
    set hash(value) {
      const next = String(value).charAt(0) === "#" ? String(value) : "#" + value;
      if (next === current) return;
      current = next;
      setImmediate(() => { for (const fn of win.listeners.hashchange || []) fn({ type: "hashchange" }); });
    },
    toString: () => BASE + current,
  };
  const fetch = async (url, init) => {
    const where = String(url).split("?")[0];
    const body = typeof init.body === "string" ? JSON.parse(init.body) : null;
    requests.push({ method: init.method, path: where, body });
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
    setTimeout: hold,
    setInterval: hold,
    clearTimeout() {},
    clearInterval() {},
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

/* The agent view, live, with its socket open: where Send is. */
async function agentView(extra) {
  const page = bootPage("#/p/" + PROJECT + "/a/coder-1/live", signedIn(extra));
  await settle();
  page.acceptSockets();
  page.live().frame("remote", { allow_write: true, auto_off_at: null, version: "test" });
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
  };
  process.stdout.write(JSON.stringify(report) + "\n");
}

main().catch((error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error) + "\n");
  process.exitCode = 1;
});
