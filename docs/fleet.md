# The fleet

`asq` with no arguments opens one view over every project you have registered,
the **manager** agent running in each, and the agents that manager spawns. Every
agent is a *real* Claude Code session — started in the background inside a
private tmux server, monitored, and surfaced in the UI exactly as it would look
in its own terminal. Nothing is relayed or re-rendered as a chat; click an agent
and you are typing into the session itself.

Three ideas carry the whole feature:

- **One view.** Left: a navigator — `Fleet ▸ projects ▸ agents ▸ Doctor`. Right:
  whatever you clicked — onboarding for a new directory, a project's manager,
  an agent's live session, the board, doctor findings with their fixes.
- **tmux is the substrate.** Agents run as windows of a per-project session on a
  tmux server that is ours alone (`tmux -L asq`, with a bundled config). Your own
  tmux sessions, config and prefix key are never touched.
- **Real sessions.** The UI is a view over that server and the board. Close it
  and every agent keeps running; reopen it and it re-attaches to what it finds;
  `aisquare fleet attach` shows the same session from any terminal.

> **Status.** The fleet lands in phases (plan §9 in
> [`docs/plans/fleet-tui.md`](plans/fleet-tui.md)). This guide describes the
> finished feature and says plainly where a piece belongs to a later phase. The
> command reference below matches the `aisquare fleet` group exactly as it
> exists in this checkout; CI validates every fenced command against the live
> command tree.

---

## Requirements and install

| Tool | Needed for | Minimum |
| --- | --- | --- |
| **`tmux`** | the fleet itself — spawning and surfacing agents | **3.2** (`new-window -e`, extended keys); **3.5+ recommended**, where shift+enter reaches the agent (3.3/3.4 would mistype it, so the fleet drops it there) |
| **`claude`** | every fleet role runs on Claude Code | 2.1.x — `--session-id`, `--permission-mode`, `--restricted`, `--effort` |
| `git` | worktrees for coders and reviewers | 2.20+ |
| `gh` | *optional* — the coder opens PRs and the reviewer reviews them with it | any current release |
| Python | the CLI | 3.11+ |

tmux is a system package. macOS ships none; Debian stable has 3.3, which is
enough; Ubuntu 22.04 has 3.2a, which is the floor.

```sh
sudo apt install tmux        # Debian / Ubuntu
sudo dnf install tmux        # Fedora / RHEL
brew install tmux            # macOS
tmux -V                      # 3.2 or newer
```

Windows: run everything inside WSL2, as you already would for Claude Desktop.
On Windows-native, `asq` prints a one-line "the fleet needs tmux — run inside
WSL2" and every other command works as before.

Then the CLI, connected to Claude Code:

```sh
pipx install 'aisquare-cli[tui]'          # or: pip install 'aisquare-cli[tui]'
aisquare agents connect claude-code       # the lifecycle hooks the fleet reads state from
aisquare doctor                           # is everything wired? (and how to fix anything)
```

Textual, the UI toolkit, becomes a core dependency of `aisquare-cli` with the
fleet (plan §3.7); `[tui]` stays as a no-op alias for one release, so the line
above is right on either side of that change. The hooks matter: an agent's
state chip (working · waiting · needs you) comes from the same five Claude Code
hooks the board already uses, so an agent launched without them shows only what
tmux can see and its row says so (`no hooks`).

---

## The first five minutes

1. **Run `asq`** (or `aisquare`) in a terminal. The UI opens: the navigator on
   the left and, until you add a project, a Welcome page on the right that
   checks `tmux`, `claude` and `gh` are on this machine. No script ever meets a
   full-screen app, and the two ways of getting no UI differ. **In a pipe or
   with `TERM=dumb`**: the help page on stdout and exit 2, byte for byte as
   before — the same ~5 KB either way (5,159 bytes at 80 columns, measured).
   **Under `--json`**, and that is checked before the terminal is, so it holds
   at a terminal too: exit 2 and one line, nothing else —
   `{"error": "usage", "message": "Missing command."}`. Under `--json` stdout
   belongs to a program, and ~40 lines of Rich-formatted help there would hand a
   `jq` pipeline a parse error.
