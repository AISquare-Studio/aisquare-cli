# Fleet TUI — one `asq` view over every project, agent and session

> **Status: implemented on this branch** (PR #71; the Decisions log at the bottom
> records what landed and every deviation). This is a living document: update it
> in the same PR as the code it describes, and keep the *Decisions log* at the
> bottom current. Branch `plan/fleet-tui`; draft PR
> <https://github.com/AISquare-Studio/aisquare-cli/pull/71>. The owner's decisions
> of 2026-08-28 (§12) are folded into the text; every default named below is a
> *default* — changeable in config, in the Settings tab and per spawn (§3.10).
>
> Verified against `main` @ `905c68b` (0.5.0) on 2026-08-28 with Textual 8.2.8,
> tmux 3.7c, Claude Code 2.1.250, Python 3.14 locally and
> 3.11–3.13 in CI. Paths marked **(new)** do not exist yet.
>
> Every fenced block in this file is tagged `text`, `toml` or `mermaid` on
> purpose: `tests/test_documented_commands.py` sweeps the whole repo and treats a
> shell-tagged or bare fence as a script whose every `aisquare …` line must
> resolve against the live command tree. The commands below are *planned*, so
> they are shown as references, not scripts. Keep it that way until they exist.

---

## 0. The ask, restated as acceptance criteria

From the owner's brief. Each line is something the finished feature must do.

1. `aisquare` / `asq` with **no arguments**, at a terminal, opens a full-screen
   UI with mouse support. Scripts, `--help` and `--json` callers see exactly
   what they see today (usage, exit 2) — a non-TTY never gets a TUI.
2. Two panes: a narrow **navigator** on the left, a wide **content area** on the
   right.
3. Left: a **Fleet** heading with a `+`. Clicking it opens onboarding on the
   right — browse or type a directory; if it resolves, the UI runs the
   equivalent of `aisquare init <path>` and then `aisquare doctor` **in the
   background, without leaving the UI or prompting**, and lists the project.
4. Doctor findings are visible with the fix for each: a **Doctor** section at
   the bottom of the left pane, detail on the right, one-click fixes where the
   fix is a known command.
5. Many projects, visually separated by alternating background; they persist
   (they already do — the `project` table).
6. Clicking a project shows its **manager**: an agent you task in prose. It
   plans, spawns sub-agents (code / test / review), loops until it judges the
   output matches the goal with production-grade edge-case testing, and reports.
7. Sub-agents appear indented under the project with a per-role icon; the
   manager keeps their names unique.
8. Clicking an agent — or the manager — shows **the real session**: the actual
   Claude Code TUI, `screen`-style. We started it in the background and
   are monitoring it; the UI surfaces it. **Not** a chat relayed through us.
9. `+` on a project lets the user spawn their own agent.
10. Onboarding a project, connecting agents, wiring Explainability, watching and
    steering a fleet — all reachable from inside the UI.
11. Open source, PR-driven, and every existing guard in this repo (CI matrix,
    seams registry, doc sweeps, JSON-stdout sweep) stays green.

**Non-goals for v1, stated so nobody has to re-derive them:**

- Replacing the agent's own UI with ours (that is Toad's project, §3.1).
- Remote or multi-machine fleets. The cloud roadmap is unchanged.
- Windows native (§3.9). WSL2 is the Windows story, as it already is.
- Codex, or any agent other than Claude Code. v1 is Claude Code only; the
  substrate is agent-agnostic and Codex is the first candidate afterwards (§8.5).
- Auto-merging PRs. A human merges in v1 (§3.5).

---

## 1. What already exists, and how the fleet reuses it

The repo is a thin Typer CLI over services over one SQLite store. Almost every
primitive the fleet needs is already there; the fleet is a new *view* plus a
process substrate.

| Capability | Where | How the fleet uses it |
| --- | --- | --- |
| Project registry and identity; worktrees resolve to the principal repo | `core/workspace.py` (`find_project_root`, `git_common_root`), `store.project` | The left pane **is** `store.list_projects()`. A coder in a worktree shares its project's board for free. |
| Setup and onboarding | `services/lifecycle.initialize()`, `services/project.onboard()` (Repomix snapshot) | Run from the Onboard view, in the background (§5.6). |
| Doctor | `services/diagnostics.doctor()` → `DoctorCheck(name, status, detail, fix)`; `explainability_ops.apply_fixes` | Doctor section + view render these; fix buttons run known fixes. Note: several checks resolve the project from the **cwd**, so per-project runs need care (§5.6). |
| Session lifecycle | Five Claude Code hooks (`core/agents._HOOKS`) → `team_session.state ∈ {working, waiting, attention}`, `transcript_path`, `model`, `effort` | Agent rows' state chips come from here, unchanged. The manager wake-up rides on `Stop` (§7.3). |
| Roles, model ladder, effort offsets, launch profiles | `core/harness.py`, `team spawn`, `team bind`, `AISQUARE_MODEL_<ROLE>` | Manager and sub-agents launch through `aisquare launch <role>` *inside tmux windows*, inheriting all of it and the explainability wiring. Zero new launch logic. |
| The board TUI | `cli/watch.py` (Textual; sessions, tasks, feed, detail, theme picker, select-text mode, transcript jump) | Its widgets become the **Board** tab of the project view; theme persistence in `state.json` is reused. |
| Task protocol | `services/team.py`: idempotent tasks, atomic leased claims, review/reopen, signals, per-prompt deltas | The manager loop is built on these. No new coordination protocol. |
| Explainability | `cli/explainability.py` (`status`, `enable`, `disable`, `register`, `ship`, `env`) and its services | An **Explainability** tab calls the same services. |
| Spawn-seam doctrine | `core/spawn.py::SEAMS` + `tests/test_spawn_seams.py` (AST walk) | Every new process start is registered with a ruling or the build fails (§10). |

Everything in the table is read-only reuse; the only *changed* behaviour in
existing code is the manager's `Stop` hook (§7.3) and the no-args entry (§3.8).

---

## 2. Architecture in one picture

```text
  asq  — one Textual process, a VIEW                       tmux server  -L asq  — the SUBSTRATE
 ┌──────────────────────────────────────────────┐          ┌────────────────────────────────────────────────┐
 │ left : Fleet ▸ projects ▸ agents ▸ Doctor    │ capture  │ session asq-amber-otter                         │
 │ right: onboard · manager · agent · board ·   │◄─────────│  ├─ window manager    : aisquare launch manager │
 │        doctor · explainability · settings    │ send-keys│  ├─ window coder-auth : aisquare launch coder … │
 └───────────────────┬──────────────────────────┘─────────►│  └─ window tester-1   : aisquare launch tester  │
                     │ reads (polls, like board -w)        │ session asq-quiet-lynx …                        │
                     ▼                                     └───────────────────────┬────────────────────────┘
        ~/.aisquare/context.db                                                     │ hooks, inside every agent:
        project · team_session · team_task · team_event                            │ SessionStart · UserPromptSubmit
        fleet_agent (new, §5.1)          ◄─────────────────────────────────────────┘ Stop · Notification · SessionEnd
```

**The TUI holds no state that matters.** Kill it and every agent keeps running
in tmux; reopen it and it re-attaches to what it finds. Anyone can also run
`tmux -L asq attach -t asq-<project>` from any terminal for full fidelity — that
escape hatch is a feature, and it is what makes the rendering hop (§6) a
convenience rather than a single point of failure.

---

## 3. Design decisions, with the alternatives rejected

### 3.1 Surfacing real sessions: tmux is the substrate, `capture-pane` is the renderer

