/* aisquare remote: the phone page aisquare-cli bundles (SPEC §6).
 *
 * One classic deferred script, ES2020, no build step, no framework, nothing
 * from another origin. It lives at /r/<token>/, so every URL here is
 * relative, routing is by hash, and the server only ever serves the top.
 *
 * Rendering rules (SPEC §6.6), held by tests/test_remote_page.py:
 * - every string the server sends reaches the DOM as text: textContent or a
 *   text node, after plainText() drops escape sequences and controls;
 * - no server string is ever written to an attribute, a URL or a style, with
 *   two exceptions, both numbers the page parses and clamps itself: an ANSI
 *   colour, clamped to 0-255 and written as rgb() through el.style.color /
 *   backgroundColor; and a pane's width, an integer clamped to 20-400, written
 *   as the --cols property that Fit width scales the pane's font by;
 * - setAttribute takes only literal names from a short list, handlers are
 *   added with addEventListener, and navigation goes through pageGo(), the
 *   one place location.hash is set (or replaced), from ids it validated.
 *
 * ansiToRuns, renderRuns and renderNeedsCard are pure: node runs them against
 * a recording fake document (tests/js/remote_page_check.js). The page boots
 * only where there is a document.
 */
"use strict";

// --- the API surface (SPEC §6.3): literal and frozen, the CI test parses both tables ---

const API = Object.freeze({
  unlock: "api/unlock", remote: "api/remote", remoteExtend: "api/remote/extend", projects: "api/projects",
  fleet: "api/fleet", board: "api/board", tasks: "api/tasks", memory: "api/memory", devices: "api/devices",
  device: "api/devices/{id}", panes: "api/panes/{agent}", transcript: "api/transcript/{agent}",
  explainability: "api/explainability/{agent}", needs: "api/needs", needsDismiss: "api/needs/dismiss",
  needsAnswer: "api/needs/answer", push: "api/push", pushSubscribe: "api/push/subscribe",
  pushSubscription: "api/push/subscription", pushTest: "api/push/test", actionsRecent: "api/actions/recent",
  ws: "ws",
});
const WRITES = Object.freeze(["send-keys", "note", "agent/tell", "agent/stop", "agent/restart", "agent/switch"]);

/* The only messages the page sends on the socket (SPEC §1.6), through wsSend. */
const SOCKET_MESSAGES = Object.freeze(["subscribe", "unsubscribe", "subscribe_fleet", "subscribe_board"]);

// --- constants ---

const NEEDS_ID = /^ny_[0-9a-f]{16}$/;
/* A project id or an agent label: what the page puts in a route or a path. Never "." or "..". */
const REF = /^(?!\.{1,2}$)[A-Za-z0-9_.-]{1,64}$/;
const DEVICE_ID = /^[A-Za-z0-9_-]{1,64}$/;
const PROJECT_TABS = ["fleet", "board", "tasks", "memory"];
const AGENT_TABS = ["live", "transcript", "card"];
const STALE_AFTER_MS = 25000;
/* How long after its tap a write whose request was lost is still sent again. */
const RETRY_WITHIN_MS = 15000;
/* A needs scan older than this, by the machine's own clock, means the scans stopped. */
const SCAN_BEHIND_MS = 30000;
const BACKOFF_SECONDS = [1, 2, 4, 8, 16, 30];
const ESC_REPEAT_MS = 1500;
const STRIP_ROWS = 10;
/* A socket watches at most 8 panes; the agent view keeps one for itself. */
const STRIPS_MAX = 6;
const PLAN_LINES = 20;
/* A card's detail text this short shows whole in its box, on any phone. */
const SHORT_TEXT = 280;
const TEXT_MAX = { keys: 2048, tell: 8000, note: 8000 };
const READ_ONLY = "writes are off — on the machine run `aisquare remote allow-write on`, " +
  "or switch Allow write actions in the R panel";
const OFF_OR_MOVED = "Remote is off on the machine, or the link changed";
const UNKEPT_SIGN_IN = "The machine took the passphrase, but this browser did not keep the sign-in — " +
  "allow cookies for this page, then unlock again.";

// --- the pure core: escapes out, runs in, DOM out ---

const C1_INTRODUCERS = { 0x9b: "[", 0x9d: "]", 0x90: "P", 0x98: "X", 0x9e: "^", 0x9f: "_" };
const BIDI = new Set([0x061c, 0x200e, 0x200f, 0x202a, 0x202b, 0x202c, 0x202d, 0x202e, 0x2066, 0x2067, 0x2068, 0x2069]);

function isPlain(code) {
  return code >= 0x20 && code !== 0x7f && (code < 0x80 || code > 0x9f) && !BIDI.has(code);
}

/* Where the control or escape at text[i] ends. A CSI ends at its final byte (an
 * SGR's parameters go to onSgr); OSC, DCS, SOS, PM and APC are strings, dropped
 * with their payload up to BEL or ST, and to the very end when unterminated, so
 * an OSC 8 link's target or a title never shows; any other control goes alone. */
function skipControl(text, i, onSgr) {
  const code = text.charCodeAt(i);
  let kind = "";
  let start = i + 1;
  if (code === 0x1b) {
    kind = text.charAt(i + 1);
    start = i + 2;
    if (!kind) return i + 1;
  } else if (C1_INTRODUCERS[code]) {
    kind = C1_INTRODUCERS[code];
  } else {
    return i + 1;
  }
  if (kind === "[") {
    let k = start;
    while (k < text.length && text.charCodeAt(k) >= 0x20 && text.charCodeAt(k) <= 0x3f) k++;
    const final = text.charCodeAt(k);
    if (final >= 0x40 && final <= 0x7e) {
      // "m" with a private marker (ESC[>4;2m sets xterm's key mode) is not a colour
      if (final === 0x6d && onSgr && "<=>?".indexOf(text.charAt(start)) < 0) onSgr(text.slice(start, k));
      return k + 1;
    }
    return k;
  }
  if ("]PX^_".indexOf(kind) >= 0) {
    for (let k = start; k < text.length; k++) {
      const byte = text.charCodeAt(k);
      if (byte === 0x07 || byte === 0x9c) return k + 1;
      if (byte === 0x1b && text.charAt(k + 1) === "\\") return k + 2;
    }
    return text.length;
  }
  let k = i + 1;
  while (k < text.length && text.charCodeAt(k) >= 0x20 && text.charCodeAt(k) <= 0x2f) k++;
  const final = text.charCodeAt(k);
  return final >= 0x30 && final <= 0x7e ? k + 1 : k;
}

/* Walk text: plain chunks to onText, SGR parameters to onSgr, everything else
 * dropped. A tab is whitespace, not a control, and stays; a newline stays only
 * where the text is more than one line. */
function scanAnsi(text, keepLines, onText, onSgr) {
  let i = 0;
  let from = 0;
  while (i < text.length) {
    const code = text.charCodeAt(i);
    if (isPlain(code) || code === 0x09 || (keepLines && code === 0x0a)) {
      i++;
      continue;
    }
    if (i > from) onText(text.slice(from, i));
    i = skipControl(text, i, onSgr);
    from = i;
  }
  if (i > from) onText(text.slice(from, i));
}

/* Any server value as display text: escapes, controls and bidi overrides out,
 * newlines and tabs kept. Whatever comes back is only ever set as text. */
function plainText(value) {
  if (value === null || value === undefined) return "";
  const text = typeof value === "string" ? value : typeof value === "number" || typeof value === "boolean" ? String(value) : "";
  let out = "";
  scanAnsi(text, true, (chunk) => { out += chunk; }, null);
  return out;
}

function toInt(value) {
  const number = parseInt(value, 10);
  return Number.isFinite(number) ? number : 0;
}

function clampByte(value) {
  const number = Math.trunc(Number(value));
  return Number.isFinite(number) ? Math.min(255, Math.max(0, number)) : 0;
}

function rgb(r, g, b) {
  return { rgb: "rgb(" + clampByte(r) + ", " + clampByte(g) + ", " + clampByte(b) + ")" };
}

/* xterm's 256 colours: 0-15 are the theme's own (classes), the rest are fixed. */
function xterm256(value) {
  const n = clampByte(value);
  if (n < 16) return { n };
  if (n >= 232) {
    const grey = 8 + 10 * (n - 232);
    return rgb(grey, grey, grey);
  }
  const level = (step) => (step === 0 ? 0 : 55 + 40 * step);
  const c = n - 16;
  return rgb(level(Math.floor(c / 36)), level(Math.floor(c / 6) % 6), level(c % 6));
}

function newSgr() {
  return { fg: null, bg: null, b: false, d: false, i: false, u: false, inv: false };
}

function sgrCode(state, code) {
  if (code === 0) Object.assign(state, newSgr());
  else if (code === 1) state.b = true;
  else if (code === 2) state.d = true;
  else if (code === 3) state.i = true;
  else if (code === 4) state.u = true;
  else if (code === 7) state.inv = true;
  else if (code === 22) { state.b = false; state.d = false; }
  else if (code === 23) state.i = false;
  else if (code === 24) state.u = false;
  else if (code === 27) state.inv = false;
  else if (code === 39) state.fg = null;
  else if (code === 49) state.bg = null;
  else if (code >= 30 && code <= 37) state.fg = { n: code - 30 };
  else if (code >= 90 && code <= 97) state.fg = { n: code - 82 };
  else if (code >= 40 && code <= 47) state.bg = { n: code - 40 };
  else if (code >= 100 && code <= 107) state.bg = { n: code - 92 };
}

/* 38/48 in the colon form: 38:5:n, 38:2:r:g:b, or 38:2:<colour space>:r:g:b. */
function colonColour(state, sub) {
  const target = sub[0] === 38 ? "fg" : sub[0] === 48 ? "bg" : null;
  if (!target) return sgrCode(state, sub[0]);
  if (sub[1] === 5 && sub.length >= 3) state[target] = xterm256(sub[2]);
  else if (sub[1] === 2 && sub.length >= 5) {
    const at = sub.length >= 6 ? 3 : 2;
    state[target] = rgb(sub[at], sub[at + 1], sub[at + 2]);
  }
  return undefined;
}

function applySgr(state, params) {
  const parts = params === "" ? ["0"] : params.split(";");
  for (let k = 0; k < parts.length; k++) {
    if (parts[k].indexOf(":") >= 0) {
      colonColour(state, parts[k].split(":").map(toInt));
      continue;
    }
    const code = toInt(parts[k]);
    if (code === 38 || code === 48) {
      const target = code === 38 ? "fg" : "bg";
      const mode = toInt(parts[k + 1]);
      if (mode === 5 && k + 2 < parts.length) {
        state[target] = xterm256(toInt(parts[k + 2]));
        k += 2;
      } else if (mode === 2 && k + 4 < parts.length) {
        state[target] = rgb(toInt(parts[k + 2]), toInt(parts[k + 3]), toInt(parts[k + 4]));
        k += 4;
      } else {
        k = parts.length; // a colour with no value: the rest of this sequence means nothing
      }
      continue;
    }
    sgrCode(state, code);
  }
}

function runOf(text, state) {
  const fg = state.inv ? state.bg : state.fg;
  const bg = state.inv ? state.fg : state.bg;
  const classes = [];
  if (state.b) classes.push("b");
  if (state.d) classes.push("d");
  if (state.i) classes.push("i");
  if (state.u) classes.push("u");
  const run = { text, classes };
  if (fg && fg.rgb) run.color = fg.rgb;
  else if (fg) classes.push("f" + fg.n);
  else if (state.inv) classes.push("rf");
  if (bg && bg.rgb) run.background = bg.rgb;
  else if (bg) classes.push("g" + bg.n);
  else if (state.inv) classes.push("rb");
  return run;
}

function sameLook(a, b) {
  return a.color === b.color && a.background === b.background && a.classes.join(" ") === b.classes.join(" ");
}

/* One pane row or transcript line as styled runs: [{text, classes, color?, background?}]. */
function ansiToRuns(row) {
  const state = newSgr();
  const runs = [];
  const text = typeof row === "string" ? row : "";
  scanAnsi(text, false, (chunk) => {
    const run = runOf(chunk, state);
    const last = runs[runs.length - 1];
    if (last && sameLook(last, run)) last.text += chunk;
    else runs.push(run);
  }, (params) => applySgr(state, params));
  return runs;
}

/* The cursor cell, inverted: the run at column x is split around one "cur" cell. */
function markCursor(runs, x) {
  const column = Math.min(toInt(x), 1000);
  if (column < 0) return runs;
  const out = [];
  let at = 0;
  let done = false;
  for (const run of runs) {
    const chars = Array.from(run.text);
    if (done || at + chars.length <= column) {
      out.push(run);
      at += chars.length;
      continue;
    }
    const cut = column - at;
    if (cut > 0) out.push(Object.assign({}, run, { text: chars.slice(0, cut).join("") }));
    out.push(Object.assign({}, run, { text: chars[cut], classes: run.classes.concat("cur") }));
    if (cut + 1 < chars.length) out.push(Object.assign({}, run, { text: chars.slice(cut + 1).join("") }));
    at += chars.length;
    done = true;
  }
  if (!done) {
    if (column > at) out.push({ text: " ".repeat(column - at), classes: [] });
    out.push({ text: " ", classes: ["cur"] });
  }
  return out;
}

const RUN_CLASSES = new Set(["b", "d", "i", "u", "rf", "rb", "cur"]);
for (let n = 0; n < 16; n++) {
  RUN_CLASSES.add("f" + n);
  RUN_CLASSES.add("g" + n);
}
const RGB = /^rgb\((\d{1,3}), (\d{1,3}), (\d{1,3})\)$/;

function safeColour(value) {
  const match = typeof value === "string" ? RGB.exec(value) : null;
  return match ? "rgb(" + clampByte(match[1]) + ", " + clampByte(match[2]) + ", " + clampByte(match[3]) + ")" : null;
}

/* Runs as one line of spans. Re-checked here, whoever built them: text through
 * plainText, classes from the fixed set, colours as clamped rgb() only. */
function renderRuns(runs, doc) {
  const line = doc.createElement("span");
  line.className = "ln";
  for (const run of Array.isArray(runs) ? runs : []) {
    if (!run || typeof run !== "object") continue;
    const text = plainText(run.text);
    if (!text) continue;
    const classes = Array.isArray(run.classes) ? run.classes.filter((name) => RUN_CLASSES.has(name)) : [];
    const color = safeColour(run.color);
    const background = safeColour(run.background);
    if (!classes.length && !color && !background) {
      line.appendChild(doc.createTextNode(text));
      continue;
    }
    const span = doc.createElement("span");
    if (classes.length) span.className = classes.join(" ");
    if (color) span.style.color = color;
    if (background) span.style.backgroundColor = background;
    span.textContent = text;
    line.appendChild(span);
  }
  return line;
}

