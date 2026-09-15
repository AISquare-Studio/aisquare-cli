# cliXR demo runbook

> **Status: 2026-09-12 — partially live.** The steps below that are written as
> instructions work today and have been run. The rest are marked **verify on
> headset (pending)** and are written from the plan rather than from a
> rehearsal; do not read them as rehearsed until the milestone that owns them
> lands. The plan is `docs/plans/clixr.md` (§15 is this runbook, §13 the traps,
> §16 the definition of done).

<!-- TODO(xr): `aisquare xr` is INLINE code everywhere in this file, on purpose.
     tests/test_documented_commands.py resolves every `aisquare …` line inside a
     FENCED block against the live Typer command tree, and the `xr` command does
     not exist until the server task lands — so fencing it would fail the guard
     today, and the obvious "fix" would be to weaken the guard. The integration
     task (plan §11/M7-M9) converts these to fenced blocks in the same PR that
     adds the command, and adds this document to DOCUMENTED there. -->

Rehearsal target: twice, end to end, before it is shown to anyone.

---

## Before you start: the three traps (plan §13)

These cost hours if you meet them live rather than here.

**1. `http://192.168.x.x:8748` is not a secure context.** `navigator.xr` will be
`undefined` and the session request fails with an error that does not say why.
Over USB, forward the port so the headset sees `localhost`, which *is* a secure
context and needs no certificates:

```sh
adb reverse tcp:8748 tcp:8748
```

For untethered use instead, add the LAN origin under `chrome://flags` →
"Insecure origins treated as secure" in the Quest browser, and restart it.

**2. Typing a URL in the Quest browser is miserable.** Bookmark
`http://localhost:8748` once, on the first run, and never plan a step that
requires retyping it.

**3. The palette is passthrough, not a monitor.** Do not judge contrast in a dim
room. Test against a window.

## Preflight: `aisquare doctor`

One line answers all three of the machine-side preconditions — the `xr` extra
installed, port 8748 free, and the whisper model already in the Hugging Face
cache:

```sh
aisquare doctor
```

Look for the row named `xr`. It is **ok when something is merely absent** — the
extra not installed, the model not yet downloaded — with the install line and
the pre-download line on the row itself, and it **warns only for a fault you can
act on**: port 8748 held by another process, a faster-whisper install missing
its ctranslate2 or onnxruntime wheel, a cached model with no loadable snapshot
(an interrupted download), or an unsupported name in `AISQUARE_XR_WHISPER_MODEL`.
It never fails: every other command works without any of this, and `install.sh`
treats any amber row but `brain` as an installer failure. The third fact is the
one worth reading before a demo, because it is the only one that fails *late*:
with no cached model the server starts perfectly and the first push-to-talk goes
to the network. The line printed beside it pre-downloads the model; run it on
the machine that will be demoing, on its own network, not five minutes before.

To use the larger model, set `AISQUARE_XR_WHISPER_MODEL=small.en` before both
the download and the run. `base.en` (the default) and `small.en` are the only
values the voice path accepts — this is command input, where latency dominates
accuracy (§10).

---

## The nine steps (plan §15)

### 1. Three Claude Code sessions running in the target repo: one planner, two coders

Works today. From the repo you want to survey:

```sh
aisquare launch planner
aisquare launch coder
aisquare launch coder
```

Confirm the board sees all three before you put the headset on — if a session
is missing here it will be missing from the ring, and you will debug the
renderer for a bug in the launch:

```sh
aisquare board
```

### 2. `aisquare xr` running; USB connected; `adb reverse` established

**Verify on headset (pending)** — the command lands with the server task.

Connect the headset over USB and accept the debugging prompt inside the
headset, run `adb reverse tcp:8748 tcp:8748`, then start `aisquare xr` from the
same repo. It prints the URL and the `adb reverse` line; if the port is held,
the doctor row above is the faster way to find out why.

### 3. Headset bookmark opens; enter AR

**Verify on headset (pending).** Open the bookmark from step 2's trap note and
press *Enter AR*. If the button is absent, the page is not in a secure
context — go back to trap 1; that is the failure it describes, and it does not
announce itself.

### 4. Survey the ring in passthrough — three panels, distinct role colours

**Verify on headset (pending).** Three panels, one per session from step 1, at
the role colours of §8. Turn your head rather than moving the panels; the ring
is body-anchored, and re-anchoring happens on summon, not on session start.

### 5. Focus the planner; read its state

**Verify on headset (pending).** Look at the planner panel and press **A**. It
pulls forward into the focus tier. The test of this step is whether the
transcript is readable *without leaning in* — that is a line in the definition
of done (§16), not a nice-to-have.

### 6. Push-to-talk: give it a task

**Verify on headset (pending).** Hold the **left trigger**, speak, release.

The host half of this path is live and tested on this branch
(`src/aisquare/services/xr/speech.py`): 16 kHz mono PCM16LE in 20 ms frames, an
RMS gate so a held trigger in a quiet room never reaches the model, an interim
decode about once a second, and a final transcript on release. What is pending
is the client that captures the audio and the server that carries it.

Watch the focus panel for the interim text while you speak. If it never
appears, the mic is not live — and without that feedback an operator repeats
themselves, which is exactly why the interim exists (§10).

Expect ASR to mangle repo names, branch names and identifiers. That is known
and not a bug to chase during a demo; keep a typing path for anything that has
to be exact.

### 7. While it works, a coder hits `needs_you` — alert bar, spatialized chime from behind the shoulder

**Verify on headset (pending).**

**This is the step that justifies the whole thing, so it must be triggerable on
demand.** Do not rehearse it by waiting for a real `needs_you` and hoping it
lands while you are looking the other way. Arrange a coder session that will
reliably ask for input, park it, and trigger it when you want it — and rehearse
from *outside* the field of view, because "visible and audible from outside the
field of view" is the claim being made (§16).

### 8. B to jump to it; resolve

**Verify on headset (pending).** Press **B** to jump focus to the alerting
session, deal with it, and watch the alert state clear.

### 9. Collapse; walk; summon

**Verify on headset (pending).** Collapse the ring, walk a few metres, and
summon it back. It re-anchors around you where you now are — re-anchoring on
summon rather than on session start is what makes this survive guardian drift
(§13).

---

## If something is wrong

- **No `xr` row in `aisquare doctor`, or it warns** — read the fix it prints;
  it names the exact command for each fault. An ok row that says the extra is
  not installed or the model is not cached is not a fault, but voice will not
  work until you run the line on that row.
- **`navigator.xr` is undefined / no *Enter AR* button** — trap 1. It is almost
  always this.
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
