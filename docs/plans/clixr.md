> **Status: 2026-09-13 — M1–M7 done on the desktop; M8 skipped by decision; M9 pending on the headset.**
>
> | | |
> |---|---|
> | **M1–M7** | Done, and verified on the desktop. Every §16 line that a desktop can settle is settled with a pasted command and its output on the integration PR — the install from a clean checkout, `aisquare xr`, the `doctor` row, `make check` under both extras, the wheel's client assets, three live panels in three role colours, a session stopping and returning without a reload, and the §15 step 7 alert fired on demand and cleared. |
> | **M8** | **Skipped by decision**, not by running out of hours. §12 names groups as the one stretch item worth taking; it was not taken, and nothing in §16 depends on it. |
> | **M9** | The runbook is written — `docs/xr-demo.md` — and rehearsed twice on the desktop. **The rehearsal on the headset has not happened.** Five §16 lines can only be settled there and are marked *verify on headset* in that document's definition-of-done table: the ring compositing in passthrough, the focus tier being readable without leaning in, push-to-talk's reply visible in the headset, collapse/summon after relocating, and whether the alert chime is *audibly located* from outside the field of view — the trigger, the bar and the positioned chime are settled on the desktop, but only a headset settles whether you can hear where it is. Nobody has claimed them. |
>
> Copied verbatim from the planning document below this header; the sections are cited by number from the code and the tests, so edit the plan and re-copy rather than editing this file in place.
>
> **Amended in place, 2026-09-14, in two places the implementation overtook** — §10's pipeline (the SERVER routes the final transcript as the prompt; the client sends no `prompt` for voice) and §6's `summary` example (a task event leads with a board-status verb). Both are marked below. A client written from the original §10 would send the final `stt` back as a `prompt` and every spoken command would reach the agent twice.

# cliXR — 24-hour implementation plan

A spatial client for the aisquare agent board. Ships inside `aisquare-cli` as the
`[xr]` extra and a new `aisquare xr` command. Target device is Quest 3 in
passthrough. Target state at hour 24 is a working localhost demo, not a release.

---

## 1. Brief

Today the board is a TUI in a terminal: one pane, one focus, sessions competing
for the same rectangle. `aisquare xr` renders the same board as panels arranged in
a ring around the operator, in passthrough, so ten agent sessions can be held in
peripheral awareness instead of in tabs. The operator turns their head to survey,
pulls one panel into focus to read it, and talks to it.

The server half already exists. Sessions, roles, tasks, notes, events and the
`working / waiting / needs-you` state model are in SQLite and exposed through the
service layer. cliXR is a new transport and a new renderer over state we already
compute. **No new agent behaviour, no changes to hooks, no changes to the MCP
surface.**

### Done means

Running `aisquare xr` in a repo with three live Claude Code sessions, then opening
the printed URL in the Quest browser, produces: a ring of three panels in
passthrough showing live per-session state; turning to look at one and pressing A
pulls it forward with a readable transcript; holding the left trigger and speaking
sends a prompt to that session; the panel's reply is readable within the normal
round-trip time. Collapse and re-summon works. The alert state visibly and audibly
pulls attention.

Anything beyond that is bonus.

---

## 2. Non-negotiables

These were decided before this plan and are not open for re-litigation during
implementation.

1. **Same repo.** `src/aisquare/` alongside everything else. Shipped as the `xr`
   extra, mirroring the existing `tui` and `serve` extras.
2. **No bundler, no build step.** Plain ES modules. three.js from a CDN pinned to
   an exact version. The client is static files shipped as package data and served
   by the Python process. `make check` must not gain a JS step.
3. **New command, not a flag.** `aisquare xr`. It starts a server, so it is a verb.
4. **Separate server from `serve`.** `serve` is MCP over streamable HTTP for agent
   clients. `xr` is static assets plus a websocket for a human client. Different
   shape, same service layer beneath. Reuse `serve`'s token generation so there is
   one auth mechanism, not two.
5. **Port 8748.** `serve` owns 8747.
6. **Fail-open, like everything else in this codebase.** If the XR server dies, no
   agent session is affected. If a websocket drops, the client reconnects and
   re-snapshots. Never block a Claude session on XR state.
7. **`immersive-ar`, not `immersive-vr`.** Passthrough is a requirement, not a
   preference.

---

## 3. Scope

### Ship (must be working at hour 24)

