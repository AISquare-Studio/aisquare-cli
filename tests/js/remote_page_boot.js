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

  setAttribute() {} // what an attribute may hold is remote_page_check.js's business

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

  frame(type, payload) {
    this.fire("message", { data: JSON.stringify({ type, payload }) });
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

/* Boot app.js at `hash`, the machine answering every request through `answer`:
 * (method, path, body) -> {status, json}, or "network" for a request that never
 * arrives, or a promise of either. */
function bootPage(hash, answer) {
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

/* A read lost while the socket is fine: the next frame says the machine is there. */
async function lostRead() {
  const page = await agentView({ "GET api/projects": () => "network" });
  click(buttonNamed(page.run("UI.nav"), "Projects"));
  await settle();
  const lost = { offline: page.run("S.offline"), banner: page.run("UI.banner.hidden") ? "" : page.run("UI.banner").textContent };
  page.live().frame("heartbeat", { needs_scanned_at: null });
  await settle();
  return { lost, offline: page.run("S.offline"), bannerHidden: page.run("UI.banner.hidden") };
}

async function main() {
  const report = {
    bareLink: await openedSignedOut(""),
    reloadAtUnlock: await openedSignedOut("#/unlock"),
    unlockNotKept: await unlockNotKept(),
    unlockKept: await unlockKept(),
    lostWrite: await lostWrite(),
    lostTwice: await lostTwice(),
    lostRead: await lostRead(),
  };
  process.stdout.write(JSON.stringify(report) + "\n");
}

main().catch((error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error) + "\n");
  process.exitCode = 1;
});
