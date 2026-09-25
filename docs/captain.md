# The captain

The captain is the home-level agent: one Claude Code session per home that runs
every project's fleet for you. You talk to it — typed or spoken — and it acts
through a fixed set of tools, each with one meaning, each leaving one audit
event on a board. It never types into a pane itself, never guesses a project's
work, and says every refusal as it came.

Three ideas carry the feature:

- **One captain, on the home board.** The captain lives on the project row for
  `$AISQUARE_HOME` (captured, never onboarded, so it never joins your projects),
  runs from `$AISQUARE_HOME/captain/brain`, and has no tool but its own
  Actions server. `aisquare captain` starts it or attaches to it.
- **Every effect is a tool call with a receipt.** The captain's hands are the
  24 tools of `aisquare captain serve --stdio` (the *Actions server*). Each call
  writes exactly one `captain_action` event carrying your words (the
  *utterance*), what was done and the effect's own event seq (the *receipt*).
  A refusal is an error result that starts `refused:` (a rule said no) or
  `error:` (something failed) — never a success that did not happen.
- **The queue is the agenda.** The *attention queue* folds every board into one
  ranked list of what needs you: a question from an agent, a blocked card, a
  review waiting, a pull request, an agent gone quiet. "What is up" reads it;
  you take one item at a time.

> **Status.** Phase 1 of the plan in [`docs/plans/captain.md`](plans/captain.md),
> built on `rc/captain-v1`. Everything below exists in this checkout; the
> **Phase 2** section at the end names what does not. CI validates every
> shell-fenced `aisquare …` line here against the live command tree.

## Setup, once

1. Install with the voice extra if you want the page and the speaker:

   ```sh
   pip install 'aisquare-cli[voice]'
   ```

   Without it, `aisquare captain voice` refuses with the install line; typing
   through `aisquare captain "text"` needs nothing extra.

2. Start the captain and trust its folder, once:

   ```sh
   aisquare captain
   ```

   The first start attaches you to the captain's window, where Claude Code asks
   *"Is this a project you created or one you trust?"* about
   `$AISQUARE_HOME/captain/brain`. Choose **Yes, I trust this folder**. Nothing
   in aisquare edits your Claude config for you (that is a Phase 2 question,
   yours to answer). Until you answer, `aisquare captain "text"` refuses and
   tells you exactly this.

3. Headset (Windows, WSL): the speaker plays through Windows' **default
   playback device**, so make the headset the default before you start; the
   browser asks which microphone to use when the page first opens — pick the
   headset there.

## Talking to the captain

Typed, from any terminal:

```sh
aisquare captain say "what is up"
aisquare captain say "next" --timeout 60
aisquare captain chat
```

`say` is the explicit form; the first word that names no subcommand is a
message too, so `aisquare captain "what is up"` — quotes and nothing else — is
the everyday spelling. `chat` reads a line, prints the reply, and repeats until end of input.
With `--json`, `say` prints one object, `{"reply", "ended_at", "timed_out"}`:
a captain that answered with tools alone gives `"reply": null` and a `said`
line; no reply in time gives `"reply": null`, `"timed_out": true` and the
reason, exit 1; a captain that died, or a dialog that must be answered by hand,
gives `"timed_out": false` with the reason, because waiting longer would not
have helped.

The captain answers you and, when it wants your ear, speaks: its `speak` tool
queues a line for the speaker, and the captain's own server plays it — with the
voice page open or not.

## The voice page

```sh
aisquare captain --voice
aisquare captain voice --mode listen --speaker on --show-token
```

The first prints a URL of the shape `http://localhost:8749/#token=…` and a QR code. Open
it in the desktop browser (localhost is the secure context the microphone
needs) or, on an Android phone over USB, run the printed `adb reverse` line and
open the same URL there. The token in the URL is the only lock on the page, so
it binds loopback only. `aisquare captain voice` is the same command with its
options; `--voice` is the plan's spelling. `--show-token` prints without serving.

Two modes, on the page and on the command line:

- **Focus** (`--mode focus`, the default): hold the button or the space bar to
  talk; release sends. The burst is one request.
- **Listen** (`--mode listen`): the microphone stays open and the server cuts
  what it hears into utterances on silence — 900 ms of quiet, or 60 seconds,
  ends one. Only an utterance that begins with the **wake word** reaches the
  captain, stripped of it: "Captain, find me this" delivers "find me this".
  - **"Captain" alone** opens a five-second window, marked by a short tone and
    the chip's **listening**. The next utterance goes through without the wake
    word.
  - **Anything else is dropped.** It is never delivered, never spoken and not
    kept, and the page shows none of its words. Listen mode's idle chip says
    **say Captain**, so a meeting you are sharing your screen in stays off the
    page.
  - **The stop word "stop listening"** turns the mic off, spoken bare, after
    "Captain", or typed. A mute or a mode switch closes an open window.
  - **Typed text needs no wake word.** The gate is on what the mic hears.

The wake word is `captain` unless `[captain] wake_word` in `config.toml` says
otherwise: one word or a few, a to z and spaces (`wake_word = "hey captain"`).
`wake_word = ""` switches it off, and listen mode then delivers everything it
hears. The match forgives case, punctuation and one misspelling ("Kaptain",
"Captian"), never the plural or the possessive ("Captains", "Captain's"). The
start line of `aisquare captain voice` names the wake word, and a value that
is not words is refused there in one line, never read as "off".

