/* The phone page's pure core, fed hostile input over a recording fake document
 * (SPEC §6.7 item 7), and the service worker's safeUrl and notice. Test-only; not packaged.
 *
 * It asserts nothing itself. It prints ONE JSON report on stdout and
 * tests/test_remote_page.py asserts on it, so a failure names the element, the
 * property or the text that got through. The fake document records every
 * element made, every property and attribute written, every style set, every
 * listener added and every piece of text, whatever the page does with them.
 *
 * usage: node tests/js/remote_page_check.js
 */
"use strict";

const path = require("path");

const WEB = path.join(__dirname, "..", "..", "src", "aisquare", "web", "remote");
const page = require(path.join(WEB, "app.js"));
const worker = require(path.join(WEB, "sw.js"));

/* A document that keeps a log of everything done through it. */
function recorder() {
  const log = { tags: [], props: [], attrs: [], styles: [], listeners: [], text: [] };
  function element(tag) {
    log.tags.push(String(tag).toLowerCase());
    const style = new Proxy({}, {
      set(target, key, value) {
        log.styles.push([String(key), String(value)]);
        target[key] = value;
        return true;
      },
    });
    const node = {
      nodeType: 1,
      tagName: String(tag).toUpperCase(),
      children: [],
      style,
      appendChild(child) {
        this.children.push(child);
        return child;
      },
      setAttribute(name, value) {
        log.attrs.push([String(name), String(value)]);
      },
      addEventListener(type) {
        log.listeners.push(String(type));
      },
      get firstChild() {
        return this.children[0] || null;
      },
    };
    return new Proxy(node, {
      set(target, key, value) {
        log.props.push(String(key));
        if (key === "textContent") log.text.push(String(value));
        target[key] = value;
        return true;
      },
    });
  }
  const doc = {
    createElement: element,
    createTextNode(text) {
      log.text.push(String(text));
      return { nodeType: 3, textContent: String(text) };
    },
  };
  return { doc, log };
}

const MARK = "OSCPAYLOAD";
const EVIL = "\x1b]8;;javascript:alert(1)" + MARK + "\x07click\x1b]8;;\x07 <img src=x onerror=alert(1)> " +
  "javascript:alert(1) ‮evil‬ \x1b]0;" + MARK + "-title\x07";

const ROWS = [
  "\x1b]8;;javascript:alert(1)" + MARK + "\x07click me\x1b]8;;\x07 after",
  "\x1b]8;;https://evil.example/" + MARK + "\x1b\\a link\x1b]8;;\x1b\\ tail",
  "before \x1b]0;" + MARK + " a title\x07 after",
  "unterminated \x1b]8;;" + MARK + " and the rest of the row",
  "\x1bP" + MARK + "-dcs\x1b\\ dcs \x1b_" + MARK + "-apc\x1b\\ apc \x1b^" + MARK + "-pm\x1b\\ pm \x1bX" + MARK + "-sos\x1b\\ sos",
  "\u009d8;;" + MARK + "-c1\u009c c1 osc \u009b31mc1 csi",
  "javascript:alert(1)",
  "<img src=x onerror=alert(1)>",
  "\x1b[38;2;999;-1;300mclamped\x1b[0m",
  "\x1b[48;2;-5;256;1000mbackground\x1b[49m",
  "\x1b[38;5;999mbig\x1b[48;5;-3mnegative\x1b[0m",
  "\x1b[38:2::255:128:0mcolon form\x1b[39m",
  "\x1b[1;2;3;4;7;31;42mstyled\x1b[22;23;24;27;39;49m plain",
  "‮evil‬ bidi ⁦x⁩ ‎‏",
  "\x00\x01\x07\x08\x0b\x7f controls",
  "\x1b[>4;2mprivate marker \x1b[?25l\x1b[2J\x1b[Hcursor moves",
  "a lone escape \x1b",
];

const report = { runs: [], main: null, control: null, clamped: null, cursor: null, routes: {}, hashes: {}, safeUrl: {}, notices: {}, exports: [] };

const main = recorder();
for (const row of ROWS) {
  const runs = page.ansiToRuns(row);
  report.runs.push(runs);
  page.renderRuns(runs, main.doc);
}

// renderRuns trusts nothing its caller built either
page.renderRuns([
  { text: "\x1b]8;;" + MARK + "\x07direct", classes: ["f1", "onclick", "x\" onerror=y", "cur"], color: "rgb(999, 0, 0)", background: "url(javascript:alert(1))" },
  { text: "<script>alert(1)</script>", classes: "b", color: "red", background: "rgb(1,2,3); background: url(x)" },
  null, 5, { text: 7 }, { text: { toString: () => MARK } },
], main.doc);