| Option | Fidelity | Persistence | Dependency | Verdict |
| --- | --- | --- | --- | --- |
| **A.** In-process PTY + a Python terminal emulator (`pyte` via `textual-terminal`, or `bittty` via `textual-tty`) | Partial. pyte 0.8.2 last shipped Nov 2023; `textual-terminal` describes itself as "extremely slow"; `textual-tty` as "buggy and a bit slow". | Sessions die with the TUI unless we write a session broker — i.e. re-implement tmux. | pure pip | **Rejected for v1.** Possible later backend for hosts without tmux. |
| **B.** ACP (Agent Client Protocol) chat — Toad's approach, `claude-code-acp` adapter | We render our own chat, not the agent's UI: slash commands, permission dialogs, `/resume`, `/model` are ours to re-implement or lose. | n/a | node adapter | **Rejected.** It is exactly the "chat that syncs and relays" the brief rules out. |
| **C.** Claude Code's native agent teams (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`, tmux split panes) | Native. | Claude-only, experimental, one team per session, the lead is Claude's not ours, no nesting, teammates' permissions bubble to the lead. | none | **Not a substrate.** Coexistence rules in §7.6. |
| **D.** A tmux server on a private socket; the TUI renders `capture-pane -e` into Textual and sends input with `send-keys` / `paste-buffer` | tmux *is* a mature terminal emulator: truecolor, alternate screen, extended keys, bracketed paste. | Survives the TUI; attachable from anywhere; `remain-on-exit` keeps a crashed agent's last screen readable. | tmux ≥ 3.2, a system package (§8.2) | **Chosen.** |

Why D: fidelity and persistence come from software that has done this job for
fifteen years, and our side is a few hundred lines of plumbing. The real costs
are a system dependency and a rendering hop, both measured in the spike (§6, §9
Phase 0) before anything is built on top.

**Rendering.** One `tmux` process per frame, two commands in it:
`capture-pane -p -e -N -t %pane -S <s> -E <e>` and
`display-message -p -t %pane '#{cursor_x} #{cursor_y} #{cursor_flag} #{pane_width} #{pane_height} #{alternate_on} #{history_size} #{pane_dead} #{pane_dead_status} #{pane_in_mode}'`.
Each captured line → `rich.text.Text.from_ansi` → a Textual `Strip`; lines are
diffed against the previous frame so only changed rows repaint. Budget: ≤ 20 fps
while output changes, ~2 fps idle. Phase 2 of the pane may move to a control-mode
client (`tmux -C`) for `%output`-driven refresh, `refresh-client -C WxH` sizing
and `refresh-client -A %pane:off` to mute non-visible panes; polling comes first
because it is fifty lines and measurable.

**A private server, never the user's.** `tmux -L asq -f <bundled conf>`. We do
not read `~/.tmux.conf`, touch the user's sessions, or change their prefix key.
Bundled options: `status off` · `escape-time 0` (Esc must interrupt Claude
immediately) · `history-limit 50000` · `remain-on-exit on` · `mouse off` (we own
the mouse) · `default-terminal tmux-256color` · `terminal-overrides ',*:Tc'` ·
`extended-keys on` (CSI-u / modifyOtherKeys, so modifier chords can reach Claude
Code — §6 on the ones tmux cannot encode) · `set-clipboard off` ·
`focus-events on` · `monitor-activity on` (kept for an attached
`tmux -L asq attach` client; headless the flag is always set, so the ▶ pulse
comes from `history_size`/cursor changing between frames — measured on tmux
3.7c, pinned by `tests/test_tmux.py`).

**`window-size` is not in that file, and no window option may be.** A window
nobody is attached to still needs a size we choose, and `window-size manual` is
what holds one — but only as a *window* option. Set globally it kills the
server below tmux 3.7: creating a window reads that global through
`default_window_size`, which has no window yet, and `clients_calculate_size()`
then dereferences the window pointer it was never given — upstream added the
`w != NULL` guard in 3.7. Measured here on 3.4, the version `ubuntu-latest`
ships, both ways in: the option in the `-f` file kills the first `new-session`,
and a `set -g` on a running server kills the next `new-window`, each leaving the
client with `server exited unexpectedly`. That is 19 of the 22 CI failures on
this branch, and the rule it leaves is general: a window-scoped option goes on
each window as it is created, never into the `-f` file and never `-g`.

**So the fleet pins each window and sizes it explicitly.** The pin alone is not
a sizing model, it is a freeze on whatever size tmux already chose — and without
the global, the birth size is tmux's default `window-size latest`, which
measures the most recent *client* on the server rather than the session's
`-x`/`-y`. Measured on 3.4 with one 80x24 client attached: a `new-window` in a
200x50 session is born 80x24, so is a brand-new `new-session -x 200 -y 50`, and
the 200x50 window that was already there is pulled down to 80x24 too. Each
window therefore gets `window-size manual` and a `resize-window` on the `@id`
tmux just printed, the moment it exists (`new-window` has no `-x`/`-y` of its
own on 3.4: `unknown flag -x`). That is also what makes the escape hatch safe:
someone running `tmux -L asq attach` from an 80x24 terminal cannot reshape the
agents in the session they attach to.

### 3.2 The TUI is a view; there is no daemon

"No daemon" is repo doctrine and this plan keeps it. What must keep running lives
in tmux (processes) and `context.db` (state). Wake-ups (§7.3) are performed by
CLI processes that already run *inside* the agents — hooks and `task` writes —
not by a supervisor loop. The TUI polls the store the way `board -w` does.

### 3.3 The manager is a planner with fleet authority, not a new orchestrator

- New role **`manager`**: same ladder as `planner` (fable → opus → sonnet),
  base effort. Its standing cycle (`harness.role_cycle`) is the planner's
  contract-writing cycle plus the fleet verbs (§7.1) and the loop protocol.
- It talks to sub-agents only through the board (tasks, notes, signals) and
  `fleet tell` nudges. It does not write code; the briefing says so and
  `fleet spawn` is cheap enough that it has no reason to.
- The fleet's roles, as the owner thinks of them, mapped onto what the repo
  already has (decided 2026-08-28):

  | Fleet role | Repo role | Job in the loop |
  | --- | --- | --- |
  | **manager** | new: `planner` + fleet authority | intake → contracts → spawn → steer → report. Never codes, never merges. |
  | **coder** | `coder` (existing) | implements one task in its own worktree; opens the PR |
  | **tester** | `runner` (existing; `tester` becomes a first-class alias) | adversarial verification: runs the full check the contract names, tries to break the change, `done` / `reopen` with evidence |
  | **reviewer** | new | reads the PR as the stranger who will maintain it; findings on the PR via `gh pr review`; read-only |
  | **validator** | `validator` (existing) | one final gate over the assembled deliverable before the manager says READY |

  `planner` stays available for standalone use outside a fleet; `runner` stays
  accepted wherever `tester` is. Five fleet roles; each one is a briefing to
  maintain, so the bar for a sixth is high.

### 3.4 One launch seam, reused

A fleet window's command is `aisquare launch <role> …` (or `team spawn --exec`),
so model and effort resolution, launch profiles (`team bind`), `AISQUARE_ROLE`,
and the explainability wiring (`--session-id` pinning, the `X-Pipeline-Id`
correlation spine) apply unchanged. New process-start sites are confined to
`core/tmux.py` (**one** `subprocess.run` seam) and the onboarding subprocesses
(§5.6); each is registered in `core/spawn.SEAMS` or `test_spawn_seams` fails.

**Session identity is minted before launch.** `explainability_service.plan_session_identity`
already mints the UUID the agent is started on when tracing is enabled. Fleet
launches do it unconditionally, so a `fleet_agent` row knows its
`team_session.id` before the agent's first hook fires — no heuristics to join a
tmux window to a board row.

### 3.5 Worktree per coder; PRs through `gh`; a human merges

Parallel coders on one working tree corrupt each other. Each `coder` (and
`reviewer`) runs in a git worktree under `<repo>/.aisquare-worktrees/<label>` —
excluded via `.git/info/exclude`, never committed — on branch
`fleet/<codename>/<task-short-id>-<slug>` (§5.7). Worktrees already resolve to the principal
repo's board: this is the feature the repo built for exactly this situation.

Default flow: coder pushes and opens a PR (`gh pr create`); runner and reviewer
report against it; the manager reports *ready to merge*; **a human merges**
(decided 2026-08-28; auto-merge on green is a later, opt-in policy knob).
Claude Code's own `--worktree` (v2.1.49+) exists, but we manage worktrees
ourselves so a later non-Claude agent and cleanup (`fleet reap`, §5.3) behave
identically.

### 3.6 Permission posture per role

Autonomy means answering permission prompts. Claude Code 2.1.250 offers
`--permission-mode acceptEdits | auto | bypassPermissions | manual | dontAsk | plan`
plus `--restricted`. **Default for every fleet role: `auto`** (decided
2026-08-28) — Claude Code's classifier-based mode, which approves routine tool
calls itself and still stops for the risky ones, so the loop runs unattended and
the fleet UI is where the remaining prompts surface (🔔 on the row, bell, one
click to answer). Per role on top of that:

