# The captain — the home-level voice-and-text agent that runs every project's fleet

> **Status: Phase 1 built 2026-09-25** on `rc/captain-v1`, cut from the fold at
> `b39f6fa`. This is the plan *as merged*: the seven cards, the seams between
> them, the contracts posted on the board before code, and the decisions that
> changed them. Board seqs are cited so a reader can pull the exact words
> (`aisquare team log --since-seq <n> --limit 1` on the `aisquare-cli` board).
> The owner's plan document is the source; this page is the engineering record.
> Commands that exist are in `sh` fences and validated by
> `tests/test_documented_commands.py`; Phase 2 commands stay in `text` fences.
> The user page is [`docs/captain.md`](../captain.md); the acceptance runbook is
> [`docs/runbooks/captain-acceptance.md`](../runbooks/captain-acceptance.md).

---

## 0. The ask, as acceptance criteria

One agent at the level of the home, not of a project, that the owner talks to
— typed or spoken — and that runs every project's fleet on the owner's behalf:
reads what needs the owner across boards, asks a manager, spawns and pastes,
unblocks a stuck coder, reports what an agent did since last time. Every action
is a board event; a refusal is said, never faked. Phase 1's seven acceptance
lines are the runbook's sections 1–7.

## 1. Shape

```text
owner ── voice page (T3) ──┐
owner ── aisquare captain "text" (T2) ──┤── brain.say ── tmux pane ── Claude Code session (the captain, T2)
owner ── TUI captain view (T4) ─────────┘                                  │ mcp__captain__* only
                                                                           ▼
                                             Actions MCP server (T1) — 24 tools, one audit each
                                                    │                 │
                                     attention queue (T7)      fleet / boards / panes
                                          ▲
                              CLI verbs (T5): aisquare captain attention | next | resolve | …
```

- **T1 — the Actions server** (`services/captain/actions.py`, `state.py`,
  `errors.py`; `aisquare captain serve --stdio`; `python -m
  aisquare.services.captain`). 24 fixed tools (13010, 13025, 13071): projects,
  board, attention, next, resolve, snooze, since, read_pane, tell, ask_manager,
  spawn, stop, restart, attach_persona, task, note, press, paste, ui, act,
  speak, thinking, bt, wololo. Every call, success or refusal, writes one
  `captain_action` event `{"v","tool","project","args","utterance","ok","said",
  "receipt"}` on the project's board, or the home board when it names none. The
  actor is a virtual session `captain:<project>` per board, so writes route by
  session and never by cwd. `stop`, `spawn` and `restart` need `confirm=true`,
  and only on the owner's own words (13013).
- **T2 — the captain as a fleet agent** (`services/captain/brain.py`,
  `cli/captain.py`). One per home, on the home board (the project row for
  `$AISQUARE_HOME`, captured never onboarded — one store-level rule). Runs from
  `$AISQUARE_HOME/captain/brain` with `--strict-mcp-config --mcp-config
  captain/mcp.json --tools "" --allowedTools mcp__captain__*`: one server and
  no other tool. `brain.say(text, timeout=180) -> Reply(text: str | None,
  ended_at)` is the one delivery T3 and T5 reuse; `NoReply(message,
  timed_out)`; one delivery at a time (`captain/say.lock`).
- **T3 — voice** (`services/captain/voice.py`, `speaker.py`,
  `web/captain/index.html`, `cli/captain_voice.py`). One page, one websocket on
  loopback, the token in the URL fragment; focus and listen modes; the
  transcriber lifted from cliXR (`BufferedTranscriber`, `FakeTranscriber`);
  four Speaker adapters behind one runner; the speech spool drained by ONE
  daemon thread in the captain's server process; the reply spoken only when the
  captain made no `speak()` that turn (13143); one mode key for every page
  (13179).
- **T4 — the TUI** (coderp): the rank-insignia button beside Fleet and +, the
  captain view with mic, mode and speaker controls and the thinking indicator,
  the `ui` action receiver on `state.ui_socket_path(create=True)`.
- **T5 — the CLI verbs** (`cli/captain_verbs.py`): attention, next, resolve,
  snooze, since, log, uav, wololo, bt, actions — each the same tool call through
  `actions.perform`, audited with the utterance `aisquare captain <verb> …`.
- **T6 — this page, the user page, the runbook, the CHANGELOG entry, the
  README section.**
- **T7 — the attention queue** (`services/captain/queue.py`): every board folded
  into one ranked list (question, blocked, waiting, review, pull request,
  stale), deduplicated by project, agent, kind, card and normalised text, kept
  in `captain/queue.json`, resolved one item at a time; T1's four queue tools
  and T5's verbs read it.

## 2. Contracts, in board order

