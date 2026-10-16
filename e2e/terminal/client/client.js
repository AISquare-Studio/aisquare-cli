// The instrumented terminal client: xterm.js 6 on ttyd's websocket.
//
// URL parameters: ws (ttyd websocket URL), cols, rows, title.
//
// Everything the test runner needs to SEE is exposed on window, read with
// page.evaluate — never inferred from pixels:
//   window.term          the xterm.js Terminal (the screen buffer a user sees)
//   window.e2e           live counters: frames, bsu, writes, marks, exit codes, ws state
//   window.e2eScreen()   the visible rows as text + timing facts, one call
//   window.e2eRow(y)     one visible row cell by cell (char, inverse, bold, width)
//
// ttyd's protocol (src/server.h, html/src/components/terminal/xterm/index.ts):
//   client → server: first a JSON handshake {AuthToken, columns, rows} (ttyd spawns
//   the command on it), then '0'+bytes (input) and '1'+JSON (resize)
//   server → client: '0'+bytes (output), '1'+title, '2'+preferences
(() => {
  "use strict";
  const params = new URLSearchParams(location.search);
  const cols = Number(params.get("cols") || 160);
  const rows = Number(params.get("rows") || 48);
  const wsUrl = params.get("ws");
  const fallbackTitle = params.get("title") || "terminal";
  const titleEl = document.getElementById("title");
  titleEl.textContent = fallbackTitle;

  const state = {
    ready: false, ws: "connecting", frames: 0, bsu: false, sync2026Seen: false,
    writes: 0, lastWrite: 0, lastRender: 0, bell: 0, title: "",
    marks: [], prompts: 0, exitCodes: [], closedAt: null, error: null,
  };
  window.e2e = state;

  // A dark palette close to GitHub's, so ANSI-16 output (shell, installer) reads well
  // in screenshots; Textual draws in truecolor and is unaffected by it.
  const theme = {
    background: "#0d1117", foreground: "#e6edf3", cursor: "#e6edf3", cursorAccent: "#0d1117",
    selectionBackground: "#264f78",
    black: "#484f58", red: "#ff7b72", green: "#3fb950", yellow: "#d29922", blue: "#58a6ff",
    magenta: "#bc8cff", cyan: "#39c5cf", white: "#b1bac4",
    brightBlack: "#6e7681", brightRed: "#ffa198", brightGreen: "#56d364", brightYellow: "#e3b341",
    brightBlue: "#79c0ff", brightMagenta: "#d2a8ff", brightCyan: "#56d4dd", brightWhite: "#ffffff",
  };

  const term = new Terminal({
    cols, rows, fontSize: 14, lineHeight: 1.0, letterSpacing: 0,
    fontFamily: '"JetBrains Mono", "Noto Sans Mono CJK SC", "Noto Sans CJK SC", ' +
      '"PingFang SC", "Apple Color Emoji", "Noto Color Emoji", monospace',
    cursorBlink: false, allowProposedApi: true, scrollback: 2000, theme,
    // Textual sends mouse modes 1003/1006; xterm.js reports clicks to the app.
    macOptionIsMeta: true,
  });
  term.loadAddon(new Unicode11Addon.Unicode11Addon());
  term.unicode.activeVersion = "11";
  window.term = term;

  // --- instrumentation --------------------------------------------------------
  const has2026 = (params) => params.some((p) => p === 2026 || (Array.isArray(p) && p.includes(2026)));
  // Returning false keeps xterm.js's own handling (it IMPLEMENTS 2026 in v6); we only count.
  term.parser.registerCsiHandler({ prefix: "?", final: "h" }, (p) => {
    if (has2026(p)) { state.bsu = true; state.sync2026Seen = true; }
    return false;
  });
  term.parser.registerCsiHandler({ prefix: "?", final: "l" }, (p) => {
    if (has2026(p)) { state.bsu = false; state.frames += 1; }
    return false;
  });
  // OSC 133 shell-integration marks from the machine's profile: A prompt, B command, D;code end.
  term.parser.registerOscHandler(133, (data) => {
    const [kind, code] = data.split(";");
    state.marks.push(kind);
    if (kind === "A") state.prompts += 1;
    if (kind === "D") state.exitCodes.push(code === undefined || code === "" ? null : Number(code));
    return true;
  });
  term.onWriteParsed(() => { state.writes += 1; state.lastWrite = performance.now(); });
  term.onRender(() => { state.lastRender = performance.now(); });
  term.onBell(() => { state.bell += 1; });
  term.onTitleChange((t) => { state.title = t; titleEl.textContent = t || fallbackTitle; });

  window.e2eScreen = () => {
    const buf = term.buffer.active;
    const lines = [];
    for (let y = 0; y < term.rows; y += 1) {
      const line = buf.getLine(buf.viewportY + y);
      lines.push(line ? line.translateToString(true) : "");
    }
    const now = performance.now();
    return {
      cols: term.cols, rows: term.rows, lines, cursorX: buf.cursorX, cursorY: buf.cursorY,
      alt: buf.type === "alternate", frames: state.frames, bsu: state.bsu,
      sync2026Seen: state.sync2026Seen, writes: state.writes,
      sinceWrite: state.lastWrite ? now - state.lastWrite : 1e9,
      sinceRender: state.lastRender ? now - state.lastRender : 1e9,
      ws: state.ws, prompts: state.prompts, exitCodes: state.exitCodes.slice(),
      title: state.title, bell: state.bell, error: state.error,
    };
  };
  window.e2eRow = (y) => {
    const buf = term.buffer.active;
    const line = buf.getLine(buf.viewportY + y);
    const out = [];
    if (!line) return out;
    const cell = buf.getNullCell();
    for (let x = 0; x < term.cols; x += 1) {
      line.getCell(x, cell);
      out.push({ ch: cell.getChars() || " ", inv: cell.isInverse() ? 1 : 0,
        bold: cell.isBold() ? 1 : 0, w: cell.getWidth() });
    }
    return out;
  };

  // --- the PTY ----------------------------------------------------------------
  const enc = new TextEncoder();
  const send = (prefix, bytes) => {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    const out = new Uint8Array(bytes.length + 1);
    out[0] = prefix.charCodeAt(0);
    out.set(bytes, 1);
    socket.send(out);
  };
  let socket = null;

  const start = async () => {
    // Measure glyphs with the vendored font, not a fallback: open only once it loaded.
    try {
      await Promise.all([
        document.fonts.load('14px "JetBrains Mono"'),
        document.fonts.load('bold 14px "JetBrains Mono"'),
      ]);
    } catch (e) { state.error = "font load: " + e; }
    term.open(document.getElementById("terminal"));
    if (!wsUrl) { state.error = "no ws= parameter"; return; }
    socket = new WebSocket(wsUrl, ["tty"]);
    socket.binaryType = "arraybuffer";
    socket.onopen = () => {
      state.ws = "open";
      socket.send(enc.encode(JSON.stringify({ AuthToken: "", columns: term.cols, rows: term.rows })));
      state.ready = true;
      term.focus();
    };
    socket.onmessage = (event) => {
      const data = new Uint8Array(event.data);
      const cmd = String.fromCharCode(data[0]);
      if (cmd === "0") term.write(data.subarray(1));
      else if (cmd === "1") { /* window title from ttyd; the shell's OSC title wins */ }
    };
    socket.onclose = () => { state.ws = "closed"; state.closedAt = performance.now(); };
    socket.onerror = () => { state.error = "websocket error"; };
    term.onData((d) => send("0", enc.encode(d)));
    term.onBinary((d) => send("0", Uint8Array.from(d, (c) => c.charCodeAt(0) & 0xff)));
    term.onResize(({ cols: c, rows: r }) => send("1", enc.encode(JSON.stringify({ columns: c, rows: r }))));
  };
  start();
})();