| Role | Default | Note |
| --- | --- | --- |
| manager | `auto` | its tool use is board and fleet CLI calls |
| coder, tester, validator | `auto` | plus a project allowlist for the project's own check commands (`make check`, `pytest`, `git`, `gh`) so they never reach the classifier |
| reviewer | `auto` + `--restricted` | read-only by construction |

Changeable per role in `[fleet.roles.<role>]`, in the Settings tab, and per
spawn with `fleet spawn --permission-mode …`. `acceptEdits` is the fallback
where `auto` is unavailable to the account: the spawn detects the refusal and
says which mode it fell back to. `bypassPermissions` is available and never a
default.

### 3.7 Textual becomes a core dependency (decided 2026-08-28)

`asq` opening a UI is the headline promise; a headline that says "install the
extra first" is a poor first minute. `textual` moves from the `[tui]`
extra (`>=1.0`) to `dependencies`, pinned `>=8.2,<9`. The code already relies on
8.2.x internals (`watch.py` cites `DataTable._on_click` in 8.2.8), so the pin
is honest. Keep `[tui]` as a no-op alias for one release.

Cost: textual pulls `rich>=14.2`, `markdown-it-py`, `mdit-py-plugins`,
`platformdirs`, `pygments`, `typing-extensions` — six pure-Python packages; the
tree-sitter grammars sit behind textual's own `[syntax]` extra, which we do not
take. Import stays lazy: `import textual` only inside the UI entry, never at
`aisquare.cli.app` import — the hook path's import cost is what
`tests/test_import_cost_of_the_integration.py` pins by module identity and what
`test_no_network_on_the_primary_path.py` measured at ~326 ms of CLI import per
hook. The UI must add nothing to `aisquare hook …`.

### 3.8 The no-args entry

Today `aisquare` with no arguments prints usage and exits 2 (`no_args_is_help=True`
on the root `Typer`; measured). New behaviour: `no_args_is_help=False`; in
`main_callback`, when `ctx.invoked_subcommand is None`:

- `--json` given, **or** stdin/stdout not a TTY, **or** `TERM=dumb` → print help,
  exit 2, byte-for-byte as today;
- otherwise → run the fleet UI.

Plus an explicit `aisquare ui` command, so the UI is reachable from a script or
alias and appears in the command tree, and so `--help` for it exists. `board -w`
stays as it is.

### 3.9 Platforms

