# cliXR demo runbook

> **Status: 2026-09-13 — every step below is an instruction, and every step a
> desktop can settle has been run.** The five lines that need a headset are
> marked **verify on headset** in the definition-of-done table at the end, and
> nowhere else in this document is anything claimed that has not been executed.
> Rehearsed twice end to end on the desktop; **not yet rehearsed on the
> headset** — that is what this document is for.
>
> The plan is `docs/plans/clixr.md`: §15 is this runbook, §13 the traps, §16 the
> definition of done, §8 the palette, §10 the voice pipeline.

Rehearsal target: twice, end to end, before it is shown to anyone. Read the
traps first — they are the only part of this that costs hours rather than
minutes.

---

## Before you start: the machine and the headset

Two prerequisites this runbook assumes rather than installs. Both bite before
step 1, and `aisquare doctor` only sees the first of them.

**The CLI, with the `xr` extra.** The extra is what carries the WebXR server
and the speech backend; a bare install has no `aisquare xr` at all:

```sh
pipx install 'aisquare-cli[xr]'
```

Working from a checkout of this repo instead — which is what the definition of
done measures — install it editable from the repo root:

```sh
pip install -e '.[xr]'
```

**`adb`, and a Quest that will talk to it.** Step 2 says to accept the
debugging prompt *inside* the headset; that prompt only ever appears if both
sides were set up first. Install Android platform-tools so `adb` is on your
`PATH` — `adb version` should answer — then enable **Developer Mode** for the
device in the Meta Quest phone app and leave **USB debugging** on in the
headset. Without it, the `adb reverse` line in trap 1 and step 2 fails with
`no devices/emulators found`, the headset never sees `localhost`, and trap 1
is then the symptom you chase instead of the cause.

## Before you start: the three traps (plan §13)

**1. `http://192.168.x.x:8748` is not a secure context.** `navigator.xr` will be
`undefined` and the session request fails with an error that does not say why.
Over USB, forward the port so the headset sees `localhost`, which *is* a secure
context and needs no certificates:

```sh
adb reverse tcp:8748 tcp:8748
```

For untethered use instead, add the LAN origin under `chrome://flags` →
"Insecure origins treated as secure" in the Quest browser, and restart it.
`aisquare xr` prints both instructions on start, so you do not have to remember
which one you are doing.

**2. Typing a URL in the Quest browser is miserable.** Bookmark
`http://localhost:8748` once, on the first run, and never plan a step that
requires retyping it. The token travels in the URL *fragment*, so the bookmark
carries it and it never reaches a server or a log.

**3. The palette is passthrough, not a monitor.** Do not judge contrast in a dim
room. Test against a window. The panel body ships at 86% alpha over a dark
slate for exactly this reason (§8), and a bright window is the case that breaks
it if anything does.

## Preflight: `aisquare doctor`

One line answers all three machine-side preconditions — the `xr` extra
installed, port 8748 free, and the whisper model already in the Hugging Face
cache:

```sh
aisquare doctor
```

Look for the row named `xr`. On a ready machine it reads:

```
✓ xr: xr extra installed; port 8748 free; whisper model base.en cached (<cache dir>)
```

It is **ok when something is merely absent** — the extra not installed, the
model not yet downloaded — with the install line and the pre-download line on
the row itself, and it **warns only for a fault you can act on**: port 8748 held
by another process, a faster-whisper install missing its ctranslate2 or
onnxruntime wheel, a cached model with no loadable snapshot (an interrupted
download), or an unsupported name in `AISQUARE_XR_WHISPER_MODEL`. It never
fails: every other command works without any of this, and `install.sh` treats
any amber row but `brain` as an installer failure. The third fact is the one
worth reading before a demo, because it is the only one that fails *late*: with
no cached model the server starts perfectly and the first push-to-talk goes to
the network. The line printed beside it pre-downloads the model; run it on the
machine that will be demoing, on its own network, not five minutes before.