const ITEM = {
  id: "ny_0123456789abcdef",
  project: { id: "prj_x", name: "project " + EVIL },
  agent: "coder-1 " + EVIL,
  agent_id: "agt_1",
  reason: "reason " + EVIL,
  excerpt: "excerpt " + EVIL,
  since: "2026-10-07T10:12:03+00:00",
  answers: [{ label: "1 " + EVIL, keys: ["1"] }, { label: { toString: () => MARK }, keys: ["2"] }, { label: "No", keys: ["Escape"] }, null, "x"],
  actions: ["answer", "open", "dismiss", "tell", "reply", "switch", "restart", "stop", "javascript:alert(1)", "onclick", "<img>"],
};
const DETAILS = [
  ["permission", { tool: "Bash " + EVIL, input: { command: "rm -rf / " + EVIL, nested: { x: EVIL }, n: 3, ok: true } }],
  ["permission", { text: "dialog " + EVIL }],
  ["question", { questions: [{ header: "<b>" + EVIL, question: "which? " + EVIL, multiSelect: true, options: [{ label: "javascript:alert(1)", description: EVIL }, "bad", null] }, null] }],
  ["plan", { plan: Array.from({ length: 30 }, (unused, n) => "step " + n + " " + EVIL).join("\n") }],
  ["asked", { text: "asked " + EVIL }],
  ["board_question", { text: "board " + EVIL, author: "coder-2 " + EVIL, seq: 4 }],
  ["crashed", { exit_status: 1, task_id: "tsk_" + EVIL }],
  ["limited", { text: "limited " + EVIL }],
  ["lost", {}],
  ["<script>alert(1)</script>", { text: "an unknown kind " + EVIL }],
];
const now = Date.parse("2026-10-07T10:20:00+00:00");
for (const [kind, detail] of DETAILS) {
  page.renderNeedsCard(Object.assign({}, ITEM, { kind, detail }), main.doc, { now, writable: true });
  page.renderNeedsCard(Object.assign({}, ITEM, { kind, detail }), main.doc, { now, writable: false, stale: true });
}
page.renderNeedsCard(null, main.doc);
page.renderNeedsCard("not an item", main.doc);
page.renderNeedsCard({ kind: "question", detail: [1, 2], answers: "x", actions: "y", project: "p" }, main.doc, {});
report.main = main.log;

// The control: the same recorder sees exactly what the assertions forbid.
const control = recorder();
const probe = control.doc.createElement("a");
probe.href = "x";
probe.setAttribute("onclick", "y");
probe.style.color = "red";
report.control = control.log;

report.clamped = page.ansiToRuns("\x1b[38;2;999;-1;300mX");
report.cursor = page.markCursor(page.ansiToRuns("ab\x1b[31mcd"), 2);

