# Captain — acceptance runbook (Phase 1)

**What this is.** The seven acceptance lines of the captain card
(`docs/plans/captain.md` §7), each with the command the owner types, what must
be seen and heard, and a slot for the **real pasted output** from the assembled
`rc/captain-v1` head. A slot marked `⟨PASTE⟩` has not been run yet on that head;
the runner replays this page with a real headset on this box and fills it. Name
what could not be heard or held rather than leaving a slot blank.

How current is this file? Ask git: `git log -1 --format='%h %ad' -- docs/runbooks/captain-acceptance.md`.

## 0. Preflight

```sh
aisquare --version
aisquare doctor
aisquare captain voice --show-token
```

Expect: the URL `http://localhost:8749/#token=…`, the QR, the `adb reverse`
line, `mode: focus · speaker: on`. Headset: the Windows default playback
device is the headset; the browser's microphone prompt gets the headset mic.
If the captain has never run in this home, `aisquare captain` first, and answer
*Yes, I trust this folder* once in its window.

```text
⟨PASTE: aisquare captain voice --show-token⟩
```

## 1. Headphones on: what is up, item one spoken, resolve, next

Open the page in focus mode, hold the button, say **"what is up"**, release.

Expect: the interim transcript, then the final; `thinking` on; the reply lists
the ranked items and **item one is spoken**; `thinking` off. Then say
**"resolve it, I told coder-1 to retry"** and **"next"**.

Board evidence (one `captain_action` per call, `ok` true):

```sh
aisquare captain log --limit 6
aisquare captain attention
```

```text
⟨PASTE: the page's log panel — interim, final, utterance, reply⟩
⟨PASTE: aisquare captain log --limit 6⟩
```

## 2. Ask the manager, then spawn and paste — three receipts

Say **"ask alpha's manager what is blocking the deploy"**, then, on its answer,
**"spawn a coder for it and paste the manager's answer to it"**, then confirm
when the captain asks (spawn needs your own words).

Expect three tool calls with receipts: `ask_manager` (the manager's note seq),
`spawn` (the new row, `confirm=true` only after your word), `paste` (chars and
`submitted`). The captain reads the refusal aloud if it tried to spawn without
your word.

```sh
aisquare captain log alpha --limit 8
```

```text
⟨PASTE: aisquare captain log alpha --limit 8 — the three receipts⟩
```

## 3. A coder stuck on a permission prompt, unblocked by "say yes to coder-1"

Have a coder sit at a permission prompt (its row reads `attention`). Say
**"say yes to coder-1"**.

Expect: `press yes`, sent as the digit of the chooser's Yes (`1` on Claude
Code's permission chooser), then the pane moves on. The press audit shows the
key sent and `answered: true`, and the reply says what the pane shows. A key
the prompt ignores (the letter `y` on that chooser) comes back as an error,
audited `ok: false`, never as a success. A coder that is `working` with no
prompt on its screen is refused with the state named, never pressed.

```sh
aisquare captain log alpha --limit 4
```

```text
⟨PASTE: the reply, and the press audit with its state⟩
```

## 4. "What did coder-1 do since my last update", spoken, watermark moved

Say **"what did coder-1 do since my last update"**.

Expect: `since alpha coder-1` with the events past the watermark and the pane
tail; the summary spoken; the watermark advanced (`advanced: true`), so the
same question again returns only what is new.

```sh
aisquare captain since alpha --agent coder-1
```

```text
⟨PASTE: the spoken summary as the page logged it; the since result with from_seq/to_seq⟩
```

## 5. Always listening: only "Captain, …" lands, and two land as two requests

```sh
aisquare captain voice --mode listen
```

The start line names the wake word (`wake word: captain`), and the idle chip
reads **say Captain**. The first utterance loads the speech model, which takes a
few seconds: wait for its transcript before the next step.

1. Say **"we should ship the fold today"**. Expect no words on the page, live or
   after, nothing delivered, nothing spoken.
2. Say **"Captain, what is up"**, pause a second, then say **"Captain, snooze
   the first one for an hour"**. Expect two deliveries without the wake word
   ("what is up", "snooze the first one for an hour"), `thinking` between them,
   and two replies.
3. Say **"Captain"** alone. Expect a short tone and the chip's **listening**.
   Within five seconds say **"what is up"**; it is delivered as it is.
4. Say **"stop listening"**. The mic turns off and nothing is delivered.

The page's mode toggle and the terminal's `--mode` agree
(`captain_voice_mode` in `state.json`).

```text
⟨PASTE: the page's log panel — the dropped sentence (no words), two deliveries, the window, the stop word⟩
```

## 6. Every action a board event; a refusal said, never faked

Say **"stop coder-1"** without having asked for it in so many words.

Expect: `stop` refused (`confirm=true` missing), the refusal spoken as it came
(`refused: …`), and one `captain_action` with `ok: false` on alpha's board. Then
**"yes, stop coder-1"** — the stop, with its receipt (`agent_exited`).

```sh
aisquare captain log alpha --limit 4
aisquare board
```

```text
⟨PASTE: the refusal audit (ok false) and the stop audit with its receipt⟩
```

## 7. `make check` green

On the assembled `rc/captain-v1` head, in a fresh worktree with its own venv and
an isolated `AISQUARE_HOME`:

```text
⟨PASTE: ruff format --check · ruff check · mypy --strict · pytest counts · exit 0⟩
```

## Not heard, not held

List here, per replay, what the box could not do (no headset, no Android
device, a model that would not load), so a green slot is never assumed.

```text
⟨PASTE⟩
```

## Known facts about CI

The RC base's Windows leg is red on five persona tests from the fold — a
persona-train follow-up, not the captain's. Two UI tests are intermittent on
Windows and on py3.11 (the sidebar's divider double-click; the accounts view);
they pass on re-run. A captain PR's Windows leg is read against that base set.

## After a reboot

If the first `aisquare captain` of the morning refuses naming the fleet's
sweep — the server is gone but its socket file is still there — run the one
command it names, then start again:

```sh
aisquare fleet reap -P <home> --server-down
aisquare captain
```