To use the larger model, set `AISQUARE_XR_WHISPER_MODEL=small.en` before both
the download and the run. `base.en` (the default) and `small.en` are the only
values the voice path accepts — this is command input, where latency dominates
accuracy (§10).

---

## The nine steps (plan §15)

### 1. Three Claude Code sessions running in the target repo: planner, coder, tester

From the repo you want to survey, in three terminals or three tmux windows:

```sh
aisquare launch planner
aisquare launch coder
aisquare launch tester
```

Confirm the board sees all three before you put the headset on — if a session
is missing here it will be missing from the ring, and you will debug the
renderer for a bug in the launch:

```sh
aisquare board
```

> **On colours, if you substitute a role above.** The three roles in that block
> are three distinct *buckets*, which is what step 4's "three distinct role
> colours" needs: planner-amber `#F2B33D`, coder-cyan `#4FC3D9`, runner-violet
> `#A78BDB`. They are also what both rehearsals and the live run used. The ring
> colours a session by its bucket rather than by its role name
> (`services/xr/projector.py`), and several names share a bucket — `coder` and
> `reviewer` are both cyan, and `tester`, `runner`, `validator` and
> `unassigned` are all violet. So swapping `tester` for a second `coder` gives
> you amber + cyan + cyan, and step 4 will show you two colours for three
> panels. That is a fine thing to demo deliberately; it is a bad thing to
> discover while wearing the headset.

### 2. `aisquare xr` running; USB connected; `adb reverse` established

Connect the headset over USB and accept the debugging prompt **inside** the
headset, then forward the port and start the server from the same repo:

```sh
adb reverse tcp:8748 tcp:8748
aisquare xr
```

It prints the URL with the token in the fragment, the `adb reverse` line, and
the `chrome://flags` note. To read those without holding the port — to put the
URL in a bookmark, say — ask for them and exit:

```sh
aisquare xr --show-token
```

If the port is held, the command says so and exits 1 rather than starting
half-way; the `doctor` row above is the faster way to find out why.

### 3. Headset bookmark opens; enter AR

Open the bookmark from trap 2 and press *Enter AR*. If the button is absent,
the page is not in a secure context — go back to trap 1; that is the failure it
describes, and it does not announce itself.

The connection chip in the corner is the transport, in words, without devtools:
`connected`, `reconnecting — attempt N`, `server gone`, `auth failed`,
`offline`. It is DOM, not part of the 3D scene, so it survives a WebGL context
loss and is still readable when the scene is not.

### 4. Survey the ring in passthrough — three panels, distinct role colours

**Verify on headset** (the passthrough half). Three panels, one per session
from step 1, at the role colours of §8. Turn your head rather than moving the
panels; the ring is body-anchored, and re-anchoring happens on summon, not on
session start.

The live-state half of this step is settled on the desktop: three sessions
produce three panels at three role colours, and a session stopping removes its
panel and starting returns it, both without a reload.

### 5. Focus the planner; read its state

**Verify on headset.** Look at the planner panel and press **A**. It pulls
forward into the focus tier. The test of this step is whether the transcript is
readable *without leaning in* — that is a line in the definition of done (§16),
not a nice-to-have. If it is not readable, stop and fix it; §11/M4 calls this
the make-or-break.

### 6. Push-to-talk: give it a task

**Verify on headset** (the reply-in-the-headset half). Hold the **left
trigger**, speak, release.

Watch the focus panel for the interim text while you speak. If it never
appears, the mic is not live — and without that feedback an operator repeats
themselves, which is exactly why the interim exists (§10).

Both halves of the path are implemented and tested on this branch: the client
captures at 16 kHz mono PCM16LE in 20 ms frames and stops itself at 55 s, and
the server feeds them to faster-whisper and routes the final transcript as the
prompt. The client deliberately sends **no** `prompt` frame for a voice
utterance — releasing the trigger is the commit, and a client that also sent one
would deliver the sentence twice.

Expect ASR to mangle repo names, branch names and identifiers. That is known
and not a bug to chase during a demo; keep a typing path for anything that has
to be exact.

### 7. While it works, a coder hits `needs_you` — alert bar, spatialized chime