2. **Click `+` beside Fleet.** Onboarding opens on the right: browse or type a
   directory (`~` and `$VAR` expand). When it resolves, the UI runs the
   equivalent of `aisquare init <path>` and then `aisquare doctor` **in the
   background, without leaving the UI or prompting**, streams the log, and the
   project appears as a card in the navigator. Doctor warnings are listed with
   their fixes; the fix is one click where it is a known command (Phase 2).
3. **Click the project.** The Project view opens on its **Manager** tab. Press
   *Start manager* and the manager's live Claude Code session fills the pane.
   Type your goal to it in prose, exactly as you would to any Claude session. It
   writes contract-carrying tasks, spawns coders, a tester and a reviewer as the
   work needs them, reopens what fails, calls a validator once everything is
   done, and posts `READY: <PRs + evidence>` when its gate passes. It never
   writes code and never merges — a human does (Phase 5).
4. **Watch the agents appear**, indented under the project, each with a role
   icon (🧭 manager · 🔨 coder · 🧪 tester · 👀 reviewer · 🛡 validator) and a
   state chip — **▶ working**, **⏸ waiting**, **🔔 NEEDS YOU** (with a terminal
   bell), **💤 exited(N)**, **✗ lost**. **Click an agent** and you see its real
   session; click into the pane and every key you type goes to it. `＋ spawn
   agent` on a project starts one of your own (Phase 4).
5. **Press `F12`** to hand focus back to the sidebar (it is the one key the pane
   never forwards; configurable). With the sidebar focused: `t` picks a theme,
   `q` quits the UI — and the agents keep running.

From any terminal, the same session at full fidelity:

```sh
aisquare fleet attach                     # the active project's fleet, in raw tmux
```

Everything the UI does is a CLI command, and every command takes `--json`:

```sh
aisquare fleet ls                         # this project's agents and their live state
aisquare --json fleet ls                  # what the UI and any automation read
```

---

## The roles

Five roles, each a briefing the harness injects at launch and a place in the
manager's loop. Model ladders and effort come from the existing harness
(`aisquare team harness` shows the matrix): the manager rides the planner's
ladder (`fable → opus → sonnet`); coder, tester and reviewer start on `sonnet`
with `opus` as the fallback rung; the validator runs `fable → opus`, one effort
tier above the work it gates.

| Fleet role | Repo role | Job in the loop | Runs in |
| --- | --- | --- | --- |
| **manager** | `planner` + fleet authority | intake → contracts → `fleet spawn` → steer → report. One per project. Never codes, never merges. | the repo root |
| **coder** | `coder` | implements one task to its acceptance criteria; pushes and opens the PR | its own git worktree |
| **tester** | `runner` (`tester` is the fleet's name for it; `runner` still works everywhere) | adversarial verification: runs the *full* check the contract names, tries to break the change, then `task done` with evidence or `task reopen` with the reason | the repo root. It gets no worktree of its own and **nothing moves it into the coder's** — so whoever spawns it names the branch or the tree to check, in the tester's `--prompt` or a later `fleet tell`, or its "full check" runs against an unchanged root |
| **reviewer** | new | reads the PR as the stranger who will maintain it; findings on the PR via `gh pr review`; read-only by construction | its own worktree, `--restricted` |
| **validator** | `validator` | one final gate over the assembled deliverable before the manager says READY | the repo root |

The manager talks to its agents only through the board — tasks, notes, signals
— and `fleet tell` nudges. When a sub-agent writes a result to the board, the
manager is woken (a fixed one-line nudge typed into its pane, only while it is
*waiting*) and reads the update on its next turn; when the manager stops with
new board events pending, its `Stop` hook lets it continue. No daemon, no poll
loop, and it works with the UI closed (Phase 5).

Standalone use is unchanged: `aisquare launch planner` and `aisquare launch
runner` keep working outside any fleet.

---

## Command reference

Every `fleet` command takes `--project PROJECT` (`-P`) — a codename, a directory
name or an id prefix; default: the active project — and honours the global
`--json`, which puts a machine-readable object on stdout and nothing else. The
service's refusals map onto stable error codes (`fleet_unavailable`,
`not_found`, `no_such_agent`, `fleet_error`) with the reason in `detail`.

### `fleet spawn`