Linux and macOS (tmux from the package manager). Windows: WSL2 — already the
README's Windows story for Claude Desktop. The fleet modules must import cleanly
on Windows (PR #65's suite runs there) and `asq` on Windows-native prints a
one-line "the fleet needs tmux — run inside WSL2" instead of a traceback; every
other command is untouched.

### 3.10 Every default is a default

Everything this plan calls a default — permission mode, model and effort per
role, worktree-per-coder, the escape key, `max_agents_per_project`, the
native-agent-teams switch, project names and codenames, labels — is
user-changeable in three places under one precedence rule, the one the harness
already uses: **per-spawn flag > environment > `[fleet]` config > built-in
default**. The Settings tab writes config through the existing `save_config`
path; the CLI flags exist so a manager or a script can override one spawn
without touching anyone's config. Nothing is hardcoded that a user could
reasonably want otherwise.

---

## 4. UI specification

```text
┌ Fleet ─────────────────── + ┐┌ aisquare-cli · manager ─────────────────────────────────── ⏸ waiting ┐
│ ▾ 🗂 aisquare-cli   3 · 🔔1 ││                                                                           │
│    🧭 manager       ⏸       ││   (the manager's live Claude Code session, rendered from its tmux pane)   │
│    🔨 coder-auth    ▶       ││                                                                           │
│    🧪 tester-1      🔔      ││   > add fleet spawn --worktree; make the runner verify on py3.11 too       │
│    👀 reviewer-1    ▶       ││   ● Planning… spawned coder-auth on tsk_01k…, runner-1 waits on review    │
│    ＋ spawn agent            ││                                                                           │
│ ▾ 🗂 explainability-sdk  1  ││                                                                           │
│    🧭 manager       ▶       ││                                                                           │
│    ＋ spawn agent            ││                                                                           │
│                             ││                                                                           │
├ Doctor · aisquare-cli ──────┤│                                                                           │
│ ✓ 11  ⚠ 2  ✗ 0              ││                                                                           │
│ ⚠ repomix not found         ││                                                                           │
│ ⚠ brain: gbrain missing     ││                                                                           │
└─────────────────────────────┘└───────────────────────────────────────────────────────────────────────────┘
 F12 back to sidebar · click a pane to type into it · wheel scrolls its history · t theme · q quit (sidebar)
```

### 4.1 Left pane — `Sidebar` (26–34 columns, collapsible)

- **Fleet** header with `+` → opens the Onboard view.
- One `ProjectCard` per registered project (`store.list_projects()`), with
  alternating `.odd` / `.even` background. Header row: disclosure ▾/▸, name
  (root basename), the fleet codename as a dim badge (§5.7), chips (agents alive
  · tasks open · 🔔 count); when two projects share a basename, the parent path
  as a dim subtitle. Click → Project
  view. A `…` menu: Board, Doctor, Explainability, Open in tmux, Remove from
  fleet (unregisters nothing destructive — the project row stays; only the card
  hides).
- Under it, one `AgentRow` per `fleet_agent` (manager first, then by
  `created_at`): role icon (🧭 manager · 🔨 coder · 🧪 tester · 👀 reviewer ·
  🛡 validator · 🤖 custom · 📡 remote — extends `watch._ROLE_EMOJI`), label,
  state chip (▶ working · ⏸ waiting · 🔔 NEEDS YOU · 💤 exited(N) · ✗ lost),
  account/model badge when several are in play (reuse `_session_lines` rules,
  including ⚠ off-ladder). Click → Agent view. Terminal bell on a transition
  into 🔔 (reuse `_ring_on_attention`).
- `＋ spawn agent` row → Spawn dialog: role, label (prefilled per §5.7, 🎲 for a
  random one), binary
  (claude, or a custom binary), model + effort (from the harness matrix, editable),
  worktree yes/no, permission mode, optional first prompt.
- **Doctor** section at the bottom: counts for the selected project (global
  when none) and the top three ⚠/✗ lines. Click → Doctor view.

### 4.2 Right pane — `ContentSwitcher`

- **Welcome** (no projects yet): what this is, `+` to add a project, inline
  presence check for `tmux`, `claude`, `gh` with install hints.
- **Onboard**: a `DirectoryTree` rooted at `~` plus a path `Input` (`~` and
  `$VAR` expanded; validation: exists, is a directory, `find_project_root`
  shows which root will be registered, "already registered" notice). Button
  **Onboard** → background worker (§5.6): `init <path>` then `doctor`
  (cwd = path), streaming a log; on success the card appears and is selected;
  warnings are summarised with their fixes; fix buttons for
  `agents connect claude-code`, `project onboard`, `doctor --fix`,
  `explainability enable`. It never prompts: `init` is non-interactive by
  design and the explainability consent stays an explicit button (#50's
  boundary — nothing ships before the user configured it).
- **Project** view — `TabbedContent`:
  - **Manager**: the manager's `TerminalPane`; "Start manager" when none. Goal
    intake happens *in the session* — the user types to the manager exactly as
    they would to any Claude session.
  - **Board**: the widgets lifted from `watch.py` (sessions, tasks, feed,
    detail; `d` archive, `o` transcript).
  - **Doctor**: this project's checks, with fix buttons.
  - **Explainability**: status · enable/disable · register roster · ship · the
    `env` block, through the services behind `cli/explainability.py`.
  - **Settings**: per role permission mode and worktree, escape key, max agents,
    worktree root, the native-agent-teams switch, the codename (rename). Model,
    effort and binary per role stay in `team harness` / `team bind` — one home per
    concept — and the tab says so.
- **Agent** view: header (label, role, state, model, cwd or worktree, task) +
  actions (stop, restart, open in tmux, open transcript — reuse
  `_transcript_command`) + the `TerminalPane`.

### 4.3 Input model

- Focus is in the sidebar **or** in a `TerminalPane`. With a pane focused,
  **every key goes to tmux** except the escape hatch (`F12` by default,
  configurable), which returns focus to the sidebar. A mouse click anywhere
  moves focus. App-level bindings (`q`, `t`, the command palette) are live only
  with the sidebar focused: `App.BINDINGS` are declared non-priority and
  checked against focus, and Textual's default `ctrl+q` / `ctrl+c` / `ctrl+p`
  are rebound so they can be forwarded to the agent (Claude Code uses ctrl+c,
  ctrl+o, ctrl+r, ctrl+t, ctrl+b, ctrl+g, ctrl+v, shift+tab).
- Wheel over a pane scrolls **its history** (our own offset over
  `capture-pane -S -k`); any key returns to live.
- Paste (Textual's `Paste` event, bracketed) → `load-buffer` +
  `paste-buffer -p`, so Claude Code sees one paste instead of N Enter presses.
- Drag-select in a pane selects our rendered text (Textual's selection API, as
  `board -w`'s `v`/`c` does) — no tmux copy-mode involved.
- The theme picker (`t`) and its autosave in `state.json` are reused verbatim.

---

## 5. Code layout

**New (all paths under `src/aisquare/`, all (new)):**

| Path | Contents |
| --- | --- |
| `core/tmux.py` | The **only** tmux call site — one `subprocess.run` seam. `server()`, `version()`, `ensure_session`, `new_window`, `capture`, `send_keys`, `send_literal`, `paste`, `resize`, `pane_facts`, `list_windows`, `kill_window`. Pure functions returning dataclasses; imports nothing from Textual. Registered in `SEAMS` as EXCLUDED with `strips_identity=True` — the tmux **server** must never carry a Run identity (it outlives every agent and would hand it to all of them); each window's agent takes its own through `aisquare launch`. |
| `core/keys.py` | Textual key → tmux key-name table, pure and unit-tested (§6). |
| `core/codenames.py` | The adjective and animal word lists behind fleet codenames, and the deterministic picker (§5.7). |
| `services/fleet.py` | `fleet_agent` lifecycle: `spawn`, `list_agents`, `state` (merges `team_session` with pane facts), `tell`, `stop`, `reap`, worktree create/remove, label uniqueness, `max_agents` enforcement, the wake-up nudge (§7.3). |
| `cli/fleet.py` | The `aisquare fleet` group (§5.3). |
| `cli/ui/` (package, lazy-imported) | `app.py` (`FleetApp`), `sidebar.py`, `terminal.py` (`TerminalPane`), `views/onboard.py`, `views/project.py`, `views/agent.py`, `views/doctor.py`, `board.py` (widgets moved out of `watch.py`; `watch.py` keeps `run_watch` and re-exports). |
| `docs/fleet.md` | User guide; listed in `DOCUMENTED` because it *will* show runnable commands. |

**Changed:**

| Path | Change |
| --- | --- |
| `cli/app.py` | `no_args_is_help=False`, the no-args dispatch (§3.8), `ui` command, `fleet` group. |
| `core/store.py` | Migration `_SCHEMA_V11` (§5.1); `ContextStore` protocol + `SqliteStore` methods for `fleet_agent`. |
| `core/config.py` | `FleetSettings` on `AppConfig` (§5.2). Older builds keep unknown keys through `_keep_unknown`, as designed. |
| `core/harness.py` | `manager`, `tester`, `reviewer` in `ROLE_PROFILES`; `role_cycle` texts (tester shares runner's). |
| `core/ids.py` | `AGENT_PREFIX = "agt_"`. |
| `models.py`, `services/project.py` | `ProjectInfo.codename`; `find_projects` / `project switch` also match a codename; the ambiguity error lists codenames (§5.7). |
| `services/team.py`, `cli/hook.py` | Manager continuation in `hook_stop` (§7.3); everything else byte-identical. |
| `services/diagnostics.py` | `_check_tmux`, `_check_gh`, `_check_fleet`. |
| `core/spawn.py` | `SEAMS` entries for `core/tmux.py::_tmux`, the onboarding subprocesses, `fleet attach`. |
| `tests/test_stubs.py` | `IMPLEMENTED` gains `ui` and every `fleet` leaf. |
| `pyproject.toml` | textual to core (§3.7). No other dependency changes. |
| `README.md`, `CHANGELOG.md` | Fleet section, command-tree lines, release notes. |

### 5.1 `fleet_agent` — migration v11

```text
CREATE TABLE fleet_agent (
    id            TEXT PRIMARY KEY,           -- agt_…, ULID-style like every other id
    project_id    TEXT NOT NULL,
    label         TEXT NOT NULL,              -- the manager names it; the CLI suffixes -2, -3 on collision
    role          TEXT NOT NULL,              -- manager | coder | runner | reviewer | validator | <custom>
    binary        TEXT NOT NULL,              -- claude | <path>, as resolved by harness.resolve_binary
    tmux_socket   TEXT NOT NULL,              -- 'asq' (config)
    pane_id       TEXT NOT NULL,              -- %N, stable for the pane's life; the session name derives from the codename (§5.7)
    session_id    TEXT,                       -- team_session.id, minted before launch; NULL for a binary without --session-id
    cwd           TEXT NOT NULL,              -- repo root, or the worktree path
    worktree      INTEGER NOT NULL DEFAULT 0,
    task_id       TEXT,
    spawned_by    TEXT,                       -- 'user' | the spawning session's id
    created_at    TEXT NOT NULL,
    ended_at      TEXT,
    exit_status   INTEGER,
    UNIQUE (project_id, label)
);
CREATE INDEX fleet_agent_project ON fleet_agent (project_id, created_at);

-- the fleet codename (§5.7); ALTER TABLE cannot add UNIQUE, so uniqueness is an index
ALTER TABLE project ADD COLUMN codename TEXT;
CREATE UNIQUE INDEX project_codename ON project (codename);
```

**State is derived, never stored.** Precedence: a fresh `team_session` row →
its `state`; else pane facts (`pane_dead` → exited(N), `window_activity_flag` →
working, otherwise waiting); no pane → lost. An agent binary without our hooks
only ever has the second source, and its row says so ("no hooks").

### 5.2 Config additions

```toml
[fleet]
tmux_socket = "asq"
escape_key = "f12"
max_agents_per_project = 4
worktree_dir = ".aisquare-worktrees"      # relative to the repo root; kept out of git via .git/info/exclude
disable_native_agent_teams = true         # exports CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=0 into fleet launches (§7.6)

[fleet.roles.manager]
permission_mode = "auto"                  # any Claude Code mode; "" = no flag

[fleet.roles.coder]
permission_mode = "auto"
worktree = true

[fleet.roles.tester]                      # `runner` is accepted as an alias
permission_mode = "auto"
worktree = false                          # verifies in the coder's worktree, named by the task

[fleet.roles.reviewer]
permission_mode = "auto"
worktree = true
extra_args = ["--restricted"]

[fleet.roles.validator]
permission_mode = "auto"
worktree = false
```

Model, effort and binary per role stay in `team harness` / `team bind` /
`AISQUARE_MODEL_<ROLE>` — one home per concept, which is the rule that deleted
`bins` in #56.

### 5.3 The `fleet` group (agents and scripts use it too; `--json` everywhere)

| Command | Does |
| --- | --- |
| `fleet spawn <role> [--label L] [--task ID] [--worktree/--no-worktree] [--permission-mode M] [--bin B] [--prompt TEXT] [-- agent args]` | Ensures the project's tmux session, creates the window running `aisquare launch <role> …` with the role's permission flags and a minted `--session-id`, records the row, prints a receipt (`✓ spawned coder-auth (agt_…) → asq-amber-otter %7`). Refuses past `max_agents_per_project` with the count in the message. |
| `fleet ls` / `fleet status` | Rows with derived state; `--json` is what the TUI and any automation read. |
| `fleet tell <label> <text>` | Injects text into an agent's pane — **only** when its state is `waiting` and the pane is alive. Otherwise files a board `note --to <label>` and says which happened. |
| `fleet stop <label> [--force]` | Sends `/exit`, waits a grace period, then kills the window; the agent's own `SessionEnd` hook releases its claims. |
| `fleet attach [<project>]` | Execs `tmux -L asq attach -t asq-<codename>` — the full-fidelity escape hatch. An EXCLUDED seam: attaching is not an agent. |
| `fleet rename <project> <codename>` | Sets the fleet codename (§5.7) and renames the tmux session to match. |
| `fleet reap` | Marks dead panes ended, records exit codes, removes worktrees of ended agents **whose branch is merged**. Never deletes unmerged work. |
| `fleet pause <project>` / `fleet resume <project>` | Sets a named signal the manager's cycle respects (no new spawns). |
| `ui` | The app — what bare `asq` runs. |

### 5.4 Roles added to the harness

- `manager`: ladder `fable → opus → sonnet`, offset 0 (as planner), mission
  "turn a goal into a fleet that ships it". Cycle text in §7.1.
- `tester`: a first-class name sharing `runner`'s profile and cycle (`sonnet`,
  offset 0); `runner` keeps working everywhere. `watch._ROLE_EMOJI` already
  knows `tester`.
- `reviewer`: `sonnet`, as coder and tester (the README's measured sweet spot
  for agentic work), offset 0. Mission "review the PR as the stranger who has
  to maintain it".
- `planner`, `coder`, `validator`: unchanged.

### 5.5 Hook changes

Only `Stop` changes, and only for `role == manager` (§7.3). The other four
hooks are byte-identical. The fail-open doctrine holds: any error in the new
branch is swallowed and reported through `_cost_of_failing_open` with a new
cost line ("the manager will not be woken by this turn's board updates").

### 5.6 Onboarding and doctor from a multi-project process

`init`, `onboard` and several doctor checks resolve the project from the
**process cwd** (`active_project`, `current_project`, `_check_snapshot`). A TUI
that hosts many projects must not `os.chdir` — it is process-wide and a race
with every worker. So the Onboard view runs them as **subprocesses of our own
CLI** with `cwd=<path>`: `aisquare --json init <path>`, `aisquare --json doctor`,
`aisquare --json project onboard --refresh`, `aisquare --json doctor --fix --yes`.
Costs ~350 ms of CLI import each — fine for a user-initiated action — and buys
crash isolation (a Repomix pack that dies cannot take the UI down). Each is a
seam, ruled EXCLUDED and **not** stripped: they are our own CLI, start no model
process, and `doctor --live` needs the `EXPLAINABILITY_*` environment to
diagnose the machine it is on (the same reasoning as `sdk_doctor`).

A small refactor is still worth doing for the *read-only* doctor:
`diagnostics.doctor(cwd=...)` threading `cwd` into the three cwd-sensitive
checks, so the Doctor tab can refresh in-process without a subprocess per tick.

### 5.7 Names: projects, codenames, tmux sessions, labels, branches

Worked out by a forked planning agent against the code and adopted as written.
Measured first: `find_project_root` returns a **non-git directory as its own
root** (a parent holding several repos is one project); `ensure_project` writes
`project.name` but nothing reads it back — all ten display sites recompute
`root.name or project.id`; tmux *creates* sessions named `a.b` or `a:b` but
cannot *target* them, because `.` and `:` are target separators — so slug-safety
is a targeting requirement, not a creation one.

**Project display name.** `root.name` — the directory basename — for git repos
and non-git directories alike; fallback `project.id` when the basename is empty.
The ten existing CLI sites are not touched.

**Fleet codename.** Every project that enters the fleet gets an
`adjective-animal` codename (`amber-otter`): lowercase ASCII,
`^[a-z]{3,7}-[a-z]{3,7}$`, from two hand-curated lists (96 adjectives × 96 animals) in
`core/codenames.py` — 9,216 combinations, family-friendly, with a test
that every word matches the charset and the lists are sorted and unique.
**Deterministic from `project.id`** (`sha256`, walking to the next pair on a
collision) rather than random: the id is already a stable hash of the resolved
root, so the same checkout gets the same codename on every machine and after a
`context.db` loss — and the tmux session and branch names below derive from it.
The user still cannot predict it, which keeps the fun. Stored in a new nullable
`project.codename` column (migration v11, alongside `fleet_agent`; SQLite cannot
add a UNIQUE constraint through `ALTER TABLE`, so uniqueness is a
`CREATE UNIQUE INDEX`), returned as `ProjectInfo.codename`, and assigned lazily
the first time a project enters the fleet — never at `init`, so a memory-only
user never sees one. Renamable: `fleet rename <project> <codename>` (same regex,
uniqueness enforced), the Settings tab, and click-to-rename in the sidebar; a
rename calls tmux `rename-session`. `find_projects` gains `OR codename = ?` so
`project switch amber-otter` works, and the ambiguity error lists codenames,
because basenames are what collide.

**Display.** Basename primary, codename as a dim badge beside it; pane titles
read `aisquare-cli · manager`. Two projects sharing a basename additionally show
the parent segment as a dim subtitle (`~/work/api`, `~/oss/api`). The codename is
primary only where a stable token is needed: the tmux session, the attach hint,
branch names, `fleet` command arguments.

**tmux.** Session `asq-<codename>`, always targeted exactly (`=asq-amber-otter`)
so `asq-ruby-fox` never prefix-matches an `asq-ruby-foxhound`. Window name =
agent label; panes are addressed by `%id`, never by name, so window renames are
harmless. `fleet_agent` therefore stores `project_id` + `pane_id` (+
`tmux_socket`) and *derives* the session name from the current codename — the
`tmux_session` / `tmux_window` columns first proposed are dropped. If tmux is
unreachable during a rename, the row is renamed anyway and `fleet reap`
reconciles from `list-sessions`.

**Agent labels.** `manager` — always, one per project (a second is refused).
Manager-spawned: `<role>-<purpose>` matching `^[a-z][a-z0-9-]{1,23}$` (≤ 24
chars, no `.`, `:` or spaces), with the manager briefed to make the purpose
descriptive (`coder-auth`, `tester-py311`, not `coder-2`). Unique per project
among **live** agents; an ended agent frees its label. Collision → append `-2`,
`-3` and print the label actually used (`✓ spawned coder-auth-2 (asked:
coder-auth)`) rather than fail — a manager mid-plan must not stall on a name.
`--label` omitted → `<role>-<task short id>` with `--task`, else `<role>-<n>`.
User-spawned agents get the same role-based default prefilled, plus a 🎲 button
offering `<role>-<adjective>-<animal>` from the same lists (still matches the
regex).

**Branches and worktrees.** Branch `fleet/<codename>/<task-short-id>-<slug>`
(`fleet/amber-otter/01k9q8p3-wire-auth`; checked with
`git check-ref-format --branch`): the codename segment groups one fleet's
branches and disambiguates two machines pushing to one remote; the short id is
`team_service.short_id`'s length, imported rather than hardcoded; the slug is the
title lowercased, `[^a-z0-9]+` → `-`, ≤ 32 chars, trailing `-` stripped. Worktree
directory `<repo>/.aisquare-worktrees/<label>`. A user spawn without a task uses
`fleet/<codename>/<label>`. **A non-git project cannot have worktrees**:
`fleet spawn coder --worktree` there refuses with "not a git repository — spawn
without --worktree or pick a repo inside it", and the Spawn dialog greys the
option out.

**Worked examples.** `~/Code/AISquare/ws2/aisquare-cli` → `aisquare-cli`,
codename `amber-otter`, session `asq-amber-otter`, branches
`fleet/amber-otter/…`. `~/Code/AISquare` (a non-git parent of several repos) →
`AISquare`, `quiet-lynx`, `asq-quiet-lynx`, no worktrees. `~/work/api` and
`~/oss/api` → `api · amber-otter` (subtitle `~/work/api`) and `api · ruby-fox`
(subtitle `~/oss/api`).

---

## 6. The `TerminalPane`, in detail — the risky core

**Render loop.** A Textual timer at 50 ms while the pane is visible *and* tmux
reports activity or a cursor/history change; backing off to 500 ms when idle.
Per frame: one tmux process, two commands (§3.1). `Text.from_ansi(line)` results
are cached by line string, so an unchanged line costs a dict lookup; the widget
uses Textual's Line API (`render_line(y)`) and only refreshes rows whose Strip
changed. The cursor is a reverse-video cell when `cursor_flag` is 1.

**Wide characters.** tmux emits lines already laid out; Rich must agree with
tmux on cell widths. Both use wcwidth tables; emoji ZWJ sequences and some
East-Asian ambiguous widths are the known disagreements. The spike measures
this with Claude Code's own status glyphs.

**Resize.** On Textual `Resize`, `resize-window -x W -y H` for the visible
window, debounced 100 ms. Windows not currently shown keep the size they last
had; Claude Code redraws on `SIGWINCH` when shown again.

**Keys** (`core/keys.py`). `Key.character` printable → `send-keys -l`;
otherwise map `Key.key`:

| Textual | tmux | Textual | tmux |
| --- | --- | --- | --- |
| `enter` | `Enter` | `shift+tab` | `BTab` |
| `escape` | `Escape` | `backspace` | `BSpace` |
| `tab` | `Tab` | `delete` | `DC` |
| `up` `down` `left` `right` | `Up` `Down` `Left` `Right` | `insert` | `IC` |
| `home` `end` | `Home` `End` | `pageup` `pagedown` | `PPage` `NPage` |
| `f1` … `f12` | `F1` … `F12` | `ctrl+<x>` | `C-<x>` |
| `alt+<x>` | `M-<x>` | `ctrl+shift+<x>` | `C-S-<x>` |
| `shift+enter` | `S-Enter`, with the caveat below | anything else | dropped, one-line notice |

The escape hatch key is consumed by us and never forwarded. Textual 8.2.7 /
8.2.8 speak the kitty keyboard protocol, so modifier-rich chords arrive **when
the outer terminal supports it** (kitty, ghostty, wezterm, foot, recent
alacritty). In VTE-based terminals and Windows Terminal, shift+enter is
indistinguishable from enter — we document that per terminal and never fake it
(`\` + Enter works everywhere in Claude Code).

**tmux is a second gate, and naming the key is only half of it.** tmux must also
ENCODE the chord for the pane, and its legacy (vt10x) encoding has nowhere to
put a shift: the keys tmux parameterises into a CSI sequence carry every
modifier (`S-Up` is `ESC [ 1 ; 2 A`), but Enter, Escape, Tab, BSpace, Space and
every ordinary character are single bytes with no room for one. When tmux cannot
encode a chord it does not fail — `cmd-send-keys` TYPES THE NAME as text, so
`send-keys S-Enter` puts those seven characters in front of the agent. Measured
on tmux 3.4 — what `ubuntu-latest` ships — under the fleet's own configuration.
Raising the tmux floor is not the fix and there is no version to raise it to:
3.4 knows the name (`bind-key S-Enter` is accepted; only a name like
`bind-key Bogus` is refused) and encodes it correctly the moment the pane's own
application turns extended keys on — `printf '\033[>4;2m'` before `cat` and
`S-Enter` arrives as `ESC [ 13 ; 2 u`. Which mode a pane is in is the agent's
business, and tmux publishes no format variable for it, so the fleet can neither
ask nor switch it. Shift+enter is a key Claude Code uses, so the affected chords
cannot be forwarded by name: `src/aisquare/core/keys.py` owns what is sent
instead, and `tests/test_keys.py` checks it against the running binary.

**Paste.** `Paste.text` → `load-buffer -b asq-paste -` (stdin) →
`paste-buffer -p -d -b asq-paste -t %pane`. `-p` gives bracketed paste when
the agent enabled it, which Claude Code does.

**Scrollback.** Offset *k* → `capture-pane -S -k -E (H-1-k)`; wheel changes *k*;
any key sets *k = 0*.

**Selection / copy.** Textual's built-in selection over our rendered text;
copy via `copy_to_clipboard` (OSC 52, with the "your terminal did not accept
it" notice `board -w` already has).

**Spike exit criteria (Phase 0).** In a fleet window: start `claude`; type a
prompt; approve a permission prompt; cycle modes with shift+tab; interrupt with
Esc; ctrl+c twice; use the `/model` picker with arrows; paste a 30-line block;
resize the outer terminal twice; scroll back and return; compare side by side
with a raw `tmux -L asq attach`. Record the matrix:

```text
key / behaviour          kitty   ghostty   wezterm   gnome-terminal   Windows Terminal (WSL)   iTerm2
shift+enter (newline)    ?       ?         ?         ?                ?                        ?
shift+tab (mode cycle)   ?       ?         ?         ?                ?                        ?
esc interrupt            ?       ?         ?         ?                ?                        ?
ctrl+c / ctrl+o / ctrl+r ?       ?         ?         ?                ?                        ?
bracketed paste          ?       ?         ?         ?                ?                        ?
emoji/width alignment    ?       ?         ?         ?                ?                        ?
ms per frame 80x24 / 200x60      ?                                    CPU % while streaming     ?
```

Go/no-go: everything above usable, and ≤ 15 % of one core while Claude streams
at 200×60. A no-go sends us to option A or the control-mode client, with the
numbers to justify it.

---

## 7. The manager loop, in detail

### 7.1 The manager's standing cycle (`role_cycle("manager")`, injected at SessionStart)

Intake the goal (ask until the acceptance criteria are executable) → write
contract tasks (`task add … --role coder --detail <objective · why · known ·
acceptance · boundaries>`, `--needs` for ordering) → `fleet spawn coder --label
<name> --task <id>` per parallelisable task, within `max_agents_per_project` →
`fleet spawn tester` once work reaches review → `fleet spawn reviewer` once a PR
exists → read results as they arrive (the per-prompt delta already does this)
→ reopen with reasons, re-spec or split → `fleet spawn validator` once every
task is done → when its gate is PASS → `note "READY: <PRs + evidence>" --kind
result` and stop. **Never write code. Never merge.** Ask the human with a `question` note
when blocked twice on one task (it surfaces as 🔔 on the project). Keep labels
unique and descriptive (`coder-auth`, not `coder-2`).

### 7.2 One iteration

```mermaid
sequenceDiagram
    participant U as User (manager pane)
    participant M as manager (claude, tmux)
    participant B as context.db (board)
    participant F as fleet CLI
    participant C as coder-auth (claude, worktree)
    participant R as tester-1
    U->>M: goal in prose
    M->>B: task add x N (contracts, --needs)
    M->>F: fleet spawn coder --task tsk_1 / tsk_2
    F->>C: tmux new-window: aisquare launch coder …
    C->>B: task next --claim … work … task review --note evidence
    C-->>M: nudge via send-keys (only if M is waiting)  §7.3
    M->>B: reads delta on its next turn
    M->>F: fleet spawn tester
    F->>R: tmux new-window: aisquare launch tester …
    R->>B: task done (evidence) | task reopen --reason
    R-->>M: nudge
    M->>M: Stop hook sees new events → continues; else waits
    M->>B: note "READY: PR #N …" --kind result
    B-->>U: 🔔 + bell in the sidebar
```

### 7.3 Wake-ups without a daemon

Two moments that already exist carry the wake-up.

1. **The manager's own `Stop` hook.** Today `hook stop` marks the session
   waiting and prints nothing. For `role == manager` it first asks the store for
   events since the session cursor authored by *others*, of kinds
   `task_review`, `task_done`, `task_blocked`, `task_reopened`, `result`,
   `question`, and the new `agent_exited`. If there are any, it emits the
   Stop **block** decision with the rendered delta as the reason → Claude Code
   continues the turn with that context, and the cursor advances. If none →
   exit 0, waiting, exactly as today.
   - Loop guards: honour `stop_hook_active` (already in the payload); never
     block twice without a *new* event; a hard cap of continuations per hour
     (config, default 30); the reason always ends with "if nothing needs you,
     stop".
   - The exact JSON shape for a Stop block must be re-checked against the hooks
     reference at implementation time — the top-level
     `{"decision": "block", "reason": …}` form and a `hookSpecificOutput` form
     both appear in current documentation, and exit code 2 with the reason on
     stderr is the documented cross-version fallback. Whatever the shape,
     `hook stop`'s stdout stays valid JSON or empty
     (`test_json_stdout_is_machine_readable`).
2. **A sub-agent's board write** — `task review|done|block|reopen`,
   `note --to manager`, and `fleet reap` on an exit — runs inside *that agent's*
   CLI process. After the write commits, it looks up the project's manager row:
   state `waiting` (never `attention`, never `working`), pane alive, and the
   pane's current command is the agent (not a shell after an exit) → one
   `send-keys` of a fixed one-line nudge plus Enter. The nudge triggers
   `UserPromptSubmit`, and the **existing delta injection** delivers the
   details — so the nudge text carries nothing, never starts with `/` or `!`
   (Claude Code's command prefixes), and is debounced through `team_meta`
   (`nudge:<session>`, 5 s).

No polling, no daemon, and it works with the TUI closed. Without tmux (agents
launched by hand), path 2 is a no-op and path 1 still works.

### 7.4 Guardrails

`max_agents_per_project` (default 4) · `fleet pause` (a signal the cycle
respects) · kill / restart per agent from the UI · claims already lease-expire
when an agent dies · a task reopened twice returns to the manager (existing
planner rule) · reviewer is `--restricted` · a human merges · the hourly
continuation cap.

### 7.5 Failure modes → behaviour

| Situation | What the user sees | What the system does |
| --- | --- | --- |
| Agent process dies | 💤 exited(N) on the row; the last screen stays readable (`remain-on-exit`) | manager nudged once with `agent_exited`; restart keeps the label, mints a new session id |
| Agent stuck on a permission prompt | 🔔 + bell | nothing nudges it; the user clicks in and answers |
| Manager needs the human | 🔔 on the project | same |
| `context.db` locked or corrupt | UI keeps the last frame; Doctor shows the failure | agents are unaffected — hooks fail open |
| tmux server killed | ✗ lost on rows | `fleet reap` ends them; worktrees preserved |
| Nudge lands while a dialog is open | (should not happen) | gated on `waiting` + pane liveness; if it ever does, the text is a harmless sentence |

### 7.6 Coexistence with Claude Code's native agent teams

Claude Code has its own experimental feature called **agent teams**
(`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`): a Claude session can spawn *teammate*
Claude sessions — in-process, or as tmux split panes — that coordinate through
Claude's own task list and mailboxes under `~/.claude/teams/`. It is Claude-only,
experimental, one team per session, and the lead is Claude's, not ours.

The question was whether our fleet should switch that feature **off** for the
agents it launches. Decided yes (2026-08-28): if it were on, a manager asked to
"spawn a coder" might use Claude's native mechanism instead of `fleet spawn`,
and those teammates would run outside our tmux server — invisible to the
sidebar, the board and the wake-up rules. So by default the fleet exports
`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=0` into its launches
(`disable_native_agent_teams`, §5.2) and the manager's briefing names
`fleet spawn` as the one way to get help. This affects only sessions the fleet
starts — a user's own `claude` sessions keep whatever they had — and it is
configurable like every other default. Cross-session messaging (native) is
untouched.

---

## 8. Dependencies — verified 2026-08-28

### 8.1 Python packages

| Package | Today | Plan | Why / risk |
| --- | --- | --- | --- |
| `textual` | `[tui]` extra, `>=1.0`; latest 8.2.8 (2026-06-30), requires `rich>=14.2` | **core**, `>=8.2,<9` | the UI becomes the default surface; the code already targets 8.2 internals; pin the major |
| `rich` | `>=13.7` | unchanged (resolution raises it to 14.2 via textual) | — |
| `typer`, `pydantic`, `tomli-w` | unchanged | — | — |
| `libtmux` (0.62.0) | — | **not used** | ~12 tmux commands need no ORM, and a wrapper library would hide the subprocess call from `test_spawn_seams` — the guard would go blind by construction |
| `pyte` (0.8.2, Nov 2023) | — | **not in v1**; candidate for a tmux-less backend | stale, no extended-keys / paste passthrough of its own |
| `textual-terminal`, `textual-tty` | — | not used | self-described slow / buggy |
| `tiktoken`, `mcp`, `aisquare` SDK | optional extras | unchanged | — |

**No new required pip dependency** beyond promoting textual.

### 8.2 System tools

| Tool | Needed for | Minimum | Doctor |
| --- | --- | --- | --- |
| `tmux` | the fleet (spawning and surfacing) | 3.2 (`new-window -e`, `extended-keys`); no higher floor buys shift+enter (§6) | new `tmux` check: present, version, private server starts; absent → fleet disabled with per-OS install hints (`apt`, `dnf`, `brew`), everything else works |
| `git` | worktrees | 2.20+ | existing |
| `gh` | PR flow (coder, reviewer) | any current | new warn-level check |
| `node` / `npx` / `repomix` | snapshots | existing | existing |
| `claude` | agents | 2.1.x (`--name`, `--session-id`, `--permission-mode`, `--restricted`, `--effort`, Stop-hook block) | existing check; add the version to its detail |

### 8.3 Agent capabilities relied on

Claude Code: the five hooks (already installed by `agents connect`),
`--session-id`, `--name`, `--permission-mode`, `--restricted`, `--effort`,
`--model`, the Stop-hook block decision, bracketed paste, extended keys.
v1 is Claude Code only (decided 2026-08-28); see §8.5.

### 8.4 Python and CI

CI stays 3.11–3.13. The local 3.14 venv installs and runs the suite today. CI
gains `apt-get install tmux` so the real-tmux integration tests run there (they
skip when tmux is absent).

### 8.5 Later: other agents, Codex first

The substrate is agent-agnostic — a fleet window runs whatever binary the role
is bound to — so *surfacing* a Codex session needs nothing new. What Codex lacks
is our lifecycle hooks, so it would have no board state and no wake-ups until an
equivalent exists (`codex exec` / `codex queue` are its headless entry points).
Out of scope for v1 (decided 2026-08-28); nothing in the schema blocks it
(`fleet_agent.binary`, nullable `session_id`).

---

## 9. Phased delivery — one PR per phase, `make check` green at each

| Phase | Scope | Size | Exit criteria |
| --- | --- | --- | --- |
| **0 — Spike** | `core/tmux.py`, `core/keys.py`, `cli/ui/terminal.py`, a throwaway `ui --spike <pane>` | S, timeboxed 2–3 days | §6 matrix recorded in this doc; **go/no-go on option D** |
| **1 — Shell** | no-args dispatch + `ui`; two panes; Sidebar from the store (alternating bg); Doctor section (global); Welcome; theme reuse; `fleet ls/status` only | M | Pilot smoke; TTY / non-TTY / `--json` dispatch tests; `IMPLEMENTED`; `--help` for every new node |
| **2 — Onboarding** | Onboard view (DirectoryTree + Input + validation) → background `init` + `doctor` subprocesses → card appears; Doctor view with fix buttons | M | worker tested with a fake runner; seams registered; the UI process never `chdir`s |
| **3 — Fleet core** | migration v11; `services/fleet.py`; `fleet spawn/ls/status/stop/tell/attach/reap/pause/resume`; private server + bundled conf; worktrees; labels; limits; doctor checks | L | fake tmux backend for units; real-tmux tests marked; JSON sweep; no-traceback sweep |
| **4 — Surfacing** | agent rows (icons, chips, bell); Agent view = `TerminalPane`; F12 / click focus; paste; scrollback; selection; resize; open-in-tmux; stop / restart | L | key table tests; paste path; scroll offsets; focus rules with Pilot |
| **5 — Manager** | roles `manager`, `tester` (alias of runner), `reviewer`; Stop-hook continuation; nudges from board writes; Manager tab; `fleet pause`; guardrails | L | `hook stop` silent for non-managers; block only on new events; `stop_hook_active` honoured; nudge gating; an end-to-end run with a fake agent script standing in for `claude` |
| **6 — Tabs** | Board tab (widgets lifted from `watch.py`, `board -w` kept); Explainability tab; Settings tab | M | existing `test_watch` still passes through the re-export |
| **7 — Polish & docs** | spawn dialog, menus, empty states; `docs/fleet.md` (+ `DOCUMENTED`); README section + tree; CHANGELOG; SVG screenshots | M | doc guards green; README tree flags valid |

Phases 1 and 2 are useful on their own (a project navigator with doctor) even if
the spike sends the terminal pane back to the drawing board; that is why they
come before 3.

---

## 10. Guards and tests this work must keep green

- `tests/test_spawn_seams.py` — every new `subprocess` / `os.exec*` site
  registered in `core/spawn.SEAMS` with a ruling.
- `tests/test_stubs.py::IMPLEMENTED` — every new leaf listed.
- `tests/test_documented_commands.py` — `docs/fleet.md` and README additions
  listed and valid; this plan stays on non-shell fences.
- `tests/test_console_markup.py` — no `Console(...)` outside `core/console.py`;
  widgets take `Text`, never markup strings with data in them.
- `tests/test_import_cost_of_the_integration.py` (the method) — `aisquare hook …`
  must not import the UI package; verify with `-X importtime`.
- `tests/test_json_stdout_is_machine_readable.py` — `fleet *` under `--json`
  emit JSON; `hook stop` stays JSON-or-empty.
- `tests/test_no_traceback_*` — new commands fail cleanly on a damaged store.
- `tests/test_every_test_can_fail.py` and CONTRIBUTING's rules — new guards need
  a positive and a negative control.
- The Windows suite (PR #65) — fleet modules import on Windows and degrade with
  a message.

New test files (new): `tests/test_keys.py`, `tests/test_tmux_backend.py` (fake +
real-marked), `tests/test_fleet.py`, `tests/test_ui_shell.py`,
`tests/test_ui_onboard.py`, `tests/test_manager_loop.py`. Textual apps are
driven headless with `App.run_test()` / `Pilot`, as `test_watch.py` already does.

---

## 11. Risks

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| The rendering hop loses fidelity (emoji widths, cursor shapes, OSC 8 links, synchronized-output mode) | medium | UX | spike matrix; "open in tmux" is always one keystroke away |
| Key chords the outer terminal cannot express (shift+enter, ctrl+shift+…) | high on VTE / Windows Terminal | UX | document per terminal; recommend kitty-protocol terminals; `\`+Enter fallback |
| Frame cost with several streaming agents | low — only the visible pane is captured | perf | activity-gated polling; control-mode client if measured |
| tmux missing or old (macOS ships none; Debian stable has 3.3) | medium | feature unavailable | doctor with install hints; everything else keeps working |
| A nudge typed into the wrong UI state | low–medium | agent confusion | gate on `waiting` + pane liveness; fixed harmless text; never during `attention` |
| Stop-hook continuation loops, token burn | medium | cost | new-events-only; `stop_hook_active`; hourly cap; `fleet pause`; agent cap |
| Parallel coders conflict | high without worktrees | correctness | worktree per coder by default; PR per task |
| Autonomy vs permissions | high | friction or risk | `acceptEdits` + allowlist; 🔔 surfaced; bypass never a default |
| Textual major bump breaks internals we lean on | medium | maintenance | pin `<9`; Dependabot; Pilot tests |
| `auto` permission mode unavailable to an account, or the classifier blocks a routine command | medium | friction | spawn detects the refusal and falls back to `acceptEdits`, saying so; project allowlist for its own check commands |
| Scope creep into "a whole IDE" | high | delivery | phases with exit criteria; v1 = surface + onboard + manager loop |
| Dual identity if a fleet launch inherits a traced parent's env | low | data | `disown_inherited_trace` already runs in `launch`; the tmux server env is stripped by the seam ruling |

---

## 12. Decisions from the owner (2026-08-28)

1. **textual is a core dependency** — yes (§3.7).
2. **Permission mode** — `auto` for every fleet role by default (§3.6);
   changeable per role and per spawn.
3. **Merge policy** — a human merges; auto-merge on green is a later opt-in (§3.5).
4. **Names** — fleets are per project. Project display name, fleet codename,
   tmux and label rules are in §5.7 (worked out by a forked planning agent and
   adopted as written).
5. **Roles** — manager, coder, tester, reviewer, validator (§3.3). The owner's
   manager/planner/coder/tester map onto the repo's planner/coder/runner, with
   `tester` as the fleet's name for `runner`, `reviewer` new, and `validator`
   kept as the one final gate.
6. **Native agent teams off** in fleet launches — yes; what the question meant
   is explained in §7.6.
7. **Codex** — later; v1 is Claude Code only (§8.5).

All of the above are defaults (§3.10). Nothing is blocking; the §6 fidelity
matrix is filled in Phase 0.

---

## 13. References

- In repo: `cli/watch.py` (the board TUI to lift from), `core/spawn.py` (seam
  registry), `core/harness.py` (roles, ladders, cycles), `services/team.py`
  (board protocol, hooks), `services/hooks.py`, `services/diagnostics.py`,
  `tests/test_documented_commands.py` (why this file uses `text` fences).
- Textual releases — 8.2.8 (2026-06-30) and 8.2.7 added kitty keyboard
  protocol handling: <https://github.com/Textualize/textual/releases>; the
  progressive-enhancement issue: <https://github.com/Textualize/textual/issues/6074>.
- Terminal-widget prior art (rejected): <https://github.com/mitosch/textual-terminal>,
  <https://github.com/ttygroup/textual-tty>, <https://github.com/selectel/pyte>.
- Toad — ACP front-end for coding agents, AGPL: <https://github.com/batrachianai/toad>.
- tmux wiki (control mode, `extended-keys`, `-L`, `window-size`):
  <https://github.com/tmux/tmux/wiki>.
- Claude Code hooks reference (Stop, Notification, SubagentStop, TeammateIdle):
  <https://code.claude.com/docs/en/hooks>; agent teams:
  <https://code.claude.com/docs/en/agent-teams>.

---

## Decisions log

| Date | Decision / change | By |
| --- | --- | --- |
| 2026-08-28 | First version of the plan. Approach D (tmux substrate) proposed; §12 open. Draft PR #71; suite on the branch: 1767 passed, 1 skipped. | PR #71 |
| 2026-08-28 | Owner decisions folded in: textual core; `auto` permissions; human merges; roles manager / coder / tester / reviewer / validator; native agent teams off in fleet launches; Codex deferred; every default configurable (§3.10). Naming scheme in §5.7 from a forked planning agent. | owner + PR #71 |
| 2026-08-28 | Implementation landed on this branch: scaffold f0a818c (contracts), then ten siloed work packages (tmux, fleet-service, fleet-cli, terminal, shell, onboard, doctor, manager, board, docs) built in parallel worktrees and merged without a conflict, plus an integration pass. Deviations, all recorded in code: the `auto`→`acceptEdits` permission fallback (§3.6) is documented, not automated; drag-select/copy inside an agent pane (§6) is not implemented (Line-API widget); a dead/vanished pane is read BEFORE the board row when deriving state (§5.1) because a killed agent fires no SessionEnd; `window_activity_flag` is not a signal on a headless server (§3.1), the frame diff is; the Settings tab omits model/effort/binary (one home per concept); the Onboard view is built on the first `+`; `ExplainabilitySettings.roles` now lists all seven roles. The §6 per-terminal key matrix still needs a human at each terminal. | PR #71 |
| 2026-08-28 | Implementation started from the scaffold (`f0a818c`: store v11, `[fleet]` config, the roles, `core/tmux.py`, `cli/fleet.py`, the UI skeleton) with the work packages in parallel. User documentation written from the scaffolded CLI: `docs/fleet.md` (listed in `DOCUMENTED`), the README fleet section and command-tree lines, the CHANGELOG entry — saying plainly where a piece lands in a later phase. | WP docs |
| 2026-08-29 | CI on `ubuntu-latest`, which runs tmux 3.4, failed 22 tests, and the repair changed two mechanisms this plan described. **§3.1:** `window-size manual` has left the bundled `-f` file. A GLOBAL one kills the server on every tmux below 3.7 — `clients_calculate_size()` dereferences a window pointer `default_window_size` never had — which was 19 of the 22 failures; it is now set on each window as that window is created, together with an explicit `resize-window`, because without the global the birth size falls back to `window-size latest`, i.e. the most recent attached client's terminal. The intent is unchanged (every fleet window pinned to a size the fleet chose) and the general rule is new: a window-scoped option must never go in the `-f` file. **§6:** `S-Enter` and the other single-byte chords are not a version gap. tmux 3.4 knows those names and cannot ENCODE them for a pane in legacy mode, typing the name as text instead — so §8.2's "3.4+ recommended (`S-Enter`)" was wrong and is gone, and `src/aisquare/core/keys.py` owns what those chords send. | PR #71 |