**This is the step that justifies the whole thing, so it is triggered on
command rather than waited for.** Do not rehearse it by hoping a real
`needs_you` lands while you are looking the other way.

First read the session id you want to alert. Every live session is in the board
as JSON, with its `id`, `role` and `state`:

```sh
aisquare --json board
```

Take the `id` of the session you want — the `sessions` array, the entry whose
`role` is the coder you parked — and feed the real `Notification` hook the same
payload Claude Code would:

```sh
printf '{"session_id":"<SESSION-ID>","cwd":"'"$PWD"'","message":"needs you"}' | aisquare hook notification
```

Run it from the repo the session belongs to, so `cwd` names that project. It
prints nothing and exits 0 — the hook path is fail-open by design and never
disrupts an agent.

Within one poll (500 ms) the panel's bar turns the alert token `#FF5A4E` — the
one colour in the palette reserved for `needs_you` and used nowhere else — and
the chime is emitted at the panel's own world position.

**Verify on headset** (the audibly-located half). That the chime *is emitted*
at the panel's position is settled on the desktop; that you can *hear where it
is* is not, and hearing it is the half §16 asks for. Trigger it with the
alerting panel behind your shoulder and outside your field of view, then turn
towards the sound before you look at the ring. If you have to hunt for which
panel it was, this line is not done — the bar going `#FF5A4E` is not evidence
for it, and this is the step the plan calls the moment that justifies the whole
thing.

**It clears when that session goes back to work.** In the demo, answering the
coder is what does it — its next prompt runs the `UserPromptSubmit` hook, which
moves the session back to `working` and the bar back to its role colour. To
clear it by hand during a rehearsal, run that same hook:

```sh
printf '{"session_id":"<SESSION-ID>","cwd":"'"$PWD"'","prompt":"carry on"}' | aisquare hook user-prompt-submit
```

The alert fires once on the transition **into** `needs_you`, not once per
notice — Claude re-notifies while it is parked, and a chime per notice would be
a chime every few seconds from a panel you have already dealt with.

### 8. B to jump to it; resolve

**Verify on headset.** Press **B** to jump focus to the alerting session, deal
with it, and watch the alert state clear. Repeated presses sweep the alerts in
angle order, so **B** always ends with an alert in front of you — including
when there is only one, which re-centres rather than doing nothing.

### 9. Collapse; walk; summon

**Verify on headset.** Collapse the ring, walk a few metres, and summon it back.
It re-anchors around you where you now are — re-anchoring on summon rather than
on session start is what makes this survive guardian drift (§13).

---

## The recovery drill (plan §11/M7)

Rehearse this once before the demo, because a server that dies mid-demo is the
failure most likely to actually happen, and the recovery needs no headset
interaction at all.

Kill `aisquare xr` while the page is open and watch the chip:

- within a second it reads `reconnecting — attempt N`, the attempt count
  climbing as the backoff widens from 500 ms to 8 s;
- after five failed attempts it changes wording to `server gone`. It has **not**
  stopped retrying — the wording is the point, because five failures with the
  backoff at its ceiling is no longer a blip, and a chip that said
  "reconnecting" for two minutes could not tell a slow reconnect from a server
  you forgot to restart.

Start it again from the same repo:

```sh
aisquare xr
```

The chip returns to `connected` and the panels come back **without reloading
the page** — which matters because reloading is exactly what you cannot
comfortably do while wearing a headset.

---

## If something is wrong

- **No `xr` row in `aisquare doctor`, or it warns** — read the fix it prints;
  it names the exact command for each fault. An ok row that says the extra is
  not installed or the model is not cached is not a fault, but voice will not
  work until you run the line on that row.
- **`navigator.xr` is undefined / no *Enter AR* button** — trap 1. It is almost
  always this.
- **The chip says `auth failed`** — the bookmark carries a stale token. The
  token is `serve`'s, from the same 0600 file; take the current URL from
  `aisquare xr --show-token` and re-bookmark it. The client stops retrying on
  this one on purpose: the same token will be rejected again.