// --- the needs card (SPEC §6.3) ---

const KINDS = {
  permission: ["Permission", "k-urgent"], question: ["Question", "k-urgent"], plan: ["Plan", "k-urgent"],
  board_question: ["Board question", "k-ask"], manager_down: ["Manager down", "k-alarm"],
  crashed: ["Crashed", "k-alarm"], limited: ["Usage limit", "k-warn"], lost: ["Pane gone", "k-alarm"],
  fleet_down: ["tmux down", "k-alarm"], asked: ["Asked you", "k-ask"], board_result: ["Result", "k-info"],
  interrupted: ["Interrupted", "k-info"],
};
/* [action, button label, is a write]: the card's buttons, in this order. */
const CARD_ACTIONS = [
  ["tell", "Tell…", true], ["reply", "Reply…", true], ["switch", "Switch account…", true],
  ["restart", "Restart…", true], ["stop", "Stop…", true], ["open", "Open", false], ["dismiss", "Dismiss", false],
];
const STRIP_KINDS = new Set(["permission", "question", "plan"]);
/* transcript._summarise_tool's keys, in its order. */
const SUMMARY_KEYS = Object.freeze(["command", "file_path", "path", "pattern", "query", "prompt", "url"]);

function isText(value) {
  return typeof value === "string" && value.trim() !== "";
}

function clip(text, limit) {
  const chars = Array.from(text);
  return chars.length > limit ? chars.slice(0, limit - 1).join("") + "…" : text;
}

function mk(doc, tag, cls, text) {
  const node = doc.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = plainText(text);
  return node;
}

/* How long ago (or how long until) an ISO time, in words; "" for nonsense. */
function ago(iso, now) {
  const then = typeof iso === "string" ? Date.parse(iso) : NaN;
  if (!Number.isFinite(then)) return "";
  const seconds = Math.round((now - then) / 1000);
  const size = Math.abs(seconds);
  if (size < 10) return "just now";
  const span = size < 60 ? size + " s" : size < 3600 ? Math.round(size / 60) + " min"
    : size < 129600 ? Math.round(size / 3600) + " h" : Math.round(size / 86400) + " d";
  return seconds > 0 ? span + " ago" : "in " + span;
}

function planBlock(box, plan, doc) {
  const text = plainText(plan);
  const lines = text.split("\n");
  const long = lines.length > PLAN_LINES;
  const pre = mk(doc, "pre", "text", long ? lines.slice(0, PLAN_LINES).join("\n") + "\n…" : text);
  box.appendChild(pre);
  if (long) {
    const more = mk(doc, "button", "link", "Show all " + lines.length + " lines");
    more.addEventListener("click", () => {
      pre.textContent = text;
      more.disabled = true;
    });
    box.appendChild(more);
  }
}

/* What the human must read before answering, by kind, as {box, text, lead}: box is null
 * when there is nothing, text is the full text the box shows, when that is what it shows,
 * and lead is what the box opens with, when the server builds the excerpt from it. */