- `aisquare xr` command: static server, websocket, token auth, URL printout
- Live board state pushed to the client; reconnect and resnapshot on drop
- Ring of panels, one per session, positioned by angle
- Two-tier rendering: ambient tier (glanceable) and focus tier (readable)
- Controller ray pointer, select, focus toggle
- Ring rotation by thumbstick; recenter
- Collapse to a point and re-summon at current pose
- Jump-to-next-alert
- Push-to-talk voice input routed to the focused session
- Alert state with spatialized audio cue

### Stretch (only if the above is solid)

- Groups: visual containers with their own colour, title, icon, intra-group switcher
- List view that spins the ring to the selected panel
- Keyboard bridge from the host machine
- Text-to-speech readout of the focused panel
- Hand tracking as an alternative to controllers

### Explicitly out of scope for these 24 hours

- Live browser panels. There is no way to render a live web page inside an
  immersive WebXR session. If a browser panel appears at all it is a **static
  image** captured host-side and pushed as a texture. Do not attempt WebRTC
  screencast.
- Apple Vision Pro support. Feature-detect and degrade; do not design for it yet.
- Multi-user. One operator, one headset.
- Persistence of panel layout across sessions.
- Any change to `serve`, the MCP tool surface, the hooks, or the task lifecycle.

---

## 4. Repo integration

```
src/aisquare/
├── cli/
│   └── xr.py                 # Typer command: `aisquare xr`
├── services/
│   └── xr/
│       ├── __init__.py
│       ├── server.py         # aiohttp (or starlette) app: static + websocket
│       ├── protocol.py       # Pydantic v2 models for every wire message
│       ├── projector.py      # board state -> wire models; delta computation
│       └── speech.py         # audio intake -> whisper -> text
└── web/
    └── xr/                   # static, shipped as package data
        ├── index.html
        ├── main.js           # session lifecycle, render loop
        ├── net.js            # websocket, reconnect, message dispatch
        ├── ring.js           # ring layout, rotation, collapse/summon
        ├── panel.js          # panel construction, ambient + focus tiers
        ├── input.js          # controller mapping, ray pointer, gaze fallback
        ├── voice.js          # getUserMedia, push-to-talk, chunk upload
        └── style.js          # design tokens (see §8), single source of truth
```

Packaging: add `xr` to `[project.optional-dependencies]`. Add `web/xr/**` to
package data in `pyproject.toml`. Verify with `pip install -e .` then
`python -c "import importlib.resources"` resolution, not by assuming.

`aisquare doctor` gains one XR check: extra installed, port free, whisper model
present. Follow the existing doctor idiom — report and suggest a fix, never fail
hard.

---

## 5. Architecture

```
Claude Code sessions ──hooks──> SQLite (existing)
                                   │
                         services/xr/projector.py
                                   │  poll 500ms, diff, emit deltas
                                   ▼
                          services/xr/server.py
                         ┌─────────┴─────────┐
                    static files        websocket :8748
                         │                   │
                         └──── Quest browser ┘
                                   │
                            three.js + WebXR
```

The projector polls the existing services layer rather than subscribing to
anything new. Polling at 500ms is fine at this scale and avoids touching the hook
path. Diff against the last emitted snapshot per client; send deltas.

**Critical performance and UX rule:** the ambient tier never renders transcripts.
The server computes a short `summary` string (target ≤ 6 words) per session from
its most recent event. Full transcript text is streamed **only** for the session
the client has explicitly subscribed to. This is the difference between a
glanceable ring and a wall of text in 3D, and it also keeps the frame budget
survivable.

---

## 6. Wire protocol

Define every message as a Pydantic v2 model in `protocol.py`. Emit a JSON schema
dump to `web/xr/protocol.schema.json` at build time so the client and server
cannot drift silently. Version the protocol from day one.

### Server → client

```jsonc
{ "t": "hello", "protocol": 1, "hub": "<hub id>", "serverTime": "<iso8601>" }

{ "t": "snapshot",
  "sessions": [ Session ],
  "tasks":    [ Task ],
  "groups":   [ Group ]        // empty array until groups ship
}

{ "t": "delta", "changed": [ Session ], "removed": [ "<session id>" ] }

{ "t": "transcript", "session": "<id>", "seq": 41, "text": "…", "final": true }

{ "t": "stt", "text": "…", "final": false }     // interim + final ASR results

{ "t": "error", "code": "…", "message": "…" }
```