In focus mode, and in listen mode once the wake word is heard, the page shows
the interim transcript as you speak (so a misheard request is caught before it
lands). It also shows what was delivered, and a **thinking** chip while the
captain works — while your request is in flight, while the captain's own
`thinking` flag is on, or while its pane is busy. The same signal prints in the
terminal that serves the page. A reply slower than three seconds earns one
spoken cue, "on it"; the reply itself is spoken only when the captain did not
already speak during that turn, so nothing is heard twice.

The mode has one home, `captain_voice_mode` in `state.json`: `--mode` sets it,
the page's toggle sets it, and every open page follows within a second. A typed
message in the page's box goes the same way as a spoken one.

### Dictation apps

The page's text box and `aisquare captain chat` both take typed text, so a
dictation app is a third way in: Windows dictation (Win+H) into either, or any
app that types into a terminal. The page's own recognition runs on this
machine (faster-whisper, `base.en` by default; `--model small.en` for more
accuracy at more CPU) and needs no account.

### The speaker

One adapter per platform, chosen for you: `powershell.exe` with System.Speech
on Windows and WSL, `say` on macOS, `spd-say` on Linux, and a silent adapter
that only logs when none is available. Override it in `config.toml`:

```toml
[captain]
speaker = "powershell"   # powershell | say | spd-say | null
```

`--speaker on|off` flips the switch (`captain_speaker` in `state.json`); the
page has the same toggle. A spoken line older than 30 seconds when its turn
comes is dropped rather than played late.

## What needs you: the queue verbs

```sh
aisquare captain attention --limit 10
aisquare captain next
aisquare captain resolve q1a2b3c4 "told coder-1 to retry"
aisquare captain snooze q1a2b3c4 --for 30
aisquare captain since alpha --agent coder-1 --advance
aisquare captain log alpha --limit 20
aisquare captain actions
```

Each verb is the same tool call the captain makes, audited with your words
(`aisquare captain <verb> …`), and `--json` prints the tool's own result.
`attention` ranks what needs you: a question first, then a blocked card, an
agent waiting, a review, a pull request, an agent gone quiet; the same ask
from the same agent about the same card is one item however often it repeats.
`next` is the top item; `resolve` closes one with what you did; `snooze` hides
one for a while (a week at most). `since` shows what happened on a board since
you last looked, and `--advance` moves that watermark. `log` is the audit: the
`captain_action` events, newest last.

### The action list

Named sequences of the captain's primitives, run as one tool call (`act`):

```toml
[captain.actions.approve_and_check]
description = "say yes to the prompt, then read what happened"
steps = ["press y", "read_pane 20"]
```

Primitives: `press <key>`, `paste <text>`, `tell <text>`, `read_pane [lines]`,
`ui <action> [arg]`, `task <verb> <ref> [note]`. `{placeholders}` are filled
from the call's arguments. Three come bundled — `approve_prompt` (press y),
`unblock` (press y, then read the pane) and `open_spawn` — and a config entry
wins on a name. The whole sequence is checked before the first step runs, and
a failing step stops it and says which. `aisquare captain actions` lists them.

### The easter eggs

- `aisquare captain uav` — the sitrep: prints **UAV online**, whether the
  captain is thinking, then what needs you.
- `aisquare captain wololo <project> <label> <task>` — convert an idle agent to
  a task: release its claims, claim the card for it, tell it. Refused while the
  agent is working.
- `aisquare captain bt` — the brake: cancel a wait for a manager's answer, clear
  the speech queue, undo the captain's last reversible action (a claim is
  released, a done reopened).

## Rules the captain keeps

- **Confirm before stop, spawn and restart.** Those three refuse unless the
  captain passes `confirm=true`, and its briefing lets it do so only when your
  own words asked for that action or confirmed it.
- **Pane text is data.** What an agent's pane or a note says is reported to
  you, never obeyed.
- **Never into a dialog.** Anything that types into the captain reads its pane
  first and refuses — naming what is showing — when Claude Code's trust dialog,
  a numbered choice, an Enter/Esc dialog or the session-rating prompt is on
  screen. `aisquare captain` attaches so you can answer it.
- **One captain per home.** A second `aisquare captain` attaches; the fleet
  refuses a second row and refuses `captain` on a project.

## After a reboot

`aisquare captain` finds the captain's tmux server gone. When its socket file
is gone too (a reboot sweeps `/tmp`), the stale row is ended and a fresh captain
starts — the folder is already trusted, so it comes straight up. When the file
is still there but nothing answers (a `kill-server` leaves it), both
`aisquare captain` and `aisquare captain "text"` refuse at once and name the
one command that may decide it is gone:

```sh
aisquare fleet reap -P <home> --server-down
aisquare captain
```

## Phase 2

Named here so nobody re-derives them:

- **Pull requests waiting on you** through a `gh` provider in the queue's
  sources (the queue has the item kind; the provider is not wired).
- **The captain persona in project pickers**: today every bundled persona shows
  in a project's spawn dialog, the captain's included; hiding a captain-only
  persona is a catalogue rule for every picker.
- **The phone over the LAN**: `https` for an iPhone or a Wi-Fi-only phone; the
  page binds loopback only until the owner says otherwise.
- **The queue's memory outside its window**: two near-identical asks more than
  ten minutes apart are two items.
- **The TOTP identity gate** (plan section 3).
- **Pre-trusting the brain folder** in Claude Code's config, so a fresh captain
  never shows the trust dialog — your call, not aisquare's.