```sh
aisquare fleet spawn manager
aisquare fleet spawn coder --label coder-auth --task tsk_01k9q8p3
aisquare fleet spawn tester --no-worktree
aisquare fleet spawn reviewer --permission-mode acceptEdits
aisquare fleet spawn coder --bin claude2 --prompt "start from the failing test" -- --model opus
```

Starts an agent in the project's tmux session — a window running
`aisquare launch <role> …` — with the role's permission flags and a session id
minted *before* launch, records it, and prints a receipt:
`✓ spawned coder-auth (agt_…) → asq-amber-otter %7`. Anything the receipt
should tell you — a label that had to be suffixed, a worktree or branch that
already existed, an agent that did not come up before its prompt was typed —
follows as a `⚠` line.

| Flag | Meaning | Default |
| --- | --- | --- |
| `<role>` | `manager`, `coder`, `tester`, `reviewer`, `validator`, or any role you have bound with `team bind` | — |
| `--label L` / `-l L` | the agent's label (see [Naming](#naming)) | `<role>-<task short id>` with `--task`, else `<role>-<n>` |
| `--task ID` | the board task this agent is for (id or prefix) | none |
| `--worktree` / `--no-worktree` | run in its own git worktree | the role's setting: on for coder and reviewer |
| `--permission-mode M` | Claude Code permission mode | the role's setting: `auto` |
| `--bin B` | the agent executable | the role's binding, else `claude` |
| `--prompt TEXT` | first message typed once the agent is up | none |
| `--as SESSION` | the acting session — a manager passes its own, so the row records who spawned it | `user` |
| `-- <agent args>` | everything after the options goes to the agent, as with `aisquare launch` | — |

Refused, with the reason in the message: a second `manager`, more agents than
`max_agents_per_project`, `--worktree` in a project that is not a git
repository, an unknown role.

### `fleet ls` / `fleet status`

```sh
aisquare fleet ls
aisquare fleet ls --all
aisquare fleet status --project amber-otter
```

One row per agent — label, role, state chip, `(worktree)`, the detail behind
the state, the pane id — under a header naming the project, its codename and
its tmux session. `ls` shows live agents; `--all` (`-a`) includes the ones that
have ended. `status` is the same data, always live only.

State is **derived, never stored**: a fresh board session row wins (working ·
waiting · attention); otherwise tmux's facts (a dead pane → exited with its
code; activity → working; else waiting); no pane at all → lost; `· unknown`
when neither source can answer. The detail beside the chip says why when that
is not obvious — an exit code, `no hooks` for a binary without our lifecycle
hooks, `pane gone`.

### `fleet tell`

```sh
aisquare fleet tell coder-auth "use the existing JWT helper, do not add a dependency"
```

Types the text into the agent — **only** when it is *waiting* and its pane is
alive. Otherwise the message is filed as a board note addressed to that agent,
and the output says which happened (`✓` typed, `→` noted). Never interrupts an
agent that is working or sitting on a permission prompt. Takes `--as SESSION`.

### `fleet stop`

```sh
aisquare fleet stop coder-auth
aisquare fleet stop coder-auth --force
```

Sends `/exit`, waits a grace period, then kills the window. The agent's own
`SessionEnd` hook releases its task claims on the way out. `--force` skips the
graceful exit.

**When tmux cannot confirm the pane died** — a wedged server, a `tmux` that
left `PATH` — the row is **left live** and the command fails saying so, rather
than reporting `✓ stopped` over an agent that is still running. Re-run it once
tmux answers again, or `fleet reap` after the server comes back.

### `fleet attach`

```sh
aisquare fleet attach
aisquare fleet attach --project amber-otter
```

Replaces this terminal with `tmux attach` on the project's fleet session — the
full-fidelity escape hatch. Under `--json` it prints the command it would run
instead of running it.

### `fleet reap`

```sh
aisquare fleet reap
aisquare fleet reap --all
```

Records agents whose panes have died as ended (with their exit code), marks
agents whose panes have vanished as lost, and removes the worktrees of ended
agents **whose branch is merged**. It never deletes unmerged work. `--all`
walks every project's fleet, not just this one.

### `fleet rename`

```sh
aisquare fleet rename ruby-fox --project api
```

Sets the project's fleet codename — same shape as a generated one, unique on
this machine — and renames the tmux session to match. If tmux is unreachable at
that moment the row is renamed anyway and the next `reap` reconciles.