```jsonc
// Session
{ "id":        "ses_01k…",
  "role":      "planner" | "coder" | "runner" | "remote",
  "title":     "auth refactor",       // short, human, from task or repo
  "state":     "working" | "waiting" | "needs_you" | "gone",
  "summary":   "doing: wiring JWT into the refresh", // <= 6 words, server-computed: a board-status verb for a task event, then the event text
  "taskId":    "tsk_01k…" | null,
  "colorKey":  "planner" | "coder" | "runner",     // client maps to hex
  "lastActivityAt": "<iso8601>",
  "unread":    3                       // events since client last focused it
}
```

### Client → server

```jsonc
{ "t": "auth",      "token": "…" }              // first frame, always
{ "t": "subscribe", "session": "<id>" | null }  // null = ambient only
{ "t": "prompt",    "session": "<id>", "text": "…" }
{ "t": "audio",     "session": "<id>", "seq": 0 }   // binary frames follow
{ "t": "audioEnd",  "session": "<id>" }
```

Audio rides as binary websocket frames between the `audio` header and `audioEnd`.
Do not base64 it into JSON.

---

## 7. Client structure

Render loop must stay under budget. Rules:

- One `XRQuadLayer` for the **focus panel only**, created with
  `XRLayerQuality: "text-optimized"`. This is what makes text readable; it avoids
  the compositor's extra resampling pass. Feature-detect `layers` in
  `requestSession` and fall back to an in-scene textured plane if absent.
- The ambient ring renders into **one shared canvas texture atlas**, not one
  texture per panel. Redraw the atlas only when a delta arrives, never per frame.
- Cap live-updating panels at 12. Beyond that, paginate the ring.
- Target 72Hz. Measure with `XRSession` frame timing and log dropped frames to the
  console during development.

Session request:

```js
navigator.xr.requestSession("immersive-ar", {
  requiredFeatures: ["local-floor"],
  optionalFeatures: ["layers", "hand-tracking", "anchors"]
})
```

Anchor the ring to a `local-floor` reference space at session start, but on
**collapse and re-summon, re-anchor to the current viewer pose** rather than the
original floor anchor. Floor anchors drift when the guardian shifts; re-anchoring
on summon is what makes "walk somewhere else and bring it back" work.

Ring geometry: radius 1.6m, panel size 0.55m × 0.40m, panels distributed over a
200° arc in front of the operator rather than a full 360° circle. Full circles
force neck rotation past comfort and hide a third of the board behind you. Fill
the arc; scroll the surplus.

---

## 8. Visual spec

The subject is a terminal board suspended in a real room. The design should read
as instrumentation, not as a website floating in space. Monospace is justified here
because the content genuinely is terminal output, not as a decorative data-label
tic.

**Colour tokens** (single source of truth in `style.js`):

| Token | Hex | Use |
|---|---|---|
| `surface` | `#12161C` @ 0.86α | Panel body. Cool slate, not tinted black — reads as a distinct object over warm room light. |
| `rule` | `#2A323D` | Panel edge, dividers |
| `ink` | `#E8EDF2` | Primary text |
| `inkDim` | `#8B97A6` | Secondary text, timestamps |
| `planner` | `#F2B33D` | Role bar |
| `coder` | `#4FC3D9` | Role bar |
| `runner` | `#A78BDB` | Role bar |
| `alert` | `#FF5A4E` | **Reserved for `needs_you` only.** Used nowhere else, ever. |

Passthrough washes out light surfaces and low contrast. Panels must be dark and
opaque enough to hold their own against a lit room. Test against a bright window.

**State is carried by a left edge bar**, full panel height, in the role colour —
not by tinting the panel body. Body tints are unreliable over variable passthrough
backgrounds. The `needs_you` state swaps that bar to `alert` and is the only
element in the scene permitted to animate.

**Type.** One monospace family throughout, loaded from CDN with a system-mono
fallback. Size in angular terms, not pixels: ambient tier cap height ≥ 1.5° at
1.6m; focus tier ≥ 2.2°. Compute the texture resolution backwards from that.
Ambient panels get at most three lines: title, state, summary.

**Motion.** Exactly one animated thing: the alert bar. Everything else is static
or responds directly to an operator action (focus pull-forward, collapse, ring
rotation). No ambient drift, no breathing, no idle pulses.

