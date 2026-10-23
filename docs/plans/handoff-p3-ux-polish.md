# Handoff: the P3 release train (UX, onboarding, polish)

Status: draft handoff, 2026-10-23. Base: `main` @ `b26dd140` (#201 personas, #240 captain (experimental, off
by default), #267 agent info line, all merged). PR #272 @ `2d80fd5c` (`perf/tmux-control-mode`, merged 2026-10-23,
mergeable) is treated as landing first. Every reference to `cli/ui/app.py`, `cli/ui/terminal.py`,
`cli/ui/views/accounts.py`, `cli/ui/views/agent.py`, `cli/ui/views/project.py`, `core/tmux.py` and
`core/tmux_control.py` below is to #272's version and says "(#272)". Every other `file:line` is `b26dd140`.

> **Base update (2026-10-23).** #272 merged at 362dd5b0 and #273 refreshed `uv.lock`; `main` is now 09f48563. References marked (#272) are to `main`. Other `file:line` references were taken on b26dd140 and still hold on `main` (#272 changed only `cli/ui/*`, `core/tmux.py`, `core/tmux_control.py` and `core/spawn.py`, whose references here are already updated).

Branch: `rc/p3-ux-polish`. Doc path once committed: `docs/plans/handoff-p3-ux-polish.md`. This document is a
reference, not a script. Commands appear in `text` fences or as inline code. The Claude Code docs pages cited
below (`code.claude.com/docs/en/…`) show no date.