### `fleet pause` / `fleet resume`

```sh
aisquare fleet pause
aisquare fleet resume
```

Sets (and clears) a named board signal, `fleet-paused`, that the manager's
cycle respects: while it is on, the manager spawns nothing. Running agents are
not touched. Both take `--as SESSION`.

### `ui`

```sh
aisquare ui
```

The app — what bare `asq` runs. Explicit so a script or alias can reach it, and
so `aisquare ui --help` exists. Without an interactive terminal it refuses with
the reason (`not_a_tty`) rather than starting a full-screen app into a pipe.

---

## Permission modes and every default you can change

Autonomy means answering permission prompts. Claude Code offers
`--permission-mode acceptEdits | auto | bypassPermissions | manual | dontAsk |
plan` plus `--restricted`. The fleet's default for **every** role is **`auto`**:
Claude Code's classifier-based mode approves routine tool calls itself and
still stops for the risky ones, so the loop runs unattended and the fleet UI
is where the remaining prompts surface — 🔔 on the row, a bell, one click to
answer.

| Role | Default | Note |
| --- | --- | --- |
| manager | `auto` | its tool use is board and fleet CLI calls |
| coder, tester, validator | `auto` | the classifier answers every tool call, the project's own check commands included. A project allowlist that pre-approves `make check`, `pytest`, `git` and `gh` is designed (plan §3.6) and **is not in this checkout**: nothing here writes or passes an allowed-tools list |
| reviewer | `auto` + `--restricted` | read-only by construction |

The mode is passed straight through to `claude` and nothing here reads its
answer, so where `auto` is unavailable to the account the refusal appears in
that agent's own pane and the spawn does not fall back: pick another mode
yourself, per spawn (`--permission-mode acceptEdits`) or for the role
(`aisquare config set fleet.roles.coder.permission_mode acceptEdits`).
`bypassPermissions` is available and is never a default.

**Every default is a default.** Everything this guide calls one — permission
mode, worktree-per-role, the escape key, the agent cap, the tmux socket, the
native-agent-teams switch, codenames, labels — is yours to change, under one
precedence rule:

> per-spawn flag  >  `[fleet]` config  >  built-in default

**No `[fleet]` setting is read from the environment**: there is no
`AISQUARE_FLEET_*` variable, and the fleet reads this section from the config
file alone. The environment layer is real one level down — the model, effort
and binary a launch resolves (`AISQUARE_MODEL_<ROLE>` and friends, below) —
which is the harness's rule, not this one.

Three places to change one:

- **per spawn** — `fleet spawn … --permission-mode acceptEdits --no-worktree --bin claude2`;
- **in config** — `aisquare config set fleet.max_agents_per_project 6`, or
  `aisquare config set fleet.roles.coder.permission_mode acceptEdits`, or edit
  the file directly;
- **in the UI** — the project's *Settings* tab writes the same config (Phase 6).

The whole `[fleet]` section of `~/.aisquare/config.toml` at its defaults, as
`aisquare config set` writes it:

```toml
[fleet]
tmux_socket = "asq"                       # the private tmux server: tmux -L asq
escape_key = "f12"                        # hands focus from an agent pane back to the sidebar
max_agents_per_project = 4                # the manager (and you) cannot exceed this
worktree_dir = ".aisquare-worktrees"      # relative to the repo root; kept out of git via .git/info/exclude
disable_native_agent_teams = true         # exports CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=0 into fleet launches
max_continuations_per_hour = 30           # cap on the manager's Stop-hook continuations

[fleet.roles.manager]
permission_mode = "auto"                  # any Claude Code mode; "" = pass no flag
worktree = false
extra_args = []

[fleet.roles.coder]
permission_mode = "auto"
worktree = true                           # one worktree per coder: parallel coders never share a tree
extra_args = []

[fleet.roles.tester]                      # `runner` is accepted as an alias
permission_mode = "auto"
worktree = false                          # runs in the repo root; point it at the branch yourself
extra_args = []

[fleet.roles.reviewer]
permission_mode = "auto"
worktree = true
extra_args = ["--restricted"]             # read-only by construction

[fleet.roles.validator]
permission_mode = "auto"
worktree = false
extra_args = []
```