**Audio.** The alert cue is positioned in 3D at the panel's location so the
operator turns toward it. This is the one thing this medium does that a monitor
cannot — spend effort here.

---

## 9. Input map

Design the input layer so controllers and hands go through one abstraction
(`input.js` emits semantic events: `select`, `focusToggle`, `nextAlert`,
`rotate`, `recenter`, `collapse`, `talkStart`, `talkEnd`). Controllers first;
hands wired to the same events if stretch time allows.

| Input | Action |
|---|---|
| Right thumbstick X / Y | Rotate ring / push–pull radius |
| Left thumbstick Y / X | Scroll focused panel / cycle within group |
| Right trigger | Select (raycast pointer) |
| Right grip | Grab panel — drag to reposition |
| Left trigger | Push-to-talk (hold) |
| A | Toggle focus on targeted panel |
| B | Jump to next `needs_you` |
| X | Mute / unmute readout |
| Y | Recenter ring to current pose |
| Left grip (hold 0.5s) | Collapse / summon ring |

Left menu button is system-reserved. Do not bind it.

---

## 10. Voice pipeline

Do not use the Web Speech API. The Quest browser's support is unreliable and where
Chromium does implement it, recognition is server-side, which defeats the point.

```
left trigger down
  → {"t":"audio"} header, then
  → getUserMedia({audio:{channelCount:1, sampleRate:16000, echoCancellation:true}})
  → AudioWorklet, 16kHz mono PCM, 20ms frames (a whole number of samples each)
  → binary ws frames to host
  → faster-whisper on host, VAD-gated
  → {"t":"stt"} interim results back for on-panel feedback
left trigger up
  → {"t":"audioEnd"}
  → SERVER: final transcript → {"t":"stt","final":true}, routed to the focused
    session as a prompt by the server (fleet.tell or a board note), then
    {"t":"ack"} — the client sends no {"t":"prompt"} for voice
```

*(Amended 2026-09-14: as drafted, the last line had the CLIENT send the final
transcript back as a `prompt`. The server routes it — releasing the trigger is
the commit, and a round trip through the headset is the latency this path
exists to remove — so a client that also sends a `prompt` delivers every spoken
command twice. The error codes a burst can be answered with, each at most once,
are listed on the `error` message in `services/xr/protocol.py`.)*

Use a **small** model here (`base.en` or `small.en`). This path is command input,
where latency dominates accuracy. Long-form transcription is a different job for a
different model and is not in this plan.

Show interim ASR text on the focused panel as it arrives. Without that feedback
the operator cannot tell whether the mic is live, and will repeat themselves.

Keep the typing path: repo names, branch names and identifiers will be mangled by
ASR. The keyboard bridge (stretch) or an on-panel correction field covers this.

---

## 11. Milestones

Hour budget assumes a single implementer. Each milestone has a verification step;
**do not proceed past a milestone whose verification fails.**

### M1 — Server skeleton (0–2h)
`aisquare xr` starts, serves static files, accepts an authenticated websocket,
emits `hello` and a `snapshot` built from real board state. Prints the URL and the
`adb reverse` command on start, following the `serve --show-token` idiom.

*Verify:* open the URL in a desktop browser, see `snapshot` JSON in the console
reflecting the actual sessions on the board.

### M2 — Desktop 3D mock (2–4h)
three.js scene, no XR. Ring of panels driven by live websocket data, orbit camera.
Deltas mutate the scene.

*Verify:* start and stop a Claude session; a panel appears and disappears in the
desktop browser without a reload. **This milestone is the one that de-risks
everything** — every layout and data problem is cheaper to solve here than in the
headset.

### M3 — Into the headset (4–6h)
`immersive-ar` session, passthrough, `adb reverse tcp:8748 tcp:8748`, controller
ray pointer, select.

*Verify:* ring visible in passthrough over a real desk, pointer highlights panels.

### M4 — Two-tier rendering (6–10h)
Ambient atlas for the ring; quad layer with `text-optimized` for the focus panel.
Focus pull-forward, transcript subscribe, scroll.

*Verify:* focused panel transcript is comfortably readable without leaning in.
If it is not, this is the make-or-break — stop and fix before continuing.

### M5 — Interactions (10–13h)
Rotate, recenter, collapse and summon with pose re-anchoring, jump-to-next-alert,
spatialized alert audio.

*Verify:* collapse, walk to another room, summon — ring appears correctly framed
at the new position and height.