- **Panels are there but frozen** — the board poll is 500 ms and deltas are only
  sent when something moved, so a still ring means a still board. Check
  `aisquare board` in a terminal before suspecting the renderer.
- **Voice produces nothing at all** — the transcriber refuses to start rather
  than degrading silently, so there is an error with a fix attached; the three
  reasons it can refuse are a missing extra, an unsupported model name in
  `AISQUARE_XR_WHISPER_MODEL`, and a model that cannot be loaded. A press that
  ends with an `stt_empty` error is a microphone that sent no audio at all (a
  suspended audio context in the headset browser); a press answered with
  `audio_misaligned` is a client sending frames that are not a whole number of
  samples. An empty final `stt` with no error is simply a press with no speech
  in it — a breath, a click, room tone — which the model's voice-activity gate
  declines to turn into a prompt.
- **Voice produces the wrong words** — expected for identifiers (§10). Not a
  configuration problem, and not one to debug live.

---

## Definition of done (plan §16)

Every row is either **done, with the evidence that settled it**, or **verify on
headset** with the step that will settle it. No row is ticked on the strength of
the code being written.

| § 16 line | Status | Evidence, or who settles it |
|---|---|---|
| `pip install -e '.[xr]'` works from a clean checkout | **done** | Fresh clone, fresh venv, `pip install -e ".[xr]"` → exit 0, 43 packages (aisquare-cli plus 42 dependencies). Pasted in full on the integration PR. |
| `aisquare xr` starts, prints URL and `adb reverse` command | **done** | `aisquare xr --show-token` prints the URL with the token fragment, the token, and `adb reverse tcp:8748 tcp:8748`; the server also prints all three plus the `chrome://flags` note on start. |
| `aisquare doctor` reports XR status | **done** | `✓ xr: xr extra installed; port 8748 free; whisper model base.en cached` — all three facts in one row, from the clean install. |
| `make check` passes | **done** | Green under **both** extras — `[dev]` and `[dev,xr]` — because the numpy stub trap only exists on the machine that has the extra. Run with an explicit `PYTHON=`; see the PR for why that matters. |
| Ring renders in passthrough with live state | **live state done · passthrough verify on headset** | Desktop: three live sessions → three panels at `#F2B33D` / `#4FC3D9` / `#A78BDB`; stopping one removed its panel and restarting returned it, both with the page's document identity unchanged (no reload). Passthrough compositing and the quad layer: **step 3–4 on the headset**. |
| Focus tier is readable without leaning in | **verify on headset** | **Step 5.** A judgement about real text at real distance in real passthrough; a desktop monitor cannot stand in for it. |
| Push-to-talk reaches a session and the reply is visible | **server + client paths done · reply-in-headset verify on headset** | The whole path is covered by the suite, and the client half was driven in a real browser against the real model. Seeing the reply land on the panel while wearing the headset: **step 6.** |
| Collapse and summon works after relocating | **verify on headset** | **Step 9.** Re-anchoring against real guardian drift needs a room to walk across. |
| Alert state is visible and audible from outside the field of view | **trigger + audio done · localisation verify on headset** | Desktop: `aisquare hook notification` flipped a live session to `needs_you` within one poll, the bar turned `#FF5A4E`, the alert fired exactly once, the chime was emitted at the panel's own world position, and the next `user-prompt-submit` cleared it. Whether it is *audibly located* behind your shoulder, and whether the bar reads against a bright window: **steps 7–8 on the headset.** |
| `docs/xr-demo.md` exists and has been rehearsed twice | **desktop done · headset pending** | This document; the nine steps rehearsed twice end to end on the desktop, the second run reproducing the first. The recovery drill was run once, between them. The headset rehearsal is **M9 and has not happened**. |
| Nothing in `serve`, the hooks, or the task lifecycle was modified | **done** | `git diff main` touches none of `services/mcp_server.py`, `cli/serve.py`, `services/hooks.py`, `cli/hook.py`, `core/agents.py`, or `services/team.py`. Shown as a diffstat on the PR. |