A role the file omits gets the built-in shape (`auto`, no worktree, no extra
args). A config file that will not parse costs you the customisation and
never the fleet: the defaults apply and nothing refuses.

**Model, effort and binary per role stay where they already live** — one home
per concept: `aisquare team harness` (the ladder and the effort offsets),
`aisquare team bind <role> --bin … --env … --arg …` (launch profiles),
`AISQUARE_MODEL_<ROLE>`, `AISQUARE_EFFORT_<ROLE>` and `AISQUARE_BIN_<ROLE>` in
the environment. A fleet launch is an `aisquare launch <role>` inside a tmux
window, so all of it applies unchanged, as does the Explainability wiring.

**Native agent teams are off in fleet launches.** Claude Code's own
experimental *agent teams* (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`) let a
session spawn teammate sessions outside our tmux server — invisible to the
sidebar, the board and the wake-up rules — so by default the fleet exports the
variable as `0` into the sessions *it* starts and briefs the manager that
`fleet spawn` is the one way to get help. Your own `claude` sessions keep
whatever they had. `disable_native_agent_teams = false` turns this off.

---

## Naming

Measured before it was designed: a non-git directory is its own project (a
parent holding several repos is one project); two checkouts can share a
directory name; and tmux will *create* a session named `a.b` or `a:b` but
cannot *target* it, because `.` and `:` are its separators. So the fleet has a
display name for people and a slug-safe token for machines.

**Project display name.** The directory's basename — `aisquare-cli` — for git
repos and plain directories alike. Two projects sharing a basename also show
their parent as a dim subtitle (`~/work/api`, `~/oss/api`).

**Codename.** Every project that enters the fleet gets an `adjective-animal`
codename — `amber-otter` — matching `^[a-z]{3,7}-[a-z]{3,7}$`, from two
hand-curated, family-friendly word lists (96 by 96, so 9,216 pairs). It is
**deterministic from the project id** — itself a stable hash of the resolved
root — walking to the next pair on a collision, so the same checkout gets the
same name on every machine and after a `context.db` loss, and two projects on
one machine never share one. It is assigned lazily, the first time the project
touches the fleet — never at `init`, so a memory-only user never sees one. The
codename is primary only where a stable token is needed: the tmux session, the
attach hint, branch names, `fleet` command arguments. Elsewhere it is a dim
badge beside the name. `fleet rename` changes it; `aisquare project switch
amber-otter` and `--project amber-otter` both resolve it, and the "matches
several projects" error lists codenames, because basenames are what collide.

**tmux.** Session `asq-<codename>`, always targeted exactly (`=asq-amber-otter`)
so `asq-ruby-fox` never prefix-matches an `asq-ruby-foxhound`. Window name =
agent label; panes are addressed by `%id`, never by name, so renames are
harmless. A row stores the pane id and *derives* the session name from the
current codename — a rename never strands an agent.

**Agent labels.** `manager` — always, one per project; a second is refused.
Everything else matches `^[a-z][a-z0-9-]{1,23}$`: 2–24 characters, lowercase
letters, digits and `-`, no `.`, `:` or spaces. The manager is briefed to make
labels descriptive (`coder-auth`, `tester-py311`, not `coder-2`). Labels are
unique per project among **live** agents — an ended agent frees its label. A
collision appends `-2`, `-3` … and the receipt names the label actually used
(`✓ spawned coder-auth-2 (asked: coder-auth)`) rather than failing: a manager
mid-plan must not stall on a name. Without `--label`, the default is
`<role>-<task short id>` when there is a `--task`, else `<role>-1`.

**Branches and worktrees.** A coder's branch is
`fleet/<codename>/<task-short-id>-<slug>` — `fleet/amber-otter/01k9q8p3-wire-auth`:
the codename groups one fleet's branches and tells two machines apart on one
remote; the slug is the task title lowercased, runs of anything but `[a-z0-9]`
collapsed to `-`, clipped to 32 characters. A spawn without a task uses
`fleet/<codename>/<label>`. Its worktree lives at
`<repo>/.aisquare-worktrees/<label>`, excluded through `.git/info/exclude`, and
shares the principal repo's board, context and snapshot for free — that is the
worktree resolution the memory layer already had. **A non-git project cannot
have worktrees:** `fleet spawn coder --worktree` there refuses with
"not a git repository — spawn without --worktree or pick a repo inside it",
and the spawn dialog greys the option out.

**A worktree reused by a later agent** — the same label, the previous agent
having ended — is put on the branch *that* spawn asked for: checked out when
the tree is clean, and refused when it holds uncommitted work on another
branch. Either way the receipt says where the tree came from and where it went.
A worktree that a *live* agent is already working in is never handed to a
second one: that spawn is refused rather than suffixed, because the tree named
by that label belongs to the spawn that won the label.

Worked examples: `~/Code/AISquare/ws2/aisquare-cli` → `aisquare-cli`,
codename `amber-otter`, session `asq-amber-otter`, branches
`fleet/amber-otter/…`. `~/Code/AISquare` (a non-git parent of several repos) →
`AISquare`, `quiet-lynx`, `asq-quiet-lynx`, no worktrees. `~/work/api` and
`~/oss/api` → `api · amber-otter` (subtitle `~/work/api`) and `api · ruby-fox`
(subtitle `~/oss/api`).

---

## What runs where

```text
  asq  — one Textual process, a VIEW                      tmux server  -L asq  — the SUBSTRATE
 ┌────────────────────────────────────────────┐          ┌───────────────────────────────────────────────┐
 │ left : Fleet ▸ projects ▸ agents ▸ Doctor  │ capture  │ session asq-amber-otter                        │
 │ right: onboard · manager · agent · board · │◄─────────│  ├─ window manager    : aisquare launch manager│
 │        doctor · explainability · settings  │ send-keys│  ├─ window coder-auth : aisquare launch coder …│
 └───────────────────┬────────────────────────┘─────────►│  └─ window tester-1   : aisquare launch tester │
                     │ reads (polls, like board -w)      │ session asq-quiet-lynx …                       │
                     ▼                                   └──────────────────────┬────────────────────────┘
        ~/.aisquare/context.db                                                  │ hooks, inside every agent:
        project · team_session · team_task · team_event                         │ SessionStart · UserPromptSubmit
        fleet_agent                     ◄───────────────────────────────────────┘ Stop · Notification · SessionEnd
```

**The private server.** `tmux -L asq -f ~/.aisquare/fleet-tmux.conf`. The
config is bundled and regenerated (do not edit it): status line off,
`escape-time 0` so Esc interrupts Claude immediately, 50,000 lines of history,
`remain-on-exit on` so a crashed agent's last screen stays readable, truecolor,
extended keys on so shift+enter and friends reach Claude Code, mouse off (the
UI owns the mouse), monitor-activity on. One server for every project; one
session per project; one window per agent.

**The UI holds no state that matters.** What must keep running lives in tmux
(the processes) and `~/.aisquare/context.db` (the `fleet_agent` rows, the
codename, the board). Kill the UI and every agent keeps running. The tmux
server inherits nothing of a tracing identity from whoever started it; each
agent takes its own through `aisquare launch`.

**Attach from any terminal.** `aisquare fleet attach` is the escape hatch, and
it is a feature: the UI's rendering of a pane is a convenience, and the raw
session is always one command away. The equivalent by hand, for a project whose
codename is `amber-otter`:

```sh
tmux -L asq attach -t asq-amber-otter
tmux -L asq list-sessions
```

**Keys.** With a pane focused, every key goes to the agent except the escape
hatch (`F12`). Printable characters travel as typed; special keys are
translated into tmux's names (Enter, BSpace, ctrl+c → `C-c`, shift+tab →
`BTab`, …). Paste is bracketed, so Claude Code sees one paste and not one Enter
per line. The wheel scrolls a pane's history; any key returns to live. Modifier
chords beyond ctrl and alt depend on your *outer* terminal speaking the kitty
keyboard protocol (kitty, ghostty, wezterm, foot, recent alacritty): in
VTE-based terminals and Windows Terminal, shift+enter arrives as plain enter
and the UI never fakes it — `\` then Enter inserts a newline in Claude Code
everywhere.

---

## Troubleshooting

**`asq` prints usage instead of opening the UI.** It is not at an interactive
terminal: stdin or stdout is a pipe, or `TERM` is empty or `dumb`. That is
deliberate — scripts must never meet a full-screen app. Run it in a terminal,
or ask `aisquare ui` for the reason. **`asq --json` prints neither the UI nor
usage**: it prints `{"error": "usage", "message": "Missing command."}` and exits
2, at a terminal as much as in a pipe, because a caller that asked for JSON gets
JSON or nothing.

```sh
aisquare ui
aisquare --json fleet ls
```

**tmux is missing or too old.** Every fleet command refuses with
`fleet_unavailable` and the reason ("tmux is not installed", "tmux 3.1 is too
old — the fleet needs 3.2 or newer"); the Welcome page shows `✗ tmux`;
everything outside the fleet keeps working. `aisquare doctor` gains a `tmux`
check — present, and new enough (the 3.2 minimum, with a note below 3.5) — with
per-OS install hints, and warn-level checks for `gh` and the fleet's own stale
rows (Phase 3). It does **not** source the bundled config: `tmux -f` queues
configuration errors for the first attached client, which the fleet never has,
so a `tmux` that rejects a line in that file surfaces as an odd or dead pane
rather than as a doctor warning. Install or upgrade as above and re-run:

```sh
tmux -V
aisquare doctor
```

**Rows say `✗ lost`, or windows outlive their rows.** The tmux server was
killed, or a pane vanished. `reap` reconciles both directions — dead panes are
recorded as ended with their exit code, vanished panes as lost, and worktrees
whose branch is merged are removed; unmerged work is never deleted:

```sh
aisquare fleet reap
aisquare fleet reap --all
```

To stop everything the fleet ever started, on every project, kill the private
server — this ends every agent at once, so prefer `fleet stop` per agent:

```sh
tmux -L asq kill-server
```

**An agent is stuck on a permission prompt.** Its row shows **🔔 NEEDS YOU**
and the terminal rings. Nothing nudges it and nothing answers for it: click the
agent, click into the pane, and answer as you would in the agent's own
terminal — or `aisquare fleet attach` and answer there. If prompts are routine
for that project, the role's `permission_mode` is the knob — per spawn
(`--permission-mode`) or in `[fleet.roles.<role>]`; there is no project
allowlist in this checkout. `bypassPermissions` exists and is never a default.

**The manager needs you.** 🔔 on the project, and a `question` note on the
board (the manager asks after being blocked twice on one task). Open the
Manager tab and answer in the session.

**`fleet spawn` refused.** The message says why: a second manager (there is
one per project), the agent cap (`max_agents_per_project`, with the count),
`--worktree` in a non-git project, an unknown role, an invalid label. A label
that was merely *taken* is not refused — it is suffixed, and the receipt says
so. The one exception: an agent **with a worktree** that loses its label in a
true parallel race is refused rather than suffixed, since the tree that label
names belongs to the winner.

**A `fleet` command says the service is not wired yet.** The checkout predates
Phase 3 (plan §9): it carries the fleet's contracts — the CLI, the config, the
schema, the names — but not the lifecycle behind `spawn`, `ls`, `tell`, `stop`,
`reap`, `attach`, `rename`, `pause` and `resume`, which land together in that
phase and say so until then. Update, or read the plan's Decisions log for what
has landed.

**Two projects match `--project`.** Basenames collide; codenames do not. The
error lists both with their codenames — use the codename.

**Something looks off in a pane.** The escape hatch is the diagnostic:
`aisquare fleet attach` shows the same session with no rendering hop in
between. If tmux shows it right and the UI does not, that is a UI bug worth an
issue with your terminal's name.

---

## Later

- **Other agents, Codex first.** The substrate is agent-agnostic — a fleet
  window runs whatever binary the role is bound to, and `--bin` already takes
  one — so *surfacing* a Codex session needs nothing new. What Codex lacks is
  our lifecycle hooks: until an equivalent exists it would have no board state
  and no wake-ups, and its row would say `no hooks`. v1 is Claude Code only
  (plan §8.5); nothing in the schema blocks the rest.
- **Auto-merge on green** as an opt-in policy. In v1 a human merges (plan §3.5).
- **Remote or multi-machine fleets** — the cloud roadmap is unchanged.
- **A tmux-less backend** for hosts without tmux (plan §3.1, option A).