### M6 — Voice (13–17h)
Full push-to-talk path end to end.

*Verify:* speak a prompt at a planner panel; the corresponding Claude session
receives it and replies on the panel.

### M7 — Polish (17–20h)
Colour tokens applied, role bars, state chips, unread counts, reconnect handling,
error states, `doctor` check.

*Verify:* kill the server mid-session; client shows a clear state and recovers on
restart without a headset reload.

### M8 — Stretch or stop (20–22h)
Take at most one stretch item. Groups is the highest-value one.

### M9 — Runbook and rehearsal (22–24h)
Write `docs/xr-demo.md`. Rehearse the demo end to end twice. Fix only what breaks
during rehearsal.

---

## 12. Cut order

Decided in advance so it is not decided at hour 19 under pressure. Cut from the
bottom up:

1. Hand tracking
2. Text-to-speech readout
3. Keyboard bridge
4. List view
5. Groups
6. Panel drag-to-reposition
7. Unread counts

Never cut: passthrough, focus-tier readability, push-to-talk, collapse/summon,
alert handling. Those five are the demo.

---

## 13. Known traps

**Secure context.** `http://192.168.x.x:8748` is not a secure context, so
`navigator.xr` will be `undefined` and the session request will fail with a
confusing error. This will eat your first hour if you let it. Use
`adb reverse tcp:8748 tcp:8748` over USB so the headset sees `http://localhost:8748`,
which *is* a secure context and needs no certificates. For untethered use, add the
LAN origin under `chrome://flags` → "Insecure origins treated as secure" in the
Quest browser. Print both instructions on server start.

**Typing URLs in the Quest browser is miserable.** Bookmark it once. Do not plan
any flow that requires retyping it.

**Quad layer feature detection.** `layers` is optional. Request it in
`optionalFeatures` and check `session.renderState` before creating one. A hard
dependency will break on any device that lacks it.

**Guardian drift.** Covered above: re-anchor on summon, not on session start.

**Passthrough contrast.** Do not evaluate the palette in a dim room. Test against
a window.

**Draw call budget.** One texture per panel will tank the frame rate at ten
panels. The shared atlas is not an optimization to do later; it is the design.

**Do not touch.** `serve`, the MCP tool surface, the five lifecycle hooks, the
task state machine, `settings.json` merging. If a change seems to require touching
any of these, stop and flag it rather than proceeding.

---

## 14. Tests

Server side, in the existing hermetic style against a temp `AISQUARE_HOME`:

- `protocol.py` round-trips every message model
- `projector.py` produces a correct snapshot from a seeded board
- `projector.py` delta computation: added, changed, removed sessions
- `summary` generation truncates and never emits raw transcript text
- Token auth rejects missing, wrong and replayed tokens
- Server start is a no-op failure when the port is occupied (fail-open, clear message)

`make check` must pass unchanged: ruff, format, mypy strict, pytest.

Client side is not meaningfully unit-testable in this window. Do not build a JS
test harness. The milestone verification steps in §11 are the test plan.

---

## 15. Demo runbook

Write this into `docs/xr-demo.md` as you go, not at the end.

1. Three Claude Code sessions running in the target repo: one planner, two coders
2. `aisquare xr` running; USB connected; `adb reverse` established
3. Headset bookmark opens; enter AR
4. Survey the ring in passthrough — three panels, distinct role colours
5. Focus the planner; read its state
6. Push-to-talk: give it a task
7. While it works, a coder hits `needs_you` — alert bar, spatialized chime from
   behind the operator's shoulder
8. B to jump to it; resolve
9. Collapse; walk; summon

Step 7 is the moment that justifies the whole thing. Make sure it can be triggered
on demand during rehearsal rather than hoped for.

---

## 16. Definition of done

- [ ] `pip install -e '.[xr]'` works from a clean checkout
- [ ] `aisquare xr` starts, prints URL and `adb reverse` command
- [ ] `aisquare doctor` reports XR status
- [ ] `make check` passes
- [ ] Ring renders in passthrough with live state
- [ ] Focus tier is readable without leaning in
- [ ] Push-to-talk reaches a session and the reply is visible
- [ ] Collapse and summon works after relocating
- [ ] Alert state is visible and audible from outside the field of view
- [ ] `docs/xr-demo.md` exists and has been rehearsed twice
- [ ] Nothing in `serve`, the hooks, or the task lifecycle was modified