function renderDetail(kind, detail, doc) {
  const d = detail && typeof detail === "object" && !Array.isArray(detail) ? detail : {};
  const box = mk(doc, "div", "detail");
  let shown = 0;
  let text = "";
  let lead = "";
  const add = (node) => {
    box.appendChild(node);
    shown++;
  };
  if (kind === "question" && Array.isArray(d.questions)) {
    for (const q of d.questions.slice(0, 8)) {
      if (!q || typeof q !== "object") continue;
      if (isText(q.header)) add(mk(doc, "h3", null, q.header));
      add(mk(doc, "p", "q", q.question));
      if (!lead && isText(q.question)) lead = q.question;
      if (q.multiSelect === true) add(mk(doc, "span", "flag", "multi-select"));
      const list = mk(doc, "ul", "options");
      const options = Array.isArray(q.options) ? q.options.slice(0, 20) : [];
      options.forEach((option, n) => {
        if (!option || typeof option !== "object") return;
        const about = isText(option.description) ? " — " + plainText(option.description) : "";
        list.appendChild(mk(doc, "li", null, n + 1 + ". " + plainText(option.label) + about));
      });
      add(list);
    }
  } else if (kind === "plan" && isText(d.plan)) {
    planBlock(box, d.plan, doc);
    shown++;
    lead = (plainText(d.plan).split("\n").map((line) => line.trim()).find((line) => line) || "").replace(/^#+/, "");
  } else if (kind === "permission" && isText(d.tool)) {
    const lines = ["tool: " + plainText(d.tool)];
    const input = d.input && typeof d.input === "object" && !Array.isArray(d.input) ? d.input : {};
    // An input over 16 KiB comes as {}: no lead, as only the excerpt names what is approved.
    const named = SUMMARY_KEYS.find((key) => isText(input[key]));
    if (named) lead = d.tool + "(" + input[named].trim().split(/[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/)[0].slice(0, 72);
    for (const key of Object.keys(input).slice(0, 20)) {
      const value = input[key];
      if (["string", "number", "boolean"].indexOf(typeof value) >= 0) lines.push(plainText(key) + ": " + plainText(value));
    }
    add(mk(doc, "pre", "mono", lines.join("\n")));
  } else if (kind === "crashed" || kind === "manager_down") {
    const parts = [];
    if (typeof d.exit_status === "number") parts.push("exit status " + d.exit_status);
    if (isText(d.task_id)) parts.push("task " + plainText(d.task_id));
    if (parts.length) add(mk(doc, "p", "muted", parts.join(" · ")));
  } else if (isText(d.text)) {
    if (isText(d.author)) add(mk(doc, "p", "muted", "from " + plainText(d.author)));
    add(mk(doc, "pre", "text", d.text));
    text = d.text;
  }
  return { box: shown ? box : null, text, lead };
}

/* Whether an excerpt only says again what the detail shows whole: said twice, it doubled a
 * card on a phone. A text's is cut from the text (all of it, its first 280 characters, its
 * last paragraph, the question it ends on), and one from the end of a long text stays, as
 * its box may hold it below the fold; the others are built from what the box leads with. */
function excerptRepeats(excerpt, detail) {
  const flat = (value) => plainText(value).replace(/\s+/g, " ").trim();
  const part = flat(excerpt).replace(/…$/, "").trim();
  if (part === "") return false;
  const lead = flat(detail.lead);
  if (lead !== "" && (part.startsWith(lead) || lead.startsWith(part))) return true;
  const whole = flat(detail.text);
  return whole.startsWith(part) || (whole.length <= SHORT_TEXT && whole.includes(part));
}

/* One needs item as a card. Pure: the page passes its handlers in opts, and
 * node passes none. Only div span pre p h2 h3 ul li button are made here. */
function renderNeedsCard(item, doc, opts) {
  const o = opts || {};
  const it = item && typeof item === "object" ? item : {};
  const kind = typeof it.kind === "string" && Object.prototype.hasOwnProperty.call(KINDS, it.kind) ? it.kind : null;
  const look = kind ? KINDS[kind] : ["Needs you", "k-info"];
  const card = mk(doc, "div", "card " + look[1]);
  const head = mk(doc, "div", "card-head");
  head.appendChild(mk(doc, "span", "badge " + look[1], look[0]));
  const project = it.project && typeof it.project === "object" ? it.project : {};
  const where = [project.name, it.agent].filter(isText).map((part) => clip(plainText(part), 40));
  head.appendChild(mk(doc, "span", "where", where.join(" · ")));
  const since = mk(doc, "span", "since", ago(it.since, typeof o.now === "number" ? o.now : Date.now()));
  head.appendChild(since);
  if (o.onSince) o.onSince(since, it.since);
  card.appendChild(head);
  card.appendChild(mk(doc, "p", "reason", it.reason));
  const detail = renderDetail(kind, it.detail, doc);
  if (isText(it.excerpt) && !excerptRepeats(it.excerpt, detail)) card.appendChild(mk(doc, "p", "excerpt", it.excerpt));
  if (detail.box) card.appendChild(detail.box);
  if (kind && STRIP_KINDS.has(kind)) {
    const strip = mk(doc, "pre", "strip", "the agent's screen shows here");
    card.appendChild(strip);
    if (o.onStrip) o.onStrip(strip, it);
  }
  const shut = !o.writable || !!o.stale;
  const answers = Array.isArray(it.answers) ? it.answers.slice(0, 12) : [];
  const row = mk(doc, "div", "answers");
  for (const answer of answers) {
    if (!answer || typeof answer !== "object" || !isText(answer.label)) continue;
    const quick = mk(doc, "button", "w qa", clip(plainText(answer.label), 60));
    quick.disabled = shut;
    quick.addEventListener("click", () => { if (o.onAnswer) o.onAnswer(it, answer, row); });
    row.appendChild(quick);
  }
  const wanted = Array.isArray(it.actions) ? it.actions : [];
  if (wanted.indexOf("answer") >= 0 && !row.firstChild) {
    const pad = mk(doc, "button", "ghost", "Answer with the key pad");
    pad.addEventListener("click", () => { if (o.onAction) o.onAction(it, "pad"); });
    row.appendChild(pad);
  }
  if (row.firstChild) card.appendChild(row);
  const bar = mk(doc, "div", "actions");
  for (const entry of CARD_ACTIONS) {
    if (wanted.indexOf(entry[0]) < 0) continue;
    const name = entry[0];
    const control = mk(doc, "button", entry[2] ? "w" : name === "dismiss" ? "a quiet" : "ghost", entry[1]);
    control.disabled = entry[2] ? shut : name === "dismiss" && !!o.stale;
    control.addEventListener("click", () => { if (o.onAction) o.onAction(it, name); });
    bar.appendChild(control);
  }
  if (bar.firstChild) card.appendChild(bar);
  // Shown by the stylesheet only while writes are off, so a flip needs no redraw.
  card.appendChild(mk(doc, "p", "ro-note", "Read-only: " + READ_ONLY));
  return card;
}

// --- routes: built only from validated ids, set only by pageGo (SPEC §6.6) ---

function parseRoute(hash) {
  const raw = typeof hash === "string" ? hash.replace(/^#/, "") : "";
  const path = raw === "" ? "/" : raw;
  if (path.charAt(0) !== "/") return null;
  const parts = path.split("/").slice(1);
  if (parts.length && parts[parts.length - 1] === "") parts.pop();
  let seg;
  try {
    seg = parts.map((part) => decodeURIComponent(part));
  } catch (error) {
    return null;
  }
  const n = seg.length;
  if (n === 0) return { name: "home" };
  if (n === 1 && ["unlock", "projects", "devices", "settings"].indexOf(seg[0]) >= 0) return { name: seg[0] };
  if (seg[0] === "n" && NEEDS_ID.test(seg[1] || "")) {
    if (n === 2) return { name: "card", id: seg[1] };
    if (n === 4 && seg[2] === "p" && REF.test(seg[3])) return { name: "card", id: seg[1], pid: seg[3] };
    if (n === 6 && seg[2] === "p" && REF.test(seg[3]) && seg[4] === "a" && REF.test(seg[5])) {
      return { name: "card", id: seg[1], pid: seg[3], label: seg[5] };
    }
    return null;
  }
  if (seg[0] === "p" && REF.test(seg[1] || "")) {
    if (n === 2) return { name: "project", pid: seg[1], tab: "fleet" };
    if (n === 3 && PROJECT_TABS.indexOf(seg[2]) >= 0) return { name: "project", pid: seg[1], tab: seg[2] };
    if ((n === 4 || n === 5) && seg[2] === "a" && REF.test(seg[3])) {
      const tab = n === 5 ? seg[4] : "live";
      if (AGENT_TABS.indexOf(tab) >= 0) return { name: "agent", pid: seg[1], label: seg[3], tab };
    }
  }
  return null;
}

/* The canonical hash of a route; "#/" for anything that does not validate. */
function routeHash(route) {
  const r = route && typeof route === "object" ? route : {};
  const enc = encodeURIComponent;
  if (["unlock", "projects", "devices", "settings"].indexOf(r.name) >= 0) return "#/" + r.name;
  if (r.name === "card" && NEEDS_ID.test(r.id || "")) {
    let hash = "#/n/" + r.id;
    if (REF.test(r.pid || "")) {
      hash += "/p/" + enc(r.pid);
      if (REF.test(r.label || "")) hash += "/a/" + enc(r.label);
    }
    return hash;
  }
  if (r.name === "project" && REF.test(r.pid || "")) {
    return "#/p/" + enc(r.pid) + "/" + (PROJECT_TABS.indexOf(r.tab) >= 0 ? r.tab : "fleet");
  }
  if (r.name === "agent" && REF.test(r.pid || "") && REF.test(r.label || "")) {
    return "#/p/" + enc(r.pid) + "/a/" + enc(r.label) + "/" + (AGENT_TABS.indexOf(r.tab) >= 0 ? r.tab : "live");
  }
  return "#/";
}

// --- the page (browser only from here) ---

const S = {
  remote: null, needs: null, actions: [], fleet: null, board: null,
  wantFleet: null, wantBoard: null, panes: new Map(), sock: null, sockState: "idle",
  opened: false, backoff: 0, retryTimer: 0, lastFrameAt: 0, stale: false, offline: false, away: null,
  off: null, locked: false, booting: false, view: null, route: null, pending: new Map(), orphans: new Map(),
  gone: new Map(), since: new Set(), push: null, padOnOpen: false, lastWake: 0, me: null, names: new Map(), scannedBehind: "",
  heard: { remote: 0, needs: 0 },
};
const UI = {};
const paneWatchers = new Map();

function el(tag, cls, text) {
  return mk(document, tag, cls, text);
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function button(cls, text, onClick) {
  const control = el("button", cls, text);
  control.type = "button";
  if (onClick) control.addEventListener("click", onClick);
  return control;
}

function checkbox(text, checked) {
  const label = el("label", "check");
  const box = el("input");
  box.type = "checkbox";
  box.checked = !!checked;
  label.appendChild(box);
  label.appendChild(el("span", null, text));
  return { label, box };
}

function writable() {
  return !!(S.remote && S.remote.allow_write === true);
}

function clampInt(value, low, high) {
  return Math.min(high, Math.max(low, toInt(value)));
}

function clock(iso) {
  const when = Date.parse(iso);
  if (!Number.isFinite(when)) return "";
  return new Date(when).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/* A transcript turn's time, dim after its speaker: the machine sends when (stamps), and the
 * phone's clock tells it, as every other time here. The machine's own said 17:05 for 10:05. */
function turnTime(iso) {
  const shown = typeof iso === "string" ? clock(iso) : "";
  return shown ? "\x1b[2m " + shown + "\x1b[0m" : "";
}

const STATES = {
  working: ["working", "s-working"], waiting: ["waiting", "s-waiting"], attention: ["NEEDS YOU", "s-attention"],
  limited: ["limited", "s-limited"], exited: ["exited 💤", "s-exited"], lost: ["lost", "s-lost"],
  unknown: ["unknown", "s-unknown"],
};

const STATE_SENTENCES = {
  working: "working", waiting: "waiting at its prompt", attention: "waiting on you", limited: "at its usage limit",
  exited: "exited", lost: "gone: its pane is lost", unknown: "in a state tmux does not say",
};

/* The state badge, and a NEEDS YOU badge beside it when the feed has an item for this
 * agent; just once when the state badge already says so. */
function stateBadges(state, needed) {
  const look = Object.prototype.hasOwnProperty.call(STATES, state) ? STATES[state] : STATES.unknown;
  const badges = [el("span", "badge " + look[1], look[0])];
  if (needed && state !== "attention") badges.push(el("span", "badge s-attention", "NEEDS YOU"));
  return badges;
}

function stateSentence(state) {
  return Object.prototype.hasOwnProperty.call(STATE_SENTENCES, state) ? STATE_SENTENCES[state] : STATE_SENTENCES.unknown;
}

function agentsOf(fleet) {
  return fleet && Array.isArray(fleet.agents) ? fleet.agents.filter((row) => row && row.agent && typeof row.agent === "object") : [];
}

function findAgent(fleet, label) {
  return agentsOf(fleet).find((row) => row.agent.label === label) || null;
}

function projectIdOf(payload) {
  return payload && payload.project && typeof payload.project.id === "string" ? payload.project.id : null;
}

function needsFor(pid, label) {
  return (S.needs || []).filter((item) => item && item.project && item.project.id === pid && (label === undefined || item.agent === label));
}

// --- talking to the server ---

function newRequestId() {
  const c = typeof crypto === "object" ? crypto : null;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  const bytes = new Uint8Array(16);
  c.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function apiPath(template, params) {
  return template.replace(/\{(\w+)\}/g, (whole, key) => encodeURIComponent(String(params[key])));
}

/* One request, answered as {ok, status, data, error, message, retryAfter, network, notJson}.
 * A 401 sends the page to unlock (but unlock's own), and an answer that is not
 * JSON is not this server: ngrok's offline page, or a link that moved. The one
 * exception is a bare 500, which is this server failing, said as such. */
async function apiCall(method, path, options) {
  const o = options || {};
  const init = {
    method, credentials: "same-origin", cache: "no-store",
    headers: { "content-type": "application/json", "ngrok-skip-browser-warning": "1" },
  };
  if (o.body !== undefined) init.body = JSON.stringify(o.body);
  let url = path;
  if (o.query) {
    const query = new URLSearchParams();
    for (const key of Object.keys(o.query)) if (o.query[key] !== null && o.query[key] !== undefined) query.set(key, String(o.query[key]));
    if (query.toString()) url += "?" + query.toString();
  }
  let response;
  try {
    response = await fetch(url, init);
  } catch (error) {
    setOffline(true);
    return { ok: false, status: 0, data: null, error: "offline", message: "", retryAfter: 0, network: true, notJson: false };
  }
  setOffline(false);
  let data = null;
  let json = true;
  try {
    const text = await response.text();
    if (text) data = JSON.parse(text);
  } catch (error) {
    json = false;
  }
  const body = data && typeof data === "object" && !Array.isArray(data) ? data : {};
  const crashed = !json && response.status === 500;
  const res = {
    ok: response.ok && json, status: response.status, data, network: false, notJson: !json && !crashed,
    error: crashed ? "internal_error" : typeof body.error === "string" ? body.error : "",
    message: crashed ? "the machine hit an error answering that" : typeof body.message === "string" ? body.message : "",
    retryAfter: toInt(response.headers.get("retry-after")),
  };
  if (res.status === 401 && o.auth !== false) toUnlock();
  else if (res.notJson) offScreen("gone");
  return res;
}

/* A write: a fresh request_id, and one retry with the SAME id after the next
 * reconnect when the phone lost the request, if that comes soon enough
 * (flushRetries). The server's ledger answers a retried id from what it
 * recorded, so a restart never runs twice. onWait hears when the answer has
 * to wait for the phone to be back. */
async function apiWrite(path, body, verb, onWait) {
  const id = newRequestId();
  const pending = {
    id, path, body: Object.assign({}, body, { request_id: id }), verb, at: Date.now(), resolve: null, retried: false, dropped: false,
  };
  S.pending.set(id, pending);
  savePending();
  let res = await apiCall("POST", path, { body: pending.body });
  if (res.network) {
    if (onWait) onWait();
    res = await new Promise((resolve) => {
      pending.resolve = resolve;
      // The retry waits for a reconnect (flushRetries). A socket that still looks
      // healthy would never give it one, and may be the half-open twin of the
      // connection that lost this request: replace it now. Offline, the reconnect
      // backs off until the phone is back.
      wake(true);
    });
    if (res.network) res = Object.assign({}, res, { unconfirmed: true });
  }
  // A retry lost too may still have run on the machine, and a retry answered
  // in_progress is the first one still running: either way the ledger reports
  // the result later, and the action frame then toasts it.
  if (res.unconfirmed || (res.status === 409 && res.error === "in_progress")) S.orphans.set(id, { verb, at: Date.now() });
  S.pending.delete(id);
  savePending();
  return res;
}

/* A new socket is open: each write whose request was lost goes out again, with
 * its request_id, but only within RETRY_WITHIN_MS of its tap. An id the machine
 * did receive is answered from its ledger; one it never received runs now, and
 * a key tapped minutes ago would land on whatever the agent shows by then: a
 * "1" or an Enter answering a prompt that came up since. So an older write is
 * not sent again, and neither is one from before the phone had to unlock (the
 * unlock may be a new device, whose ledger knows none of its ids). The page
 * says so, and still shows the result if the machine had it after all. */
function flushRetries() {
  const now = Date.now();
  for (const pending of S.pending.values()) {
    if (!pending.resolve || pending.retried) continue;
    pending.retried = true;
    if (pending.dropped || now - pending.at > RETRY_WITHIN_MS) {
      finishPending(pending, notSentAgain(pending.dropped ? "gone" : "late"));
    } else {
      apiCall("POST", pending.path, { body: pending.body }).then((res) => finishPending(pending, res));
    }
  }
}

/* Signed out, or Remote off: no write still in hand goes out again after it. */
function dropRetries() {
  for (const pending of S.pending.values()) {
    pending.dropped = true;
    if (!pending.resolve || pending.retried) continue;
    pending.retried = true;
    finishPending(pending, notSentAgain("gone"));
  }
}

/* The answer to a lost write that flushRetries or dropRetries kept from going out again. */
function notSentAgain(why) {
  return { ok: false, status: 0, data: null, error: "offline", message: "", retryAfter: 0, network: true, notJson: false, notSent: why };
}

function finishPending(pending, res) {
  const resolve = pending.resolve;
  pending.resolve = null;
  if (resolve) resolve(res);
}

/* What this tab still waits on, kept across a reload so a result the ledger
 * reports later is still shown (SPEC §3.9: restart and switch take 40 s). */
function savePending() {
  try {
    const rows = [];
    for (const p of S.pending.values()) rows.push([p.id, p.verb, Date.now()]);
    for (const [id, row] of S.orphans) rows.push([id, row.verb, row.at]);
    sessionStorage.setItem("asq.pending", JSON.stringify(rows));
  } catch (error) {
    // private mode or storage off: results still arrive while this tab lives
  }
}

function loadPending() {
  try {
    const rows = JSON.parse(sessionStorage.getItem("asq.pending") || "[]");
    for (const row of Array.isArray(rows) ? rows : []) {
      if (Array.isArray(row) && typeof row[0] === "string" && Date.now() - toInt(row[2]) < 15 * 60000) {
        S.orphans.set(row[0], { verb: plainText(row[1]), at: toInt(row[2]) });
      }
    }
  } catch (error) {
    S.orphans.clear();
  }
}

/* The ledger's word on requests this page sent (the action frame, or actions/recent). */
function settleFromLedger(entries) {
  if (!Array.isArray(entries)) return;
  S.actions = entries;
  for (const entry of entries) {
    if (!entry || typeof entry.request_id !== "string") continue;
    const body = entry.body && typeof entry.body === "object" ? entry.body : {};
    const status = toInt(entry.status);
    const res = {
      ok: status >= 200 && status < 300, status, data: body, network: false, notJson: false, retryAfter: 0,
      error: typeof body.error === "string" ? body.error : "", message: typeof body.message === "string" ? body.message : "",
    };
    const pending = S.pending.get(entry.request_id);
    if (pending && pending.resolve) {
      finishPending(pending, res);
    } else if (S.orphans.has(entry.request_id)) {
      const verb = S.orphans.get(entry.request_id).verb;
      S.orphans.delete(entry.request_id);
      savePending();
      toast(verb + ": " + (res.ok ? "done" : failText(res)));
    }
  }
}

/* SPEC §6.4, as one sentence per refusal. A network error reaches here only
 * once nothing will retry it: a write's one retry was lost as well, or a call
 * that is never retried. */
function failText(res, max) {
  if (res.notSent) {
    const why = res.notSent === "late" ? "the phone was away too long to be sure the agent still shows what you saw" : "the phone was signed out, or Remote went off, before the machine answered";
    return "Not sent again — " + why + ". If the machine got it, its result shows here; if not, look, then send it again.";
  }
  if (res.unconfirmed) return "Not confirmed — the connection dropped again. If the machine got it, its result shows here.";
  if (res.network) return "Could not reach the machine — try again " + (phoneOffline() ? "once the phone is back online." : "in a moment.");
  if (res.notJson) return OFF_OR_MOVED + ".";
  const message = plainText(res.message);
  if (res.status === 401) return "Unlock again to do that.";
  if (res.status === 403) return res.error === "bad_origin" ? "Open this page from the link the machine shows." : "Read-only: " + (message || READ_ONLY);
  if (res.status === 404 && res.error === "no_such_agent") return "That agent is gone.";
  if (res.status === 409 && (res.error === "busy" || res.error === "in_progress")) return "Still running — " + (message || "the result shows here when it finishes.");
  if (res.status === 413) return "Too long (max " + (max || "the limit") + " characters).";
  if (res.status === 429) return "Too many tries — wait " + (res.retryAfter || "a few") + " s.";
  if (res.status === 503) return "The machine could not answer — try again in a moment.";
  return message || plainText(res.error) || "That did not work (" + res.status + ").";
}

/* What a refusal does beyond its sentence. A read_only means writes are off now: the page
 * shows it at once, where it kept the pad and Send live until the next remote frame. */
function afterFailure(res, route) {
  if (res.status === 403 && res.error === "read_only") {
    if (writable()) {
      S.remote = Object.assign({}, S.remote, { allow_write: false });
      drawStatus();
      gateButtons();
    }
    readOnlySheet(res.message);
  }
  if (res.status === 404 && res.error === "no_such_agent" && route && route.pid) pageGo({ name: "project", pid: route.pid, tab: "fleet" }, true);
}

// --- the socket (SPEC §1.6, §6.4) ---

function wsSend(kind, value, project) {
  if (SOCKET_MESSAGES.indexOf(kind) < 0 || !S.sock || S.sockState !== "open") return;
  const message = {};
  message[kind] = value;
  if (project) message.project = project;
  S.sock.send(JSON.stringify(message));
}

function connect() {
  clearTimeout(S.retryTimer);
  if (S.off || S.locked) return;
  const old = S.sock;
  S.sock = null;
  if (old) {
    try {
      old.close(1000);
    } catch (error) {
      // a socket a sleeping phone left half-open: dropping it is the point
    }
  }
  const url = new URL(API.ws, location.toString());
  url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
  let opened = false;
  let sock;
  try {
    sock = new WebSocket(url.toString());
  } catch (error) {
    scheduleReconnect();
    return;
  }
  S.sock = sock;
  S.sockState = "connecting";
  drawStatus();
  sock.addEventListener("open", () => {
    if (S.sock !== sock) return;
    opened = true;
    S.sockState = "open";
    S.backoff = 0;
    S.lastFrameAt = Date.now();
    setOffline(false);
    resubscribe();
    flushRetries();
    drawStatus();
  });
  sock.addEventListener("message", (event) => {
    if (S.sock === sock) onFrame(event.data);
  });
  sock.addEventListener("close", (event) => {
    if (S.sock !== sock) return;
    S.sock = null;
    S.sockState = "closed";
    drawStatus();
    onClose(event.code, opened);
  });
}

function scheduleReconnect() {
  clearTimeout(S.retryTimer);
  const base = BACKOFF_SECONDS[Math.min(S.backoff, BACKOFF_SECONDS.length - 1)] * 1000;
  S.backoff++;
  S.retryTimer = setTimeout(connect, base * (0.8 + Math.random() * 0.4));
}

function onClose(code, opened) {
  if (code === 4401) return toUnlock();
  if (code === 4404) return offScreen("link");
  if (code === 4410) return offScreen("off");
  if (code === 4403) return offScreen("origin");
  if (code === 4409) {
    S.sockState = "replaced";
    drawStatus();
    drawBanner();
    return undefined;
  }
  if (opened) return scheduleReconnect();
  return probe();
}

/* A handshake that failed before open says nothing; GET api/remote does. */
async function probe() {
  const res = await apiCall("GET", API.remote);
  if (res.status === 401 || res.notJson) return; // apiCall already went to unlock, or off
  if (res.status === 404) return offScreen("gone");
  if (res.ok) setRemote(res.data);
  scheduleReconnect();
}

function resubscribe() {
  wsSend("subscribe_fleet", S.wantFleet);
  if (S.wantBoard) wsSend("subscribe_board", S.wantBoard); // a new socket sends no board until asked
  for (const watcher of paneWatchers.values()) wsSend("subscribe", watcher.label, watcher.pid);
}

function onFrame(text) {
  let frame;
  try {
    frame = JSON.parse(text);
  } catch (error) {
    return;
  }
  if (!frame || typeof frame !== "object" || typeof frame.type !== "string") return;
  S.lastFrameAt = Date.now();
  setOffline(false); // a frame is the machine answering, whatever a lost fetch said
  if (S.stale) checkStale();
  const payload = frame.payload;
  if (frame.type === "remote") {
    S.heard.remote++;
    setRemote(payload);
  } else if (frame.type === "needs_you") {
    S.heard.needs++;
    setNeeds(payload && payload.items);
  } else if (frame.type === "action") settleFromLedger(payload && payload.actions);
  else if (frame.type === "heartbeat") noteScan(frame.ts, payload);
  else if (frame.type === "fleet") {
    S.fleet = payload;
    noteName(payload);
    viewCall("fleet");
  } else if (frame.type === "board") {
    S.board = payload;
    viewCall("board");
  } else if (frame.type === "pane" && typeof frame.agent === "string") {
    const key = (typeof frame.project === "string" ? frame.project : "") + "\n" + frame.agent;
    const watcher = paneWatchers.get(key);
    if (watcher && payload && typeof payload === "object") {
      S.panes.set(key, payload);
      for (const fn of watcher.fns) fn(payload);
    }
  } else if (frame.type === "error" && payload && typeof payload === "object") toast(plainText(payload.message));
}

/* The heartbeat says when the machine last looked for what needs you, by its own clock as
 * the frame's ts is. A watcher that stopped (a tmux call hung in a scan) froze the feed
 * while the link stayed green: the feed now says how old it is instead. */
function noteScan(ts, payload) {
  const scanned = payload && typeof payload.needs_scanned_at === "string" ? payload.needs_scanned_at : "";
  const behind = Date.parse(ts) - Date.parse(scanned) > SCAN_BEHIND_MS ? scanned : "";
  if (behind === S.scannedBehind) return;
  S.scannedBehind = behind;
  viewCall("needs");
}

/* Watch one agent's pane; the returned function stops watching. */
function paneWatch(pid, label, fn) {
  if (!REF.test(pid || "") || !REF.test(label || "")) return () => {};
  const key = pid + "\n" + label;
  let watcher = paneWatchers.get(key);
  if (!watcher) {
    watcher = { pid, label, fns: new Set() };
    paneWatchers.set(key, watcher);
    wsSend("subscribe", label, pid);
  }
  watcher.fns.add(fn);
  if (S.panes.has(key)) fn(S.panes.get(key));
  return () => {
    watcher.fns.delete(fn);
    if (watcher.fns.size || paneWatchers.get(key) !== watcher) return;
    paneWatchers.delete(key);
    S.panes.delete(key);
    wsSend("unsubscribe", label, pid);
  };
}

function wantProject(pid) {
  if (S.wantFleet !== pid) {
    S.wantFleet = pid;
    if (projectIdOf(S.fleet) !== pid) S.fleet = null;
    wsSend("subscribe_fleet", pid);
  }
}

/* Board frames only while the Board tab shows (null stops them): a board is every session
 * and task of its project, sent again with every session's heartbeat, and no other screen
 * draws it. One kept from before is not shown again: no frame came while it was not asked. */
function wantBoard(pid) {
  if (S.wantBoard === pid) return;
  S.wantBoard = pid;
  S.board = null;
  wsSend("subscribe_board", pid || false);
}

// --- state that every screen shows ---

function setRemote(payload) {
  if (!payload || typeof payload !== "object") return;
  const before = S.remote;
  S.remote = payload;
  if (before && before.allow_write !== payload.allow_write) {
    toast(payload.allow_write === true ? "Writes are on" : "Writes are off — read-only");
  }
  drawStatus();
  gateButtons();
}

function setNeeds(items) {
  if (!Array.isArray(items)) return;
  S.needs = items.filter((item) => item && typeof item === "object" && NEEDS_ID.test(item.id || ""));
  const count = S.needs.length;
  document.title = (count ? "(" + count + ") " : "") + "aisquare remote";
  drawNav();
  viewCall("needs");
}

/* A read answered after a frame of its kind came is no newer than the frame, and may be
 * older (a wake reads and reconnects at once). It is dropped: the socket sends a kind again
 * only once it changes, so an older answer kept stayed, a card hidden until the next. */
async function refreshNeeds() {
  const heard = S.heard.needs;
  const res = await apiCall("GET", API.needs);
  if (S.heard.needs !== heard) return;
  if (res.ok && res.data && typeof res.data === "object") setNeeds(res.data.items);
  else if (res.status === 404 && !res.notJson && S.needs === null) setNeeds([]);
}

async function refreshActions() {
  const res = await apiCall("GET", API.actionsRecent);
  if (res.ok && res.data && typeof res.data === "object") settleFromLedger(res.data.actions);
}

async function refreshRemote() {
  const heard = S.heard.remote;
  const res = await apiCall("GET", API.remote);
  if (res.ok && S.heard.remote === heard) setRemote(res.data);
}

/* Whether the browser says this phone has no network. Only then is the phone the one
 * away: with serve stopped or the tunnel down the phone is online, and the banner that
 * told it to get back online blamed the wrong end. */
function phoneOffline() {
  return navigator.onLine === false;
}

function setOffline(offline) {
  // Who is away words the banner, so a change of that redraws it as well.
  const away = offline ? (phoneOffline() ? "phone" : "machine") : null;
  if (S.away === away) return;
  S.offline = offline;
  S.away = away;
  drawStatus();
  drawBanner();
}

function checkStale() {
  const stale = S.lastFrameAt > 0 && Date.now() - S.lastFrameAt > STALE_AFTER_MS;
  if (stale === S.stale) return;
  S.stale = stale;
  document.body.classList.toggle("stale", stale);
  gateButtons();
  drawStatus();
}

/* Writes off, or nothing heard for 25 s: every action button waits. "w" marks a
 * write, "a" an action that is not one. Sign out is neither: it is always there
 * (SPEC §6.3), and a plain DELETE that needs no live socket. */
function gateButtons() {
  const shut = !writable() || S.stale;
  document.body.classList.toggle("ro", !writable());
  for (const control of document.querySelectorAll("button.w")) control.disabled = shut || control.classList.contains("busy");
  for (const control of document.querySelectorAll("button.a")) control.disabled = S.stale || control.classList.contains("busy");
}

function viewCall(name) {
  const view = S.view;
  if (view && typeof view[name] === "function") view[name]();
}

// --- the frame around every screen: status strip, banner, nav, sheet, toast ---

function buildShell() {
  const app = document.getElementById("app");
  UI.top = el("header", "top");
  UI.dot = el("span", "dot");
  UI.dot.setAttribute("aria-hidden", "true");
  UI.where = el("span", "where", "aisquare");
  UI.ro = button("pill ro-pill", "READ-ONLY", () => readOnlySheet());
  UI.off = el("span", "autooff");
  UI.extend = button("pill w extend", "Extend 1 h", extendAutoOff);
  UI.top.append(UI.dot, UI.where, UI.ro, UI.off, UI.extend);
  UI.banner = el("div", "banner");
  UI.banner.setAttribute("aria-live", "polite");
  UI.main = el("main", "main");
  UI.nav = el("nav", "bottom");
  UI.navNeeds = button("tab", "Needs", () => pageGo("#/"));
  UI.badge = el("span", "count");
  UI.navNeeds.appendChild(UI.badge);
  UI.navProjects = button("tab", "Projects", () => pageGo("#/projects"));
  UI.navDevices = button("tab", "Devices", () => pageGo("#/devices"));
  UI.navSettings = button("tab", "Settings", () => pageGo("#/settings"));
  UI.nav.append(UI.navNeeds, UI.navProjects, UI.navDevices, UI.navSettings);
  UI.sheet = el("div", "sheet-wrap");
  UI.sheet.addEventListener("click", (event) => {
    if (event.target === UI.sheet && !UI.sheet.classList.contains("busy")) closeSheet();
  });
  UI.toast = el("div", "toast");
  UI.toast.setAttribute("role", "status");
  UI.toast.setAttribute("aria-live", "polite");
  app.append(UI.top, UI.banner, UI.main, UI.nav, UI.sheet, UI.toast);
  drawStatus();
  drawBanner();
}

function drawStatus() {
  if (!UI.dot) return;
  const live = S.sockState === "open" && !S.stale;
  UI.dot.className = "dot " + (S.offline ? "down" : live ? "live" : S.stale ? "stale" : "wait");
  UI.ro.hidden = !S.remote || writable();
  const at = S.remote && typeof S.remote.auto_off_at === "string" ? Date.parse(S.remote.auto_off_at) : NaN;
  if (Number.isFinite(at) && !S.locked) {
    const minutes = Math.max(0, Math.round((at - Date.now()) / 60000));
    UI.off.textContent = "off in " + (minutes >= 90 ? Math.round(minutes / 60) + " h" : minutes + " min");
    UI.off.classList.toggle("soon", minutes <= 15);
    UI.off.hidden = false;
    UI.extend.hidden = false;
  } else {
    UI.off.hidden = true;
    UI.extend.hidden = true;
  }
}

function drawBanner() {
  if (!UI.banner) return;
  clear(UI.banner);
  if (S.offline) {
    UI.banner.appendChild(el("p", null, S.away === "phone" ? "Offline — the page reconnects once the phone is back online."
      : "The machine is not answering — the page reconnects as soon as it does."));
  }
  if (S.sockState === "replaced") {
    UI.banner.appendChild(el("p", null, "Another tab of this phone took over the live view."));
    UI.banner.appendChild(button("ghost", "Reconnect here", () => wake(true)));
  }
  UI.banner.hidden = !UI.banner.firstChild;
}

function drawNav() {
  if (!UI.nav) return;
  const name = S.route ? S.route.name : "";
  const count = (S.needs || []).length;
  UI.badge.textContent = count ? String(count) : "";
  UI.navNeeds.classList.toggle("on", name === "home" || name === "card");
  UI.navProjects.classList.toggle("on", name === "projects" || name === "project" || name === "agent");
  UI.navDevices.classList.toggle("on", name === "devices");
  UI.navSettings.classList.toggle("on", name === "settings");
  UI.nav.hidden = name === "unlock" || !!S.off;
}

function toast(text) {
  if (!UI.toast || !text) return;
  const shown = plainText(text);
  UI.toast.textContent = shown;
  UI.toast.classList.add("show");
  clearTimeout(UI.toastTimer);
  // Four seconds reads a short line; a sentence from the machine gets time to be read.
  UI.toastTimer = setTimeout(() => UI.toast.classList.remove("show"), Math.min(10000, 4000 + Math.max(0, shown.length - 60) * 60));
}

function openSheet(title, build) {
  closeSheet();
  const panel = el("div", "sheet");
  panel.setAttribute("role", "dialog");
  panel.appendChild(el("h2", null, title));
  const body = el("div", "sheet-body");
  const status = el("p", "status");
  status.setAttribute("aria-live", "polite");
  const bar = el("div", "sheet-bar");
  panel.append(body, status, bar);
  UI.sheet.appendChild(panel);
  UI.sheet.classList.add("open");
  const sheet = {
    body, status, bar,
    busy(on) {
      UI.sheet.classList.toggle("busy", on);
      for (const control of bar.querySelectorAll("button")) {
        control.classList.toggle("busy", on);
        control.disabled = on;
      }
      if (!on) gateButtons();
    },
  };
  build(sheet);
  bar.appendChild(button("quiet", "Close", closeSheet));
  gateButtons();
  return sheet;
}

function closeSheet() {
  if (!UI.sheet) return;
  UI.sheet.classList.remove("open", "busy");
  clear(UI.sheet);
}

function confirmSheet(title, sentence, verb, onYes) {
  openSheet(title, (sheet) => {
    sheet.body.appendChild(el("p", "lead", sentence));
    sheet.bar.appendChild(button("w primary", verb, () => {
      closeSheet();
      onYes();
    }));
  });
}

function readOnlySheet(message) {
  openSheet("Read-only", (sheet) => {
    sheet.body.appendChild(el("p", "lead", "This page can watch but not act: " + (plainText(message) || READ_ONLY) + "."));
  });
}

// --- routing ---

/* THE one place the page navigates (SPEC §6.6): a route object, or a hash that validates
 * like any route (a notification's postMessage brings one). A redirect (replace) takes the
 * place of the entry it leaves: pushed, Back went to #/unlock, or to a gone agent's tab,
 * which sent the page on again, so Back never left it. */
function pageGo(target, replace) {
  const route = typeof target === "string" ? parseRoute(target) : target;
  const hash = routeHash(route);
  if (location.hash === hash) renderRoute();
  else if (replace) location.replace(hash);
  else location.hash = hash;
}

const VIEWS = {};

function renderRoute() {
  const route = parseRoute(location.hash);
  if (!route) return pageGo("#/", true);
  if (S.view && typeof S.view.cleanup === "function") S.view.cleanup();
  S.view = null;
  S.route = route;
  S.since.clear();
  closeSheet();
  clear(UI.main);
  drawNav();
  if (S.off) return drawOff();
  if (S.booting) return UI.main.appendChild(el("p", "empty", "Connecting to the machine…"));
  if (route.name !== "unlock" && S.locked) return toUnlock();
  if (route.name === "unlock" && !S.locked) return pageGo("#/", true);
  S.view = VIEWS[route.name](route, UI.main) || {};
  gateButtons();
  return undefined;
}

function toUnlock() {
  const route = S.route && S.route.name !== "unlock" ? routeHash(S.route) : null;
  try {
    if (route) sessionStorage.setItem("asq.after", route);
  } catch (error) {
    // the page still unlocks; it just lands on the feed
  }
  S.locked = true;
  dropRetries();
  clearTimeout(S.retryTimer);
  const sock = S.sock;
  S.sock = null;
  S.sockState = "idle";
  if (sock) sock.close(1000);
  if (!S.route || S.route.name !== "unlock") pageGo("#/unlock", true);
  // Already at #/unlock, as a page (re)loaded there is: its route was drawn while
  // the boot still asked who this is, so no form is on screen and no hashchange
  // will come. Draw it now. A form already shown keeps what is typed in it.
  else if (!S.view) renderRoute();
}

function offScreen(kind) {
  if (S.off === kind) return;
  S.off = kind;
  dropRetries();
  clearTimeout(S.retryTimer);
  const sock = S.sock;
  S.sock = null;
  S.sockState = "idle";
  if (sock) sock.close(1000);
  renderRoute();
}

const OFF_SCREENS = {
  off: ["Remote is off on the machine", "Turn it on again in the R panel of aisquare ui, or with aisquare remote serve, then retry."],
  gone: [OFF_OR_MOVED, "Turn it on again, or open the link the machine shows now: the link changes when ngrok " +
    "restarts without a static domain, and with regenerate-password --new-link."],
  link: ["This link is no longer valid", "Open the link the machine shows now."],
  origin: ["Open this page from the link the machine shows", "This copy of the page came from somewhere else."],
};

function drawOff() {
  const text = OFF_SCREENS[S.off] || OFF_SCREENS.gone;
  const box = el("section", "off");
  box.append(el("h2", null, text[0]), el("p", "muted", text[1]));
  box.appendChild(button("primary", "Retry", () => {
    S.off = null;
    S.backoff = 0;
    start();
  }));
  UI.main.appendChild(box);
  drawNav();
}

/* Visible again, back on the page, or back online: the socket may be a dead
 * one a sleeping phone left behind, so replace it and read everything again. */
function wake(force) {
  const now = Date.now();
  if (!force && now - S.lastWake < 1000) return;
  S.lastWake = now;
  S.backoff = 0;
  // Nothing to wake before the first answer: a page still booting, or one not unlocked.
  if (S.off || S.locked || S.booting) return;
  if (S.sockState === "replaced" && !force && document.visibilityState !== "visible") return;
  connect();
  drawBanner();
  refreshNeeds();
  refreshRemote();
  refreshActions();
}

/* Ask the machine who this is before drawing anything that would ask it again. */
async function start() {
  S.booting = true;
  renderRoute();
  const res = await apiCall("GET", API.remote, { auth: false });
  if (res.network) {
    S.retryTimer = setTimeout(start, 3000);
    return undefined;
  }
  S.booting = false;
  if (res.status === 401) return toUnlock();
  if (res.notJson) return undefined; // apiCall already drew the off screen
  if (!res.ok) return offScreen("gone");
  S.locked = false;
  goLive(res.data);
  renderRoute();
  return undefined;
}

function goLive(remote) {
  setRemote(remote);
  connect();
  refreshNeeds();
  refreshActions();
}

// --- unlock (SPEC §6.3) ---

VIEWS.unlock = (route, main) => {
  const form = el("form", "unlock");
  form.appendChild(el("h2", null, "Unlock"));
  const input = el("input", "pass");
  input.type = "password";
  input.setAttribute("aria-label", "Passphrase");
  input.setAttribute("autocomplete", "current-password");
  input.setAttribute("autocapitalize", "off");
  input.setAttribute("autocorrect", "off");
  input.setAttribute("spellcheck", "false");
  input.setAttribute("enterkeyhint", "go");
  form.appendChild(input);
  form.appendChild(el("p", "muted", "Four words, spaces or dashes — the passphrase the machine shows."));
  const show = checkbox("Show", false);
  show.box.addEventListener("change", () => { input.type = show.box.checked ? "text" : "password"; });
  form.appendChild(show.label);
  const go = button("primary", "Unlock");
  go.type = "submit";
  const said = el("p", "status");
  said.setAttribute("aria-live", "polite");
  form.append(go, said);
  let countdown = 0;
  const wait = (seconds, why) => {
    clearInterval(countdown);
    let left = seconds;
    go.disabled = true;
    const tick = () => {
      said.textContent = why + " — try again in " + left + " s";
      if (left-- <= 0) {
        clearInterval(countdown);
        go.disabled = false;
        said.textContent = "";
      }
    };
    tick();
    countdown = setInterval(tick, 1000);
  };
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!input.value.trim()) {
      said.textContent = "Type the passphrase.";
      return;
    }
    go.disabled = true;
    said.textContent = "Unlocking…";
    const res = await apiCall("POST", API.unlock, { body: { password: input.value }, auth: false });
    go.disabled = false;
    if (res.ok) {
      if (!(await unlocked(res.data)) && S.locked) said.textContent = UNKEPT_SIGN_IN;
      return undefined;
    }
    if (res.status === 401) said.textContent = "That is not the passphrase.";
    else if (res.status === 429) wait(res.retryAfter || 60, plainText(res.message) || "Too many tries");
    // The token is wrong (a new link was made) or auto-off passed: no passphrase helps,
    // and the bare "not_found" under the button never said to open the link anew.
    else if (res.status === 404) offScreen("gone");
    else said.textContent = failText(res);
    return undefined;
  });
  main.appendChild(form);
  input.focus();
  return { cleanup: () => clearInterval(countdown) };
};

/* A good passphrase: go live, then back to the route the lock interrupted. False
 * when the page is locked or off again instead: the machine answering the next
 * request as signed out means this browser did not keep the cookie. */
async function unlocked(data) {
  S.locked = false;
  S.me = data && data.device && typeof data.device.id === "string" ? data.device.id : null;
  let after = "#/";
  try {
    after = sessionStorage.getItem("asq.after") || "#/";
  } catch (error) {
    after = "#/";
  }
  pushResend();
  const res = await apiCall("GET", API.remote);
  if (S.locked || S.off) return false;
  try {
    sessionStorage.removeItem("asq.after"); // kept until now, for an unlock that has to be tried again
  } catch (error) {
    // nothing was stored
  }
  goLive(res.ok ? res.data : S.remote);
  pageGo(after, true);
  return true;
}

// --- the needs feed and one card (SPEC §6.3) ---

function trackSince(node, iso) {
  S.since.add([node, iso]);
}

function drawStrip(pre, payload) {
  clear(pre);
  if (isText(payload.error)) {
    pre.appendChild(el("span", "ln muted", payload.error));
    return;
  }
  const rows = Array.isArray(payload.rows) ? payload.rows.slice() : [];
  while (rows.length && !plainText(rows[rows.length - 1]).trim()) rows.pop();
  for (const row of rows.slice(-STRIP_ROWS)) pre.appendChild(renderRuns(ansiToRuns(row), document));
}

/* A card on screen, with its pane strip when it gets one. */
function cardEntry(item, withStrip) {
  let unwatch = null;
  const project = item.project && typeof item.project === "object" ? item.project : {};
  const node = renderNeedsCard(item, document, {
    now: Date.now(), writable: writable(), stale: S.stale, onAnswer: answerCard, onAction: actOnCard, onSince: trackSince,
    onStrip: withStrip ? (pre) => { unwatch = paneWatch(project.id, item.agent, (payload) => drawStrip(pre, payload)); } : null,
  });
  return {
    node, json: JSON.stringify(item), strip: withStrip,
    drop() {
      if (unwatch) unwatch();
      if (node.parentNode) node.parentNode.removeChild(node);
    },
  };
}

function noLongerText(item, current) {
  const now = Array.isArray(current) ? current.filter((one) => one && isText(one.reason)) : [];
  if (now.length) return "No longer needs you: " + plainText(now[0].reason);
  return "No longer needs you: nothing waits on " + plainText(item.agent || "it") + " now.";
}

VIEWS.home = (route, main) => {
  const notice = el("div", "notice");
  const behind = el("p", "notice-line");
  const list = el("div", "cards data");
  const empty = el("p", "empty", "Loading…");
  main.append(notice, behind, list, empty);
  const cards = new Map();
  const draw = () => {
    const items = S.needs || [];
    behind.hidden = !S.scannedBehind;
    behind.textContent = S.scannedBehind ? "Last looked at " + clock(S.scannedBehind) + ": the machine has stopped checking, so this may be out of date." : "";
    list.classList.toggle("behind", !!S.scannedBehind);
    empty.textContent = S.needs === null ? "Loading…" : "Nothing needs you.";
    empty.hidden = items.length > 0;
    const seen = new Set();
    let strips = 0;
    let after = null;
    for (const item of items) {
      const gone = S.gone.get(item.id);
      if (gone && gone.until > Date.now()) continue;
      seen.add(item.id);
      let entry = cards.get(item.id);
      const json = JSON.stringify(item);
      if (!entry || entry.json !== json) {
        if (entry) entry.drop();
        entry = cardEntry(item, STRIP_KINDS.has(item.kind) && strips < STRIPS_MAX);
        cards.set(item.id, entry);
      }
      if (entry.strip) strips++;
      const slot = after ? after.nextSibling : list.firstChild;
      if (entry.node !== slot) list.insertBefore(entry.node, slot);
      after = entry.node;
    }
    for (const [id, entry] of cards) {
      if (!seen.has(id)) {
        entry.drop();
        cards.delete(id);
      }
    }
    for (const [id, gone] of S.gone) {
      if (gone.until <= Date.now()) {
        if (gone.node.parentNode) gone.node.parentNode.removeChild(gone.node);
        S.gone.delete(id);
      } else if (!gone.node.parentNode) list.insertBefore(gone.node, list.firstChild);
    }
    gateButtons();
  };
  pushBanner(notice);
  draw();
  if (S.needs === null) refreshNeeds();
  return { needs: draw, cleanup: () => { for (const entry of cards.values()) entry.drop(); } };
};

VIEWS.card = (route, main) => {
  const box = el("div", "data");
  main.appendChild(box);
  let entry = null;
  let goneShown = false;
  const draw = () => {
    const item = (S.needs || []).find((one) => one.id === route.id);
    if (item) {
      if (!entry || entry.json !== JSON.stringify(item)) {
        if (entry) entry.drop();
        clear(box);
        entry = cardEntry(item, true);
        box.appendChild(entry.node);
        gateButtons();
      }
      return;
    }
    if (S.needs === null) {
      if (!box.firstChild) box.appendChild(el("p", "empty", "Loading…"));
      return;
    }
    if (entry) entry.drop();
    entry = null;
    if (!goneShown) {
      goneShown = true;
      drawCleared();
    }
  };
  const drawCleared = async () => {
    clear(box);
    box.appendChild(el("h2", null, "No longer needs you"));
    const what = el("p", "muted", route.label ? "Asking the machine about " + route.label + "…" : "It was answered, or it cleared by itself.");
    box.appendChild(what);
    if (route.pid && route.label) {
      box.appendChild(button("ghost", "Open " + route.label, () => pageGo({ name: "agent", pid: route.pid, label: route.label, tab: "live" })));
      const res = await apiCall("GET", API.fleet, { query: { project: route.pid } });
      if (res.ok) {
        const row = findAgent(res.data, route.label);
        what.textContent = row ? route.label + " is " + stateSentence(row.state) + " now." : route.label + " is not in the fleet any more.";
      } else what.textContent = failText(res);
    }
    box.appendChild(button("ghost", "Back to the feed", () => pageGo("#/")));
  };
  draw();
  if (S.needs === null) refreshNeeds();
  return { needs: draw, cleanup: () => { if (entry) entry.drop(); } };
};

/* A quick answer. Its row waits while it is in flight: a second tap would go out
 * under a second request_id, and the server's re-check would answer it "no longer
 * needs you" because the first one worked. */
async function answerCard(item, answer, row) {
  const keys = Array.isArray(answer.keys) ? answer.keys.filter((key) => typeof key === "string") : [];
  if (!keys.length) return;
  const label = plainText(answer.label);
  const quick = row ? Array.from(row.querySelectorAll("button.qa")) : [];
  for (const control of quick) control.classList.add("busy");
  gateButtons();
  const res = await apiWrite(API.needsAnswer, { id: item.id, keys }, "Answer " + label);
  for (const control of quick) control.classList.remove("busy");
  gateButtons();
  if (res.ok) return toast("Sent " + label + " to " + plainText(item.agent || "the agent"));
  if (res.status === 409 && res.error === "stale") return noLonger(item, res.data && res.data.current);
  toast(failText(res, TEXT_MAX.keys));
  return afterFailure(res);
}

/* SPEC §6.3: a card that went stale says so in its place, then the feed is read again. */
function noLonger(item, current) {
  const node = el("div", "card gone", noLongerText(item, current));
  S.gone.set(item.id, { node, until: Date.now() + 6000 });
  if (S.view && S.route && S.route.name === "card") toast(noLongerText(item, current));
  viewCall("needs");
  refreshNeeds();
  setTimeout(() => viewCall("needs"), 6100);
}

async function dismissItem(item) {
  const res = await apiCall("POST", API.needsDismiss, { body: { id: item.id } });
  if (res.ok || res.status === 404) {
    setNeeds((S.needs || []).filter((one) => one.id !== item.id));
    if (S.route && S.route.name === "card") pageGo("#/");
    return;
  }
  toast(failText(res));
}

function contextOf(item) {
  const project = item.project && typeof item.project === "object" ? item.project : {};
  return { pid: project.id, label: item.agent, agentId: typeof item.agent_id === "string" ? item.agent_id : null, needsId: item.id, item };
}

function actOnCard(item, name) {
  const ctx = contextOf(item);
  if (name === "dismiss") dismissItem(item);
  else if (name === "open" || name === "pad") {
    S.padOnOpen = name === "pad";
    if (REF.test(ctx.label || "")) pageGo({ name: "agent", pid: ctx.pid, label: ctx.label, tab: "live" });
    else pageGo({ name: "project", pid: ctx.pid, tab: "fleet" });
  } else if (name === "tell") tellSheet(ctx, "prompt");
  else if (name === "reply") replySheet(ctx);
  else if (name === "switch" || name === "restart" || name === "stop") actionSheet(name, ctx);
}

// --- writes to one agent: tell, stop, restart, switch (SPEC §3) ---

const TELL_MODES = {
  auto: ["Tell", "Typed into its prompt when it waits there; otherwise left as a board note it reads at its next prompt."],
  prompt: ["Tell", "Typed into its prompt now. Refused while it works or shows a prompt of its own."],
  interrupt: ["Interrupt & tell", "One Esc stops what it is doing; this is typed once it is back at its prompt."],
};

function tellSheet(ctx, mode) {
  const label = plainText(ctx.label);
  openSheet(TELL_MODES[mode][0] + " " + label, (sheet) => {
    const lead = el("p", "lead", TELL_MODES[mode][1]);
    const text = el("textarea", "compose");
    text.setAttribute("aria-label", "Message");
    text.maxLength = TEXT_MAX.tell;
    sheet.body.append(lead, text);
    let current = mode;
    const send = async () => {
      if (!text.value.trim()) {
        sheet.status.textContent = "Type something first.";
        return;
      }
      const body = { agent: ctx.label, project: ctx.pid, text: text.value, mode: current };
      if (ctx.needsId) body.needs_id = ctx.needsId;
      if (ctx.agentId) body.agent_id = ctx.agentId;
      sheet.busy(true);
      sheet.status.textContent = current === "interrupt" ? "Interrupting " + label + "…" : "Sending…";
      const res = await apiWrite("api/agent/tell", body, "Tell " + label, () => {
        sheet.status.textContent = "The phone lost the connection; this goes out again if it is back within 15 seconds.";
      });
      sheet.busy(false);
      if (res.ok) {
        closeSheet();
        const told = res.data && typeof res.data === "object" ? res.data : {};
        const delivered = told.delivered === true;
        // Not typed in, the machine says what happened instead: in "auto" a board note
        // the agent reads at its next prompt; in the other two, text pasted at the
        // prompt that tmux could not send with Enter, which holds the agent's next
        // question back until Enter is pressed on the pad.
        if (delivered) toast("Typed into " + label);
        else if (isText(told.how)) toast(label + ": " + plainText(told.how));
        else toast(current === "auto" ? "Left a note for " + label + " — it reads it at its next prompt" : "Not typed into " + label + " — look at its pane");
        if (delivered && ctx.needsId) dismissItem({ id: ctx.needsId });
        return;
      }
      if (res.status === 409 && res.error === "stale" && ctx.item) {
        closeSheet();
        noLonger(ctx.item, res.data && res.data.current);
        return;
      }
      sheet.status.textContent = failText(res, TEXT_MAX.tell);
      if (res.status === 409 && res.error === "agent_busy" && current !== "interrupt") {
        current = "interrupt";
        go.textContent = "Interrupt & tell";
        lead.textContent = TELL_MODES.interrupt[1];
      }
      afterFailure(res, ctx);
    };
    const go = button("w primary", TELL_MODES[mode][0], send);
    sheet.bar.appendChild(go);
    text.focus();
  });
}

function replySheet(ctx) {
  const detail = ctx.item.detail && typeof ctx.item.detail === "object" ? ctx.item.detail : {};
  const author = typeof detail.author === "string" ? detail.author : "";
  openSheet("Reply on the board", (sheet) => {
    sheet.body.appendChild(el("p", "lead", isText(author) ? "A note to " + author + " on the board." : "A note on the board."));
    const text = el("textarea", "compose");
    text.setAttribute("aria-label", "Reply");
    text.maxLength = TEXT_MAX.note;
    sheet.body.appendChild(text);
    sheet.bar.appendChild(button("w primary", "Post", async () => {
      if (!text.value.trim()) {
        sheet.status.textContent = "Type something first.";
        return;
      }
      const body = { text: text.value, kind: "note", project: ctx.pid };
      if (isText(author)) body.to = author;
      sheet.busy(true);
      const res = await apiWrite("api/note", body, "Reply");
      sheet.busy(false);
      if (res.ok) {
        closeSheet();
        toast("Posted on the board");
        dismissItem(ctx.item);
        return;
      }
      sheet.status.textContent = failText(res, TEXT_MAX.note);
      afterFailure(res, ctx);
    }));
    text.focus();
  });
}

const AGENT_ACTIONS = {
  stop: { title: "Stop", busy: "Stopping", done: "Stopped" },
  restart: { title: "Restart", busy: "Restarting", done: "Restarted" },
  switch: { title: "Switch account", busy: "Switching", done: "Switched" },
};

function effectSentence(kind, label, o) {
  let text;
  if (kind === "stop") text = o.force ? "Stop " + label + " now: its window is killed without /exit." : "Stop " + label + ": /exit, then its window is killed after 5 s.";
  else if (kind === "restart") text = "Restart " + label + ": it stops, then starts again on " + (o.fresh ? "a fresh conversation." : "its own conversation.") + " This can take 40 s.";
  else text = "Switch " + label + (o.to ? " to " + o.to : " to the account with the most headroom") + ": it hands over and carries on there. This can take 40 s.";
  return o.dismiss ? text + " Its prompt is dismissed (No) first." : text;
}

function doneSentence(kind, label, data) {
  const d = data && typeof data === "object" ? data : {};
  if (kind === "stop" && isText(d.release_failed)) return "Stopped " + label + ", but its claims were not released: " + plainText(d.release_failed);
  if (kind === "restart" && d.resumed === true) return "Restarted " + label + " on its own conversation";
  return AGENT_ACTIONS[kind].done + " " + label;
}

/* Stop, restart or switch: a sheet that says what will happen, sends confirm and
 * agent_id (and needs_id from a card), and offers Esc first on dialog_open. */
function actionSheet(kind, ctx) {
  const label = plainText(ctx.label);
  const meta = AGENT_ACTIONS[kind];
  openSheet(meta.title + " " + label, (sheet) => {
    const lead = el("p", "lead");
    sheet.body.appendChild(lead);
    const force = kind === "stop" ? checkbox("Force: kill the window now, without /exit", false) : null;
    const fresh = kind === "restart" ? checkbox("Fresh: start a new conversation", false) : null;
    let to = null;
    if (kind === "switch") {
      to = el("input", "field");
      to.setAttribute("aria-label", "Account (optional)");
      to.setAttribute("autocapitalize", "off");
      to.setAttribute("autocorrect", "off");
      to.setAttribute("spellcheck", "false");
      to.placeholder = "account (optional): slot, alias or email";
      to.maxLength = 200;
      sheet.body.appendChild(to);
    }
    for (const option of [force, fresh]) if (option) sheet.body.appendChild(option.label);
    let dismiss = false;
    const say = () => {
      lead.textContent = effectSentence(kind, label, {
        force: force && force.box.checked, fresh: fresh && fresh.box.checked,
        to: to && to.value.trim() ? plainText(to.value.trim()) : "", dismiss,
      });
    };
    for (const option of [force, fresh]) if (option) option.box.addEventListener("change", say);
    if (to) to.addEventListener("input", say);
    say();
    const run = async () => {
      if (!ctx.agentId) {
        sheet.status.textContent = "Waiting for the fleet to say which " + label + " this is — try again in a second.";
        return;
      }
      const body = { agent: ctx.label, project: ctx.pid, agent_id: ctx.agentId, confirm: ctx.label };
      if (ctx.needsId) body.needs_id = ctx.needsId;
      if (dismiss) body.dismiss_dialog = true;
      if (force && force.box.checked) body.force = true;
      if (fresh && fresh.box.checked) body.fresh = true;
      if (to && to.value.trim()) body.to = to.value.trim();
      sheet.busy(true);
      sheet.status.textContent = meta.busy + " " + label + "…";
      const res = await apiWrite("api/agent/" + kind, body, meta.title + " " + label, () => {
        sheet.status.textContent = "The phone lost the connection. If the machine got this it carries on, and the result shows here once the phone is back.";
      });
      sheet.busy(false);
      if (res.ok) {
        closeSheet();
        toast(doneSentence(kind, label, res.data));
        refreshNeeds();
        return;
      }
      if (res.status === 409 && res.error === "stale") {
        closeSheet();
        if (ctx.item) noLonger(ctx.item, res.data && res.data.current);
        else toast(label + " changed since this screen loaded — look again, then retry.");
        return;
      }
      if (res.status === 409 && res.error === "dialog_open" && !dismiss) {
        // The machine's sentence is for curl ("send dismiss_dialog: true"); here that
        // is the button. "May": a tool still waiting on its result counts as a prompt.
        sheet.status.textContent = label + " may be showing a prompt that " + meta.busy.toLowerCase() +
          " it now would answer. Press Esc (No) first to dismiss it.";
        dismiss = true;
        say();
        go.textContent = "Press Esc (No) first";
      } else sheet.status.textContent = failText(res);
      afterFailure(res, ctx);
    };
    const go = button("w primary", meta.title, run);
    sheet.bar.appendChild(go);
  });
}

// --- projects ---

VIEWS.projects = (route, main) => {
  main.appendChild(el("h2", "title", "Projects"));
  const list = el("div", "rows data");
  main.appendChild(list);
  let rows = null;
  const draw = () => {
    clear(list);
    if (!rows) return;
    if (!rows.length) list.appendChild(el("p", "empty", "No projects on this machine yet."));
    for (const row of rows) {
      if (!row || typeof row !== "object" || !REF.test(row.id || "")) continue;
      if (isText(row.name)) S.names.set(row.id, plainText(row.name));
      const line = button("row", null, () => pageGo({ name: "project", pid: row.id, tab: "fleet" }));
      const top = el("span", "row-top");
      top.appendChild(el("span", "name", (row.pinned === true ? "📌 " : "") + plainText(row.name || row.id)));
      const waiting = needsFor(row.id).length;
      if (waiting) top.appendChild(el("span", "badge s-attention", "NEEDS YOU " + waiting));
      line.appendChild(top);
      const counts = row.agents && typeof row.agents === "object" ? row.agents : {};
      const words = Object.keys(counts).filter((state) => toInt(counts[state]) > 0).map((state) => toInt(counts[state]) + " " + plainText(state));
      const sub = [isText(row.group) ? plainText(row.group) : "", words.length ? words.join(" · ") : "no agents"].filter(Boolean);
      line.appendChild(el("span", "muted", sub.join(" — ")));
      list.appendChild(line);
    }
  };
  const load = async () => {
    const res = await apiCall("GET", API.projects);
    if (res.ok && Array.isArray(res.data)) {
      rows = res.data;
      draw();
    } else if (!rows) list.appendChild(el("p", "empty", failText(res)));
  };
  load();
  const timer = setInterval(load, 15000);
  return { needs: draw, cleanup: () => clearInterval(timer) };
};

function tabBar(tabs, current, go) {
  const bar = el("div", "tabs");
  bar.setAttribute("role", "tablist");
  for (const tab of tabs) {
    const control = button("tab" + (tab[0] === current ? " on" : ""), tab[1], () => go(tab[0]));
    control.setAttribute("role", "tab");
    bar.appendChild(control);
  }
  return bar;
}

const TASK_GROUPS = ["doing", "review", "blocked", "todo", "done"];

/* A project's name as the machine last said it (projects or fleet), else its id. */
function projectName(pid) {
  return S.names.get(pid) || pid;
}

function noteName(payload) {
  const pid = projectIdOf(payload);
  if (pid && isText(payload.name)) S.names.set(pid, plainText(payload.name));
}

VIEWS.project = (route, main) => {
  const pid = route.pid;
  wantProject(pid);
  const title = el("h2", "title", projectName(pid));
  main.appendChild(title);
  main.appendChild(tabBar([["fleet", "Fleet"], ["board", "Board"], ["tasks", "Tasks"], ["memory", "Memory"]], route.tab,
    (tab) => pageGo({ name: "project", pid, tab })));
  const body = el("div", "data");
  main.appendChild(body);
  const view = {};
  if (route.tab === "fleet") {
    const draw = () => {
      const fleet = projectIdOf(S.fleet) === pid ? S.fleet : null;
      title.textContent = projectName(pid);
      clear(body);
      if (!fleet) return body.appendChild(el("p", "empty", "Loading…"));
      const agents = agentsOf(fleet);
      if (!agents.length) body.appendChild(el("p", "empty", "No agents running in this project."));
      for (const row of agents) {
        const label = row.agent.label;
        if (!REF.test(label || "")) continue;
        const line = button("row", null, () => pageGo({ name: "agent", pid, label, tab: "live" }));
        const top = el("span", "row-top");
        top.append(el("span", "name", label), ...stateBadges(row.state, needsFor(pid, label).length > 0));
        line.appendChild(top);
        const sub = [plainText(row.agent.role), isText(row.detail) ? plainText(row.detail) : ""].filter(Boolean);
        line.appendChild(el("span", "muted", sub.join(" · ")));
        body.appendChild(line);
      }
      return undefined;
    };
    view.fleet = draw;
    view.needs = draw;
    draw();
    if (projectIdOf(S.fleet) !== pid) {
      apiCall("GET", API.fleet, { query: { project: pid } }).then((res) => {
        if (projectIdOf(S.fleet) === pid) return; // a frame came first, and is no older
        if (res.ok && S.wantFleet === pid && projectIdOf(res.data) === pid) {
          S.fleet = res.data;
          noteName(res.data);
          draw();
        } else if (!res.ok) {
          clear(body);
          body.appendChild(el("p", "empty", failText(res)));
        }
      });
    }
  } else if (route.tab === "board") {
    wantBoard(pid);
    view.cleanup = () => wantBoard(null);
    const compose = noteComposer(pid);
    const list = el("div", "events");
    body.append(compose, list);
    const draw = () => {
      const board = projectIdOf(S.board) === pid ? S.board : null;
      clear(list);
      if (!board) return list.appendChild(el("p", "empty", "Loading…"));
      const authors = new Map();
      for (const session of Array.isArray(board.sessions) ? board.sessions : []) {
        if (session && typeof session.id === "string") authors.set(session.id, plainText(session.label || session.role || "an agent"));
      }
      const events = (Array.isArray(board.events) ? board.events : []).filter((one) => one && one.payload && typeof one.payload === "object");
      events.sort((a, b) => toInt(b.payload.seq) - toInt(a.payload.seq));
      if (!events.length) list.appendChild(el("p", "empty", "Nothing on the board yet."));
      for (const event of events.slice(0, 200)) {
        const p = event.payload;
        const kind = typeof event.kind === "string" ? event.kind.replace(/^team\./, "") : "note";
        const card = el("div", "event");
        const by = p.session_id ? authors.get(p.session_id) || "an agent" : "you";
        const to = isText(p.to_role) ? " → " + plainText(p.to_role) : "";
        card.appendChild(el("p", "muted", plainText(kind) + " · " + by + to + " · " + ago(event.ts, Date.now())));
        card.appendChild(el("p", "text", p.text));
        list.appendChild(card);
      }
      return undefined;
    };
    view.board = draw;
    draw();
    if (projectIdOf(S.board) !== pid) {
      apiCall("GET", API.board, { query: { project: pid } }).then((res) => {
        if (res.ok && S.wantBoard === pid && projectIdOf(res.data) === pid && projectIdOf(S.board) !== pid) {
          S.board = res.data;
          draw();
        }
      });
    }
  } else {
    const tasks = route.tab === "tasks";
    body.appendChild(el("p", "empty", "Loading…"));
    apiCall("GET", tasks ? API.tasks : API.memory, { query: { project: pid } }).then((res) => {
      clear(body);
      if (!res.ok || !Array.isArray(res.data)) return body.appendChild(el("p", "empty", failText(res)));
      if (tasks) drawTasks(body, res.data);
      else drawMemory(body, res.data);
      return undefined;
    });
  }
  return view;
};

function drawTasks(body, rows) {
  const tasks = rows.filter((task) => task && typeof task === "object");
  if (!tasks.length) body.appendChild(el("p", "empty", "No tasks on this board."));
  for (const status of TASK_GROUPS) {
    const group = tasks.filter((task) => task.status === status);
    if (!group.length) continue;
    body.appendChild(el("h3", "group", status + " · " + group.length));
    for (const task of group) {
      const line = el("div", "event");
      line.appendChild(el("p", "text", task.title));
      const sub = [isText(task.role) ? "for " + plainText(task.role) : "", isText(task.claimed_by) ? "claimed" : ""].filter(Boolean);
      if (sub.length) line.appendChild(el("p", "muted", sub.join(" · ")));
      body.appendChild(line);
    }
  }
}

function drawMemory(body, rows) {
  const entries = rows.filter((entry) => entry && typeof entry === "object" && !entry.deleted_at);
  if (!entries.length) body.appendChild(el("p", "empty", "Nothing remembered for this project yet."));
  for (const entry of entries) {
    const line = el("div", "event");
    line.appendChild(el("p", "text", entry.text));
    const tags = Array.isArray(entry.tags) ? entry.tags.filter(isText).map(plainText) : [];
    line.appendChild(el("p", "muted", [plainText(entry.pool), tags.join(", "), ago(entry.updated_at, Date.now())].filter(Boolean).join(" · ")));
    body.appendChild(line);
  }
}

function noteComposer(pid) {
  const box = el("div", "composer");
  const text = el("textarea", "compose");
  text.setAttribute("aria-label", "Note");
  text.maxLength = TEXT_MAX.note;
  const kind = el("select", "field");
  kind.setAttribute("aria-label", "Kind");
  for (const name of ["note", "decision", "question", "result"]) {
    const option = el("option", null, name);
    option.value = name;
    kind.appendChild(option);
  }
  const to = el("input", "field");
  to.setAttribute("aria-label", "To (optional)");
  to.setAttribute("autocapitalize", "off");
  to.placeholder = "to (optional): a label or a role";
  to.maxLength = 200;
  const said = el("p", "status");
  const post = button("w primary", "Post", async () => {
    if (!text.value.trim()) {
      said.textContent = "Type something first.";
      return;
    }
    const body = { text: text.value, kind: kind.value, project: pid };
    if (to.value.trim()) body.to = to.value.trim();
    post.classList.add("busy");
    gateButtons();
    const res = await apiWrite("api/note", body, "Note");
    post.classList.remove("busy");
    gateButtons();
    if (res.ok) {
      text.value = "";
      said.textContent = "Posted.";
      return;
    }
    said.textContent = failText(res, TEXT_MAX.note);
    afterFailure(res);
  });
  const row = el("div", "row-inline");
  row.append(kind, to, post);
  box.append(text, row, said);
  return box;
}

// --- one agent: live pane, transcript, card, input bar and key pad (SPEC §6.3) ---

/* Draws pane frames into a pre, replacing only the rows that changed, so the
 * scroll position holds; the cursor's cell is inverted, unless the program hid
 * its cursor (Claude Code does): drawn anyway, it was a stray block. */
function paneRenderer(pre) {
  const rows = [];
  return (payload) => {
    if (isText(payload.error)) {
      clear(pre);
      rows.length = 0;
      pre.appendChild(el("span", "ln muted", payload.error));
      return;
    }
    if (rows.length === 0) clear(pre);
    const lines = Array.isArray(payload.rows) ? payload.rows : [];
    const cursor = Array.isArray(payload.cursor) ? payload.cursor : [];
    const cy = payload.cursor_visible === false ? -1 : toInt(cursor[1]);
    for (let y = 0; y < lines.length; y++) {
      const source = String(lines[y]) + (y === cy ? "\u0000" + toInt(cursor[0]) : "");
      if (rows[y] && rows[y].source === source) continue;
      let runs = ansiToRuns(String(lines[y]));
      if (y === cy) runs = markCursor(runs, cursor[0]);
      const node = renderRuns(runs, document);
      if (rows[y]) pre.replaceChild(node, rows[y].node);
      else pre.appendChild(node);
      rows[y] = { source, node };
    }
    while (rows.length > lines.length) pre.removeChild(rows.pop().node);
  };
}

/* Columns of the monospace font that fit, for transcript lines wrapped to this phone. */
function measureColumns(box) {
  const probe = el("span", "measure", "0000000000");
  box.appendChild(probe);
  const width = probe.getBoundingClientRect().width / 10;
  box.removeChild(probe);
  return clampInt(width > 0 ? Math.floor((box.clientWidth - 8) / width) : 40, 20, 200);
}

/* Enter is ⏎, as on the input bar: "Enter" ran out of its key into ↑ on a 390 px phone. */
const PAD_ROW = [["Esc", "Escape"], ["1", "1"], ["2", "2"], ["3", "3"], ["⏎", "Enter"], ["↑", "Up"], ["↓", "Down"]];
const PAD_MORE = [
  ["Tab", "Tab"], ["⇧Tab", "BTab"], ["⌫", "BSpace"], ["Space", "Space"], ["←", "Left"], ["→", "Right"],
  ["PgUp", "PageUp"], ["PgDn", "PageDown"], ["Home", "Home"], ["End", "End"], ["y", "y"], ["n", "n"],
  ["4", "4"], ["5", "5"], ["6", "6"], ["7", "7"], ["8", "8"], ["9", "9"], ["0", "0"],
  ["F1", "F1"], ["F2", "F2"], ["F3", "F3"], ["F4", "F4"], ["F5", "F5"], ["F6", "F6"], ["F7", "F7"],
  ["F8", "F8"], ["F9", "F9"], ["F10", "F10"], ["F11", "F11"], ["F12", "F12"],
  ["^L", "C-l"], ["^R", "C-r"], ["^U", "C-u"], ["^O", "C-o"], ["^C", "C-c"], ["^D", "C-d"],
];
/* What a screen reader says for a key whose label is a glyph or a short form: "⏎" was
 * read out as a symbol, or not at all, never as the Enter it sends. */
const KEY_SPOKEN = Object.freeze({
  Escape: "Escape", Enter: "Enter", Up: "Up arrow", Down: "Down arrow", Left: "Left arrow", Right: "Right arrow",
  BTab: "Shift Tab", BSpace: "Backspace", PageUp: "Page up", PageDown: "Page down",
  "C-l": "Control L", "C-r": "Control R", "C-u": "Control U", "C-o": "Control O", "C-c": "Control C", "C-d": "Control D",
});

VIEWS.agent = (route, main) => {
  const pid = route.pid;
  const label = route.label;
  wantProject(pid);
  const head = el("div", "agent-head");
  const back = button("ghost", "‹ Fleet", () => pageGo({ name: "project", pid, tab: "fleet" }));
  const name = el("h2", "title", label);
  const state = el("span", "state");
  const menu = button("w", "Actions…", () => actionsMenu());
  head.append(back, name, state, menu);
  main.appendChild(head);
  main.appendChild(tabBar([["live", "Live"], ["transcript", "Transcript"], ["card", "Card"]], route.tab,
    (tab) => pageGo({ name: "agent", pid, label, tab })));
  const body = el("div", "agent-body data");
  main.appendChild(body);
  const cleanups = [];
  const row = () => (projectIdOf(S.fleet) === pid ? findAgent(S.fleet, label) : null);
  const drawState = () => {
    clear(state);
    const current = row();
    const needed = needsFor(pid, label).length > 0;
    if (current) state.append(...stateBadges(current.state, needed));
    else if (needed) state.appendChild(el("span", "badge s-attention", "NEEDS YOU"));
  };
  const ctx = () => {
    const current = row();
    return { pid, label, agentId: current && typeof current.agent.id === "string" ? current.agent.id : null, needsId: null, item: null };
  };
  const actionsMenu = () => {
    const current = row();
    const limited = current && current.state === "limited";
    openSheet("Act on " + label, (sheet) => {
      const items = [
        ["Tell…", () => tellSheet(ctx(), "auto")], ["Interrupt & tell…", () => tellSheet(ctx(), "interrupt")],
        ["Stop…", () => actionSheet("stop", ctx())], ["Restart…", () => actionSheet("restart", ctx())],
        ["Switch account…", () => actionSheet("switch", ctx())],
      ];
      if (limited) items.unshift(items.pop());
      for (const item of items) sheet.body.appendChild(button("w row", item[0], item[1]));
    });
  };
  if (route.tab === "live") {
    const tools = el("div", "row-inline");
    const fit = checkbox("Fit width", false);
    tools.appendChild(fit.label);
    const pane = el("pre", "pane");
    const draw = paneRenderer(pane);
    let width = 80;
    const fitNow = () => {
      pane.classList.toggle("fit", fit.box.checked);
      // The second style write from server data (see the top of this file): an integer, clamped below.
      pane.style.setProperty("--cols", String(width));
    };
    fit.box.addEventListener("change", fitNow);
    body.append(tools, pane);
    let landed = false;
    cleanups.push(paneWatch(pid, label, (payload) => {
      width = clampInt(payload.width, 20, 400);
      fitNow();
      draw(payload);
      // The first screen opens at its foot, where a prompt waits; later ones keep the scroll.
      // Once the view is built: a cached frame is drawn before the input bar is added.
      if (!landed && Array.isArray(payload.rows) && payload.rows.length) {
        landed = true;
        Promise.resolve().then(() => { UI.main.scrollTop = UI.main.scrollHeight; });
      }
    }));
  } else if (route.tab === "transcript") {
    const older = button("ghost", "Load older", () => load(cursor));
    const lines = el("pre", "transcript");
    const refresh = button("ghost", "Refresh", () => load(null));
    body.append(older, lines, refresh);
    let cursor = null;
    older.hidden = true;
    const load = async (before) => {
      const query = { project: pid, width: measureColumns(lines) };
      if (before) query.before = before;
      const res = await apiCall("GET", apiPath(API.transcript, { agent: label }), { query });
      if (!res.ok || !res.data || typeof res.data !== "object") {
        toast(failText(res));
        return afterFailure(res, route);
      }
      const page = res.data;
      const stamps = page.stamps && typeof page.stamps === "object" ? page.stamps : {};
      const nodes = (Array.isArray(page.lines) ? page.lines : []).map((line, n) => renderRuns(ansiToRuns(String(line) + turnTime(stamps[n])), document));
      if (before) {
        const first = lines.firstChild;
        for (const node of nodes) lines.insertBefore(node, first);
      } else {
        clear(lines);
        for (const node of nodes) lines.appendChild(node);
        if (!nodes.length) lines.appendChild(el("span", "ln muted", "No conversation recorded yet."));
        UI.main.scrollTop = UI.main.scrollHeight;
      }
      cursor = typeof page.cursor === "string" ? page.cursor : null;
      older.hidden = !(page.more === true && cursor);
      return undefined;
    };
    load(null);
  } else {
    body.appendChild(el("p", "empty", "Loading…"));
    apiCall("GET", apiPath(API.explainability, { agent: label }), { query: { project: pid } }).then((res) => {
      clear(body);
      if (!res.ok || !res.data || typeof res.data !== "object") {
        body.appendChild(el("p", "empty", failText(res)));
        return afterFailure(res, route);
      }
      drawExplainability(body, res.data);
      return undefined;
    });
  }
  if (route.tab !== "card") main.appendChild(inputBar(pid, label, cleanups));
  drawState();
  return { fleet: drawState, needs: drawState, cleanup: () => { for (const fn of cleanups) fn(); } };
};

function drawExplainability(body, card) {
  const lines = [];
  lines.push(card.available === true ? "Explainability: on" : "Explainability: off — " + (plainText(card.reason) || "unavailable"));
  if (isText(card.model)) lines.push("model: " + plainText(card.model));
  if (typeof card.tokens_in === "number") lines.push("tokens in: " + card.tokens_in);
  if (typeof card.tokens_out === "number") lines.push("tokens out: " + card.tokens_out);
  if (typeof card.cost_estimate_usd === "number") lines.push("cost estimate: $" + card.cost_estimate_usd.toFixed(2));
  const policy = card.policy && typeof card.policy === "object" ? card.policy : null;
  if (policy) {
    for (const key of ["tracing", "shipping", "target", "gateway", "redaction"]) {
      const value = policy[key];
      if (["string", "number", "boolean"].indexOf(typeof value) >= 0) lines.push(key + ": " + plainText(value));
    }
  }
  if (isText(card.updated_at)) lines.push("updated " + ago(card.updated_at, Date.now()));
  body.appendChild(el("pre", "mono", lines.join("\n")));
}

/* The bar under the pane: a growing textarea, ⏎ on by default (text left in
 * Claude Code's input box holds back its next question), Send, and the key
 * pad, which the soft keyboard and it never share the screen with. */
function inputBar(pid, label, cleanups) {
  const bar = el("div", "inputbar");
  const line = el("div", "row-inline");
  const text = el("textarea", "say");
  text.rows = 1;
  text.setAttribute("aria-label", "Type to the agent");
  text.maxLength = TEXT_MAX.keys;
  const enter = checkbox("⏎", true);
  enter.box.setAttribute("aria-label", "Press Enter after the text");
  const send = button("w primary", "Send", () => sendText());
  const padToggle = button("ghost", "Keys", () => setPad(!pad.classList.contains("open")));
  line.append(text, enter.label, send, padToggle);
  const pad = el("div", "pad");
  const more = el("div", "pad-more");
  const keyButton = (key) => {
    const control = button("w key", key[0], () => sendKey(key[1]));
    if (Object.prototype.hasOwnProperty.call(KEY_SPOKEN, key[1])) control.setAttribute("aria-label", KEY_SPOKEN[key[1]]);
    return control;
  };
  for (const key of PAD_ROW) pad.appendChild(keyButton(key));
  pad.appendChild(button("ghost key", "More", () => more.classList.toggle("open")));
  for (const key of PAD_MORE) more.appendChild(keyButton(key));
  pad.appendChild(more);
  bar.append(line, pad);
  const setPad = (open) => {
    // The pad grows the bar over the foot of the pane, where the prompt it answers waits:
    // a view at its foot stays there. One scrolled up to read is left where it is.
    const main = UI.main;
    const atFoot = main.scrollHeight - main.scrollTop - main.clientHeight < 2;
    pad.classList.toggle("open", open);
    if (open) text.blur();
    if (open && atFoot) main.scrollTop = main.scrollHeight;
  };
  text.addEventListener("focus", () => setPad(false));
  text.addEventListener("input", () => {
    text.style.height = "auto";
    text.style.height = Math.min(text.scrollHeight, 160) + "px";
  });
  if (S.padOnOpen) {
    S.padOnOpen = false;
    setPad(true);
  }
  let lastEsc = 0;
  const post = async (body, what) => {
    const res = await apiWrite("api/send-keys", Object.assign({ agent: label, project: pid }, body), what);
    if (res.ok) return true;
    if (res.status === 409 && res.error === "double_press") {
      confirmSheet("Send it again?", "A second Ctrl-C or Ctrl-D within 3 s exits Claude Code, and the agent with it.", "Send and exit",
        () => post(Object.assign({}, body, { confirm_exit: true }), what));
      return false;
    }
    toast(failText(res, TEXT_MAX.keys));
    afterFailure(res, { pid });
    return false;
  };
  const sendKey = (key) => {
    if (key === "Escape") {
      const now = Date.now();
      if (now - lastEsc < ESC_REPEAT_MS) {
        lastEsc = 0;
        confirmSheet("Press Esc again?", "Two Esc in a row open Claude Code's Rewind selector.", "Send Esc", () => post({ keys: [key] }, "Esc"));
        return;
      }
      lastEsc = now;
    }
    if (key === "C-c" || key === "C-d") {
      const which = key === "C-c" ? "Ctrl-C" : "Ctrl-D";
      confirmSheet("Send " + which + "?", which + " interrupts " + label + "; a second one within 3 s exits Claude Code.", "Send " + which,
        () => post({ keys: [key] }, which));
      return;
    }
    post({ keys: [key] }, "Key " + key);
  };
  // Send needs words. An empty box with ⏎ on was a bare Enter into the pane, which
  // picks a dialog's highlighted option ("1. Yes"); Enter on its own is the pad's.
  const sendText = async () => {
    const value = text.value;
    if (!value) {
      toast("Type something first — Enter on its own is ⏎ on the key pad.");
      return;
    }
    if (value.length > TEXT_MAX.keys) {
      toast("Too long (max " + TEXT_MAX.keys + " characters) — use Actions › Tell for a longer message.");
      return;
    }
    const body = { text: value, enter: enter.box.checked };
    send.classList.add("busy");
    gateButtons();
    const sent = await post(body, "Send");
    send.classList.remove("busy");
    gateButtons();
    if (sent) {
      text.value = "";
      text.style.height = "auto";
    }
  };
  cleanups.push(() => setPad(false));
  return bar;
}

// --- devices and settings ---

VIEWS.devices = (route, main) => {
  main.appendChild(el("h2", "title", "Devices"));
  const list = el("div", "rows data");
  main.appendChild(list);
  const load = async () => {
    const res = await apiCall("GET", API.devices);
    clear(list);
    if (!res.ok || !Array.isArray(res.data)) return list.appendChild(el("p", "empty", failText(res)));
    for (const device of res.data) {
      if (!device || typeof device !== "object" || !DEVICE_ID.test(device.id || "")) continue;
      const own = device.current === true;
      if (own) S.me = device.id;
      const line = el("div", "event");
      const top = el("p", "row-top");
      top.appendChild(el("span", "name", own ? "This device" : clip(plainText(device.id), 16)));
      if (device.signed_in === false) top.appendChild(el("span", "badge s-exited", "signed out"));
      line.appendChild(top);
      line.appendChild(el("p", "muted", clip(plainText(device.ua) || "unknown browser", 80)));
      const when = ["last seen " + (ago(device.last_seen, Date.now()) || "never")];
      if (isText(device.expires_at)) when.push("sign-in ends " + ago(device.expires_at, Date.now()));
      line.appendChild(el("p", "muted", when.join(" · ")));
      const id = device.id;
      if (own) line.appendChild(button(null, "Sign out", () => signOut(id)));
      else {
        line.appendChild(button("w", "Revoke", async () => {
          const out = await apiCall("DELETE", apiPath(API.device, { id }));
          if (out.ok) load();
          else {
            toast(failText(out));
            afterFailure(out);
          }
        }));
        line.appendChild(el("p", "ro-note", "Revoking another device is a write: " + READ_ONLY + "."));
      }
      list.appendChild(line);
    }
    gateButtons();
    return undefined;
  };
  load();
  return {};
};

async function signOut(id) {
  if (!DEVICE_ID.test(id || "")) return;
  const res = await apiCall("DELETE", apiPath(API.device, { id }));
  if (!res.ok) {
    toast(failText(res));
    return;
  }
  S.me = null;
  toast("Signed out");
  toUnlock();
}

function isIos() {
  const nav = navigator;
  return /iPad|iPhone|iPod/.test(nav.userAgent) || (nav.platform === "MacIntel" && nav.maxTouchPoints > 1);
}

function isStandalone() {
  return navigator.standalone === true || (typeof matchMedia === "function" && matchMedia("(display-mode: standalone)").matches);
}

function pushCapable() {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window && window.isSecureContext;
}

function b64urlToBytes(value) {
  const text = typeof value === "string" && /^[A-Za-z0-9_-]+$/.test(value) ? value : "";
  const padded = text.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((text.length + 3) % 4);
  const raw = atob(padded);
  return Uint8Array.from(raw, (char) => char.charCodeAt(0));
}

async function workerRegistration() {
  if (!("serviceWorker" in navigator)) return null;
  const existing = await navigator.serviceWorker.getRegistration();
  return existing || navigator.serviceWorker.register("sw.js", { scope: "./" });
}

/* A subscription only works with the key it was made against. One made against a
 * key the machine no longer has (remote-push.json lost, and its keys made again) is
 * refused by the push service at every push, and only a new one works again. A
 * browser that does not say which key its subscription used is believed. */
function madeWithKey(sub, vapid) {
  const own = sub && sub.options && sub.options.applicationServerKey;
  if (!own) return true;
  const mine = new Uint8Array(own);
  const key = b64urlToBytes(vapid);
  return mine.length === key.length && mine.every((byte, i) => byte === key[i]);
}

/* This browser's subscription for the machine's key: the one it has, or a new one. */
async function subscribeFor(reg, vapid) {
  let sub = await reg.pushManager.getSubscription();
  if (sub && !madeWithKey(sub, vapid)) {
    await sub.unsubscribe();
    sub = null;
  }
  return sub || reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64urlToBytes(vapid) });
}

async function pushState() {
  const res = await apiCall("GET", API.push);
  S.push = res.ok && res.data && typeof res.data === "object" ? res.data : { supported: false, reason: res.status === 404 ? "this server does not send notifications" : failText(res) };
  let local = null;
  if (pushCapable()) {
    try {
      const reg = await navigator.serviceWorker.getRegistration();
      local = reg ? await reg.pushManager.getSubscription() : null;
    } catch (error) {
      local = null;
    }
  }
  if (local && S.push.supported && !madeWithKey(local, S.push.vapid_public_key)) local = null; // dead: "Turn on" mends it
  return { server: S.push, local, permission: "Notification" in window ? Notification.permission : "unsupported" };
}

/* SPEC §6.5: permission first, inside the tap; then subscribe; then tell the machine. */
async function pushEnable() {
  const asked = Notification.requestPermission();
  const permission = await asked;
  if (permission !== "granted") return "Notifications were not allowed for this page.";
  const state = await pushState();
  if (!state.server.supported) return plainText(state.server.reason) || "The machine cannot send notifications.";
  const reg = await workerRegistration();
  await navigator.serviceWorker.ready;
  const sub = await subscribeFor(reg, state.server.vapid_public_key);
  const res = await apiCall("POST", API.pushSubscribe, { body: sub.toJSON() });
  return res.ok ? "" : failText(res);
}

async function pushDisable() {
  const reg = await navigator.serviceWorker.getRegistration();
  const sub = reg ? await reg.pushManager.getSubscription() : null;
  if (sub) await sub.unsubscribe();
  await apiCall("DELETE", API.pushSubscription);
}

/* After every unlock: a subscription this browser already has goes to the
 * machine again, so a re-unlocked or new device keeps its notifications; made
 * anew first if the machine's key changed. */
async function pushResend() {
  try {
    if (!pushCapable()) return;
    const reg = await navigator.serviceWorker.getRegistration();
    if (!reg || !(await reg.pushManager.getSubscription())) return;
    const res = await apiCall("GET", API.push);
    if (!res.ok || !res.data || res.data.supported !== true) return;
    const sub = await subscribeFor(reg, res.data.vapid_public_key);
    await apiCall("POST", API.pushSubscribe, { body: sub.toJSON() });
  } catch (error) {
    // notifications stay as they were; Settings says so
  }
}

function pushBanner(box) {
  clear(box);
  let hidden = false;
  try {
    hidden = localStorage.getItem("asq.push-banner") === "off";
  } catch (error) {
    hidden = false;
  }
  if (hidden) return;
  pushState().then((state) => {
    const on = state.server.supported && state.server.subscribed === true && state.local && state.permission === "granted";
    if (on || !state.server.supported) return;
    const line = el("div", "notice-line");
    line.appendChild(el("span", null, "Notifications are not on for this device."));
    line.appendChild(button("ghost", "Settings", () => pageGo("#/settings")));
    line.appendChild(button("quiet", "Hide", () => {
      try {
        localStorage.setItem("asq.push-banner", "off");
      } catch (error) {
        // shown again next time: harmless
      }
      clear(box);
    }));
    box.appendChild(line);
  });
}

VIEWS.settings = (route, main) => {
  main.appendChild(el("h2", "title", "Settings"));
  const notes = el("section", "panel");
  notes.appendChild(el("h3", null, "Notifications"));
  const said = el("p", "muted", "Checking…");
  const controls = el("div", "row-inline");
  notes.append(said, controls);
  main.appendChild(notes);
  const draw = async () => {
    clear(controls);
    if (!pushCapable()) {
      if (isIos() && !isStandalone()) {
        said.textContent = "On iPhone and iPad, notifications need the page on the Home Screen: tap Share, then Add to Home Screen, then open aisquare from the Home Screen and unlock it there. The installed app keeps its own sign-in, so it unlocks once more and shows as a second device.";
      } else said.textContent = "This browser cannot show notifications from this page.";
      return;
    }
    const state = await pushState();
    const on = state.server.supported && state.server.subscribed === true && state.local && state.permission === "granted";
    if (!state.server.supported) said.textContent = plainText(state.server.reason) || "The machine cannot send notifications.";
    else if (state.permission === "denied") said.textContent = "Notifications are blocked for this page in the browser's settings.";
    else if (on) said.textContent = "Notifications are on for this device.";
    else said.textContent = "Notifications are not on for this device.";
    if (!state.server.supported) return;
    if (on) {
      controls.appendChild(button("a", "Send test", async () => {
        const res = await apiCall("POST", API.pushTest);
        if (res.ok) return toast("A test notification is on its way.");
        if (res.status === 404 && res.error === "not_subscribed") return toast("The machine has no subscription for this device — turn notifications on again.");
        return toast(failText(res));
      }));
      controls.appendChild(button("quiet", "Turn off", async () => {
        await pushDisable();
        draw();
      }));
    } else if (state.permission !== "denied") {
      controls.appendChild(button("primary", "Turn on", () => {
        pushEnable().then((problem) => {
          if (problem) toast(problem);
          draw();
        }, () => {
          toast("Notifications could not be turned on here.");
          draw();
        });
      }));
    }
    if (isIos() && isStandalone()) controls.appendChild(el("p", "muted", "This installed app is a device of its own: its sign-in and notifications are separate from Safari's."));
    gateButtons();
  };
  draw();
  const about = el("section", "panel");
  about.appendChild(el("h3", null, "This page"));
  about.appendChild(el("p", "muted", "aisquare " + plainText(S.remote && S.remote.version ? S.remote.version : "") + " · writes " + (writable() ? "on" : "off")));
  about.appendChild(button(null, "Sign out", async () => {
    if (!S.me) {
      const res = await apiCall("GET", API.devices);
      const own = res.ok && Array.isArray(res.data) ? res.data.find((device) => device && device.current === true) : null;
      if (own) S.me = own.id;
    }
    signOut(S.me);
  }));
  main.appendChild(about);
  return {};
};

// --- auto-off ---

async function extendAutoOff() {
  const res = await apiWrite(API.remoteExtend, {}, "Extend");
  if (res.ok && res.data && typeof res.data.auto_off_at === "string") {
    S.remote = Object.assign({}, S.remote, { auto_off_at: res.data.auto_off_at });
    drawStatus();
    toast("Remote stays on until " + clock(res.data.auto_off_at));
    return;
  }
  toast(failText(res));
  afterFailure(res);
}

// --- boot ---

function trackViewport() {
  const viewport = window.visualViewport;
  if (!viewport) return;
  const update = () => {
    const root = document.documentElement.style;
    root.setProperty("--vvh", Math.round(viewport.height) + "px");
    root.setProperty("--vvt", Math.round(viewport.offsetTop) + "px");
    document.body.classList.toggle("kb", window.innerHeight - viewport.height > 120);
  };
  viewport.addEventListener("resize", update);
  viewport.addEventListener("scroll", update);
  update();
}

function boot() {
  buildShell();
  loadPending();
  trackViewport();
  window.addEventListener("hashchange", renderRoute);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") wake(S.sockState === "replaced");
  });
  window.addEventListener("pageshow", () => wake(false));
  window.addEventListener("online", () => wake(true));
  window.addEventListener("offline", () => setOffline(true));
  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.register("sw.js", { scope: "./" }).catch(() => undefined);
    navigator.serviceWorker.addEventListener("message", (event) => {
      const data = event.data;
      if (data && data.type === "open" && typeof data.hash === "string") pageGo(data.hash);
    });
  }
  setInterval(() => {
    checkStale();
    drawStatus();
    const now = Date.now();
    for (const pair of S.since) {
      if (pair[0].isConnected) pair[0].textContent = ago(pair[1], now);
      else S.since.delete(pair);
    }
  }, 1000);
  start();
}

if (typeof document !== "undefined") boot();

if (typeof module === "object" && module && module.exports) {
  module.exports = {
    API, WRITES, SOCKET_MESSAGES, ansiToRuns, renderRuns, renderNeedsCard, markCursor, plainText,
    parseRoute, routeHash, ago,
  };
}