// The excerpts a card shows over a detail text, cut from it as the server cuts them.
const cards = recorder();
const QUESTION = "Should I cut the 0.8.0 release branch now, or wait until the remote-control PR lands?";
const LONG = Array.from({ length: 12 }, (unused, n) => "Paragraph " + n + " of what was done, and why it took this long.").join("\n\n");
const excerptsOf = (kind, excerpt, detail) => {
  const card = page.renderNeedsCard(Object.assign({}, ITEM, { kind, excerpt, detail }), cards.doc, { now });
  return card.children.filter((node) => node.className === "excerpt").map((node) => node.textContent);
};
const excerptsOver = (kind, excerpt, text) => excerptsOf(kind, excerpt, { text });
report.excerpts = {
  whole: excerptsOver("interrupted", "I'll run the cache tests first.", "I'll run the cache tests first."),
  head: excerptsOver("board_question", LONG.replace(/\s+/g, " ").slice(0, 279).trimEnd() + "…", LONG),
  shortTail: excerptsOver("asked", QUESTION, "The board is quiet: the cache work is merged.\n\n" + QUESTION),
  longTail: excerptsOver("asked", QUESTION, LONG + "\n\n" + QUESTION),
  elsewhere: excerptsOver("asked", "Which store?", "The board is quiet."),
};
// The kinds whose excerpt the server builds from what their detail leads with, as it builds them.
const STORE = {
  header: "Cache", question: "Which store should the cache use?", multiSelect: false,
  options: [{ label: "Redis", description: "shared" }, { label: "SQLite", description: "a local file" }, { label: "none", description: "" }],
};
const BASH = { tool: "Bash", input: { command: "pytest -q tests/test_cache.py", description: "Run the cache tests" } };
const HEREDOC = "cat > schema.sql <<'EOF'\n" + "CREATE TABLE t (id INTEGER);\n".repeat(3) + "EOF";
const LONG_COMMAND = "pytest -q " + Array.from({ length: 12 }, (unused, n) => "tests/test_part_" + n + ".py").join(" ");
const EDIT = { tool: "Edit", input: { file_path: "/home/me/app/cache.py", old_string: "redis", new_string: "sqlite" } };
report.builtExcerpts = {
  question: excerptsOf("question", "Which store should the cache use? — Redis · SQLite · none", { questions: [STORE] }),
  questions: excerptsOf("question", "Which store should the cache use? — Redis · SQLite · none (+1 more)", { questions: [STORE, STORE] }),
  cutQuestion: excerptsOf("question", "Which store should the c…", { questions: [STORE] }),
  permission: excerptsOf("permission", "Bash(pytest -q tests/test_cache.py)", BASH),
  heredoc: excerptsOf("permission", "Bash(cat > schema.sql <<'EOF')", { tool: "Bash", input: { command: HEREDOC } }),
  longCommand: excerptsOf("permission", "Bash(" + LONG_COMMAND.slice(0, 72) + ")", { tool: "Bash", input: { command: LONG_COMMAND } }),
  path: excerptsOf("permission", "Edit(/home/me/app/cache.py)", EDIT),
  plan: excerptsOf("plan", "Cache plan", { plan: "## Cache plan\n\n1. Add Redis behind a flag\n2. Fall back to SQLite" }),
  // The controls: an excerpt the detail does not lead with, and one with no detail to repeat.
  otherQuestion: excerptsOf("question", "Pick one before the release", { questions: [STORE] }),
  otherCommand: excerptsOf("permission", "Bash(rm -rf build)", BASH),
  bareTool: excerptsOf("permission", "Bash(pytest -q tests/test_cache.py)", { input: BASH.input }),
  dialog: excerptsOf("permission", "Allow access to the keychain?", { text: "Allow access to the keychain?" }),
  // An input over 16 KiB reaches the card as {}, its excerpt still built from the whole call.
  droppedWrite: excerptsOf("permission", "Write(/home/me/app/src/big_module.py)", { tool: "Write", input: {} }),
  droppedHeredoc: excerptsOf("permission", "Bash(cat > schema.sql <<'EOF')", { tool: "Bash", input: {} }),
};

for (const hash of [
  "", "#/", "#/unlock", "#/projects", "#/n/ny_0123456789abcdef", "#/n/ny_0123456789abcdef/p/prj_x/a/coder-1",
  "#/p/prj_x/a/coder-1/transcript", "#/p/prj_x/board", "#/n/javascript:alert(1)", "#/p/../fleet",
  "#/p/prj_x/a/%3Cimg%3E/live", "#/n/ny_0123456789ABCDEF", "#/p/prj_x/a/coder-1/evil", "#/%E0%A4%A", "#/settings/extra",
]) report.routes[hash] = page.parseRoute(hash);
for (const [name, route] of Object.entries({
  card: { name: "card", id: "ny_0123456789abcdef", pid: "prj_x", label: "coder-1" },
  badCard: { name: "card", id: "javascript:alert(1)" },
  agent: { name: "agent", pid: "prj_x", label: "coder-1", tab: "card" },
  badAgent: { name: "agent", pid: "prj_x", label: "<img>", tab: "live" },
  badTab: { name: "project", pid: "prj_x", tab: "javascript:" },
  nothing: null,
})) report.hashes[name] = page.routeHash(route);

const OWN = "https://own.example";
for (const url of [
  OWN + "/r/t/#/n/ny_0123456789abcdef", "https://x.ngrok-free.app/r/t/", "https://evil.example/r/t/",
  "http://x.ngrok-free.app/r/t/", "https://x.ngrok-free.app.evil.com/r/", "javascript:alert(1)",
  "https://u:p@x.ngrok.app/r/t/", "https://x.ngrok.io/no-token-path", "not a url",
]) report.safeUrl[url] = worker.safeUrl(url, OWN);
report.suffixes = worker.NGROK_SUFFIXES;
report.notices = {
  needs: worker.pushNotice({ title: "api: coder-1 needs you", body: "coder-1 asks you a question", tag: "asq-needs", url: null }),
  bare: worker.pushNotice({}),
};
report.exports = Object.keys(page).sort();
// What the script really holds, for the Python side to check its own parse of the tables against.
report.api = page.API;
report.writes = page.WRITES;
report.socketMessages = page.SOCKET_MESSAGES;

process.stdout.write(JSON.stringify(report) + "\n");