| seq | what |
|---|---|
| 13010 | T1's final tool signatures — the 24 tools, one audit per call, `refused:` / `error:` |
| 13013 | `confirm=true` on stop, spawn, restart; `paste` gains `submit` |
| 13019 | the `gh` provider for pull requests is Phase 2; the queue keeps the item kind |
| 13020 / 13026 | the phone path tonight is the desktop browser at `localhost:8749` plus `adb reverse`; https on the LAN is Phase 2 unless the owner says yes |
| 13025 | results wrapped with `action_seq`; the captain session `captain:<12>`; `captain_action` a human-board kind |
| 13071 | T1 amendment 2: confirm, `paste submit`, receipts per effect, `ui_socket_path`, `errors.Refused` / `Failed`, every call audited, the idle watchdog counts a running call |
| 13121 / 13123 | T2's contract and the manager's yes with two riders (`--json` for `say`; the delivery as a service function) |
| 13136 | `brain.say` named for T3; `--json` shape; `CaptainSection` for T4; `register(app)` for T5 |
| 13142 / 13143 | T3's six defaults and the manager's riders: `--voice` alias, the brain decides what is said, one spool drainer, the thinking signal on the terminal |
| 13151 | the captain persona stays in project pickers for Phase 1 |
| 13172 / 13175 | `Reply.text` is `None` for a turn without text; `NoReply(timed_out)`; one delivery at a time |
| 13179 | `captain_voice_mode` in `state.json` is the mode's single home |
| 13189 | after a reboot: auto-recover only when the tmux server is provably gone (socket file absent), otherwise refuse fast naming `aisquare fleet reap -P <home> --server-down` |
| 13206 | T6's riders: the owner's spelling `--voice`, the headset as default playback device, the reboot fallback, the Phase 2 list, the known CI fact, real pasted output |
| 13227 | nothing types into a dialog: read the pane first; the trust dialog names the one-time step |

## 3. Decisions log

- **One store-level rule keeps the home captured** rather than a flag at four
  onboarding call sites (13121 (a), 13123).
- **A busy captain is waited for, never sent a board note** — the shell-less
  captain would never read one (13121 (b)).
- **`--tools ""`**: the captain has no built-in tool, not even Read; every
  effect is a captain tool (13121 (c), plan section 6).
- **A restart keeps the label and the session; it mints a new row id** (#138).
- **The captain window carries `AISQUARE_HOME` and `AISQUARE_TEAM_HUB=<home>`**:
  `.aisquare` is a project-root marker, and without the hub a brain under
  `~/.aisquare` would resolve its board to `$HOME`; the launcher's own
  activation lands on the home board and onboards nothing (13123, T2 fix round).
- **`say` never types into a dialog** — the trust dialog, a numbered choice, an
  Enter/Esc dialog, the session-rating prompt — and a fresh captain is started
  bare and typed into once its prompt shows (13227). Pre-trusting the brain
  folder in Claude Code's config is the owner's Phase 2 call.
- **After a reboot**: the row is ended and a fresh captain started only when the
  server is provably gone (its socket file absent where the fleet resolves it);
  a present socket with nothing behind it, or a question tmux could not answer,
  is a fast refusal naming `aisquare fleet reap -P <home> --server-down`, never
  "already running" and never a silent wait (13189).
- **The brain decides what is worth saying**: the page speaks the reply only
  when the captain made no `speak()` that turn; a textless turn is `None`, shown
  as the page's own note, never a placeholder in the captain's voice (13143,
  13175).
- **Exactly one drainer of the speech spool**, in the captain's server process,
  with a 30 s TTL read from the line's id (13143 (5)).
- **Speaker and Voice methods are named `utter`**, not `say`, and the drainer's
  thread body is module-level: `tests/test_config_writes_stay_in_the_cli.py`
  fuses functions by name and read `say` as the CLI command that reaches a
  config write.
- **No test may spawn on a real tmux socket or launch the real `claude`**
  (13220): a bite check that removed the `--voice` dispatch fell through to the
  bare command and started a real captain on the owner's socket. The voice CLI
  tests refuse the spawn loudly; a suite-wide fixture gives every test a private
  socket and a stand-in binary.

## 4. Where the state lives

| place | what |
|---|---|
| `$AISQUARE_HOME/captain/brain/` | the captain's working directory (never a repo) |
| `$AISQUARE_HOME/captain/mcp.json` | the one-server MCP config, rewritten at every start |
| `$AISQUARE_HOME/captain/queue.json` | the attention queue and its cursors |
| `$AISQUARE_HOME/captain/speech/spk_*.txt` | the speech spool, oldest first |
| `$AISQUARE_HOME/captain/say.lock` | one delivery at a time |
| `$AISQUARE_HOME/captain/ui.sock` | T4's action receiver (or `/tmp/aisquare-<uid>/captain-<hash>.sock` when the home path is too long) |
| `state.json` | `captain_busy`, `captain_watermarks`, `captain_speaker`, `captain_voice_mode`, `captain_brake_at`, `captain_undo` |
| `config.toml` | `[captain] speaker`, `[captain.actions.<name>]` |

## 5. Gates

Every card: `make check` green in a fresh worktree with its own venv and an
isolated `AISQUARE_HOME`, ruff and `mypy --strict` clean, a non-author gate
against the workspace 9-dimension framework with the comment id on the card,
the runner's live replay with a fresh fixture home, CI green on the head with
the Windows leg read against the base's persona set. Merge order: T1, T7, T2,
then T3 and T5 (stacked), T4 (on T3), T6 last, then a real-claude end-to-end
pass on the RC head before READY.

## 6. Phase 2

The `gh` provider for pull requests; the captain-only persona out of project
pickers; https on the LAN for a phone; the queue's near-duplicate memory
outside its ten-minute window; the TOTP identity gate; pre-trusting the brain
folder; the TUI starting the voice server itself.

## 7. Acceptance

The seven lines, with their pasted output, are the runbook:
[`docs/runbooks/captain-acceptance.md`](../runbooks/captain-acceptance.md).