**What releasing this train means.** A signed-in new user reaches a working manager without help and reads the
board as a list of what needs them. Fleet usage is one number, also visible inside every Claude Code session.
Images reach an agent by drag, paste or path. A role can opt in to Claude Code's Remote Control. Existing
Claude logins and hand-launched sessions can join the fleet, if the owner reverses the 2026-09-09 decision
that blocks it. Four correctness bugs are fixed (#269, #270, #271, the label limit); instruction precedence is
documented; the multi-machine fleet gets a design sketch and an SSH runbook; the persona and setup follow-ups
are closed, narrowed or built.

---

## How to run this train with agents

### Workstreams

| ID | Title | Issues | Size | Depends on | Parallel-safe | Hot files |
| --- | --- | --- | --- | --- | --- | --- |
| W1 | First-run guidance | #286, #215 | M | #272 | yes, but sequence `app.py` with W3 | `cli/ui/app.py`, `cli/ui/views/accounts.py`, `cli/ui/views/welcome.py`, `cli/ui/spawn.py`, `cli/ui/attach.py`, `cli/ui/board.py` |
| W2 | Usage at a glance | #287 | L, as three PRs (2a S, 2b S, 2c M) | #272 (2b); P0 W3 merges first in `services/claude_accounts.py` | yes, between slices | `services/claude_accounts.py`, `models.py`, `cli/ui/sidebar.py`, `cli/ui/views/project.py`, `cli/accounts.py`, new `aisquare/statusline.py`, `pyproject.toml` |
| W3 | Image drop and paste into a pane | #288 | S–M | #272 | after W1 on `app.py` | `cli/ui/app.py`, `cli/ui/terminal.py`, `core/tmux.py`, `core/tmux_control.py` |
| W4 | Claude Code Remote Control per role | #289 | S–M | none | yes | `cli/launch.py`, `core/config.py`, `cli/fleet.py`, `cli/ui/spawn.py`, `services/diagnostics.py` |
| W5 | Adopt existing Claude logins and sessions | #290 | M | **owner decision**; P2's #198 rewrite | yes, once decided | `core/claude_accounts.py`, `services/claude_accounts.py`, `cli/accounts.py`, `cli/fleet.py`, `cli/ui/views/accounts.py` |
| W6 | Small fixes | #291, #269, #270, #271 | 4 × S | none | yes, one PR each | `services/fleet.py`, `core/harness.py`, `core/store.py`, `services/team.py`, `services/project.py` |
| W7 | Docs: instruction precedence | #292 | S | none | yes | new `docs/instructions.md`; `services/team.py` only for the optional W7b |
| W8 | Multi-machine fleet (postponed) | #293 | S (docs) | none | yes | new `docs/runbooks/multi-machine-ssh.md` |
| W9 | Persona and captain follow-ups | #210 (close), #211, #212, #294, #213, #216 | M in total | #272 (#212 only) | yes, per issue | `services/persona_import.py`, `cli/persona.py`, `cli/ui/persona_dialogs.py`, `services/captain/actions.py`, `core/config.py` |
| W10 | Setup and config | #98 | M | P2's W4 (#51) also adds doctor rows | yes | `cli/ui/views/app_settings.py`, `services/settings.py`, `services/diagnostics.py`, `core/claude_accounts.py` |

### Recommended order

1. #272 merged on 2026-10-23; `rc/p3-ux-polish` is cut from a `main` that includes it.
2. Start in parallel on day one: W6 (four PRs), W7, W8, the #210 close, and W9's #211 and batch import. None
   of them touch the UI files that #272 changes.
3. Next W1, then W3, both on `app.py`, one after the other. W2a, W2b and W4 run beside them.
4. Then W10, W2c, #212 and #216.
5. W5 runs only after the owner's decision. Its phase B (adopting a session) does not depend on phase A
   (adopting a directory) and can go first.

### File-overlap hotspots across the trains

- `services/fleet.py` (9,113 lines): P0 delivery and switching, P1 stop events and P2 capacity all edit it. W6
  changes about ten lines in `next_label` (`services/fleet.py:1089-1124`); land it early, or rebase it last.
- `core/harness.py`, the manager cycle (`core/harness.py:1028-1060`): P0 (#122 spawn wording), P1 (wake and
  delta wording) and W6 (one label sentence) all edit it. The text is pinned byte for byte by
  `tests/test_harness.py`; merge one PR at a time and re-pin after each.
- `services/claude_accounts.py`: P0 W3 (weekly windows, pick by reset), W2a (summary fields) and W5 (adopt).
  P0 merges first.
- `cli/ui/app.py`, `cli/ui/terminal.py`, `cli/ui/views/accounts.py` and `cli/ui/views/project.py` are all
  rewritten by #272. Build on #272's head, never on `b26dd140`'s copy.
- `services/diagnostics.py` (3,390 lines): P2 (stale `ANTHROPIC_BASE_URL`), W4 and W10 all add rows. Each row
  is its own `_check_*` function; do not reorder existing ones.
- `core/config.py`: P1 and P2 add keys, and so do W4 (`remote_control`) and W9 (`[captain] gate`). Add fields;
  never reshape a model.
- `docs/fleet.md`: every train edits it. Add sections; do not reflow paragraphs another PR also touches.

### Agents

One coder per workstream (W6's four fixes can be one coder, four PRs), plus a reviewer per PR who did not
write it, checking: the failing-first test (a CI log, or a pasted local run, of the new test failing on the
base commit); the macOS and Linux runs, plus WSL where the item says so; `make check` green; docs and the
CHANGELOG entry; `--json` honoured; no markup in data (CONTRIBUTING: build visible strings with
`Text.append`); and no new widget attribute shadowing a Textual name (`docs/plans/spawn-personas.md` §10 lists
`_running`, `visible`, `_attach` and `layer`, each found the hard way). The verdict goes on the board; the
manager merges only on a reviewer PASS with green CI on the current head.

### Definition of Done (every workstream)

- **Production-ready, not "code exists".** The failure is reproduced by a test that fails without the change
  (CONTRIBUTING: "prove a test can fail").
- **Platforms.** macOS and Linux, plus WSL where the area is platform-sensitive (W3, W5, W10).
- **`make check` is green:** ruff, mypy strict, and pytest on 3.11–3.13.
- **Docs.** User docs updated under `docs/`; the README stays under 250 lines; every config key is documented
  in `docs/reference.md`. **CHANGELOG:** an `[Unreleased]` entry in the repo's existing voice. **New
  commands** are added to `IMPLEMENTED` in `tests/test_stubs.py`.
- **Layering.** Services return data and the CLI prints. `--json` is honoured.
- **Measured numbers.** Where the item is about tokens, latency or minutes, take the baseline on a scratch
  fleet first and give before and after numbers in the PR.

---

## W1 — First-run guidance (#215)

**Problem.** A user who has just signed in gets no next step: the Accounts page confirms the sign-in and
stops, and nothing says a manager is started from a project's Manager tab (*Start manager*). The board shows
tasks and a live feed but never says what a human does there. And #215 (filed 25 Sep): the persona picker's *+
New account* is one-way; nothing brings the user back after the sign-in.

**Where the code is today.**
- **Welcome (0.8.0)** walks three steps: a project, Claude Code, the fleet (`cli/ui/views/welcome.py:1-33`
  (#272)). Step 3 ends on "Your fleet is up. Open the manager and tell it what to build."
  (`welcome.py:1179-1180`, `FLEET_UP` at `:93`). Step 2's *Sign in* opens Accounts, and nothing brings the
  user back.
- **After a sign-in lands**, `_complete_finished` (`cli/ui/views/accounts.py:1781-1790` (#272)) shows "✓
  <name>: <who> · hooks installed", posts `AccountsChanged`, and stops.
- **The sidebar's `＋ spawn agent`** now opens the Spawn dialog (`cli/ui/sidebar.py:856-870`; `on_spawn_agent`
  at `cli/ui/app.py:2126-2134` (#272)). The 0.8.2 release shows the toast "the spawn dialog is not built yet"
  instead (`v0.8.2:src/aisquare/cli/ui/app.py:1712-1724`).
- **The Manager tab with no manager** says "<name> has no manager yet. Start one to task this project in prose
  …", then *Start manager* (`cli/ui/views/project.py:56-86`, `:149` (#272)).
- **The board.** Its help (`?`) lists keys and the icon legend only (`cli/ui/board.py:133-145`; `BOARD_KEYS`
  at `:101-110`; `STATUS_ICONS` at `:86-94`). With no open tasks, `_refresh_tasks` (`:539-575`) draws an empty
  table titled "tasks — d for done archive". The panel is read-only ("nothing here writes", `board.py:20`).
  `docs/orchestration.md:194-230` gives the icon table; nothing says what a human does on the board.
- **#215.** The picker dismisses with `Target("new-account")` (`cli/ui/attach.py:355-357`); the Spawn dialog's
  *Pick…* path calls `dismiss(None)` (`cli/ui/spawn.py:1022-1034`), throwing away everything typed in it;
  `on_new_account_requested` (`cli/ui/app.py:2214-2217` (#272)) opens Accounts and starts a sign-in. That is
  the whole hop.
- **The captain's attention queue already classifies what needs the owner** (question, blocked, waiting,
  review, stale) through `observe()` (`services/captain/queue.py:558`, `is_owner_question` at `:487`). It is
  not gated on the captain flag.

**What to build.**
1. **A return-to intent in the shell.** `FleetApp` holds at most one `after_sign_in: ReturnTo | None`, a small
   dataclass of `kind` (`"welcome"`, `"picker"` or `"spawn"`), `project_id` and `form` (the Spawn dialog's
   form state, or the picker's persona, intent and filter). Welcome step 2's *Sign in*, the picker's *+ New
   account* and the Spawn dialog's *Pick… ▸ + New account* set it. `AccountsView` posts a new
   `SignInLanded(slot)` from `_complete_finished` on success, and the app consumes the intent — **welcome**:
   show Welcome and focus step 3's next button; **picker**: reopen `AttachTargetScreen` with the new slot
   preselected (#215); **spawn**: reopen `SpawnDialog(project, …)` with the typed fields restored and
   `account` set to the new slot. A cancelled or abandoned sign-in (`_abandon_finished`) clears the intent and
   shows one toast; nothing reopens. `SpawnDialog` gains `form_state()`; its constructor already takes role,
   binary, account and persona presets (P4 and P7, `cli/ui/spawn.py:24-33`, `:358-383`), and label, task and
   first prompt become new keyword presets.
2. **The Accounts page's next step.** When a sign-in lands with no intent, the notice line adds one step from
   the shell's frame: no project → "Next: add a project (`+` beside Fleet)"; a project with no live manager →
   "Next: open <name> and press *Start manager*", with a *Go to Welcome* button (`w`); otherwise → "Next: `＋
   spawn agent` under a project". Derived, never stored.
3. **"What needs you" on the board.** A one-line strip at the top of `BoardPanel` (`board.py:369`), for
   example `needs you: 🔔 1 waiting · ❓ 2 questions · 👀 1 in review · ⛔ 1 blocked — click a task or a 🔔 row`,
   or "nothing needs you — the manager is working; talk to it on the Manager tab". It classifies with the
   captain queue's pure helpers (`observe()` with a one-project `Sources` that reads no pane tails), so there
   is no second definition of "needs the owner". It never writes `captain/queue.json` and does not need the
   captain on. Clicking a count moves the cursor to the first matching task, or selects the agent row.
4. **The board's empty state.** With no open tasks the table body reads "No open tasks. The manager writes
   them when you give it a goal on the Manager tab." (NN/g: show status, teach, give a path.)
5. **"What to do here" in the board's help.** `board_help()` gains three lines: 🔔 and ❓ need you (click the
   agent and answer in its pane); 👀 waits for a reviewer agent, or for you on the PR; nothing here is typed
   into — you talk to the manager.

**Acceptance criteria.**
- [ ] Headless: Welcome ▸ *Sign in* ▸ (stub accounts service) the sign-in lands; the content shows `welcome`
  and step 3's next button has focus.
- [ ] Headless: the picker's *+ New account* ▸ the sign-in lands; the picker is open with the new slot
  selected, and confirming opens the Spawn dialog preset with it (#215's acceptance).
- [ ] Headless: the Spawn dialog with a typed label and persona ▸ *Pick… ▸ + New account* ▸ the sign-in lands;
  the dialog reopens with the same label and persona, and the account is the new slot.
- [ ] A cancelled sign-in reopens nothing and shows exactly one toast.
- [ ] A board with 1 blocked, 1 review and 1 attention session renders those counts in the strip; one with
  none renders the "nothing needs you" line; one with no open tasks renders the empty-state text.
- [ ] The strip adds no tmux call and no store read beyond the panel's current tick, pinned with a recorder on
  `services.team` and `TmuxServer`.

**Tests.** `tests/test_ui_welcome.py`: the round trip, failing first (on `b26dd140` the content stays on
`accounts`). `tests/test_ui_personas.py` and `tests/test_ui_spawn.py`: the picker and dialog round trips, and
the cancel case. `tests/test_ui_accounts.py`: `SignInLanded` posted once per landing, never on an abandon. A
board test beside `tests/test_watch.py`: strip counts, the empty state, and the recorder showing no extra
reads.
**Docs.** `docs/fleet.md` "The first five minutes": a step 3a "Reading the board" (what needs you, what you
never type into), and one sentence in step 1 that Welcome returns after a sign-in. `docs/orchestration.md`
§"The live board": "What a human does here".
**Telemetry/measurement.** Key presses from "sign-in landed" to "manager pane focused" in the headless Welcome
test: before, no path exists (the test cannot reach it); after, ≤ 3. On the RC build, someone who has not used
asq starts from an empty `AISQUARE_HOME` and reaches a working manager without help; record on the issue where
they stopped, if anywhere.

**Risks and open questions.** A modal reopening by itself after a sign-in may feel too forward; the intent is
single-use, and any sidebar selection clears it. Whether the strip also counts stale agents (the queue's
`stale`) is the owner's call.

**Industry grounding.**
- NN/g, "Designing Empty States in Complex Applications: 3 Guidelines" (Kate Kaplan, 2021-09-19) —
  https://www.nngroup.com/articles/empty-state-interface-design/. "Totally empty states cause confusion about
  how and whether the system is working"; show status, give learning cues, offer direct paths to key tasks.
- NN/g, "Progressive Disclosure" (Jakob Nielsen, 2006-12-03) —
  https://www.nngroup.com/articles/progressive-disclosure/. Show the few most important options first; staged
  disclosure suits sequential steps: one next step at a time, not a manual.
- Command Line Interface Guidelines — https://clig.dev/ (no date shown). "suggesting to the user commands they
  can run next helps them learn how to use your program": the model for the "Next:" line.

**Linked issues.** #286, #215.

---

## W2 — Usage at a glance

**Problem.** Usage is shown per account and, since #267, per agent, but no single figure says how much of the
fleet's five-hour and weekly allowance is left (the sidebar dot has no number; `accounts usage` has no total).
Inside a Claude Code session the only readout is a typed `/usage`, covering only this machine's local history.
Nothing lists the machine's Claude Code sessions, hand-launched ones included, and the Manager tab says
nothing about the team.

**Already done by #267 (do not rebuild).** Every sidebar agent row shows `#<slot>·NN%`, the agent's account
and its five-hour window used (`cli/ui/sidebar.py:245-310`, colours at `:365-379`). The agent info line, the
Agent view header and the Manager tab's first line show `<account> · NN%`, model and effort
(`cli/ui/views/agent.py:98-186` (#272)).

**Where the code is today.**
- **The sidebar status line** is `● aisquare  ● 3× claude`: a band-coloured dot and a count, no number
  (`sidebar.py:399-422`, bands at `:348-361`).
- **The figure behind the dot** is `fleet_headroom` (`services/claude_accounts.py:1455-1511`): the mean of
  `100 − max(5h used, week used)` over usable accounts, kept in `HeadroomSummary` (`models.py:382-402`). No
  weekly-only figure and no "soonest reset". Only `five_hour` and `seven_day` are parsed
  (`services/claude_accounts.py:817-818`); P0 W3 owns per-model windows.
- **`accounts usage`** prints one row per account and no total (`cli/accounts.py:272-326`); its `--json`
  output is a list.
- **Nothing in the repo sets a Claude Code `statusLine`.** The only `status_line` symbols are the sidebar's
  and the remote panel's.
- **The Manager tab** shows the manager's own info line and the hint, nothing about the team
  (`cli/ui/views/project.py:144-151` (#272)).
- **Shared settings.** `core/claude_sharing.py` can link every managed slot's `settings.json` to the plain
  claude's ("permissions, hooks, model, status line", `:30-36`), so a `statusLine` set once can reach every
  slot that shares settings.

**What to build** (three PRs).

*2a — the aggregate number (S).* `HeadroomSummary` gains `week_left_by_slot: dict[int, float]` (`100 − week
used`), `week_percent_left: float | None` (the mean over usable accounts), `weeks_left: float | None` (the sum
of `(100 − week used) / 100`, read as "1.8 of 3 account-weeks left") and `week_resets_soonest: tuple[int,
datetime] | None`, filled by `fleet_headroom` as pure functions with the formula in the docstring. The sidebar
line shows the number, `● 3× claude 58%` (percent LEFT, as the band already means). The Accounts header adds
"Fleet: 58% left now · week: 1.8 of 3 accounts left · next weekly reset Thu 14:00 (account 3)". `aisquare
accounts usage` prints a final `all` row and that line; `aisquare --json accounts usage --summary` prints
`{"accounts": [...], "summary": {...}}`. Plain `--json` stays a list, so consumers do not break.

*2b — the team strip (S).* A `TeamStrip` under the Manager tab's info line, for example `🔨 2▶  🧪 1⏸  👀 1🔔  ·
board for detail`. It reads only the agents in the shell's frame (`FleetAgentStatus` per role and state) and
adds no reads. A click opens the Board tab; with only the manager running, it is hidden.

*2c — the Claude Code statusline and the sessions registry (M).*
- **A lean entry point.** `[project.scripts] asq-statusline = "aisquare.statusline:main"`, a module that
  imports only the stdlib (`json`, `os`, `sys`, `time`, `pathlib`). It reads Claude Code's stdin JSON;
  atomically writes `~/.aisquare/cache/statusline/<session_id>.json` with `session_id`, `cwd`, `model.id`,
  `CLAUDE_CONFIG_DIR` (from its env), `AISQUARE_FLEET_AGENT` when set, `rate_limits.{five_hour,seven_day}`,
  `context_window.used_percentage`, `cost.total_cost_usd` and `updated_at`; reads
  `~/.aisquare/cache/statusline/fleet.json` (a summary that asq's headroom read and `accounts usage` write);
  and prints one line no wider than `COLUMNS`, for example `acct 3 · 5h 42% · wk 61% · fleet 58% left (3×) ·
  ctx 34%`.
- **Installing it.** `aisquare accounts statusline --install [--slot N|--all] [--wrap]` sets `statusLine` in
  each chosen config dir's `settings.json` only when none is set; `--wrap` keeps a user's own command and
  prints its output first. `--uninstall`, and `aisquare uninstall`, remove only an entry aisquare wrote.
- **Free readings.** asq's headroom worker (`refresh_headroom`, `cli/ui/app.py:1407` (#272)) ingests
  statusline records newer than the cached sample, maps them to a slot by `CLAUDE_CONFIG_DIR`
  (`core/claude_accounts.py:182` `managed_slot`) and stores them as `claude_usage` samples (`_record_sample`,
  `services/claude_accounts.py:940`). `cached_usage` serves them, so the OAuth usage endpoint is asked less
  often. No store migration.
- **The device-wide view.** `aisquare accounts sessions [--since 15m] [--json]` lists every session that
  reported in the window (account, short session id, cwd, model, context %, 5h %, age, fleet label when it has
  one), including hand-launched and non-project sessions under any config dir carrying the statusline. The
  Accounts page gets the same table as "Sessions on this machine". Records older than 24 h are pruned.

**Acceptance criteria.**
- [ ] With readings 5h 10%/wk 90% and 5h 30%/wk 20%, `fleet_headroom` gives `week_percent_left` 45.0,
  `weeks_left` 0.9, and the soonest reset of the two.
- [ ] The sidebar line paints the number. `accounts usage` prints the `all` row. `--json --summary` matches
  the model.
- [ ] The strip shows per-role counts from a frame with 2 coders working, 1 tester waiting and 1 reviewer in
  attention, and is hidden with only the manager.
- [ ] `asq-statusline` with the documented sample payload writes one record and prints one line. With no
  `rate_limits` (an API-key session) it prints without the windows and exits 0.
- [ ] `asq-statusline` imports no `typer`, `rich`, `textual`, `pydantic` or `sqlite3`, pinned like
  `tests/test_import_cost_of_the_integration.py`.
- [ ] `--install` never overwrites a foreign `statusLine` without `--wrap`; `--uninstall` and `aisquare
  uninstall` remove only aisquare's entry.
- [ ] Two sessions on two slots appear in `accounts sessions --json` with their slots.

**Tests.** `tests/test_usage_aware_accounts.py` (summary maths, failing first: the fields do not exist).
`tests/test_captain_sidebar.py` or a sidebar unit test (status line). `tests/test_ui_project.py` (strip). New
`tests/test_statusline.py`: payload parsing, the import pin, install/wrap/uninstall on a scratch
`CLAUDE_CONFIG_DIR`, ingestion into `claude_usage`. `tests/test_stubs.py`: add `("accounts", "statusline")`
and `("accounts", "sessions")`.
**Docs.** `docs/fleet.md` §Accounts: the number, the summary line, the statusline, the sessions view.
`docs/reference.md`: the new flags and the cache paths.
**Telemetry/measurement.** Baseline first: usage-endpoint requests per hour on a scratch fleet of 4 agents on
3 accounts for 1 h, before and after 2c ingestion (count `claude_usage` rows written by `sample_usage` against
rows ingested from statusline records). `asq-statusline` cost as CPU self-time per `-X importtime`, the repo's
own method (`tests/test_import_cost_of_the_integration.py`): target ≤ 30 ms of imports, against ~326 ms for a
full CLI import measured there.

**Risks and open questions.** *Coverage:* `rate_limits` "appears only for claude.ai Pro and Max subscribers …
and only after the first API response in the session" (statusline docs); Team and Enterprise slots keep the
OAuth endpoint. *Privacy:* records hold `cwd` and session id, never prompts; should `capture.enabled = false`
also stop them? Owner's call; the proposed default is no. *Payload change:* the statusline fields are
documented, unlike the internal transcript format; the parser still fails open.

**Industry grounding.**
- Claude Code docs, "Customize your status line" — https://code.claude.com/docs/en/statusline. The stdin JSON
  includes `rate_limits.five_hour.used_percentage` / `.resets_at`, `rate_limits.seven_day.*`,
  `context_window.used_percentage`, `cost.total_cost_usd`, `session_id`, `transcript_path` and `model.id`; the
  script runs on each assistant message, debounced at 300 ms, and `refreshInterval` re-runs it on a timer.
- Claude Code docs, "Manage costs effectively" — https://code.claude.com/docs/en/costs. `/usage` figures "are
  approximate and computed from local session history on this machine, so usage from other devices or
  claude.ai is not included": the gap a device-wide view fills.
- Claude Code docs, "Commands" — https://code.claude.com/docs/en/commands. `/usage`: "Show session cost, plan
  usage limits, and activity stats".

**Linked issues.** #287. Related: #198 (P2, shared state), P0 W3 (weekly windows).

---

## W3 — Image drop and paste into an agent pane

**Problem.** Claude Code accepts an image three ways: drag and drop, paste from the clipboard, or the file's
path. Inside `asq`, dragging an image or screenshot onto the window does not deliver it to the agent pane. Two
code-grounded causes are below; neither is reproduced yet, so step 1 is a reproduction matrix.

**Where the code is today.**
- **The pane forwards a `Paste` event** as one bracketed paste: `TerminalPane.on_paste`
  (`cli/ui/terminal.py:2844-2852` (#272)) calls `_forward` (`:2573-2609`); over the control-mode pipe,
  `set-buffer` then `paste-buffer -p -d` (`core/tmux_control.py:678-702`); polled, `load-buffer` then
  `paste-buffer -p -d` (`core/tmux.py:1795-1828`).
- **Textual 8.2.8 delivers a `Paste` to the FOCUSED widget only** (Textual `app.py:4142-4146`: `if
  self.focused is not None: self.focused._forward_event(event)`). The sidebar has focus at mount
  (`welcome.py:25-27` (#272)) and after F12, and has no paste handler; only the rename `Input` does
  (`sidebar.py:760-775`). A terminal's drop arrives as pasted text, so a drop while the sidebar has focus
  reaches no pane. Leading hypothesis; confirm it first.
- **An EMPTY paste is dropped before tmux** (`core/tmux.py:1806`, `if not text: return`;
  `core/tmux_control.py:688`). Claude Code documents `Cmd+V` (iTerm2) as image paste; if a terminal signals an
  image clipboard with an empty bracketed paste, asq swallows it. Unconfirmed; measure it in step 1.
- **Ctrl+V reaches the pane as `C-v`,** and Claude Code reads the clipboard itself. Since 0.7.0 each spawn
  carries `DISPLAY`, `WAYLAND_DISPLAY` and the rest so this works on Linux (`docs/fleet.md:1563-1572`).

**What to build.**
1. **Reproduce and record first (no code).** Log `events.Paste` (length, first 80 characters) and the focused
   widget across terminals (iTerm2, Terminal.app, kitty, WezTerm, GNOME Terminal, Windows Terminal on WSL) and
   gestures (drag a PNG from the file manager, Cmd/Ctrl+V of a screenshot, Alt+V on WSL). Paste the table into
   the issue; it decides whether item 3 is needed.
2. **A paste router.** `FleetApp.on_paste` handles a `Paste` that bubbled up unhandled (one the focused widget
   did not stop). When the view shows a live `TerminalPane` (the Agent view, or the Manager tab with a
   manager), it focuses that pane and delivers the text through a new public `TerminalPane.paste_text(text)`,
   which `on_paste` also uses. Otherwise one toast: "nothing to paste into — open an agent first". Inputs and
   dialogs keep their own pastes, because they stop the event.
3. **The empty paste, only if step 1 shows a terminal sending one.** Deliver an empty bracketed paste
   (`ESC[200~ESC[201~` via `send_bytes`), only while the pane's program has bracketed paste on (tmux knows:
   `paste-buffer -p` brackets "if the application has requested bracketed paste mode"). Never send the markers
   to a program that did not ask for them.
4. **Leave paths alone.** A dropped path reaches Claude Code exactly as the terminal quoted it; Claude Code
   handles its own terminal's drops.
5. **WSL (optional, owner's call).** A paste that is exactly one Windows path (`^[A-Za-z]:\\`) of an existing
   file, under WSL, is rewritten with `wslpath -u`.

**Acceptance criteria.**
- [ ] Headless, sidebar focused and an Agent view shown: a posted `Paste("/tmp/shot.png")` reaches the pane (a
  recorder on `_forward` gets `("paste", "/tmp/shot.png")`), and the pane has focus. On `b26dd140`/#272 the
  recorder gets nothing.
- [ ] The same paste with no agent view shown produces one toast and no tmux call.
- [ ] A paste into the Spawn dialog's label `Input` never reaches a pane.
- [ ] If item 3 lands: an empty paste reaches a pane that requested bracketed paste, and never one that did
  not.
- [ ] The step 1 matrix is on the issue, and after the change every listed gesture results in `[Image #N]` in
  Claude Code or a readable path.

**Tests.** `tests/test_terminal_pane.py` and #272's `tests/test_terminal_control.py` (pane and pipe);
`tests/test_ui_shell.py` (the router, failing first).
**Docs.** `docs/fleet.md` "Images" paragraph beside the key table: drag a file onto `asq` (the pane takes it,
focused or not); Ctrl+V pastes an image from the clipboard (Alt+V on Windows and WSL; Cmd+V in iTerm2); or
type the file's path; and what asq does with each.
**Telemetry/measurement.** The step 1 matrix before and after: gestures that worked, out of the gestures
tried.

**Risks and open questions.** A paste routed to a pane the user did not mean: the router only acts when
exactly one live pane is on screen. Remote Control (W4) also lets a phone "attach a photo or file", per its
docs; document it as an alternative, not a fix.

**Industry grounding.**
- Claude Code docs, "Common workflows ▸ Work with images" — https://code.claude.com/docs/en/common-workflows.
  Drag and drop an image into the Claude Code window, paste with `Ctrl+V` (`Alt+V` on Windows/WSL), or give
  the image's path.
- Claude Code docs, "Interactive mode" — https://code.claude.com/docs/en/interactive-mode. "`Ctrl+V` or
  `Cmd+V` (iTerm2) or `Alt+V` (Windows and WSL) | Paste image from clipboard | Inserts an `[Image #N]` chip".
- WezTerm docs, `quote_dropped_files` — https://wezterm.org/config/lua/config/quote_dropped_files.html (since
  20220624-141144-bd1b7c5d). A drop reaches the program as a quoted file name (`SpacesOnly` by default off
  Windows): terminals deliver drops as text, so focus decides where they land.
- tmux(1), `paste-buffer -p` — https://man7.org/linux/man-pages/man1/tmux.1.html (2026-07-29). It inserts
  "paste bracket control codes … if the application has requested bracketed paste mode."

**Linked issues.** #288. #272 (merged) is the base.

---

## W4 — Claude Code Remote Control for fleet agents

**Problem.** Claude Code's Remote Control (`claude --remote-control`) lets a local interactive session be
continued from claude.ai/code or the Claude app. Fleet agents cannot opt in: aisquare never passes the flag,
and `aisquare remote` is a different, narrower tool. Nothing for this has shipped as of 0.8.2.

**Where the code is today.**
- **`aisquare remote`** is aisquare's own phone page: an HTTP server on loopback behind your own ngrok, with
  tell, stop, restart and switch (`docs/remote.md:1-29`, `services/remote_actions.py:100`).
- **Nothing passes Claude Code's `--remote-control`**, and no role setting exists. A grep for `remote-control`
  and `remoteControl` in `src/` finds only aisquare's own remote panel module.
- **The launch-time seam** for Claude Code-only flags already exists for personas: `_persona_system_prompt`
  (`cli/launch.py:174-245`) checks `harness.is_default_agent(binary)`, defers to the operator's own flag, and
  prints one dim line when it steps aside.
- **Per-role launch config** lives in `FleetRoleSettings` (`core/config.py:287-305`), which a restart and a
  switch replay through the window's `launch`.

**What to build.**
- **Config.** `[fleet.roles.<role>] remote_control = false` (bool; default off for every role).
- **Flags.** `fleet spawn --remote-control/--no-remote-control` overrides it per spawn, recorded as an agent
  argument so `LaunchSpec` replays it on restart and switch. **The Spawn dialog** gains a *Remote control*
  switch that follows the role's default.
- **In `cli/launch.py`**, a new `_remote_control_args(role, project, binary, args)` returns
  `["--remote-control", f"{project_name} · {label}"]` (the session title shown in claude.ai/code and the
  Claude app). It returns nothing, with one dim line naming the reason, when the binary is not Claude Code;
  the operator's arguments already carry `--remote-control` or `--rc`; `ANTHROPIC_BASE_URL` names a host other
  than `api.anthropic.com`, as the explainability proxy does; or `CLAUDE_CODE_OAUTH_TOKEN` is set. Claude Code
  refuses Remote Control in the last two cases.
- **`fleet ls --json`** gains `remote_control: bool`, read from the row's spec. **A doctor row, `remote
  control`,** lists the roles with it on and any blocker in the launch environment, such as a proxy base URL.

**Acceptance criteria.**
- [ ] `launch coder` with `remote_control = true` puts `--remote-control "<project> · coder-1"` on argv for
  `claude`; nothing for `codex` or a wrapper, and the dim line prints.
- [ ] With `ANTHROPIC_BASE_URL=https://proxy.example`, nothing is added and the line names the variable.
- [ ] A spawn with `--remote-control` that is then restarted keeps the flag (spec replay).
  `--no-remote-control` over a role default of `true` adds nothing.
- [ ] `fleet ls --json` reports the field. The doctor row lists the roles and blockers.

**Tests.** `tests/test_launch.py` (argv, failing first), `tests/test_fleet_service.py` (replay),
`tests/test_ui_spawn.py` (the switch), `tests/test_doctor_fleet.py` (row).
**Docs.** `docs/remote.md`: a new "Claude Code's own Remote Control" section, with a two-column comparison
against `aisquare remote` (what each shows, auth, where traffic goes). `docs/reference.md`: the key.
`docs/fleet.md`: the Spawn dialog field.
**Telemetry/measurement.** Time from spawn until the session appears in claude.ai/code, measured once by hand
on the RC build and recorded in the PR. No ongoing metric.

**Risks and open questions.** *Plan and auth:* Remote Control needs Pro, Max, Team or Enterprise; API keys are
not supported; on Team and Enterprise an Owner must enable it. *Explainability:* it cannot run behind the
explainability proxy; should doctor recommend turning tracing off for remote-controlled roles? Owner's call.
*Settings-level alternative:* `remoteControlAtStartup: true` in a slot's user `settings.json` turns it on for
every session on that account, hand-launched ones included, and through `core/claude_sharing.py` for every
slot that links its settings. Document it as the all-sessions route; aisquare does not set it.

**Industry grounding.**
- Claude Code docs, "Continue local sessions from any device with Remote Control" —
  https://code.claude.com/docs/en/remote-control. `claude --remote-control "My Project"` starts an interactive
  session that is also controllable remotely; "One remote session per interactive process"; "outbound HTTPS
  requests only and never opens inbound ports"; unavailable when `ANTHROPIC_BASE_URL` points at a
  non-Anthropic host; "API keys are not supported."
- Claude Code docs, "Authentication" — https://code.claude.com/docs/en/authentication. A `claude setup-token`
  / `CLAUDE_CODE_OAUTH_TOKEN` token "can only make model requests, so it can't establish Remote Control
  sessions".

**Linked issues.** #289.

---

## W5 — Adopt existing Claude logins and sessions

**Problem.** A fresh start today means deleting `~/.aisquare`, losing onboarded projects and account slots. A
hand-made Claude Code profile (`~/.claude2`, `~/.claude-work`, …) joins only through a new sign-in into a new
slot, and a hand-started session cannot move under the fleet. Such profiles are common: #98's original report
describes `~/.claude` and `~/.claude2` to `~/.claude5` on one machine (2026-09-05), and #198 a hand-built
shared-state setup.

**Where the code is today.**
- **`accounts add` only creates a fresh numbered slot** under `~/.aisquare/claude-accounts/<n>/` with a marker
  (`cli/accounts.py:592-652`; `core/claude_accounts.py:292-318`). **Slot 1 is whatever a plain `claude` uses**
  (`core/claude_accounts.py:18-25`).
- **Owner decision, 2026-09-09:** "the CLI never adopts a directory it did not create — only CLI-made slots
  exist, for consistency" (`docs/plans/claude-accounts.md:20-23`). Phase A reverses it.
- **On macOS the Keychain item is named by a hash of the exact `CLAUDE_CONFIG_DIR` string**
  (`keychain_service`, `core/claude_accounts.py:609-626`), so moving or symlinking a directory loses its
  login.
- **The fleet can already start a window on an existing session**: `ResumeSpec(session_id, transcript_path)`
  (`services/fleet.py:228-246`), used by restart, switch and the Spawn dialog's fork (`hand_off`,
  `services/fleet.py:7701`). No command offers it for a session with no fleet row.
- **A non-destructive reset exists:** `config reset` moves the config aside (`cli/config.py:85-115`); `project
  forget/prune` and "Remove from fleet…" exist since 0.8.1.

**What to build.**
- **Phase A, `aisquare accounts adopt <dir> [--alias NAME]`** (owner decision first). It creates the next slot
  directory with a marker carrying `"adopted": "<dir as given>"`; `_managed(slot)` reads it, so `config_dir`
  is the adopted path byte for byte, which keeps the Keychain item, while `tmp_dir` stays ours. It refuses a
  directory that does not exist, slot 1's directory, one already adopted, anything inside `~/.aisquare`, and a
  directory with no login (no `.claude.json` `oauthAccount`, and no credentials file or Keychain item). It
  offers the hooks the way `complete_sign_in` installs them. `accounts remove` on an adopted slot removes only
  our slot directory, never the user's, and the receipt says so.
- **Phase B, `aisquare fleet adopt <session-id|transcript.jsonl> [--role R] [--label L]`.** It finds the
  transcript under the known config dirs (`<dir>/projects/<slug>/<id>.jsonl`); the account is the config dir
  it lives in. It refuses while a `claude` process still holds that session (`ps`/`/proc`: `--resume <id>` or
  the transcript open), since two processes would write one conversation, and otherwise spawns with
  `resume=ResumeSpec(id, path)`. The asq Accounts page gets "Adopt…" next to *Add*; the sessions view (W2c)
  gets "Adopt into fleet…".
- **Phase C: docs only.** A "Fresh start without losing accounts" recipe: `config reset`, `project prune`, and
  never deleting `~/.aisquare/claude-accounts`.

**Acceptance criteria.**
- [ ] (A) Adopting a scratch directory with a stub login lists it as slot N with `config_dir` equal to the
  given path, and `launch --account N` exports exactly that string.
- [ ] (A) On macOS, an adopted directory's existing Keychain login reads as signed in with no new login (a
  manual check on the RC, recorded in the PR).
- [ ] (A) `accounts remove N` leaves the adopted directory untouched.
- [ ] (B) Adopting a transcript whose session is held by a live process is refused. One that is not held
  starts a fleet row whose session id equals the adopted one.
- [ ] (B) The adopted session's board row keeps its id. No second session row appears.

**Tests.** `tests/test_claude_accounts.py` (marker pointer, refusals; failing first: `adopt` does not exist).
`tests/test_claude_keychain.py` (service name of an adopted path). `tests/test_fleet_service.py` (adopt via
`ResumeSpec`). `tests/test_stubs.py`: add `("accounts", "adopt")` and `("fleet", "adopt")`.
**Docs.** `docs/fleet.md` §Accounts: "Adopting an existing profile" and "Adopting a session".
`docs/plans/claude-accounts.md`: the decision log records the reversal, or the refusal.
**Telemetry/measurement.** None ongoing; the PR records fresh logins needed to bring hand-made `~/.claudeN`
profiles in (today one per profile; after, zero).

**Risks and open questions.** *Owner: reverse the 2026-09-09 "never adopt" decision?* If not, phase A becomes
a documented manual recipe, and only phase B ships. *Cross-account `--resume`* is still marked unverified
(`docs/plans/claude-accounts.md:343-346`); phase B resumes on the transcript's own account, which never
crosses accounts. *The transcript format is internal* (Claude Code docs); phase B needs only the path, never
the content.

**Industry grounding.**
- Claude Code docs, "Authentication" — https://code.claude.com/docs/en/authentication. Multiple accounts are
  separate `CLAUDE_CONFIG_DIR`s (`alias claude-work='CLAUDE_CONFIG_DIR=~/.claude-work claude'`); with the
  variable set, Claude Code "keys the macOS Keychain entry to that directory too, so a session with a
  different `CLAUDE_CONFIG_DIR` reads a different entry."
- Claude Code docs, "Sessions" — https://code.claude.com/docs/en/sessions. Transcripts are at
  `~/.claude/projects/<project>/<session-id>.jsonl`; `--resume` takes an id or an absolute `.jsonl` path; "The
  entry format is internal … scripts that parse these files directly can break on any release."

**Linked issues.** #290. #198 (P2) is related.

---

## W6 — Small fixes: the label limit, #269, #270, #271

Four independent PRs, each a one-sitting fix with a failing-first test.

### W6a — The label limit

- **Problem.** A `fleet spawn --label` over 24 characters is refused only at spawn time. By then the manager
  has created the task in a separate `task add`, so the task is left without an agent. The limit is stated
  nowhere the manager reads, and two code paths produce an over-long label on their own.
- **Code today.** `LABEL = ^[a-z][a-z0-9-]{1,23}$` (`services/fleet.py:120`) is checked in `_fixed_label`
  (`:1060-1086`) before anything starts. `next_label`'s collision suffix (`:1113-1118`) builds `f"{stem}-{n}"`
  without re-validating, so a 24-character label held by a live agent becomes 26 characters. The task-derived
  base `f"{role}-{task[:8]}"` (`:1108`) is not validated either; a long custom role exceeds 24. The manager
  cycle says "Labels are unique and descriptive (coder-auth, not coder-2)" (`core/harness.py:1058-1059`), with
  no limit. Only the Spawn dialog live-checks the rule.
- **Build.** (1) `next_label` trims the stem so `stem + "-N"` fits in 24 characters, and validates every base
  it returns. (2) The refusal names a fitting suggestion, for example `label 'coder-authentication-refactor'
  is 29 characters — at most 24; try 'coder-authentication-ref'`, so the manager can retry in the same turn.
  (3) The cycle sentence becomes "Labels are unique, descriptive and at most 24 characters (coder-auth, not
  coder-2)"; re-pin it in `tests/test_harness.py`.
- **Acceptance.**
  - [ ] A held 24-character label suffixes to a valid 24-character label.
  - [ ] A long custom role with a task gives a valid label.
  - [ ] An over-long `--label` is refused with a suggestion that itself passes `is_label`.
  - [ ] The cycle names the limit.
- **Tests.** `tests/test_fleet_service.py` (failing first: the 26-character label is returned today) and
  `tests/test_harness.py`.

### W6b — #269: context search crashes on AND/OR/NOT and never matches non-ASCII

- **Still reproduces on `b26dd140`.** `_fts_match` (`core/store.py:1751-1758`) tokenizes `[A-Za-z0-9]+` and
  emits `token*`. Run against `b26dd140` for this audit: `'AND' -> 'AND*' ERROR fts5: syntax error near
  "AND"`, the same for `OR` and `NOT`; `'вход' -> ''`, so no results. The index uses FTS5's default
  `unicode61` tokenizer (`core/store.py:86-88`), so stored text is tokenized correctly; only the query builder
  is wrong.
- **Build.** Tokenize the query with `[^\W_]+` (Unicode letters and digits, mirroring `unicode61`'s token
  classes). Emit each token as an FTS5 string with a prefix star, `'"' + t.replace('"', '""') + '"*'`, joined
  by ` OR `.
- **Acceptance.**
  - [ ] `AND`, `OR`, `NOT` and `NEAR` return "No entries match" and exit 0.
  - [ ] `вход`, `ログイン` and `हिन्दी` match stored entries.
  - [ ] A token containing `"` does not break the query.
- **Tests.** `tests/test_store.py`, plus `tests/test_context.py` for the CLI exit code.

### W6c — #270: tasks whose titles have no ASCII letters merge

- **Still reproduces.** `task_key` (`services/team.py:620-623`) returns `"task"` for any title whose ASCII
  slug is empty; in this audit both `Исправить вход` and `Добавить тесты` gave `task`. `add_task` keys the
  upsert with it (`services/team.py:1426`).
- **Build.** (1) NFKC-normalise and casefold the title. (2) Keep characters whose Unicode category starts with
  `L`, `N` or `M` (marks matter: `\w` alone splits Devanagari at the virama); replace every other run with
  `-`, then strip and clip to 64. (3) If nothing is left (an emoji-only title), use `t-<sha256[:12]>` of the
  normalised title. (4) Pure-ASCII titles must give exactly the old key; pin this with a table of titles. (5)
  Mixed titles change key; so that re-adding the same title after the upgrade stays idempotent, `add_task`
  also looks up the legacy key, but only when the legacy key is not `"task"` and the stored title is
  identical.
- **Acceptance.**
  - [ ] Two Cyrillic titles create #1 and #2.
  - [ ] Two CJK titles create distinct tasks.
  - [ ] ASCII titles keep their keys.
  - [ ] Re-adding a pre-upgrade mixed title answers "already tracked".
- **Tests.** `tests/test_team.py` (failing first: the second add is idempotent today).

### W6d — #271: `project forget` from inside the project writes a machine-wide pin

- **Still reproduces.** `forget_project` (`services/project.py:452-482`) computes `was_active` from
  `active_project(store)`, which falls back to the working directory when there is no pin, then calls `_repin`
  (`:472`), which pins the most recently touched project (`:918-939`, through `pin_project`,
  `core/workspace.py:368-390`). `prune` does the same (`:876`).
- **Build.** Repin only when a pin existed and named the forgotten project, and then clear it
  (`pin_project(None)`) rather than move it. The CLI line becomes "the pin on <name> was cleared", or nothing
  when no pin existed.
- **Acceptance.**
  - [ ] Forgetting from inside the project with no pin creates no `state.json`.
  - [ ] With a pin naming it, the key is removed.
  - [ ] With a pin naming another project, the pin is unchanged.
  - [ ] Prune behaves the same in all three cases.
- **Tests.** `tests/test_project_forget_prune.py` (failing first: `state.json` appears today).

**For all of W6.**
- **Docs.** `docs/fleet.md` §Naming: one sentence on the suffix rule. `docs/memory.md`: context search matches
  any language, and `AND`/`OR`/`NOT` are plain words. `docs/orchestration.md` §Tasks: how a task's key is
  derived for non-Latin titles.
- **Telemetry/measurement.** None; these are correctness fixes. **Risks.** #270's key change for mixed titles;
  the legacy lookup covers re-adds.

**Industry grounding (W6).**
- SQLite, "FTS5 Extension" — https://www.sqlite.org/fts5.html (page footer 2026-10-09). A bareword may not be
  "AND", "OR" or "NOT" (case sensitive); in a string "any embedded double quote characters may be escaped
  SQL-style - by adding a second double-quote character"; "If a "\*" character follows a string … the final
  token … is marked as a prefix token"; `unicode61` is the default tokenizer.
- Python docs: `re` (`\w` "Matches Unicode word characters" for str patterns), `unicodedata` (NFKC) and
  `str.casefold` ("intended to remove all case distinctions") — https://docs.python.org/3/library/re.html,
  https://docs.python.org/3/library/unicodedata.html, https://docs.python.org/3/library/stdtypes.html.
- Django, `slugify(allow_unicode=True)` — https://docs.djangoproject.com/en/stable/ref/utils/ (Django 6.1
  docs). It applies NFKC and keeps Unicode word characters; the documented example is `'你好-world'`.

**Linked issues.** #291, #269, #270, #271.

---

## W7 — Docs: instruction precedence

**Problem.** A user's own CLAUDE.md files and memories can contradict aisquare's role instructions, for
example a global "use subagents for parallel work" against the manager cycle's "spawn coder agents". Nothing
documents which one an agent follows, or how to resolve the conflict.

**Where the code is today.** A fleet agent receives instructions through these channels, in this order:
1. **Claude Code's system prompt**, plus the persona through `--append-system-prompt-file`
   (`cli/launch.py:168-245`; `[persona] system_prompt`, `core/config.py:693-700`). This is #210, shipped with
   #201 and #240.
2. **The user's CLAUDE.md files**, which Claude Code loads itself.
3. **The SessionStart hook's stdout**, added as context: the `<aisquare-team>` briefing with the role cycle
   (`core/harness.py:931-951`), the board, the snapshot pointer and the persona block (`cli/hook.py:120-139`,
   `services/team.py:1870`).
4. **The per-prompt team delta** (`cli/hook.py:142-160`) and the manager's Stop-hook wake.
5. **aisquare's own memory.** `agents connect` ingests `~/.claude/CLAUDE.md` into the user context pool
   (`services/agents.py:645-651`), so its entries can come back through injection too.

Native agent teams are off in fleet launches (`[fleet] disable_native_agent_teams = true`,
`core/config.py:614-631`). No user doc explains any of this; `docs/` has no CLAUDE.md guidance at all (grep).

**What to build.**
- **W7a (docs). A new `docs/instructions.md`, "Which instructions an agent follows".** It covers the channels
  above and where each lands (system prompt, user message, system reminder); Anthropic's rule that CLAUDE.md
  files are concatenated and conflicts resolved arbitrarily, so the fix is to remove a conflict, not rank it;
  a recommended CLAUDE.md snippet for fleet users ("When you are briefed as an aisquare fleet agent (an
  `<aisquare-team>` block), delegate through the board and `fleet spawn` as it says; use subagents only for
  read-only research inside your own task"); that subagents load CLAUDE.md but not the session's hook context,
  so a rule that must reach them belongs in CLAUDE.md; `/doctor prompt-audit` (Claude Code ≥ 2.1.283) to find
  conflicts; removing ingested CLAUDE.md entries from aisquare's user pool; and the 10,000-character cap on
  hook output. Link it from `docs/orchestration.md` and `docs/personas.md`.
- **W7b (small code, optional; owner's call because it overlaps P1's lean role context work).** A test
  measures the session-start output per role with a persona at the 4,000 soft cap and fails above 10,000
  characters. When the output would pass 9,500 characters and the persona already rides the system prompt
  (Claude Code, `system_prompt = true`), the hook names the persona instead of repeating its body. Measured on
  `b26dd140` for this audit: the manager's cycle alone is 2,399 characters (28 lines). Above the cap Claude
  receives only a file path and a 2,000-character preview, which does not even hold the whole manager cycle.

**Acceptance criteria.**
- [ ] `docs/instructions.md` exists, every command in it is in a `text` fence, and it passes
  `tests/test_documented_commands.py`.
- [ ] It is linked from `docs/orchestration.md`, `docs/personas.md` and the README's docs list.
- [ ] (W7b) The size test is green for the bundled personas (the largest, `captain/SKILL.md`, is 1,433 bytes)
  and red for a 9,000-character user persona without the trim.

**Tests.** `tests/test_documented_commands.py`, and `tests/test_persona_briefing.py` (W7b). **Docs.** As
above.
**Telemetry/measurement.** For W7b, session-start output characters per role, before and after, in the PR.

**Risks and open questions.** W7b changes what the hook prints, which P1's lean role context work also
budgets. Decide which train owns it.

**Industry grounding.**
- Claude Code docs, "How Claude remembers your project" — https://code.claude.com/docs/en/memory. "All
  discovered files are concatenated into context rather than overriding each other"; "if two instructions
  contradict each other, Claude may pick one arbitrarily"; "CLAUDE.md content is delivered as a user message
  after the system prompt" (for system-prompt-level instructions use `--append-system-prompt`); `/doctor
  prompt-audit` finds conflicting files.
- Claude Code docs, "Hooks" — https://code.claude.com/docs/en/hooks. `additionalContext` is wrapped "in a
  system reminder and inserted … at the point where the hook fired". Hook strings and plain stdout "are capped
  at 10,000 characters"; over the cap, Claude gets "the file path and a preview of up to the first 2,000
  characters".
- Claude Code docs, "Subagents" — https://code.claude.com/docs/en/sub-agents. "Subagents receive only this
  system prompt plus basic environment details like the working directory"; they still load the CLAUDE.md
  hierarchy.
- Claude Code docs, "CLI reference ▸ System prompt flags" — https://code.claude.com/docs/en/cli-reference.
  `--append-system-prompt[-file]` appends to the default prompt; `--system-prompt` replaces it, dropping tool
  guidance and safety instructions.

**Linked issues.** #292. #210 is closed by this train.

---

## W8 — Multi-machine fleet (postponed; design sketch and interim runbook)

**Problem.** A fleet cannot span machines (a captain on one machine tasking managers on others; tests and
docker on one machine, coding on another). A workaround works today: give a manager SSH access to another
machine with aisquare installed. The feature is postponed and not in this train, which ships a runbook for the
workaround and a design sketch.

**Where the code is today.** No remote-host fleet, SSH spawn or cross-machine messaging exists. `aisquare
serve --bind <LAN>` lets a remote Claude client join one board over MCP (`docs/orchestration.md:253-294`), but
its tools only read and write the board (`team_board`, `task_add`, `task_next`, `task_update`, `note_add`,
`team_log`, `verify`, `signal`, `recall`; `services/mcp_server.py:196-372`) and can neither spawn nor steer.
`aisquare remote`'s authenticated HTTP API acts on agents (`agent/tell|stop|restart|switch`,
`services/remote_actions.py:100`) but has no spawn and is built for a phone.

**What to build now (docs only).** A new `docs/runbooks/multi-machine-ssh.md`.
- On box B: install aisquare with the one-line installer; run `aisquare project onboard` in the repo; run
  `aisquare accounts add` per account and connect Claude Code.
- On box A: key-based SSH with `ControlMaster auto`, `ControlPersist 10m` and `ServerAliveInterval 15` in
  `~/.ssh/config`. Brief the manager (a persona or a CLAUDE.md line) with the commands below; use an absolute
  path or `bash -lc`, because non-interactive SSH may not have `~/.local/bin` on `PATH`. Watch B by running
  `asq` there inside tmux (`ssh -t boxb tmux new -As asq asq`), or use mosh on flaky links.

  ```text
  ssh boxb -- ~/.local/bin/aisquare --json fleet ls --project <codename>
  ssh boxb -- ~/.local/bin/aisquare fleet spawn coder --project <codename> --label coder-docker --task 12 --prompt "<brief>"
  ssh boxb -- ~/.local/bin/aisquare team log --since-seq <N> --json
  ssh boxb -- ~/.local/bin/aisquare fleet tell coder-docker --project <codename> "<steer>"
  ```

- Security: B trusts A's key completely. Use a dedicated user, or a key restricted to aisquare commands.

**The design sketch (in the issue, postponed).** The recommended first version is an SSH transport for the
fleet verbs: `[fleet.hosts.<name>] ssh = "boxb"`, and `fleet spawn --host boxb` runs the same CLI remotely;
the row is recorded locally with a `host` column (a store migration); the pane renders through #272's
control-mode client over `ssh boxb tmux -L asq -C attach …`, so asq shows remote panes like local ones; board
events route back through a pull of `team log --since-seq` (or P2's parent-manager routing, #199).
Alternatives considered: extending `aisquare remote`'s HTTP API with spawn and a machine token, or extending
the MCP `serve` tools.

**Acceptance criteria.**
- [ ] The runbook exists, every command passes `tests/test_documented_commands.py`, and it is walked once on
  two machines (or a VM), with the transcript of that walk attached to the issue.
- [ ] The issue carries the design sketch, and its title and body say "postponed" (there is no such label in
  the repo's set).

**Tests.** `tests/test_documented_commands.py`. **Docs.** As above, plus one line under `docs/fleet.md`
§Later. **Telemetry/measurement.** None.

**Risks and open questions.** The SSH trust model: a manager with a shell on B can do anything that user can.
Owner: should the runbook recommend a forced command?

**Industry grounding.**
- Claude Code docs, Remote Control ▸ Limitations — https://code.claude.com/docs/en/remote-control. "If you run
  `claude` from a terminal on a remote machine, start it inside `tmux` or `screen` to keep the session running
  after you disconnect from SSH."
- OpenBSD, `ssh_config(5)` — https://man.openbsd.org/ssh_config.5 (2026-10-09). `ControlMaster` "Enables the
  sharing of multiple sessions over a single network connection"; `ServerAliveInterval` with
  `ServerAliveCountMax 3` drops a dead link in about 45 seconds.
- Mosh — https://mosh.org/ (1.4.0, 2022-10-31). Survives roaming and sleep and syncs only the visible screen:
  "the workaround is to use screen or tmux on the remote side."

**Linked issues.** #293. #199 (P2) is related.

---

## W9 — Persona and captain follow-ups (#210, #211, #212, #213, #216)

- **#210: close.** It shipped in `b39f6fa9` ("the persona rides the system prompt too, with a switch"), fixed
  in `d9cafd74`, merged with #201 and #240. `_persona_system_prompt` (`cli/launch.py:174-245`) writes the
  block under `~/.aisquare/cache/persona-prompts/` and passes `--append-system-prompt-file`, respecting
  `[persona] system_prompt = false` (`core/config.py:693-700`) and an operator's own flag. The CHANGELOG
  `[Unreleased]` entry cites #210. Residual: `docs/plans/spawn-personas.md` §3.2 (`:204-227`) still says
  "Rejected: `--append-system-prompt-file`"; this train adds a dated note there pointing to decision 8 of
  2026-09-24.
- **#211: narrowed.** Already landed in `7895091a`: Esc or Cancel on a running import publishes nothing, and
  the dialog closes (`cli/ui/persona_dialogs.py:418-431`). Left: the `claude -p` child still runs until
  `MANAGER_TIMEOUT_SECONDS = 180` (`services/persona_import.py:35`; `subprocess.run(..., timeout=timeout)` at
  `:184-196`), and the CLI has no exit-130 handling. Build: (1) `draft_with_manager(..., cancel:
  threading.Event | None)` uses `Popen` with `communicate(timeout=0.2)` in a loop; on `cancel` it calls
  `terminate()`, waits 2 s, then `kill()`, and raises `ImportCancelled`. (2) `draft`, `draft_with_api` and
  `import_source` pass the token on; the API engine checks it before and after its call, discards a late
  answer, and closes its client. (3) The dialog's `action_cancel` sets the token before `dismiss`. (4)
  `persona import` maps Ctrl-C to the token and exits 130. Cancel-to-exit: up to 180 s today; target ≤ 2 s.
- **#212 (update only).** The `$EDITOR` pane builds on the Accounts sign-in pane's `TerminalPane`, which #272
  rewrites for control mode; build on #272. The `TextArea` is still at `cli/ui/persona_dialogs.py:637`; size
  S–M (on #272, merged).
- **#294** (split out of #213). `persona import <a> <b> …` and `--from-dir <dir>`;
  each source goes through the existing ladder (`services/personas.py:142` `import_source`). One receipt table
  (source, name, engine, outcome); `--json` gives a list of results. One refusal does not stop the rest. Exit
  0 when all were imported, 1 when any was refused. Without a TTY, a draft that needs confirmation reports
  `needs_confirmation` unless `--yes` is given, as today.
- **#213: rewritten to `extends` alone, as an owner decision.** Recommendation: close as won't do. A persona
  is a Claude Code skill "byte for byte" (`docs/personas.md:5-8`); Claude Code does not resolve `extends`, so
  `/child` in Claude Code would run without its parent; and the provenance sha256 would no longer describe
  what the agent was briefed with. For composition, `persona new <name> --from <parent>` would copy the
  parent's body and keep interchange.
- **#216: unblocked by #240** (update with the design anchors). Every captain tool funnels through `_run`
  (`services/captain/actions.py:204-265`); the gate checks there against a set of privileged tool names:
  `press`, `paste`, `stop`, `spawn`, `restart`, `wololo`, `attach_persona`, `act`. `tell` and `ask_manager`
  also spend quota; gating them is the owner's call. `[captain] gate = true` applies only while the captain is
  on (experimental, off by default). pyotp is not a dependency today (grep `src/` and `pyproject.toml`); add
  it to an extra, not core. Follow RFC 6238 (a 30 s step, at most one step of drift, no reuse of an accepted
  code) and RFC 4226 §7.3 for throttling, which also gives the issue's "three wrong codes" its rule. Stays
  Phase 2 per `docs/captain.md:387`.

**Acceptance criteria.**
- [ ] #210 closed with the comment in `issues.md`, and the plan doc's note added.
- [ ] #211: a headless Cancel during a fake `claude -p` (a `sleep 60` stand-in through the registered seam)
  ends the child within 2 s. CLI Ctrl-C exits 130 with no child left and no persona written.
- [ ] Batch import: three sources, one invalid, give two imported and one refused, exit 1, and one table, with
  `--json` as a list.
- [ ] #213 decided by the owner, and closed or kept accordingly.

**Tests.** `tests/test_persona_import.py` and `tests/test_a_cancelled_import_publishes_nothing.py` (#211;
failing first: the stand-in child survives Cancel today). `tests/test_persona_cli.py` (batch import, exit
codes).
**Docs.** `docs/personas.md`: batch import, and what cancel does. The plan doc note for #210.
**Telemetry/measurement.** #211's cancel latency, as above.

**Industry grounding.**
- RFC 6238, TOTP — https://www.rfc-editor.org/rfc/rfc6238.txt (May 2011). "We RECOMMEND a default time-step
  size of 30 seconds"; "at most one time step is allowed as the network delay"; "The verifier MUST NOT accept
  the second attempt of the OTP after the successful validation".
- RFC 4226 §7.3 — https://www.rfc-editor.org/rfc/rfc4226.txt. "We RECOMMEND setting a throttling parameter T".
- PyOTP docs — https://pyauth.github.io/pyotp/ (no date shown). `verify(otp, for_time=None, valid_window=0)`
  and `provisioning_uri(name=…, issuer_name=…)`; its checklist says to reject reused codes and to throttle.

**Linked issues.** #210, #211, #212, #213, #216, #294.

---

## W10 — Setup and config: #98

### #98 — role bindings in Settings, and doctor validating each binding

- **Code today.** The service writers exist: `bind_role` (`services/settings.py:86-142`) and
  `clear_role_binding` (`:158-167`); `team bind` takes `--unset`, `--clear` and `--clear-account`
  (`cli/team.py:860-940`). #201 added a *+ New bind* form inside the persona picker, but nothing edits, unsets
  or clears a binding in the UI. App settings excludes model, effort and binary per role "on purpose"
  (`cli/ui/views/app_settings.py:18-24`), which #98's 2026-09-06 comment asked to reverse. Doctor checks only
  bindings that name an account (`_claude_account_default_checks`, `services/diagnostics.py:1908-1992`); an
  `env` binding's `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_TMPDIR` are never checked. `team bind --import` was
  never built and is moot: the second-home workaround existed for a 305-project store (#83), which 0.8.1's
  capture-off removed.
- **Build.** App settings gets a "Role bindings" table: one row per bound role with bin, env
  (`CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_TMPDIR` called out) and args; *Edit* opens the existing bind form
  prefilled; *Unset key…* and *Clear* write through `services.settings`. A doctor row, `role bindings`, checks
  that each binding's `CLAUDE_CONFIG_DIR` expands to a directory with aisquare's hooks (the same "connected"
  answer Welcome uses) and that `CLAUDE_CODE_TMPDIR` is writable; a mismatch warns with the `team bind` fix
  line. Drop the import item. Record the owner's decision on model and effort in the issue and in the
  docstring.
- **Acceptance.**
  - [ ] Headless edit, unset and clear round-trip `config.toml` exactly as `team bind` writes it.
  - [ ] Doctor warns on a binding pointing at a directory with no hooks, and on an unwritable TMPDIR. It is
    silent when no env bindings exist.
- **Tests.** `tests/test_ui_app_settings.py`, `tests/test_settings.py`, and `tests/test_doctor_fleet.py`
  (failing first: no row today).

### `/login` inside a fleet pane (closed #262)

#262 was closed on 2026-10-23: 0.8.1/0.8.2 read each account's login from the macOS
Keychain (#263), and sign-in works in practice on macOS. No work remains in this train.

**For all of W10.**
- **Docs.** `docs/reference.md`: no new keys. **Telemetry/measurement.** None.
- **Risks and open questions.** Owner: should model, effort and binary per role go in Settings (#98's scope
  addition), or stay out under the "one home per concept" rule? `CLAUDE_CODE_OAUTH_TOKEN` cannot run Remote
  Control (W4); the docs must say so.

**Industry grounding.**
- Claude Code docs, "Authentication ▸ Credential management" — https://code.claude.com/docs/en/authentication.
  "When the Keychain rejects the write, such as when it's locked in an SSH session, Claude Code stores your
  login in `~/.claude/.credentials.json` with file mode `0600` instead"; `claude setup-token` makes a one-year
  token that "can only make model requests".
- Claude Code docs, "Sessions" — https://code.claude.com/docs/en/sessions. Each `CLAUDE_CONFIG_DIR` keeps its
  own session history, which is why a binding's directory must carry the hooks.

**Linked issues.** #98; #262 (closed).

---

## Release checklist for the train

- [ ] **Version.** All P3 work lands under `[Unreleased]` beside #201, #240 and #267. `pyproject.toml` reads
  `0.8.2` on `b26dd140`; the next release's number is the owner's call (`origin` carries `*0.9-*` branches).
- [ ] **CHANGELOG.** One entry per workstream, in the file's voice: bold lead, then what changed, then the
  measured figure where there is one.
- [ ] **Migration notes.** Store: none (W5 uses marker files, W2c uses cache files plus existing
  `claude_usage` rows, the W8 `host` column is postponed). **#270 changes task keys** for titles with
  non-ASCII words; the legacy lookup keeps re-adds idempotent; say so under "Upgrading". **#271** stops
  writing pins; a pin an earlier build wrote stays until the next forget of that project or a `project unpin`.
- [ ] **New config keys** with defaults, documented in `docs/reference.md`: `[fleet.roles.<role>]
  remote_control = false` (W4); `[captain] gate = true`, effective only with the captain on (#216, if built
  this train). No `config_version` step expected; confirm against `core/config.py`'s `_keep_unknown` rules.
- [ ] **New commands** in `tests/test_stubs.py` `IMPLEMENTED`: `accounts statusline`, `accounts sessions`,
  `accounts adopt` (if decided), `fleet adopt`; and the console script `asq-statusline` in `pyproject.toml`
  `[project.scripts]`.
- [ ] **Uninstall.** `aisquare uninstall` removes the `statusLine` entries aisquare wrote (W2c).
- [ ] **Rollout defaults.** Remote Control is off. The statusline is opt-in (`--install`). The board strip and
  the Accounts "Next:" line are on. Phase A of adopt is behind the owner's decision.
- [ ] **README** stays under 250 lines; add only a link to `docs/instructions.md`.
- [ ] **A real-Claude pass on the RC head** covers W1's round trip, W3's matrix, W4 on one Pro/Max account and
  W2c on two slots. Paste the outputs into the RC PR.

## Out of scope

- **Integration with external session-tracking tools.** Each account has its own `CLAUDE_CONFIG_DIR`, so a
  tool that reads only `~/.claude` sees one account. W2c's sessions registry is a local file such a tool could
  read, but nothing here targets one.
- **Web UI components** (for example a branched menu built in React): relevant only to a web UI.
- **Replacing tmux, or a browser UI**: P2 and #272.
- **Delivery, wake, capacity and account-switching work**: P0, P1 and P2. W2 depends on P0 W3's parsing of
  weekly windows, but does not change how an account is picked.
- **The multi-machine fleet itself.** W8 ships the runbook and the sketch only.
