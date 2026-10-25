# Handoff: P0 reliability train — every prompt reaches its agent

Status: draft handoff, 2026-10-23. Base `main` @ b26dd140 (includes #201, #240, #267);
PR #272 (head 2d80fd5c) merged 2026-10-23; references to `cli/ui/terminal.py` are to its version.
Branch: `rc/p0-reliability`. Path when committed: `docs/plans/handoff-p0-reliability.md`.

> **Base update (2026-10-23).** #272 merged at 362dd5b0 and #273 refreshed `uv.lock`; `main` is now 09f48563. References marked (#272) are to `main`. Other `file:line` references were taken on b26dd140 and still hold on `main` (#272 changed only `cli/ui/*`, `core/tmux.py`, `core/tmux_control.py` and `core/spawn.py`, whose references here are already updated).

This document is a work order for coding agents. Commands are references (inline
code, or `text` fences), never scripts for the documented-commands guard.

**What releasing this train means.** A prompt the fleet sends — a spawn's first
message, a restart's or a switch's resume line, a `--task` kickoff, a `fleet tell`,
a manager nudge — either reaches the agent and starts a turn, or the board says it
did not, within a bounded time. Nothing is typed into a human's half-written draft.
An idle agent reads `waiting`, not `working`. A coder that claims a task sees its
contract. A usage limit moves an agent once, to an account that can take it. Turning
prompt capture off stops prompt capture. `uv run --frozen` works. What it removes: an
agent whose first prompt was typed but not submitted sits idle until someone presses
Enter, and because the board reads it as `working`, nothing else in the fleet tries to
reach it. A manager stuck this way stalls its whole project.

## How to run this train with agents

### Workstreams

| ID | Title | Issues | Size | Depends on | Parallel-safe? | Hot files |
|---|---|---|---|---|---|---|
| W0 | Spike: delivery facts on a live box | none (results go into W1/W2/W3) | S (1 day) | — | yes | none; scratch fleet only |
| W1 | Verified prompt delivery | #122 (absorbs the unsent-first-prompt bug), #123, #74 | L, three slices | W0 for 1a | 1b and 1c yes; 1a owns the migration | `services/fleet.py`, `services/team.py`, `core/store.py`, `cli/launch.py`, `core/harness.py`, `cli/task.py` |
| W2 | Delivery guards | #276 | M | W1a (the door), W1b | after W1a | `services/fleet.py` (`tell`, `nudge_manager`), `services/team.py` (`hook_stop`), `cli/ui/terminal.py` (#272), `services/captain/screen.py` |
| W3 | Account picks and switches | #277 | M | none; coordinate `fleet.switch` with W1a | yes, with the file notes below | `services/claude_accounts.py`, `services/hooks.py`, `services/team.py` (`hook_stop_failure`, `_limited_text`), `services/fleet.py` (`switch`), `core/harness.py`, `core/config.py` |
| W4 | `capture.enabled = false` stops capture | #268 | S | — | yes | `services/hooks.py` (`capture_prompt`) |
| W5 | CI checks that `uv.lock` matches `pyproject.toml` (contents fixed by #273) | #278 | S | — | yes | `uv.lock`, `.github/workflows/ci.yml` |

**Order.** Day 1: W0, W4 and W5 in parallel (W4 and W5 can merge on their own). Then
W1b and W1c in parallel with W1a, which starts once W0 has answered its questions.
W2 starts when W1a's delivery door has merged to the branch. W3 runs alongside W1
from day 1; it rebases on W1a for `services/fleet.py` and `core/harness.py`.

**File-overlap hotspots.**

- `services/fleet.py` (9,113 lines): W1a owns `type_then_enter` (:439), `_type_prompt`
  (:4369), the spawn argv assembly (:3285-3354) and `_respawn` (:7074); W1b owns
  `_derive` (:2438); W2 owns `tell` (:4848) and `nudge_manager` (:8991); W3 owns the
  top of `switch` (:6829-6960). Keep each change inside those functions; new helpers
  go in a new module (`services/delivery.py`, below) rather than into `fleet.py`.
- `services/team.py`: W1b owns `hook_session_start` (:1870); W1a owns
  `hook_prompt_heartbeat` (:1972); W2 owns `hook_stop` (:2083); W3 owns
  `hook_stop_failure` (:2394) and `_limited_text` (:2521); W1c adds a task-feedback
  helper beside `stopped_because` (:1507).
- `core/harness.py`: W1a/W1c edit the coder and manager cycles (:984-993,
  :1023-1061); W3 edits the manager cycle's switch sentence (:1051-1054). One
  coder lands harness text; the other rebases.
- `core/store.py`: only W1a adds a migration (schema version 34 as built; 36 since the
  sync of main 8eb18a8a, whose #38 and #346 hold 34 and 35). Anyone else who
  needs one takes the next version after W1a merges.

**Agents.** One coder per workstream (W1 may run three: 1a, 1b, 1c). One reviewer
per PR, read-only (`REVIEWER_ARGS`), who checks: the failing-first test exists and
fails on `main`; every typed write still goes through one door; no new path presses
Enter twice except under W1a's rule; docs and CHANGELOG match the code. Reviewers
re-read `_type_prompt`'s and `pasted_unsent`'s docstrings first: they record why the
current code refuses a second Enter (review of #259, round 10), and W1a changes that
rule on purpose.

### Definition of done (every workstream)

Production-ready, not "code exists":

- The reported failure is reproduced by a test that fails without the change
  (CONTRIBUTING's "prove a test can fail").
- macOS and Linux; WSL where the area is platform-sensitive (tmux input, sockets).
- `make check` green: ruff, mypy strict, pytest on 3.11–3.13.
- User docs updated under `docs/` (README stays under 250 lines).
- A CHANGELOG `[Unreleased]` entry in the repo's existing voice.
- Config keys documented in `docs/reference.md` (which has no config table today:
  add a "Configuration keys" section there and link it from `docs/fleet.md`).
- New commands added to `IMPLEMENTED` in `tests/test_stubs.py`.
- Services return data; the CLI prints and honours `--json`.
- Measured before/after numbers for every item about minutes or latency.

## W0 — Spike: delivery facts on a live box

Four design choices below depend on Claude Code behaviour that is either observed
by third parties or undocumented. Measure them once, on the current Claude Code
release and on the oldest version `aisquare doctor` accepts, with a scratch
`AISQUARE_HOME` and a scratch tmux socket. Record results in this
document under "W0 results" before W1a's design freezes.

1. **The stranded Enter.** Spawn 20 agents with a one-line `--prompt` on today's
   `main`. Count prompts still in the composer after 60 s. Then measure, per agent,
   the seconds from the input box first drawn (`screen.input_box_at` non-null) to the
   first Enter that submits. A third-party guide reports that Claude Code 2.1.277 and
   later ignores Enter for roughly 30–50 s after the box appears (getcodeman.com,
   updated 2026-09-25). Confirm or refute; this is the "before" number for W1a.
2. **A first prompt on the command line.** Confirm on both versions that
   `claude "<prompt>"` and `claude --resume <transcript path> "<prompt>"` submit the
   prompt as the first turn in an interactive session (documented in the CLI
   reference), that `UserPromptSubmit` fires for it, what happens when the folder-trust
   question is up, and whether `--` before a prompt that starts with `-` is accepted.
   Also run the cross-account leg `docs/plans/claude-accounts.md` §10 still marks
   unverified: resume a transcript written under one `CLAUDE_CONFIG_DIR` from another.
3. **The inbox socket.** Claude Code 2.1.224+ binds a per-session inbox socket and
   exports its path to hooks as `CLAUDE_CODE_MESSAGING_SOCKET`; an idle session starts
   a turn with a message, a busy one reads it between tool calls (cross-session
   messaging docs). The docs invite "a script or hook to post into a session" and
   document the optional auth line `{"type":"auth","token":…}`, but not the message
   line itself. Record the message schema, what the receiving pane shows, the inbound
   decision for a fleet agent in `--permission-mode auto` and in `bypassPermissions`,
   and whether `--settings '{"crossSessionInbound":"accept"}'` makes delivery
   unconditional. If the schema cannot be pinned with confidence, W1a ships without
   the socket tier and this stays an open question.
4. **The usage payload.** Capture one full `GET /api/oauth/usage` answer (token
   redacted) from an account that has hit a per-family limit, to see which of
   `seven_day_opus`, `seven_day_sonnet`, `limits[]` and `*.locked_reason` carry it. Add
   it beside `LIVE_USAGE` in `tests/test_claude_accounts.py:51`.

Output: a "W0 results" section in this document and the fixtures. No product code.

### W0 results (2026-10-23)

Measured on macOS 27 (arm64) with tmux 3.8, on `rc/p0-reliability` @ 6393998e (`main`
09f48563 plus this document).

**Versions.** Claude Code 2.1.309, the current release (native install), and 2.1.223,
installed from npm into a scratch prefix with the auto-updater off. `aisquare doctor`
has no Claude Code floor: `diagnostics.claude_code_version`
(`services/diagnostics.py:809`) only reads the version for its detail line. 2.1.223
is the oldest version the fleet relies on: `ResumeSpec` (`services/fleet.py:232`)
needs `--resume <transcript path>`, "Claude Code ≥ 2.1.223".

**Scale.** Reduced, per the owner's decision of 2026-10-23: at most 5 Claude sessions
at a time, about 20 minutes of live runs, all on the account with the most headroom
(slot 2, by `aisquare accounts usage`); the cross-account leg also used slot 3. The
runs used a scratch `AISQUARE_HOME`, scratch tmux sockets (`w0spike` for the fleet,
`w0probe` for the probes), a clean environment (no `TMUX`, none of this session's
`CLAUDE_CODE_*`) and a scratch git folder. The real `asq` socket,
`~/.aisquare`'s store and the running fleet were not touched. Under `~/.aisquare`
only Claude Code's own files changed: the scratch folder's transcripts in slots 2 and
3, and that folder's trust, written by Claude Code when its trust dialog was answered.
The full 20-agent run happens once all four trains are on `main` (owner decision,
2026-10-23).

**Method.** A probe launches `claude` in a tmux window and reads the pane every 100 ms
with `services/captain/screen.py` (`input_box_at`, `modal_showing`). "Submitted" means
`UserPromptSubmit` fired. A hook passed with `--settings` appends its timestamp,
alongside the account's own `asq` hooks. Every prompt was "Reply with the single word
PONG and the code <token>."

#### 1. The stranded Enter

Through the fleet (`fleet spawn coder --no-worktree --prompt <one line>`, today's code,
2.1.309), prompts still in the composer 60 s later:

| Run | Still in the composer |
|---|---|
| 5 spawns, one after another | 0 of 5 |
| `fleet restart` of those 5 (the resume line is typed) | 0 of 5 |
| 5 spawns started together | 0 of 5 |

The probe, with times from launch or from the input box first drawn:

| Case | 2.1.309 | 2.1.223 |
|---|---|---|
| Launch to box, one launch every 0.5 s | 0.60–1.56 s | 0.71–1.19 s |
| Launch to box, five launches within 0.2 s | 1.72–2.75 s | — |
| Paste once the box is drawn, Enter 0.1 s after the text renders | the first Enter submits, 5 of 5: box to Enter 0.16–0.30 s, box to `UserPromptSubmit` 0.22–0.39 s | the first Enter submits, 5 of 5: 0.28–0.33 s and 0.33–0.49 s |
| Paste 0.3 s after launch (no box yet), Enter once the text renders | the first Enter submits, 5 of 5 | — |
| Paste 0.3 s and Enter 0.4 s after launch, both before the box | **unsent, 5 of 5**, still in the composer at 65 s | **unsent, 5 of 5** |
| One more Enter, at 65 s, into those composers | submitted, 5 of 5; `UserPromptSubmit` 0.06–0.10 s after it | — |

- **The 30–50 s window is refuted** on both versions: once the box is drawn, the first
  Enter is taken within 0.3 s.
- **A prompt is stranded by an Enter that arrives before Claude Code draws its input
  box.** That Enter is dropped. The pasted text is kept and shows in the composer
  once the box appears. This happened 10 of 10 times, on both versions.
- `_type_prompt` (`services/fleet.py:4369`) types once the pane's foreground process is
  no longer the launcher, plus `_PROMPT_SETTLE` (2.0 s). It does not wait for the box.
  The box comes 0.6–1.6 s after a lone launch and 1.7–2.75 s after five concurrent
  ones, so the margin is about half a second and goes negative under load. At this
  scale nothing was stranded; at 20 agents, or on a busy box, the Enter can beat the
  box. W1a's readiness should be `input_box_at` non-null, not a fixed settle.
- One Enter after the box submitted every stranded prompt here. W1a's re-press rule
  (press again only while our text is still in the composer) fits every case measured.
  No case needed a third press.
- W1a's "before" number at this scale: 0 of 15 through the fleet. The mechanism
  reproduced directly: 10 of 10.
- Field evidence from the manager of this train (2026-10-23, about 16:40 UTC, 2.1.309,
  four accounts): seven coders spawned with `fleet spawn coder --task <n>` and no
  `--prompt` sat at an empty composer for about 4 minutes. Nothing had been typed: this
  is #122's `--task` gap, not a stranded Enter. All the while, `fleet ls` read every
  one of them as `working` (#123). Each kickoff, typed by hand more than 3 minutes after
  the box appeared, submitted on its first Enter.

#### 2. A first prompt on the command line

These results hold on both versions unless marked, in a trusted folder, with
`--permission-mode auto`.

- `claude "<prompt>"`: `SessionStart` (`source: startup`) 0.5–0.6 s after launch,
  then `UserPromptSubmit` with the prompt at 1.2–1.3 s, and the first turn runs.
- `claude --resume <transcript path> "<prompt>"`: `SessionStart` has
  `source: resume` and the transcript's own session id. `UserPromptSubmit` fires at
  1.9 s (2.1.309) and 2.2 s (2.1.223), and the turn runs after the old history.
- `claude -- "-…"` is accepted, and the prompt arrives with its dash. Without `--`,
  2.1.309 exits with `error: unknown option '-starts with a dash'`. W1a should put `--`
  before every positional prompt.
- A prompt with a newline on argv arrives as one message, newline kept (2.1.309).
- **The folder-trust question.** The argv prompt waits behind it, and no hook fires.
  Once the folder is trusted, `SessionStart` and `UserPromptSubmit` follow within 0.5 s
  and the prompt runs as the first turn. The dialog differs between the versions:
  2.1.309 lists `No, exit` first and highlights it, while 2.1.223 lists
  `1. Yes, I trust this folder` first and highlights that. A stray Enter therefore
  exits 2.1.309 but trusts the folder on 2.1.223. With the prompt on argv, nothing is
  typed into the dialog.
- **Cross-account (`docs/plans/claude-accounts.md` §10).** A transcript written under
  slot 2's `CLAUDE_CONFIG_DIR` and resumed by path under slot 3 works on both versions:
  same session id, history present, and the prompt runs. New records are appended to
  the original file in slot 2's `projects/`, and nothing is written under slot 3.
  **However, `SessionStart` hands hooks a `transcript_path` in slot 3's
  `projects/<slug>/<id>.jsonl`, a file that is never created.** `UserPromptSubmit`
  hands them the original path. Checked against the scratch store, this has three
  consequences on today's code:
  - `hook_session_start` stores the missing path on `team_session`. Its `account`,
    slot 3, is right.
  - Every later prompt compares the original path with the stored one, so
    `_shared_row_banner` (`services/team.py:1808`) injects a false "being written by
    TWO live agents" warning.
  - The agent's next `fleet restart` or `fleet switch` starts fresh, because `_respawn`
    resumes only when `team_session.transcript_path` is a file
    (`services/fleet.py:7125-7130`).

  Every resumed `fleet switch` and every usage-limit hand-over takes this path. It is
  not fixed here (no product code); routed to the manager for W1a and W3.

#### 3. The inbox socket

- **Where.** 2.1.309 binds `$CLAUDE_CODE_TMPDIR/cc-socks/<claude pid>.sock` (mode
  0600). When that path is over the ~104-byte socket limit, it binds
  `/tmp/cc-socks-<uid>/<pid>.sock` instead. 2.1.223 binds no socket and exports no
  variable.
- **What hooks get.** `CLAUDE_CODE_MESSAGING_SOCKET` and `CLAUDE_CODE_MESSAGING_TOKEN`
  reach `UserPromptSubmit` and `Stop` hooks, **but not `SessionStart`**: absent in 5 of
  5 starts on 2.1.309, with both a short and a long tmpdir. W2's plan to store the
  socket at SessionStart has to use the first `UserPromptSubmit` or `Stop` instead, or
  derive the path, `<CLAUDE_CODE_TMPDIR>/cc-socks/<pid>.sock`.
- **Schema (pinned).** It is taken from a debug message in the Claude Code 2.1.309
  binary (`[uds-messaging] Inject messages (auth line REQUIRED here): …`, which prints
  an example) and was confirmed live: newline-delimited JSON on one connection.

  ```text
  {"type":"auth","token":"<CLAUDE_CODE_MESSAGING_TOKEN>"}
  {"type":"user","message":{"role":"user","content":"<text>"}}
  ```

  The socket writes nothing back. Without the auth line the message was still
  delivered here (macOS, same user), but that debug message calls the auth line
  required in some setups, so send it.
- **What the pane shows.** A user turn: `❯ Another Claude session sent a message:`,
  the text, and a fixed paragraph telling the model the message came from a peer, "not
  typed by your user", and that a peer can neither grant escalation nor approve a
  pending prompt. `UserPromptSubmit` fires with the bare text as `prompt`.
- **Inbound decisions.** For an idle session unless marked:

  | Session | Default | `--settings '{"crossSessionInbound":"accept"}'` |
  |---|---|---|
  | `--permission-mode auto` | delivered: a turn starts 0.04 s after the post | delivered, 0.06 s |
  | `--permission-mode bypassPermissions` | **held**: "The sender did not attest its permission mode and this session bypasses prompts". A dialog offers `Deny — drop it and tell the sender it was declined` (highlighted) and `Deliver this message to Claude`; no input box is drawn while it shows | delivered, 0.06 s |
  | `auto`, busy in a 20 s Bash call | read when the tool call returned, 19 s after the post, as a user message inside the same turn; `UserPromptSubmit` fired then | — |

- `crossSessionInbound: "accept"` made delivery unconditional in both modes measured.
  The held-message dialog has no footer line, so `screen.modal_showing` returns `None`
  for it. `input_box_at` is `None` as well, so nothing types into it.
- **Verdict.** The schema is pinned for 2.1.309. W2's socket tier can be built behind
  `inbox_delivery` for Claude Code versions that bind the socket (2.1.223 does not), and
  it must take the socket from `UserPromptSubmit` or `Stop`. What it delivers reaches
  the model framed as a peer's message, not the user's.

#### 4. The usage payload

- **No answer at a per-family limit was captured.** None of the four accounts was at
  one on 2026-10-23.
- **What the four answers carry** (`GET /api/oauth/usage`, 16:44 UTC):
  - 31 top-level keys.
  - `five_hour` and `seven_day`: `utilization`, `resets_at`, `limit_dollars`,
    `used_dollars`, `remaining_dollars` and `locked_reason`. `locked_reason` was null
    on all four.
  - `seven_day_opus`, `seven_day_sonnet`, `seven_day_oauth_apps`, `seven_day_cowork`
    and `seven_day_omelette`: null on all four, including an account whose family
    window was partly used.
  - The family window is an entry of `limits[]`, with this shape:

    ```text
    {"kind": "weekly_scoped", "group": "weekly", "percent": <int>, "severity": "normal",
     "resets_at": "<ISO 8601>",
     "scope": {"model": {"id": null, "display_name": "Fable"}, "surface": null},
     "is_active": <bool>}
    ```

    It sits beside `{"kind": "session", "group": "session", …}` and
    `{"kind": "weekly_all", "group": "weekly", …}`. On every answer, `is_active` was
    true on the fuller of `session` and `weekly_all`, so it appears to mark the window
    that binds now. `weekly_scoped_shares` gives the scoped window's use and allowance
    as percentages of the week, and points at its entry by `limit_index`.
  - `extra_usage`, `spend`, `pace` and seventeen other keys with opaque names, all
    null or off, carry nothing a pick needs. `seven_day_breakdown` is a dict under a
    `seven_day_` key but not a window (`as_of`, `window_started_at`, `rows[]` of
    per-product shares).
- **For W3:** read a family's window from `limits[]` (`kind == "weekly_scoped"`,
  `scope.model.display_name`), not from `seven_day_<family>`. The entry names a
  family, not a model id. A reader of "every `seven_day_*` key that holds a dict"
  would take `seven_day_breakdown` for a window, so it must skip it.
- The fixture is `LIVE_USAGE_FULL` in `tests/test_claude_accounts.py`, beside
  `LIVE_USAGE`. Its structure was taken from a real answer of 2026-10-23; its values
  are invented, and the keys a pick does not need are left out (owner's call, via the
  manager). The test with it shows that today's parser reads the two windows and
  ignores the rest.

#### Also seen

- **`busy_quiet_seconds = 30` holds on 2.1.309 (W1b).** During a 25 s silent foreground
  Bash call the pane redrew at least every 1.1 s, because the spinner counts the
  seconds. Asked for a 40 s `sleep`, 2.1.309 ran it as a background shell and ended the
  turn ("1 shell still running"). Asked again, it declined to run it in the
  foreground.
- **Dim text sits in an empty composer.** After a turn it is a suggestion
  (`check the board again`, `aisquare board`); in a fresh session it is a placeholder
  (`Try "edit <filepath> to..."`). Both are SGR 2 (`ESC[2m`), after the mark and a
  no-break space (`❯` U+00A0). A reader that strips escapes first reads them as drafts,
  which matters for W2's `composer_text`.
- `--permission-mode bypassPermissions` opens a one-time `Yes, I accept` warning on an
  account that has never accepted it. `--settings '{"skipDangerousModePermissionPrompt": true}'`
  skips it for that one session.
- `fleet spawn` started in parallel for a project with no tmux session yet: 3 of 5
  failed with `tmux could not start the window: duplicate session: <codename>`.
- `uv lock --check` fails on this branch: `pyproject.toml`'s `voice` extra is missing
  from `uv.lock` (W5).

## W1 — Verified prompt delivery

### Problem

- **First prompts typed but not submitted (#122).** Every first message the fleet
  sends — a spawn's `--prompt`, the resume line after `fleet restart` or `fleet switch`
  (including the automatic usage-limit hand-over: "You are <label>, moved to another
  Claude account…", `services/fleet.py:8121`), a fresh hand-off — is pasted with one
  Enter about two seconds after the launcher leaves the foreground, and nothing checks
  that a turn started. If Claude Code is not yet accepting input, the Enter is dropped
  and the text stays in the input box. A third-party guide reports that Claude Code
  2.1.277 and later ignores Enter for roughly 30–50 s after the box appears (W0
  checks). Observed in multi-agent use after restarts, switches and hand-overs. The
  agent sits idle until someone presses Enter; a manager stuck this way stalls its
  whole project. The prior art below points at two remedies: pass the first prompt on
  the command line, and for anything still typed, press Enter as its own step after
  the text renders, confirm a turn started, and press again only while the text is
  still waiting.
- **`--task` types nothing (#122).** `fleet spawn coder --task <id>` — the exact form
  the manager's briefing names — types nothing, so the coder waits at an empty prompt
  (the original report on #122).
- **Idle agents read `working` (#123).** A just-spawned idle agent reads `working` for
  up to 30 minutes (`_STALE_AFTER`), so `fleet tell` files a note it never reads. The
  same misreading holds an agent with an unsent first prompt at `working`: the text
  sits in its composer while the board says it is busy.
- **The contract stays behind (#74).** A coder that claims a task sees its title only;
  the contract (`--detail`) is a separate `task show` nobody names. Humans paste it.

### Where the code is today (b26dd140)

- `services/fleet.py:439-466` `type_then_enter`: one bracketed paste, then one Enter
  in the next tmux call. No render check, no check that a turn started. Its docstring
  and `pasted_unsent` (:468) rule out a second Enter.
- `services/fleet.py:4369-4466` `_type_prompt`: "ready" is the pane's foreground
  process no longer being the launcher, or any scrollback (:4432); then a fixed
  `_PROMPT_SETTLE = 2.0` (:151, slept at :4439); past `PROMPT_TIMEOUT = 20.0` (:137)
  a one-line prompt is typed anyway. Every first prompt goes through it: `spawn`
  (:3588-3592), `restart` and `switch` via `_respawn` (:7074; resume lines
  `_restart_prompt` :8090 and `_resume_prompt` :8111, fresh hand-off `_handoff_prompt`
  :8131), and `hand_off` (:7701, prompt at :7825). The remote's paste
  (`services/remote_actions.py:1125`) shares `type_then_enter`.
- `services/fleet.py:3588-3592`: with `--task` and no `--prompt`, `typed = … if prompt
  else None` — nothing is typed. `cli/fleet.py:187` (`--task`), `:204` (`--prompt`).
- The agent's argv is assembled at `services/fleet.py:3285-3354` and exec'd by
  `cli/launch.py:621` (`[binary, *profile.args, *role_args, *persona_args, *ctx.args,
  *pinned_id]`). No prompt travels on the command line today.
- `core/harness.py:1040`: the manager cycle says `fleet spawn coder --label
  coder-<purpose> --task <id> --as <sid>`, no `--prompt`.
- `services/team.py:3381`: an agent spawned with `--task` gets an ASSIGNED TO YOU
  block in its SessionStart context naming `task claim` and `task show`. Context alone
  starts no turn, so the agent still waits at an empty prompt.
- #123: `core/store.py:2402` (`upsert_session` forces `state = 'working'` on
  conflict), `models.py:675` (`TeamSession.state` defaults to `working`),
  `services/team.py:1908` (SessionStart upsert). `services/fleet.py:2476-2520`
  (`_derive`) returns a fresh board state (`_STALE_AFTER = 30 min`,
  `services/team.py:61`) before it looks at pane activity. The captain already has
  the fix for its own role: `services/team.py:1934-1941` writes `waiting` at every
  non-compact SessionStart "or the row read busy for its whole fresh window".
- #74: `cli/task.py:39-56` `_emit_task` prints `id [status] title` for every verb,
  `task claim` (:196) and `task next --claim` (:230) included; `--detail` help is
  still "Longer description." (:66); `task show` (:147-184) prints the contract. The
  coder cycle (`core/harness.py:986-993`) says "Read the task's contract" and never
  names `task show`. `docs/orchestration.md:120` says "Nothing needs forwarding".
  `--json` (`cli/task.py:47-50`) and the MCP `task_next` (`services/mcp_server.py:245`)
  already carry `detail`. Reopen feedback disappears once a reopened task is claimed:
  `stopped_because` maps only `todo` to `task_reopened` (`services/team.py:1500-1504`).
- Reusable from #240: `services/captain/screen.py` reads Claude Code's pane by
  structure — `input_box_at` (:104), `box_idle` (:115, "a fresh claude draws its idle
  box before its first Stop hook"), `modal_showing` (:195). The captain's own first
  prompt (`services/captain/brain.py:445` `_wait_until_ready`) waits for the box, but
  its `_type` (:394) still pastes and presses one Enter with no acknowledgement.

### What to build

New module `services/delivery.py`: the one door every write into an agent goes
through. `fleet.py` keeps thin wrappers so the call sites and their tests keep their
names.

**1a. First prompts: on the command line, acknowledged.**

- `spawn(prompt=…)` writes the prompt to `$AISQUARE_HOME/run/prompts/<agent-id>.txt`
  (0600) and passes `aisquare launch … --first-prompt-file <path>`. The launcher reads
  and deletes the file, then appends the prompt as Claude Code's positional argument
  after `pinned_id` (with `--` first if W0 confirms Claude Code accepts it). A file,
  not an argv string, because the tmux client refuses a command over 16 KB
  (`MAX_IMSGSIZE`, "command too long"), and the window command is sent through it.
- The recorded `LaunchSpec` never holds prompt text: `_without_session_choice` drops
  `--first-prompt-file` from `recorded_args`, and `LaunchSpec.command` stores the flag
  with the path only. A restart replays no old prompt.
- Restart and switch pass their resume line the same way: `claude --resume <path>
  "<line>"`, documented as `claude -r "<session>" "query"`. A fresh hand-off passes
  `_handoff_prompt`'s multi-line text, which today is refused past the 20 s timeout.
- A binary that is not Claude Code (`claude_code=False` and no Claude-shaped name), or
  `[fleet] first_prompt = "type"`, keeps the typed path below.
- **Kickoff for `--task` (#122).** `spawn(task_id=…, prompt=None)` passes a kickoff
  built by `team_service.kickoff_prompt(task, role, label, session_id)`, which reuses
  `_assignment_lines` (`services/team.py:3381`) so the kickoff and the ASSIGNED TO YOU
  block never disagree: "You are coder-auth. <the block's lines>". A `--prompt` given
  with `--task` is appended after it. The manager cycle (`core/harness.py:1040`) adds:
  "`--task` delivers the task; add `--prompt` only for extra instructions."
- **Acknowledgement.** Migration to schema version 36 (34 as built, before the sync of
  main 8eb18a8a): `team_session.prompted_at`
  (set by `hook_prompt_heartbeat`, `services/team.py:1972`, on every `UserPromptSubmit`),
  `fleet_agent.first_prompt` (`pending` | `acked` | `unsent` | NULL) and
  `fleet_agent.first_prompt_at`. Spawn records `pending`; the heartbeat flips the
  bound row to `acked`. The CLI waits up to `[fleet] prompt_ack_seconds` (default
  45) and reports `acked` or `pending` on the receipt. A listing that finds a row
  `pending` past the timeout with an idle pane moves it to `unsent` once (compare-and-set
  in the store), emits `prompt_unsent` on the board and derives `attention` with the
  detail "first prompt not taken — `aisquare fleet tell <label> …` or restart it".
  `prompt_unsent` joins `MANAGER_WAKE_KINDS` (`services/team.py:133`): the manager has
  a decision to make.
- **Typed path (fallback, and for `tell` until W2's socket tier).** `deliver_typed()`:
  1. Ready: `_its_agent(facts)`; on tmux 3.7+, `#{bracket_paste_flag}` is 1 (the
     application has asked for bracketed paste; added in tmux 3.7); `input_box_at`
     non-null; `modal_showing` null; for a tell also `box_idle`.
  2. Paste (bracketed, as today).
  3. Render check: poll `capture` every 100 ms, up to 3 s, until the composer shows
     the head of the text or `[Pasted text #`.
  4. Wait 100 ms, then Enter as its own tmux call.
  5. Acknowledge: `prompted_at >= typed_at` for a hook-bound agent; for an agent with
     no hooks, the composer empty and a turn showing (`box_idle` false).
  6. Re-press, at most 8 times on the schedule 2, 3, 5, 5, 5, 10, 10, 10 s (60 s in
     all), **only while** no acknowledgement has come, the composer still holds the
     head of this text, and no dialog is drawn. Stop at once if the composer is empty,
     holds other text, or a dialog shows. This replaces the "never a second Enter" rule
     with a narrower one: a second Enter is pressed only when the first provably did
     not submit.
  7. Outcome: `yes` (acknowledged), `unsent` (text still in the composer; board note
     and `attention`), `maybe` (tmux did not answer). The `Typed` literal and
     `PaneWrite` keep their meanings.
- Receipts: `SpawnReceipt` and `SwitchReceipt` keep `prompt_typed` (JSON unchanged)
  and add `prompt_delivery` (`argv` | `typed` | `socket` | null) and `prompt_acked`
  (bool | null). `fleet spawn --json` prints both.

**1b. An idle agent reads `waiting` (#123).**

- Generalise `services/team.py:1934-1941` to every role: at SessionStart with
  `source != "compact"`, write `waiting` after the upsert. `UserPromptSubmit` writes
  `working` as it does now (:2067, :2071).
- `_derive` (`services/fleet.py:2476-2520`): a fresh board `working` is trusted while
  the pane has printed within `[fleet] busy_quiet_seconds` (default 30; Claude Code
  redraws its spinner every second during a turn). Past that, the pane is captured
  once and `box_idle` decides: idle box, `waiting` with the detail "idle (the board
  said working)"; otherwise `working`. This also covers a turn ended with Esc, which
  fires no Stop hook (hooks reference). The capture happens only for rows in that
  state, so the 2 s refresh (#272) pays nothing for healthy rows.

**1c. The contract reaches whoever claims it (#74).**

- `cli/task.py`: a claim renderer for `task claim` and `task next --claim` prints the
  title line, then the contract (`detail`), then the newest reopen feedback
  (`team_service.last_feedback(task)`, a new read beside `stopped_because`), then
  "full task: `aisquare task show <ref>`". `task next` without `--claim` prints the
  title and "has a contract" when `detail` is set. Every other verb is unchanged.
  `--json` is unchanged.
- `core/harness.py:986-993`: the coder cycle names `aisquare task show <id>`.
- `--detail` help: "The task's contract: objective · why · known · acceptance ·
  boundaries. Printed to whoever claims the task."
- `docs/orchestration.md:120`: replace "Nothing needs forwarding" with what holds: a
  claim prints the contract; the board lists titles.

### Acceptance criteria

- [ ] `fleet spawn coder --task <id>` with no `--prompt` starts a turn on the task;
      `prompt_acked` is true within `prompt_ack_seconds` on a live box.
- [ ] `fleet restart`, `fleet switch` (resumed and `--fresh`) and the automatic
      hand-over deliver their first line on the command line; 20 of 20 acknowledged in
      W0's repro on the current Claude Code release, against W0's "before" count.
- [ ] A typed delivery whose first Enter is swallowed is re-pressed and acknowledged;
      one whose composer was cleared or replaced by a human is never re-pressed.
- [ ] A first prompt never acknowledged reads `attention` with the detail above and
      writes one `prompt_unsent` event.
- [ ] No prompt text in `fleet_agent.launch_spec`; a restart replays no prompt.
- [ ] A spawned agent with no prompt reads `waiting` in `fleet ls` within
      `busy_quiet_seconds` of its idle box; `fleet tell` types into it.
- [ ] `task claim` and `task next --claim` print the contract and the newest reopen
      feedback; the coder cycle names `task show`.

### Tests

- `tests/test_fleet_service.py` (FakeTmux, fake clock): **failing first** —
  `test_a_swallowed_enter_is_pressed_again_until_the_turn_starts` (FakeTmux keeps the
  text in the composer after the first Enter); `test_a_composer_a_human_changed_is_never_pressed_again`;
  `test_spawn_with_a_task_and_no_prompt_passes_a_kickoff`; `test_the_first_prompt_rides_the_command_line_in_a_file_and_never_the_spec`;
  `test_a_restart_replays_no_first_prompt`; `test_an_unacknowledged_first_prompt_reads_attention_once`;
  `test_a_fresh_working_row_over_a_quiet_idle_pane_reads_waiting`.
- `tests/test_team.py`: SessionStart writes `waiting` for every role, not just the
  captain; `test_session_state_tracks_working_waiting_attention` (:657) extended;
  `UserPromptSubmit` stamps `prompted_at`.
- `tests/test_store.py`: migration 34 applies over a schema-33 store and is idempotent
  (`_converge_by_presence`).
- `tests/test_task_show_says_why_it_is_stopped.py` or a new
  `tests/test_a_claim_prints_its_contract.py`: **failing first** — the #74 repro
  (planner adds a task with `--detail`, coder claims it, output contains the contract
  and the reopen feedback).
- `tests/test_harness.py`: the coder cycle names `task show`; the manager cycle names
  what `--task` delivers.
- `tests/captain_screens.py` captures reused for the composer reader.

### Docs to update

`docs/fleet.md` (`fleet spawn` table row for `--prompt` and `--task`, :218-297;
`fleet tell` :333-350; Troubleshooting: "first prompt not taken"),
`docs/orchestration.md:120`, `docs/reference.md` (`UserPromptSubmit` row; new config
keys `[fleet] first_prompt`, `prompt_ack_seconds`, `busy_quiet_seconds`),
`docs/plans/claude-accounts.md` §10 (cross-account resume result from W0).

### Telemetry / measurement

- Baseline to measure first: on `main`, with a scratch `AISQUARE_HOME` and tmux
  socket, W0's 20-spawn repro plus 20 `fleet restart`s and 20 `fleet switch`es of idle
  agents. Count first lines still in the composer after 60 s and, for each, the
  minutes until a turn starts with no human help (poll the pane; cap at 60 min).
  Record both in the PR. After: the same repro, and in use `prompt_unsent` events per
  week from the board.
- Acknowledgement latency: `first_prompt_at` to `prompted_at`, median and p95, shown
  by a new doctor row `fleet-delivery` (last 7 days: acknowledged N of M).
- #123: time a just-spawned idle agent reads `working`: before up to 30 min (fake
  clock test), after at most `busy_quiet_seconds`.

### Risks and open questions

- The re-press rule reverses #259 round 10's ruling. The guard is "our text is still
  in the composer and no dialog is drawn"; the owner signs off on it.
- A prompt on the command line is visible to local users in `ps` for the life of the
  process, as with `claude "query"` itself. Acceptable on a single-user laptop; the
  owner decides whether a shared box needs the typed path by default.
- `busy_quiet_seconds = 30` assumes Claude Code redraws during every turn. W0 checks
  a long silent tool call; the screen check (`box_idle`) is the second signal.
- The explainability identity planner reads argv (`services/explainability.py:135`);
  a positional prompt must not confuse it. Pin with a test.

### Industry grounding

- Claude Code CLI reference (code.claude.com/docs/en/cli-reference, read
  2026-10-22): `claude "query"` starts an interactive session with an initial prompt;
  `claude -r "<session>" "query"` resumes with one; `--resume` takes a transcript path.
- obra/claude-session-driver issue #20 (github.com/obra/claude-session-driver/issues/20,
  opened 2026-05-31, fixed in v3.0.1, commit c7a0d5c): a fixed 0.1 s sleep before
  Enter left prompts pasted and unsubmitted; the fix confirms submission through the
  worker's `user_prompt_submit` hook event and re-sends Enter until it appears
  (defaults 10 s timeout, 2 s retry).
- Codeman guide "Claude Code ignores Enter from tmux send-keys"
  (getcodeman.com/guides/claude-code-enter-key-ignored-tmux, updated 2026-09-25):
  text and Enter as separate tmux calls 50–100 ms apart; read the `❯` line and press
  Enter again only while it still holds the start of the prompt; observed early-Enter
  rejection on 2.1.277+ (third-party observation, W0 verifies).
- tmux manual and source (github.com/tmux/tmux, master, read 2026-10-22):
  `paste-buffer -p` brackets only "if the application has requested bracketed paste
  mode"; `#{bracket_paste_flag}` exposes that flag (CHANGES, 3.7); the client refuses a
  command over `MAX_IMSGSIZE` (16,384 bytes) with "command too long" (`client.c`).

### Linked issues

#122 (rewritten, absorbs the unsent-first-prompt bug), #123 (comment), #74
(rewritten). Related: #136 (stale PR with a kickoff synthesizer — not revived; its
idea lands as `kickoff_prompt`).

## W2 — Delivery guards

### Problem

- **A nudge types into a human's draft.** `nudge_manager` types `NUDGE_TEXT` and
  presses Enter whenever the manager derives `waiting`, without reading the input box.
  A human halfway through a message to the manager in the asq pane gets the nudge
  appended to the draft, and the Enter sends both. Every board write that wakes the
  manager can do it: a task marked done, for review, reopened or blocked, a `result`
  or `question`, a note to the manager (`services/team.py:1378-1386`, `:1634`,
  `:1645`, `:1675`, `:1742`).
- **A tell to an idle or busy agent becomes a note nobody reads.** `fleet tell` types
  only into an agent that derives `waiting`; otherwise it files a board note, which the
  agent reads only with its next prompt. An idle agent that reads `working` (#123) gets
  no next prompt, and a non-manager whose turn ends is given nothing, so the note waits
  until a human prompts the agent. The fix: type into an idle agent; deliver at turn
  end to a busy one.

### Where the code is today (b26dd140)

- `services/fleet.py:8991-9046` `nudge_manager`: when the manager derives `waiting`
  (:9039) it runs `send_literal(NUDGE_TEXT)` then `send_keys(Enter)` (:9042-9043). It
  never reads the composer. `NUDGE_TEXT` is fixed (:129).
- `services/fleet.py:4848-4949` `tell`: types only when the agent derives `waiting`
  (:4904); otherwise files a note to its label (:4949). It never reads the composer.
  With #123, an idle agent often derives `working`, so the tell becomes a note.
- `services/team.py:2083-2160` `hook_stop`: only a manager can be kept going
  (`_manager_wakeup`); every other role is marked `waiting` (:2144) and gets nothing,
  so an addressed note waits for a prompt that may never come.
- Input from asq reaches a pane through one method on #272's branch:
  `cli/ui/terminal.py:2573` `_forward` ("The ONE way input reaches the agent").

### What to build

- **Composer reader.** Move `services/captain/screen.py` to `core/claude_screen.py`
  (it imports nothing of aisquare); keep `services/captain/screen.py` as a re-export
  so the captain and its tests are untouched. Add `composer_text(lines) -> str | None`:
  the text on the input line(s) after `❯`, with dim runs (SGR 2 and Claude Code's
  placeholder grey) dropped, read before escapes are stripped. `None` when no box is
  drawn.
- **Typing lease.** In `_forward` (#272), for `key`, `text` and `paste` input, touch
  `$AISQUARE_HOME/run/typing/<server-key>-<pane-id>` at most once a second (mtime
  only). `delivery.human_typing(agent)` is true while that mtime is within
  `[fleet] draft_quiet_seconds` (default 10).
- **Draft guard.** Every typed write (W1a's door, `nudge_manager`, `tell`, the
  remote's paste and persona attach through `tell`) refuses to type while
  `composer_text` is non-empty or `human_typing` is true. A nudge refused this way is
  recorded (`team_meta` key `nudge_deferred:<session>`); nothing is lost, because the
  human's own submit runs `UserPromptSubmit`, which delivers the delta. The next board
  write retries the nudge, and asq's refresh worker retries a deferred nudge once the
  composer is empty. A `tell` refused this way files its note and says why ("a draft
  is in its input box").
- **Tell to an idle agent.** Falls out of W1b: the agent derives `waiting`, and the
  door types and acknowledges it.
- **Delivery at turn end.** `hook_stop` for a non-manager session, with
  `[fleet] deliver_notes_at_turn_end = true` (default): when a note addressed to the
  session's label or role is past its cursor and `stop_hook_active` is false, return a
  `StopDecision` whose reason is the delta its next prompt would carry
  (`_wake_candidates` and `_render_delta`, as the manager wake does), and advance the
  cursor the same way. `max_continuations_per_hour` caps it as for managers. Only
  addressed notes trigger it; other events ride along.
- **Socket tier (only if W0 pins the schema).** `[fleet] inbox_delivery = false` by
  default. SessionStart stores `CLAUDE_CODE_MESSAGING_SOCKET` on the session row
  (migration to schema version 35, `team_session.inbox`). With the key on, `tell` and
  `nudge_manager` post to the inbox instead of typing: no composer, no Enter, delivered between tool
  calls when busy and as a new turn when idle. Fleet launches add
  `--settings '{"crossSessionInbound":"accept"}'` so a `bypassPermissions` agent does
  not hold the message behind an approval dialog. Typed delivery stays the fallback.

### Acceptance criteria

- [ ] With a draft in the manager's composer, a board write types nothing into it;
      the draft is intact; the delta reaches the manager with the human's submit.
- [ ] With the draft cleared, the deferred nudge is delivered within one refresh.
- [ ] `fleet tell` to an idle agent types and acknowledges; to a busy agent files a
      note that the agent receives at its turn end without a new prompt.
- [ ] A turn-end delivery never loops: `stop_hook_active` and the hourly cap hold.
- [ ] With `inbox_delivery = true` (when built), a tell to a busy agent is read before
      its turn ends, and nothing is typed into its pane.

### Tests

- `tests/test_fleet_service.py`: **failing first** —
  `test_a_nudge_never_types_into_a_draft` (FakeTmux screen with text after `❯`);
  `test_a_tell_never_types_into_a_draft_and_says_so`; `test_a_deferred_nudge_goes_out_once_the_composer_is_empty`;
  `test_a_dim_placeholder_is_not_a_draft` (real captures from `tests/captain_screens.py`).
- `tests/test_team.py`: **failing first** — `test_a_note_to_an_idle_coder_is_delivered_at_its_turn_end`;
  `test_turn_end_delivery_respects_stop_hook_active_and_the_cap`.
- `tests/test_terminal_control.py` (#272): `_forward` touches the lease, throttled.
- `tests/test_claude_screen.py`: `composer_text` over every capture in
  `tests/captain_screens.py`; the captain's own screen tests still pass unchanged.

### Docs to update

`docs/fleet.md` (`fleet tell`: drafts, turn-end delivery; "What runs where" for the
typing lease), `docs/orchestration.md` (how a manager is woken), `docs/reference.md`
(`Stop` row; keys `draft_quiet_seconds`, `deliver_notes_at_turn_end`,
`inbox_delivery`).

### Telemetry / measurement

- Baseline to measure first: on `main`, on a scratch fleet, (1) type a draft into the
  manager's pane in asq, then mark a coder's task done, and record whether the draft
  was submitted; (2) `fleet tell` an idle coder and a busy one, and record the time
  from the `note` event to the recipient's next turn, capped at 60 min (expected on
  `main`: no turn until a human prompts it).
- Note-to-read latency for notes addressed to a label: from the `note` event to the
  recipient's next turn (`prompted_at`, or the Stop decision). Before: unbounded (the
  note waits for a prompt that may never come). After: at most one turn, or immediate
  when idle.
- Drafts hit: count of deferred nudges and refused tells per day (debug log line), so
  the owner can see how often a draft would have been overwritten.

### Risks and open questions

- Placeholder styling can change between Claude Code versions. The reader leans to
  "a draft" (refuse) and the lease is a second signal; fixtures pin today's screens.
- A human typing through `tmux attach` leaves no lease; the composer reader covers it.
- The socket tier depends on a message schema the docs do not print. Owner call:
  build it behind the key once W0 pins it, or wait for documentation.

### Industry grounding

- Claude Code hooks reference (code.claude.com/docs/en/hooks, read 2026-10-22): a
  Stop hook's `decision: "block"` with a required `reason` keeps Claude going;
  `stop_hook_active` guards loops; Claude Code overrides the ninth consecutive block;
  Stop "does not run if the stoppage occurred due to a user interrupt".
- Claude Code cross-session messaging (code.claude.com/docs/en/cross-session-messaging,
  read 2026-10-22): a message to an idle session starts a turn; a busy session reads
  it between tool calls; inbound controls `accept | hold | refuse`; a script may post
  to the socket named by `CLAUDE_CODE_MESSAGING_SOCKET`.
- anthropics/claude-code issue #15553 (opened 2025-12-28, open, no maintainer reply):
  programmatic input into interactive mode lands as text without submitting; users
  report Enter swallowed by autocomplete and long prompts; no supported submit path.

### Linked issues

#276. Related: #122, #123 (W1), #214 (stop events, P1).

## W3 — Account picks and switches

### Problem

Three gaps in how the fleet picks and switches Claude accounts, read from the code
below: (a) a pick can send an agent to an account that refuses it at once — per-family
weekly windows and locked windows are not parsed, and nothing remembers a 429 — so the
agent hits a limit straight after the switch and is moved again; (b) one usage limit
can switch an agent twice, because the `limited` event both wakes the manager with a
`fleet switch` hint and starts the automatic hand-over, and once the hand-over has
finished the manager's switch moves the replacement again; (c) there is no way to
prefer an account whose weekly window resets soonest over the one with the most room,
so an account's unused weekly allowance can lapse at its reset while the fleet drains
accounts that still have days to run.

### Where the code is today (b26dd140)

- (a) Picks rank by the fuller of the five-hour and seven-day windows since #203
  (`services/claude_accounts.py:1384-1402` `headroom_percent`), so a spent week alone
  does not explain the bounce. What the parser ignores: only `five_hour` and
  `seven_day` are read (:817-818); `seven_day_opus` (present, null, in the recorded
  payload `tests/test_claude_accounts.py:62`), any `seven_day_sonnet`, `limits[]` and
  `five_hour.locked_reason` are dropped. An account whose `seven_day` is missing reads
  as its five-hour figure alone (:1400). Claude Code's errors reference documents
  separate Opus and Sonnet limits that "apply only to requests to that model family".
  Nothing records that an account just refused with a 429: the next pick asks the
  endpoint again.
- (b) `hook_stop_failure` (`services/team.py:2394`) emits `limited` with text naming
  `fleet switch` (`_limited_text` :2521-2542) and nudges the manager (:2479);
  `limited` is a wake kind (:133-147). Then `hooks.turn_failed`
  (`services/hooks.py:235`) starts the automatic hand-over in a detached worker
  (:278-326). The manager's cycle tells it to `fleet switch` a limited agent
  (`core/harness.py:1051-1054`). A manual switch is refused only while a hand-over is
  in flight (`services/fleet.py:6900`, `_handed_over`); once the automatic switch has
  finished, the manager's switch moves the replacement again. `HANDOVER_COOLDOWN`
  (10 min, `services/hooks.py:356`) brakes only the automatic path. A refused
  hand-over posts a plain `note` (`hook_note`), which wakes nobody.
- (c) No pick mode by reset time. `AccountsSettings` (`core/config.py:700-746`):
  `pick`, `switch_at = 85`, `on_limit = "switch"`, `wait_if_reset_within_minutes = 15`.

### What to build

- **(a) Read every window, and remember refusals.** `_window` runs over every
  top-level key that starts with `seven_day_` and holds a dict (model windows, keyed by
  family) and over `limits[]` (`kind`, `percent`). A non-null `locked_reason` on any
  window reads the account as 100 % used. `headroom_percent(reading, family=None)`
  takes the maximum of five-hour, week, and the family's window when the agent's family
  is known (`session.model` through `core.harness.MODEL_FAMILIES`). On every
  `rate_limit` StopFailure, record `account_limit:<slot>:<window>` = reset time in
  `team_meta` (window from `parse_limit_notice`: `session`, `weekly`, `opus`,
  `sonnet`). `headroom_choice` skips a candidate whose `session` or `weekly` mark, or
  the agent's family mark, is still in the future, and says so in its notes ("account 3
  skipped — weekly limit until Mon 00:00, refused 10:42"). No migration.
- **(b) One switch per limit.** `hooks.turn_failed` decides whether a hand-over will
  run (`on_limit`, the wait window, `somewhere_to_hand_over`) before
  `hook_stop_failure` writes the event, and passes `handing_over`. When true, the
  `limited` text says "moving it to another account automatically
  (`on_limit = "switch"`)" and the manager is not nudged. The worker's refusals become
  a `handover_failed` event (new, a wake kind) naming `fleet switch` or the reset;
  success stays `switched`. `fleet.switch` refuses, without `--force` (new flag), a row
  the automatic hand-over spawned less than `HANDOVER_COOLDOWN` ago ("moved 3 min ago by
  the usage-limit hand-over; `--force` moves it again"). The manager cycle's sentence
  becomes: "with `on_limit = switch` the fleet moves a limited agent itself; act on
  `handover_failed`, not on `limited`."
- **(c) `[accounts] prefer = "priority" | "reset-soonest"`**, default `priority`.
  With `reset-soonest`, the candidates under `switch_at` (after (a)) are ordered by
  their week's `resets_at`, earliest first, priority breaking ties. Applies to spawn
  picks and hand-overs. App settings gets the choice beside `switch_at`
  (`cli/ui/views/app_settings.py:456`); `accounts usage` shows each week's reset.

### Acceptance criteria

- [ ] An account whose family window or `locked_reason` says it is spent is never
      picked for an agent of that family, by spawn, manual switch or hand-over.
- [ ] An account that refused with a weekly 429 is skipped until the reset it named.
- [ ] One `limited` with `on_limit = switch` produces exactly one `switched` and no
      manager wake; a refused hand-over produces `handover_failed`, which wakes it.
- [ ] `fleet switch` of a fresh hand-over replacement is refused without `--force`.
- [ ] `prefer = "reset-soonest"` picks the under-the-line account whose week resets
      first; `priority` behaves as today.

### Tests

- `tests/test_usage_aware_accounts.py`: **failing first** —
  `test_a_spent_opus_week_is_no_room_for_an_opus_agent`; `test_a_locked_window_reads_full`;
  `test_an_account_that_refused_weekly_is_skipped_until_its_reset`;
  `test_reset_soonest_prefers_the_week_that_ends_first`.
- `tests/test_claude_accounts.py`: the W0 payload parses; unknown keys still ignored.
- `tests/test_fleet_service.py`: **failing first** —
  `test_a_limit_with_on_limit_switch_moves_the_agent_once_and_wakes_no_manager`
  (automatic switch, then the manager's switch is refused);
  `test_force_moves_a_fresh_replacement_again`.
- `tests/test_team.py`: `limited` text with and without `handing_over`;
  `handover_failed` is in `MANAGER_WAKE_KINDS`.
- `tests/test_harness.py`: the manager cycle's switch sentence.

### Docs to update

`docs/fleet.md` Accounts (:735-958: per-family limits, the refusal memory, `--force`,
`prefer`), `docs/orchestration.md` (several accounts, :420-440), `docs/reference.md`
(`StopFailure` row, `[accounts] prefer`), `docs/plans/claude-accounts.md` §5 and §10.

### Telemetry / measurement

- Baseline to measure first: on `main`, in a test with a fake clock, faked usage
  readings and a faked `rate_limit` StopFailure (or on a scratch fleet with two
  accounts), run one limit with `on_limit = switch` and a manager that follows its
  cycle, and count `switched` events per label. Separately, pick an account for an
  Opus agent when one candidate's `seven_day_opus` reads full, and record which account
  is chosen.
- Double switches: count of two `switched` events for one label within 10 minutes.
- Bounces: a `switched` followed by a `limited` on the target account within 10
  minutes. Both read from the board; after, zero in the tests and over a week of use
  on a fleet with several accounts.

### Risks and open questions

- The usage endpoint is undocumented (`docs/plans/claude-accounts.md` §5); W0's
  payload decides which fields are real. The 429 memory works whatever the endpoint
  says.
- `reset-soonest` can drain an account that someone needs interactively later in the
  day. Default stays `priority`.

### Industry grounding

- Claude Code errors reference (code.claude.com/docs/en/errors, "You've hit your
  session limit", read 2026-10-22): four limit messages — session, weekly, Opus,
  Sonnet; session and weekly are shared across models, Opus and Sonnet apply per
  family; usage counts against session and weekly at the same time.
- Anthropic Help Center, "What is the Max plan?" (support.claude.com/en/articles/
  11049741, "updated over a week ago" on 2026-10-22): the session limit resets every
  five hours; the weekly limit applies across all models and resets at a fixed time
  assigned to the account; other caps may apply "at our discretion".
- Claude Code status line docs (code.claude.com/docs/en/statusline): the status line
  gets `rate_limits.five_hour` and `rate_limits.seven_day` (plus a gateway's spend
  limit), each possibly absent — no per-family window, which is why the 429 memory
  matters.

### Linked issues

#277. Related: #198 (shared account state, P2), #263 (macOS
Keychain, shipped 0.8.1).

## W4 — `capture.enabled = false` stops prompt capture

### Problem

#268 (QA-09): with `capture.enabled = false`, `UserPromptSubmit` still
stores every prompt and `aisquare log` lists it. Someone who turns capture off has
every reason to believe their prompts are not stored.

### Where the code is today (b26dd140)

- `core/config.py:36-39` `CaptureSettings.enabled = True` (described as "the
  background capture pipeline", which is `services/capture.py`, all stubs); version-1
  default at `:470`. Nothing in `src/` reads the key.
- `cli/hook.py:142-159` → `services/hooks.py:167` `prompt_submitted` →
  `capture_prompt` (:432): `store.add_prompt` (:473) and the Explainability spool
  `insights.record_prompt` (:475) run unconditionally; then the CI consult
  (`ci_augment.for_prompt`, which sends the prompt when the CI experiment is on) and
  the turn's metrics row.

### What to build

- `capture_prompt` reads `capture.enabled` once per call. Off: no `add_prompt`, no
  `record_prompt`. The turn row is still opened without text; the teammate delta is
  still returned (it is coordination, not capture).
- A config that cannot be read records nothing and writes one stderr line (privacy
  fails closed, unlike the house default for other settings). Owner confirms.
- The CI consult with capture off: proposed to keep running, because the CI
  experiment is its own opt-in; the owner decides (open question below).
- `aisquare config set capture.enabled false` prints one line saying what stops and
  what does not.

### Acceptance criteria

- [ ] The #268 repro prints `0`.
- [ ] With capture off, no prompt row and no spooled prompt; the delta still arrives.
- [ ] With capture on, behaviour is byte-identical to today.

### Tests

`tests/test_hooks.py`: **failing first** — `test_capture_off_stores_no_prompt_and_spools_none`
(the #268 repro through the CLI); `test_capture_off_still_delivers_the_teammate_delta`;
`test_an_unreadable_config_records_no_prompt`.

### Docs to update

`docs/memory.md` (after "aisquare log", :43-49), `docs/reference.md` (the
`UserPromptSubmit` row, :12, and the new config section),
`docs/connecting-your-agents-to-explainability.md` (:220-226, what capture off stops).

### Telemetry / measurement

None beyond the test: the fix is binary. Report the repro's before (`1`) and after
(`0`) in the PR.

### Risks and open questions

- Does capture off also stop the CI consult, which sends prompt text off the machine
  when the experiment is on? Proposed: no, documented; owner call.
- W1a's command-line prompt must not become a second capture path: W1a's acceptance
  forbids prompt text in `launch_spec`.

### Industry grounding

- Claude Code monitoring docs (code.claude.com/docs/en/monitoring-usage, read
  2026-10-22): prompt text is redacted by default (`OTEL_LOG_USER_PROMPTS` "default:
  disabled"); recording it is an explicit opt-in.
- Claude Code data usage docs (code.claude.com/docs/en/data-usage): telemetry has a
  one-variable opt-out (`DISABLE_TELEMETRY`), and `DO_NOT_TRACK` is honoured for
  surveys; metrics "never include your code, prompts, or file paths".

### Linked issues

#268 (comment with this plan).

## W5 — CI checks that `uv.lock` matches `pyproject.toml`

### Problem

`uv.lock` drifted from `pyproject.toml` for several days: `pyyaml` (required since the
persona core, #201), `types-pyyaml`, `anthropic` (dev), `jiter`, `docstring-parser` and
`sniffio` were missing, so `uv run --frozen` raised `ModuleNotFoundError: yaml` on every
persona path. Dependabot's #273 regenerated the lock and merged on 2026-10-23
(09f48563), so the contents are right again. Nothing would catch the next drift: CI
installs with pip and `ci/constraints.txt` (`Makefile` `install`), and no step runs
`uv lock --check`.

### What to build

- Add a CI step to `.github/workflows/ci.yml`: install uv (action pinned like the
  others) and run `uv lock --check`.
- `make check` gains a `lockcheck` target that runs only when `uv` is on PATH.
- CONTRIBUTING's "Before you open a PR" says to relock after editing dependencies.

### Acceptance criteria

```text
uv lock --check
uv run --frozen aisquare persona list
```

- [ ] Both commands pass in a fresh clone of the branch.
- [ ] A PR that adds a dependency to `pyproject.toml` without relocking fails CI (link
      the red run that proves it).

### Docs to update

`CONTRIBUTING.md` (the lock and the check), CHANGELOG `Changed`.

### Risks

Dependabot's uv entry keeps opening lock PRs; with the check in CI they prove
themselves.

### Industry grounding

- uv docs, "Locking and syncing" (docs.astral.sh/uv/concepts/projects/sync/): `uv
  lock --check` checks whether the lockfile is up to date and is equivalent to the
  `--locked` flag for other commands; `--frozen` uses the lockfile without checking.

### Linked issues

#278. Related: #273 (fixed the contents).

## Results (2026-10-23)

Every live run was at **reduced scale** (owner decision 2026-10-23): at most 5 Claude
sessions, at most 30 minutes, the account slot with the most headroom, a scratch
`AISQUARE_HOME` and a scratch tmux socket, never the real `asq` socket, `~/.aisquare`
or a working fleet. The full run as written above (20 agents, the day-long soak) happens
once, after all four trains are on `main`.

| Workstream | PR | Scale | Before | After |
| --- | --- | --- | --- | --- |
| W0 spike | #320 | ≤5 sessions, ~20 min, slots 2 and 3, Claude Code 2.1.309 and 2.1.223, sockets `w0spike`/`w0probe` | — | answers in "W0 results (2026-10-23)" under W0: the 30–50 s Enter-ignore is refuted; an Enter before the input box is dropped (10/10); argv first prompts work on both versions; the inbox socket is pinned for 2.1.309 and absent on 2.1.223; the per-family window is `limits[]` `weekly_scoped` |
| W1a first messages | #330, #343 | ≤5 sessions, ~15 min, slot 2, 2.1.309, socket `w0spike` | typed after a fixed 2.0 s; no acknowledgement; `--task` typed nothing (field report: 7/7 coders at an empty composer); on 0.8.2 under load, 2 of 8 `fleet restart` resume lines sat typed but unsent while the board read `working`, one needing two Enters | **19/19 first messages acknowledged**: 5 spawns 1.6–1.7 s each, 5 restarts 2.5 s, a `--task` kickoff 1.6 s, 3 typed (`first_prompt = "type"`) 1.8–1.9 s, 5 parallel spawns; no message text left in `run/prompts/` or any `launch_spec` |
| W1b idle reads waiting | #315, #358 | tests (fake clock) | a just-spawned idle agent read `working` for up to 30 min; an Esc-ended turn likewise | `waiting` from SessionStart; an Esc-ended turn reads `working` for `busy_quiet_seconds` (30 s), then one capture |
| W1c contracts on claim | #313, #358 | tests | a claim printed the title only | the claim prints the whole contract, the last reopen reason and `aisquare task show <n>`, which survives a shell (#358) |
| W2 delivery guards | #337, #363 | ≤2 sessions, ~12 min, slot 2, 2.1.309, socket `w0spike` | a manager nudge was appended to a half-written draft and sent ("Your message got cut off"); a `fleet tell` to a busy coder was filed and never delivered (idle 45 s+ after its turn) | the draft stays intact and the nudge is deferred, then sent once the box was cleared; the tell is handed over at turn end, reply 31.9 s after the tell (~17 s of it the coder's own sleep). A draft that overtakes a paste stops it before its Enter, and the message says so (#363) |
| W3 account switches | #304, #308, #350 | tests only: forcing a real usage limit is unsafe at reduced scale | one limit with `on_limit = "switch"`: 2 `switched` + 1 manager nudge; an Opus agent picked onto a slot whose `seven_day_opus` was 100 | 1 `switched`, 0 nudges (the manager's switch is refused); the Opus agent goes to slot 3. W3c: the family week is read from `limits[]` `weekly_scoped`, and a spent one blocks every headroom pick. W3b: a cross-account resume keeps its real transcript (no false collision banner, no fresh restart) |
| W4 capture off | #305 | test (the #268 repro through the CLI) | `1` prompt logged with capture off | `0` |
| W5 lock check | #307 | CI | no step read `uv.lock`; it had drifted (the `voice` extra) | the `lock` job fails a dependency change without a relock ([red run](https://github.com/AISquare-Studio/aisquare-cli/actions/runs/40000167349/job/120573893291)) |
| P1-WS5d xdist (landed via this train) | #309 | CI runners | `Test (pytest)` 42–56 min per Linux leg, serial | 16–22 min with `-n auto` (50–66 % saved) |
| CI flakes hardened | #344, #347, #351 | CI full matrix | six tests that failed only under load on the full matrix | each hardened without loosening what it guards |
| Lean CI for sub-PRs | #322 | CI | 10 jobs per sub-PR run | 2–3 (one Linux check leg, Linux package, the lock job) |

**Not built in this train, and why.** The inbox-socket tier (W1a/W2 "if built"): W0
pinned its schema for 2.1.309 only, 2.1.223 has no socket, and its migration collided
with P1's reserved schema 35; follow-up behind `[fleet] inbox_delivery = false`. The
follow-ups all landed in this train: the per-family `weekly_scoped` window W0 found
(P0-W3c, #350); W1a's round-2 minors, the prompt file confined to its agent and a turn's
evidence taking a pending first message (#343); and the owed items on W1b, W1c and W4
(#358).

### Owner calls

The owner approves the train on these. Each row is one call: the default this train
built (the handoff's proposal where it had one), the PR that built it, and how to flip
it. "Code" means no key turns it: flipping it is a change in a later PR. The security
trade-off is first.

| # | Call | Default taken | Where | How to flip |
| --- | --- | --- | --- | --- |
| 1 | **A first message on the command line is visible to other local users in `ps`** (`/proc/<pid>/cmdline`) for the life of the agent, as with `claude "query"` itself; the 0600 file it travels in is deleted at launch | `[fleet] first_prompt = "argv"` for Claude Code agents; decide whether a shared box should default to `type` | #330 | `aisquare config set fleet.first_prompt type`: typed once the box is drawn, acknowledged, never on a command line |
| 2 | A second Enter (reverses #259 round 10's "never a second Enter") | Only while our text is provably still in the box, no turn has started and no dialog is up; at most 8 presses in 60 s; a pane with no box gets one Enter | #330, #337 | Code. Under the default `argv` a Claude Code agent's first message is never typed, so the rule applies to `type` and to tells |
| 3 | How long a spawn, restart or switch waits for the first message to be taken | `[fleet] prompt_ack_seconds = 45`; the CLI waits, `asq`, the Remote and the hand-over do not | #330 | `aisquare config set fleet.prompt_ack_seconds <s>` |
| 4 | Which rows can read "first prompt not taken" | Only rows whose session is on the board; a `Stop` or a session's `prompted_at` also takes it; a manager's own `prompt_unsent` nudges nobody | #330, #343 | Code |
| 5 | The inbox-socket tier | Not built in this train (W0 pinned it for 2.1.309 only; its migration collided with P1's schema 35) | #337, #320 | A later train, behind `[fleet] inbox_delivery = false` |
| 6 | The manager nudge | A bracketed paste through the delivery door; held while a draft is in the box or you typed within `[fleet] draft_quiet_seconds = 10`; dropped once the manager has prompted since | #337 | `aisquare config set fleet.draft_quiet_seconds <s>` |
| 7 | Turn-end delivery | On, for notes addressed to the session only, within `max_continuations_per_hour` | #337 | `aisquare config set fleet.deliver_notes_at_turn_end false` |
| 8 | The receipt when a draft overtakes a paste | `prompt_typed = "no"`, so a restart or switch line still says its hand-off prompt "was NOT typed" while the note says what happened | #363 | Code: a new `prompt_typed` value through `--json` and the Remote page |
| 9 | That message's wording | "ours is in the box with it", not "after it": tmux pastes at the cursor | #363 | Code |
| 10 | When a `working` row is checked against its screen | `[fleet] busy_quiet_seconds = 30`; a capture that fails reads `working` (fail closed) | #315 | `aisquare config set fleet.busy_quiet_seconds <s>` |
| 11 | `switched` as a manager wake | Not a wake: one `switched` per limit, on the manager's next delta; the hand-over's line is `limited_handing_over` and wakes nobody; `handover_failed` wakes | #304 | Code |
| 12 | Every candidate account marked, or its family week spent | The headroom rung returns nothing: a launch falls to the machine default (which can be a spent account); a hand-over or a switch without `--to` refuses | #304, #350 | Name the account: `fleet switch <agent> --to <slot>` |
| 13 | A replacement minutes old | Refused for another switch; the phone's Switch has no `--force`, and the refusal names it for the CLI | #304 | `aisquare fleet switch <agent> --force` |
| 14 | Which account a pick prefers | `[accounts] prefer = "priority"` | #304 | `aisquare config set accounts.prefer reset-soonest` |
| 15 | When a family week is spent | At 100 % or more, skipped for that family by every headroom pick; below 100 it ranks with the other windows | #350 | Code |
| 16 | A `weekly_scoped` entry naming no known family | Ignored for picks | #350 | Code |
| 17 | A family named by both `seven_day_<family>` and `weekly_scoped` | The fuller figure wins, the window's reset kept | #350 | Code |
| 18 | A cross-account resume | Keeps the recorded transcript; `account` follows the reported path | #308 | Code |
| 19 | An unreadable config with capture | Records no prompt, one stderr line (privacy fails closed, unlike other settings) | #305 | Fix the config |
| 20 | The CI consult with capture off | Keeps running: the CI experiment is its own opt-in and sends the prompt to its endpoint while on | #305 | Turn the CI experiment off |
| 21 | The ref a printed hint names | The bare board number, `aisquare task show 2` (`#2` starts a shell comment) | #358 | Code |
| 22 | uv in CI | uv 0.13.0; `setup-uv` pinned to v10.3.0's commit; a lock-format bump is by hand | #307 | Edit the pin in `ci.yml` |
| 23 | Lean CI for sub-PRs into `rc/**` | One Linux check leg (py3.12, captain on), the Linux package job and `lock`; the demo renders on rc pushes and train PRs | #322 | `gh workflow run ci.yml --ref <branch>` runs the full matrix |
| 24 | P1-WS5d (pytest-xdist) landing via this train | Merged into `rc/p0-reliability` so every train gets it through the syncs; P1's release notes say so; `-n auto` in the Makefile and CI `check`, not `addopts`; the ambient job stays serial | #309 | `pytest -p no:xdist` runs serially |
| 25 | W0's scale and oldest Claude Code version | 5 sessions, not 20; 2.1.223 as the oldest (the fleet's resume floor) | #320 | The as-written run after all four trains are on `main` |
| 26 | The version number | 0.9.0 proposed: a store migration (schema 36) and new receipt fields | — | The owner sets it when cutting the release |
| 27 | A command-line first message at Claude Code's first-run Security notes (main's #32 L2, the owner's D1) | (a): the spawn presses past the notes only where it types the first message (`first_prompt = "type"`). An argv agent at the notes reads `attention` ("Claude Code's first-run Security notes screen — press Enter in its window"): before any hook through the trust-question read, and once its SessionStart has fired, past `prompt_ack_seconds`, when its row moves to `unsent` and the board gets one `prompt_unsent` that wakes the manager (P0-FU1). A prompt after a human's Enter takes it. Checked in the post-main live run: does the positional prompt run as the first turn after that Enter | #404, P0-FU1 | (b): the argv spawn presses past the notes itself, a screen poll on every spawn; a P0 follow-up if the live check fails |

### Known gaps

- **The Remote's paste can be overtaken by a draft, undetected (W2).** `remote_actions.action_paste`
  asks the draft guard, then pastes and presses Enter at once with no check of the box, so
  a human's keys that land in the milliseconds between are sent with ours. The fleet's
  own typed writes stop on that race (#337, worded by #363). Proposed follow-up: route the
  Remote's paste through `deliver_typed` and give its 409 the same sentence.

### Deferred to the post-main live run (owner decision 2026-10-23)

Run once, after all four trains are on `main`, as the manager tracks them (seq 1135):

1. The soak: a multi-agent fleet on several accounts for a day; `fleet-delivery` shows
   every first prompt acknowledged.
2. W2's deferred manager nudge retried by `asq` with a hooked manager, which reviewer-b's
   walk could not drive (tests cover it).
3. W0 and W1a at the written scale: 20 agents.

## Release checklist

Status 2026-10-23 is given in **bold** after each item.


- Version: the next release cut from `main` after this train. `[Unreleased]` already
  carries the captain (#240) and personas (#201); with a store migration (schema
  version 36) and new receipt fields, expect 0.9.0. The owner sets it. **Owner.**
- CHANGELOG `[Unreleased]`: `Fixed` — first prompts acknowledged (#122), idle agents
  read waiting (#123), claimed contracts (#74), drafts never overwritten (W2), tells
  delivered at turn end (W2), one switch per limit and per-family windows (W3),
  a cross-account resume keeps its real transcript (W3b, found by W0),
  `capture.enabled` honoured (#268), `uv.lock`. `Added` — `[fleet] first_prompt`,
  `prompt_ack_seconds`, `busy_quiet_seconds`, `draft_quiet_seconds`,
  `deliver_notes_at_turn_end`, `inbox_delivery` (if built); `[accounts] prefer`;
  `fleet switch --force`; events `prompt_unsent`, `handover_failed`; doctor row
  `fleet-delivery`. **Done: one entry per workstream (P0-REL coherence pass); `inbox_delivery` not built.**
- Migration notes: store schema version 36 adds `team_session.prompted_at`, `fleet_agent.first_prompt`,
  `fleet_agent.first_prompt_at` (nullable; old rows read NULL, meaning "not tracked").
  No config migration; new keys default as above. **Done: `docs/fleet.md` "Upgrading".**
- Rollout defaults: command-line first prompts on for Claude Code agents; typed
  fallback with acknowledgement for everything else; socket tier off. **As built; the socket tier is not built at all.**
- Docs: `docs/fleet.md`, `docs/orchestration.md`, `docs/reference.md` (new
  configuration section), `docs/memory.md`, `docs/plans/claude-accounts.md`,
  `CONTRIBUTING.md`. `tests/test_documented_commands.py` and
  `tests/test_fleet_docs_are_true.py` green. **Done in each workstream; both guards run on the train head.**
- Release evidence: the full-matrix CI run on this branch's final head (its push run)
  and the train PR's own run, both linked from the train PR. **No full local `make
  check` (manager's rule, 2026-10-23: the build box is shared); locally `make lint
  typecheck` and targeted tests only.**
- Soak before tagging: a multi-agent fleet with several accounts runs a day on the
  branch; `fleet-delivery` shows every first prompt acknowledged. **Deferred (owner
  decision 2026-10-23) to the post-main live run, with W2's nudge retry and the 20-agent
  scale: see "Deferred to the post-main live run" above.**
- Owner calls: **listed in "Owner calls" above, one row each, with how to flip it.**

## Out of scope

- Fewer manager wakes and leaner deltas — P1. W2's turn-end delivery reuses the
  existing delta and adds no new wake for managers.
- A non-AI watcher and auto-stopping finished workers — P2.
- Restart all / Stop all view jumps — not in this train.
- Channels (Claude Code research preview): a development channel needs a full-screen
  confirmation at every launch and an allowlist, which an unattended fleet cannot
  answer. Revisit when channels leave preview.
- Sharing account state (#198) and multi-machine fleets — P2 and P3.
