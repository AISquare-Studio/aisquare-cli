# The fleet

`asq` with no arguments opens one view over every project you have registered,
the **manager** agent running in each, and the agents that manager spawns. Every
agent is a *real* Claude Code session — started in the background inside a
private tmux server, monitored, and surfaced in the UI exactly as it would look
in its own terminal. Nothing is relayed or re-rendered as a chat; click an agent
and you are typing into the session itself.

Three ideas carry the whole feature:

- **One view.** Left: a navigator — `Fleet ▸ projects ▸ agents`, then under a
  line *⚙ Settings* (App settings, Accounts, Doctor) with a dim status line: the
  AISquare sign-in and the Claude accounts' headroom as two dots, and with Codex
  accounts a line of their own under it, `● 1× codex` (`?` explains the colours;
  a click opens Accounts). Right: whatever you clicked — onboarding
  for a new directory, a project's manager, an agent's live session, the board,
  the settings, doctor findings with their fixes.
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
| **`tmux`** | the fleet itself — spawning and surfacing agents | **3.2** (`new-window -e`, extended keys); **3.5+ recommended**, where shift+enter and the other extended chords reach the agent (3.3/3.4 would mistype them, so there shift+enter travels as ctrl+J — Claude Code's same newline — and the rest are refused with a warning) |
| **`claude`** or **`codex`** | every fleet role runs on a terminal coding agent: Claude Code, Codex, or each role its own ([coding agents](coding-agents.md)) | Claude Code 2.1.x — `--session-id`, `--permission-mode`, `--effort`; Codex CLI 0.162.1 — `--approve-for-me`, hooks |
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

Then the CLI, connected to your coding agent (the Welcome page in `asq` does
this step for you):

```sh
pipx install 'aisquare-cli[tui]'          # or: pip install 'aisquare-cli[tui]'
aisquare agents connect claude-code       # the lifecycle hooks the fleet reads state from
aisquare agents connect codex             # or Codex's, in CODEX_HOME
aisquare agents use codex                 # Codex for every launch (the default is Claude Code)
aisquare doctor                           # is everything wired? (and how to fix anything)
```

Textual, the UI toolkit, becomes a core dependency of `aisquare-cli` with the
fleet (plan §3.7); `[tui]` stays as a no-op alias for one release, so the line
above is right on either side of that change. The hooks matter: an agent's
state chip (working · waiting · needs you) comes from the lifecycle hooks the
board already uses (Claude Code's, or Codex's once you trust them in its
`/hooks`), so an agent launched without them shows only what tmux can see and
its row says so (`no hooks`).

---

## The first five minutes

1. **Run `asq`** (or `aisquare`) in a terminal. The UI opens: the navigator on
   the left and a Welcome page on the right that walks you to a fleet in three
   steps: pick a project, set up your coding agent, then start the manager and
   two coders. Step 2 is the agent the fleet will launch: Claude Code, or Codex
   where you chose it or where it is the only one installed. It names the
   install commands when the agent is missing (both agents' when neither is
   installed), and with both installed it asks which to use and saves the pick as
   your default (`aisquare agents use`). It shows the version, whether the account
   the manager will run on is signed in — *Sign in* opens the Accounts page, for
   Codex with `codex login` already open there — and *Connect* installs aisquare's
   hooks. Codex runs hooks only once you trust them: the card says so, and you do
   it in the manager's pane (`/hooks`), the first time it starts. Step 3 starts
   the manager and the coders on that agent, each on the account the account
   ladder picks (see [Accounts](#accounts)); a role bound to an agent of its own
   (`aisquare team bind coder --agent claude-code`) keeps it. Both agents ask
   whether to trust a new folder on their first start, so step 3 types nothing
   into the manager: you answer it in its pane. Press
   `w` in the sidebar to come back to it. No script ever meets a
   full-screen app, and the two ways of getting no UI differ. **In a pipe or
   with `TERM=dumb`**: the help page on stdout and exit 2, byte for byte as
   before — the same ~5 KB either way (5,159 bytes at 80 columns, measured).
   **Under `--json`**, and that is checked before the terminal is, so it holds
   at a terminal too: exit 2 and one line, nothing else —
   `{"error": "usage", "message": "Missing command."}`. Under `--json` stdout
   belongs to a program, and ~40 lines of Rich-formatted help there would hand a
   `jq` pipeline a parse error.
2. **Or click `+` beside Fleet** (or press `+` in the sidebar). Onboarding
   opens on the right: browse or type a directory (`~` and `$VAR` expand).
   When it resolves, the UI runs the equivalent of `aisquare init <path>` and
   then `aisquare doctor` **in the background, without leaving the UI or
   prompting**, streams the log, and the project appears as a card in the
   navigator. Doctor warnings are listed with their fixes; the fix is one click
   where it is a known command (Phase 2).
3. **Click the project.** The Project view opens on its **Manager** tab. Press
   *Start manager* and the manager's live coding-agent session fills the pane.
   Type your goal to it in prose, exactly as you would to any supported terminal
   agent session. It
   writes contract-carrying tasks, spawns coders, a tester, a ui-tester for
   anything a user sees, and a reviewer as the
   work needs them, reopens what fails, calls a validator once everything is
   done, and posts `READY: <PRs + evidence>` when its gate passes. It never
   writes code and never merges — a human does (Phase 5).
4. **Watch the agents appear**, indented under the project, each with a role
   icon (🧭 manager · 🔨 coder · 🧪 tester · 🌐 ui-tester · 👀 reviewer · 🛡 validator) and a
   state chip — **▶ working**, **⏸ waiting**, **🔔 NEEDS YOU** (with a terminal
   bell), **⏳ limited** (parked on a usage limit, Claude Code's or Codex's, with the reset time),
   **💤 exited(N)**, **✗ lost**. **Click an agent** and you see its real
   session; click into the pane and every key you type goes to it. `＋ spawn
   agent` on a project starts one of your own (Phase 4): its dialog is `aisquare
   fleet spawn` with the role, the agent, the account (that agent's slots) and
   the permission in that agent's words (Claude Code's `--permission-mode`;
   Codex's `--sandbox` and `--approval`), and shows the flags the launch will pass.
5. Selecting an agent gives its pane the keyboard at once — type, and it reaches
   the agent. **Press `F12`** to hand focus back to the sidebar (it is the one key the pane
   never forwards; configurable). With the sidebar focused: `t` picks a theme,
   `q` quits the UI — and the agents keep running.
   The line between the navigator and the content is a divider: drag it to
   give an agent's pane more columns or the navigator room for long labels
   (bounded so neither side becomes unusable, and re-bounded when the terminal
   shrinks), or, with the sidebar focused, `>` / `<` step it by four columns
   and `=` puts it back; a double click on the divider does the same. It
   lights up while the sidebar has focus — where `q`, `t` and the arrows go.
   The width is remembered in `~/.aisquare/state.json` beside the theme, and
   `?` lists the keys.

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

Six roles, each a briefing the harness injects at launch and a place in the
manager's loop. Model ladders and effort come from the existing harness
(`aisquare team harness` shows the matrix): the manager rides the planner's
ladder (`fable → opus → sonnet`); coder, tester, ui-tester and reviewer start on `sonnet`
with `opus` as the fallback rung; the validator runs `fable → opus`, one effort
tier above the work it gates. `fleet spawn` passes the role's model and effort as
`team spawn` resolves them, without a paid probe at the spawn (the cached verdicts,
else the first rung; Claude Code runs its own default where the account lacks the
model). A Codex role takes `[agents.models.codex]` (and `[agents.models.codex.roles.
<role>]`), else Codex's own default; `[agents.models.claude-code]` pins Claude Code's
over the ladder, and `AISQUARE_MODEL_<ROLE>`/`AISQUARE_EFFORT_<ROLE>` pin either. A
hand `aisquare launch <role>` leaves Claude Code's argv as it always was (its own
default), and gives Codex its configured pins.

| Fleet role | Repo role | Job in the loop | Runs in |
| --- | --- | --- | --- |
| **manager** | `planner` + fleet authority | intake → contracts → `fleet spawn` → steer → report. One per project. Never codes — a direct "fix this" becomes a coder spawn — never merges. | the repo root |
| **coder** | `coder` | implements one task to its acceptance criteria; pushes and opens the PR | its own git worktree |
| **tester** | `runner` (`tester` is the fleet's name for it; `runner` still works everywhere) | adversarial verification: runs the *full* check the contract names, tries to break the change, then `task done` with evidence or `task reopen` with the reason | the repo root. It gets no worktree of its own and **nothing moves it into the coder's** — so whoever spawns it names the branch or the tree to check, in the tester's `--prompt` or a later `fleet tell`, or its "full check" runs against an unchanged root |
| **reviewer** | new | reads the PR as the stranger who will maintain it; findings on the PR via `gh pr review`. Launched without the tools that edit files (`--disallowedTools=Edit Write NotebookEdit`) and with its shell: Claude Code's `--restricted` removes the Bash that `gh pr diff`, `gh pr review` and its `aisquare` cycle need (0.6.0 and 0.7.0 launched it so, and it could do none of them). Its shell could still push or merge: its briefing forbids both and `auto` mode checks each action, but only the credentials it runs with can refuse them (see the table below) | its own worktree |
| **validator** | `validator` | one final gate over the assembled deliverable before the manager says READY | the repo root |
| **ui-tester** | new | verifies tasks titled `UI: …` in a **real browser** — Claude in Chrome, the Chrome DevTools MCP or a Playwright MCP, whichever this window has — and measures (screenshots, computed sizes, console, network) instead of eyeballing; `task done` names the branch or commit and the URL it verified, or `task reopen` carries a screenshot. With no browser tool it runs the non-browser checks and reopens the task as "not browser-verified", never passes it. **Asked to be read-only, not made read-only** — its briefing says never edit and never push, and nothing enforces that (see the permission-mode table below: no allowed-tools list is written or passed, and `--restricted` would remove the Bash its own `task done`/`task reopen` need). Launched with `--chrome` by the role itself (`RoleProfile.default_args`), on any machine, unless `--no-chrome` is given; on Codex it gets no flag and browses with Codex's own `browser_use` feature and bundled browser plugin, which `aisquare doctor`'s browser-tools line reads from its `config.toml` | the repo root. Like the tester it gets no worktree and **nothing moves it into the coder's** — worse here, because it fails silently: it opens the URL, sees a working page and measures the PRE-change build honestly. So whoever spawns it names the branch or tree and the URL in its `--prompt` (the manager's loop does), and its verdict says which build it measured |

The manager talks to its agents only through the board — tasks, notes, signals
— and `fleet tell` nudges. When a sub-agent writes a result to the board, the
manager is woken (a fixed one-line nudge typed into its pane, only while it is
*waiting*) and reads the update on its next turn; when the manager stops with
new board events pending, its `Stop` hook lets it continue. No daemon, no poll
loop, and it works with the UI closed (Phase 5). A Codex agent's board write
is run by its launch's broker, outside Codex's sandbox (where the fleet's tmux
socket is refused, and stays refused: from there it could type into any pane), so
the nudge is sent from there; where tmux refuses it all the same, the write
leaves the nudge owed in the store and the agent's own `PostToolUse` hook, which
Codex runs outside the sandbox, sends it as the command ends (its `Stop` hook, if
that one could not), only while the manager has not yet read the news.

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
aisquare fleet spawn coder --agent claude-code --bin claude2 --prompt "start from the failing test" -- --model opus
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
| `<role>` | `manager`, `coder`, `tester`, `ui-tester`, `reviewer`, `validator`, or any role you have bound with `team bind` | — |
| `--label L` / `-l L` | the agent's label (see [Naming](#naming)) | `<role>-<task short id>` with `--task`, else `<role>-<n>` |
| `--task ID` | the board task this agent is for (id or prefix) | none |
| `--worktree` / `--no-worktree` | run in its own git worktree | the role's setting: on for coder and reviewer |
| `--permission-mode M` | one permission for any agent, `WRITE:APPROVALS` (`workspace:auto`, `read_only:auto`, `workspace:ask`, `full:never`…), mapped to the agent's own flags ([the table](#one-permission-for-every-agent)); or a value in the agent's own words (Claude Code's `auto`, `acceptEdits`, …; Codex's sandbox) | the role's setting: `permission`, else the agent's own (`auto` on Claude Code, `--approve-for-me` on Codex) |
| `--agent A` | the coding agent (`claude-code`, `codex`) | the role's binding, the project's, the user's default |
| `--sandbox S` / `--approval P` | Codex's own sandbox and approval policy for this spawn; not beside a `WRITE:APPROVALS` `--permission-mode` | the role's setting |
| `--bin B` | the agent executable | the role's binding, else the agent's (`claude`, `codex`) |
| `--prompt TEXT` | its first message: typed once Claude Code's session has started, on Codex's command line ([below](#the-first-prompt-and-the-folder-trust-dialog)) | none |
| `--as SESSION` | the acting session — a manager passes its own, so the row records who spawned it | `user` |
| `-- <agent args>` | everything after the options goes to the agent, as with `aisquare launch` | — |

Refused, with the reason in the message: a second `manager`, more agents than
`max_agents_per_project`, `--worktree` in a project that is not a git
repository, an unknown role, a `--task` that is already done or dropped.

#### The first prompt and the folder-trust dialog

A folder an account has not trusted (each account slot keeps its own trust, so a
spawn onto a new `--account` meets it) opens Claude Code at its folder-trust
dialog. The agent is already in the pane's foreground there, the dialog fires no
hook, and an Enter answers it with its default, *No, exit* (measured on Claude Code
2.1.300). So `--prompt` waits for the agent's `SessionStart`, which Claude Code
fires once it sits at its prompt, past the dialog, and types the prompt then (a
folder inside one the account trusts asks nothing, and it starts in a few
seconds). If the session has not started within 20 s and a dialog is on the
agent's screen (its `Enter to confirm` row, under the folder-trust and MCP-server
dialogs alike), the prompt is not typed: it is filed as a board note to the agent's
label, and the receipt says so. Answer the dialog in the agent's pane (`aisquare
fleet attach`), then `aisquare fleet tell <label> …` types its first prompt, whose
delta carries the note. With no dialog on screen the start is waited for up to two
minutes (a slow machine; Claude Code 2.1.300 started within a second resuming a
67 MB transcript), and the same note follows only past that. `fleet restart`,
`fleet switch` and the automatic hand-over type their one line the same way. Where
no hook can say the session started — the board off (`AISQUARE_TEAM=0`), or a
launch that names no session (`-- --continue`) — the prompt is typed once the agent
is in the foreground, as before, and not while a dialog is on its screen: past 20 s
at one it is not typed, and the receipt says to answer it and `fleet tell` the
prompt. Codex takes its first prompt on its
command line and submits it after its own folder-trust and hook-review dialogs are
answered, so nothing is typed into a Codex pane at spawn.

**What the agent is told.** `fleet spawn` exports `AISQUARE_FLEET_AGENT` — the
agent's row — onto its window, and the session-start hook joins the session to
that row and puts an **ASSIGNED TO YOU** block at the top of the briefing: what
the task's state asks of *this* role. A coder is told to claim a `todo` task
(unless it still waits on other tasks — then to take pool work until it is
ready), to carry on with a `doing` task it holds, to do the rework on a task
back in `review`, to take over a `doing` task whose holder's lease has run out,
or to stand down and ask the manager when a teammate is live on it right now. A
verifier (tester, ui-tester, reviewer) is told to verify a task in `review`
and, in any other state, that it is not yet its turn. The validator gates the
assembled deliverable, not a task, so in every state it is told the task is the
context for its GATE note — nothing to claim, work or verify. `task next` puts
the assigned task first — for the agent's own session, and for a verifier's
cycle or the MCP server's `task_next` running under the same window (the order
only; a claim needs the session). The assignment ends when its task is done or
dropped: nothing is said about it afterwards, and the agent header's `task …`
chip goes with it — the label and the branch keep the task's short id. The
row is written after the window starts, so `aisquare launch` inside the window
waits for it (ten seconds at most) before it starts the agent: the first hook
always finds the row.

The row belongs to the *process* in the pane, not to a session id. Claude Code
mints a new session id on `/clear` and keeps the process, and it hands every
hook the pid of the process that fired it (`CLAUDE_PID`), which the hook
compares with the pane's own (`aisquare launch` execs the agent, so tmux's
`#{pane_pid}` is the agent). So a `/clear` keeps the agent's claims: the
`SessionEnd` hook sees the reason and the process, and the `SessionStart` that
follows moves every claim to the new id together with the row, in one store
transaction. Should tmux not answer that `SessionStart`, the bind is tried
again at the agent's next prompt and the **ASSIGNED TO YOU** block arrives with
it; and a row that ends — `fleet stop`, `fleet reap` — releases whatever its
session still held, so a claim parked for a clear never outlives the row it
was parked for. A nested `claude -p` started from the agent's shell inherits
`AISQUARE_FLEET_AGENT` but not the pid, so it is never briefed on the parent's
task and never takes its row, whatever start it reports. A binary that exports
no `CLAUDE_PID` binds its row on first arrival and keeps that session; a
`/clear` there releases its claims, as any session end does.

A **Codex** agent hands its hooks no pid of its own (the `CLAUDE_PID` one finds
is whoever started the tmux server's). It is told apart the other way round:
Codex sets `CODEX_THREAD_ID` in the shells its tools run in and never in its own
hooks (measured on 0.162.1), so a `codex exec` one of its tools starts fires every
hook with its parent's thread there, and is never briefed on the parent's task
and never takes its row; `aisquare launch` clears the variable for the agent it
starts. Codex's `/clear` (and `/new`) fires no `SessionEnd` for the thread it
leaves, only a `SessionStart` with source `clear` for a new id, which comes with the
new thread's first prompt: the row and every claim move to that id there. A `/resume` to an earlier thread takes the row, the
claims and the MCP server's identity back to it; a fresh hand-over's replacement
takes over the parked claims at its first start.

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
waiting · attention · limited — the last one trusted until the reset it names
has passed, since a parked agent fires no hook; and a Codex agent's attention
until one of its hooks moves it or its screen shows Codex's working line again,
which is the moment the human allows the command, since Codex redraws its
approval view every second it waits); otherwise tmux's facts (a dead
pane → exited with its code; activity → working; else waiting); no pane at all → lost; `· unknown`
when neither source can answer. A row whose socket another tmux server answers on
reads `unknown (another tmux server on its socket)` whatever its board says (see
*The server was stopped outside the CLI*). A Claude Code agent no hook has
spoken for yet whose screen is Claude Code's folder-trust question reads
`attention` with
`waiting for folder trust — approve it once, or aisquare accounts trust <dir>`
(see *Folder trust* under [Accounts](#accounts)); only those rows have their
screen read — a row with a board session, one started on a session id (only
Claude Code is, whatever its binary is called), or whose binary is `claude` by
name, never another harness's agent — and the screen is the question when it holds
the question's words and *No, exit* as an option of its own, in a box or not,
with no input box under it: the dialog takes the input box's place, so the words
above an input box are an agent at work. A screen that may be the question is
read as it, so a spawn never types its prompt into the dialog, where Enter picks
*No, exit*. The detail beside the chip says why
when that is not obvious — an exit code, `no hooks` for a binary without our
lifecycle hooks, `pane gone`.

### `fleet tell`

```sh
aisquare fleet tell coder-auth "use the existing JWT helper, do not add a dependency"
```

Types the text into the agent — **only** when it is *waiting* and its pane is
alive. Otherwise the message is filed as a board note addressed to that agent,
and the output says which happened (`✓` typed, `→` noted, `…` neither yet: the
line says what is at its prompt or on its way). Never interrupts an
agent that is working or sitting on a permission prompt. Takes `--as SESSION`.
Under `--json`, `typed` is `yes`, `no`, `unsent` or `maybe` beside `delivered`:
`unsent` when the text is at its prompt and tmux refused its Enter (press Enter in
its pane to send it), `maybe` when tmux did not answer in time with the text or its
Enter, which may still arrive. Neither is to be told again: read the agent's pane.

A **Claude Code** agent is *waiting* from its `SessionStart` on: Claude Code fires
it once the agent sits at its prompt, past the folder-trust dialog, so a fresh
agent is typed into before its first prompt (a compaction's `SessionStart` comes
mid-turn and changes nothing). Until it has started, the message is a board
note: the folder-trust dialog fires no hook and is as quiet as a prompt, and an
Enter there picks its default, *No, exit* (measured on Claude Code 2.1.300).
Started is what the agent's own hook wrote in this launch's record, never the
board session's last-seen time: a resumed replacement keeps its predecessor's
session, which a `team prune` may end after the launch, and that stamp is the
prune's. An
agent whose start no hook can say (the board off, `-- --continue`) is typed into
when it reads *waiting*, as before, unless a dialog of its own is on its screen:
then the message is the board note, and with the board off `fleet tell` refuses.

A **Codex** agent's pane going quiet does not show that it sits at its prompt —
its folder-trust and hook-review dialogs are as quiet — so its own hooks decide:
typed when they freshly say *waiting*; when they freshly say *working*, queued
for its next turn with `codex queue --thread <its thread> --message=<text>` under
its own `CODEX_HOME` (`✓ queued for its next turn`; measured on 0.162.1: the
message ran as the next turn of an interactive session busy with one). When they
say nothing fresh — Codex fires no hook before its first prompt, so an agent
started without one (asq Welcome's coders) has none yet — it is typed into when
its pane is quiet and shows Codex's empty prompt (the footer `? for shortcuts`);
`codex queue` cannot reach a thread before its first prompt (under
`--no-alt-screen`, which draws no such footer, the empty composer with only its
model line under it). Hooks that say *waiting* need that empty prompt too: after a turn ends Codex can open a dialog of its
own (*Switch to gpt-6-luna for lower credit usage?*, past a turn near its limit),
where the Enter after a typed line picks for you, so a message to a Codex showing
anything else is a board note that says so. A fleet window's Codex launches with
`-c notice.hide_rate_limit_model_nudge=true`, which keeps that dialog closed, and
`-c check_for_update_on_startup=false`, which keeps its *Update available* dialog out of
a starting pane (whose first choice, *Update now*, an Enter picks), each unless your own
config (or `-c`, or the `-p` profile) sets that key. Anything else, or a
queue that fails, is the board note. Every typed line is one paste with its Enter
a moment later, as `fleet stop` types `/exit`: Codex reads keys in one burst as a
paste and keeps an Enter inside it as text. The manager's wake-up nudge is typed
the same way, under the same rule.

### `fleet stop`

```sh
aisquare fleet stop coder-auth
aisquare fleet stop coder-auth --force
```

Sends the agent's exit command (`/exit` for Claude Code and Codex, typed with its
Enter a moment later), waits a grace period, then kills the window. The agent's own
`SessionEnd` hook releases its task claims on the way out; whatever the ended
session still held (a killed process fires no hook) is released here and named
in the output — `🔓 N claimed task(s) released back to the board`, each task
listed under it, `claims_released` under `--json` — so the work the next agent
inherits is visible. A release the store refused — or one the board could not
be told about — is reported, never swallowed: the row is down, the operator is
told which claims stayed with the ended session, and the command **exits 1**,
the same contract `fleet shutdown` and `fleet reap` keep for the same fact.
`--force` skips the graceful exit. A Codex agent whose screen does not show its
empty prompt (kept under an empty composer through a turn) is showing a dialog or
a draft that `/exit` and its Enter would answer, so nothing is
typed: the window is killed at once and the output says why (`killed` under
`--json`). The automatic usage-limit hand-over, which nobody watches, presses Esc
there first (the agent's `dismiss_key`), and types `/exit` once the empty prompt
is back, so the agent's `SessionEnd` ends the session it leaves; a screen Esc does
not bring back is killed as above.

**When tmux cannot confirm the pane died** — a wedged server, a `tmux` that
left `PATH`, a socket that is there but refuses this user (`Permission denied`)
— the row is **left live** and the command fails saying so, rather than
reporting `✓ stopped` over an agent that is still running. Re-run it once
tmux answers again, or `fleet reap` after the server comes back. So is a row
whose server's socket file is gone, the row `fleet shutdown` leaves live (below):
no tmux client reaches that server, but it may still run, so `fleet stop` and
`fleet restart` refuse it rather than end it, and a restart never starts a
second agent beside it. If the server is
genuinely gone — a reboot, `kill-server` — `fleet reap --all --server-down` marks
the rows on it lost; it acts only where tmux itself reports no server behind the
socket, never on a server that is merely not answering, and never on a row whose
agent may still run by the rule above (its socket file gone from under its
server in this boot, or another server on its socket): those it leaves live and
names, and `fleet shutdown --forget` ends them on your word.
To take a whole project's fleet down and record every row in one go, use
`fleet shutdown`.

### `fleet shutdown`

```sh
aisquare fleet shutdown
aisquare fleet shutdown --all --yes
```

The fleet's off switch — **this** project's by default, `--all` for every
project's, like `fleet reap`. Every agent on an answering server is stopped the
way `fleet stop` stops one (`/exit`, grace, kill), then the fleet's own tmux
**sessions** are killed and **every row is recorded**: stopped rows with their
exit status where tmux exposed one, and rows whose socket had no server as
*lost*, each with the reason the service gave.

Each agent's server is asked at the socket its row recorded at spawn (`tmux
-S`), as every fleet command asks it (see *The private server*), so a server
started under another `TMUX_TMPDIR` is reached and stopped from any shell.
Another server that `tmux -L` reaches from this shell under the same socket
name is not the agent's, and is never asked about it: its pane ids name other
agents.

A row whose server did not answer is ended as lost only where it cannot be
running: it started in another boot, or its socket file is there with nothing
listening, which is what tmux leaves when its server exits. A row with no boot
to compare (one from before 0.8.0, or on a machine that keeps no boot id: the
BSDs, a macOS whose `sysctl` failed) is ended where its socket file is gone, or
another server answers there, and it started before this boot did, as a reboot
clears `/tmp`. Otherwise
its row is **left live**, with what to do, because `aisquare uninstall` asks
the same socket and would still count it. Most often the socket file is gone
(systemd-tmpfiles or a logout removed it while the server ran on), so no tmux
client can reach the server: if it still runs, send that tmux server
`SIGUSR1`, which makes tmux create its socket again, and run it again.

It ends running work, so it asks first: it prints what it would end and
confirms at a terminal (default **no**), and off a terminal it is a dry run
unless `--yes`. Under `--json` without `--yes` it prints the plan
(`dry_run: true`) and changes nothing.

`--force` skips the `/exit` and kills a live pane, which means **no exit status
is recorded for it** — a status is only ever read from a pane that already reads
dead — and no `SessionEnd` hook runs.

`--forget` is your word that the agents it cannot reach are not running: the
rows it would leave live as above are ended as lost instead, but for a row
whose socket answers — a server listens there, so no word ends it, and the plan
shows it left live. Where tmux cannot be used here (removed, too old, or not
answering for its version), every other row is ended as lost, unchecked, and
nothing is stopped or killed: a socket is asked without tmux. Without
`--forget` such a run refuses, because a tmux server outlives its program
leaving `PATH`. `aisquare uninstall` names it for a row it counts as running
that no tmux here can reach.

What it kills is the fleet's own sessions, never the server: `tmux kill-server`
would take down every session on that socket, and the fleet's claim is only ever
to its own (a hand-made session is not the fleet's to destroy — and `[fleet]
tmux_socket` may point anywhere). A project's sessions are every one its spawns
made, by tmux's own id for it (`$3`), kept beside the launch records, plus the ones
its records name and the session its agents from before this version run in (the
one tmux says holds their panes) — whatever rows are left, or how long ago they
ended, and never by the project's codename in the store, which no agent's sandbox
can write now (the store is read-only there) and none trusts. A row ended in the store while its window runs is stopped like a running
one. `--all` also sweeps every `asq-` session tmux lists, sparing one that holds a
row the run left live. A server with nothing left on it exits by itself, so the
next `asq` or `fleet spawn` starts a fresh one on whichever account that shell
carries.

An agent spawned before this version is stopped like any other: the first fleet
command of this version took a census of such rows, before anything sandboxed could
write the store, and placed each by its row as it stood then. A row with no record
that the census did not find (a fake row, or one whose record was lost) is stopped
only by removing a dead window of its, ended as lost when it was spawned before this
boot, and otherwise left live, saying so: end it in its own window, and `--forget`
ends its row on your word.

Board notes and tasks are kept, but the ended rows' **claims are released**: a
task left `doing` by a session that no longer exists is not untouched, it is
stuck for four hours. Only a row whose pane tmux CONFIRMED dead or gone is ended
and released — a `kill-window` tmux refused is not a death, so that row stays
live, keeps its claim and its board session, and is reported LEFT LIVE. A
`fleet-paused` signal is cleared for each project this run CONFIRMED down, and
the output says so — the fleet it paused is gone. A project with a row left
live, a session left up or a listing that failed keeps its signal
(`paused_kept` in the report): the manager's "spawn nothing while paused" is
exactly the standing order wanted while you are trying to stop it. A signal the
store would not let it read or clear is named in `pause_scan_failed` rather than
passed over — the pause outlives the shutdown either way, and the next manager
is still under orders to spawn nothing.

A row that spawns AFTER the sessions are killed is reconciled too: still
running, it is left live and reported; already exited on its own, it is ended
with the exit status tmux kept — **and the pane tmux is still holding for it**
(`remain-on-exit`) is removed with it. Otherwise that pane's window holds its
session up, and the session the server, under a report saying the fleet is down.
Whatever tmux will not confirm gone there is reported instead (`sessions_failed`
or `sessions_left_up`), never assumed.

It refuses rather than guess. With no usable tmux (`fleet_unavailable`, unless
`--forget`), on a socket that cannot be ASKED whether a server is there (a wedged server's 30 s
timeout), or when run from INSIDE the fleet's own tmux server — a `fleet attach`
pane included, where the kill would take down the process printing the report —
nothing is touched and the message says why. The plan it asks you to confirm
refuses on the same principle: if a session query fails after the probe
answered, you get the refusal rather than a list that may be short of what the
run would kill. A row whose `stop` refused because its pane was seen ALIVE — or
whose kill tmux would not carry out — is left live, reported, and its session is
spared; the command then exits 1, because the fleet is not down.

Why this exists rather than `tmux -L asq kill-server` by hand: `stop` and
`reap` refuse to end a row on a server they cannot reach, because an unreachable
server is not proof a pane died. A hand-run kill leaves every row saying
`unknown (tmux unavailable)` and `reap` reaping nothing — correctly. `shutdown`
is the operator saying so, which is the one thing that resolves it.

### `fleet attach`

```sh
aisquare fleet attach
aisquare fleet attach --project amber-otter
```

Replaces this terminal with `tmux attach` on the project's fleet session — the
full-fidelity escape hatch. Under `--json` it prints the command it would run
instead of running it. The session is one the project's spawns made, its live
agents' records name (the manager's first) or its agents from before this version
run in, by tmux's own id for it, on the server their spawns recorded (`tmux -S`),
the line the Manager tab prints, so it works from a shell under another
`TMUX_TMPDIR` and after a rename tmux was not told of. A session the project's
spawns made is asked however long ago its last agent ended: an ended manager's
window lingers there and holds the session, the screen behind the 💤 row.

**Scope and exit codes.** `--all` and `--project` name different scopes and are
refused together rather than one silently winning — a usage error, exit **2**,
and under `--json` the object `{"error": "usage", "message": …}` on stdout like
every other usage error. The command exits **0** when
every row is down and every release landed; **1** when a row was left live, a
session could not be taken down, the final scan or the pause pass could not run,
or a claim release was refused — a fleet whose work stays claimed by dead
sessions is not a clean shutdown; and **130** when interrupted, after printing
the report of the rows already ended and the claims already released. Nothing
else is killed on an interrupt.

### `fleet reap`

```sh
aisquare fleet reap
aisquare fleet reap --all
aisquare fleet reap --all --server-down
```

Records agents whose panes have died as ended (with their exit code), marks
agents whose panes have vanished as lost, and removes the worktrees of ended
agents **that hold nothing found only there**: a clean tree whose commits are on
the default branch or pushed to **any** remote branch (an integration branch
counts). A tree with uncommitted changes, an untracked file that is not ignored
(even under `status.showUntrackedFiles=no`), or unpushed commits is kept and
named, with why (`worktrees_kept` under `--json`, beside `worktrees_removed`);
push it, and the next `reap` removes it. `fleet stop` says so for an agent that
had a worktree. The removed tree's branch stays. Every `reap` then looks at
each `fleet/…` branch an ended agent's row recorded (`branch` in `fleet ls
--json`) that no worktree has out, the ones kept before included, and deletes it
once the default branch has its work (`branches_deleted`, each `{repo, branch}`
under `--json`): the default branch
holds its tip, or the branch is pushed and merging it would change nothing, as
after a squash or rebase merge (git 2.38 or later). Only those are ever deleted:
a branch no row names — made by hand, or one an agent left for another — and a
branch that is not `fleet/…` are left as they are, and so is one a live agent's
row names; a project no agent with a worktree has ended in is not looked at. A
spawn makes its tree, and a reap deletes, under one lock in the repo's `.git`,
so a reap never deletes a branch a spawn is checking out: one that meets a spawn
deletes nothing, and the next reap does. A recorded branch with commits the
default branch lacks — pushed to an integration branch, say — is kept and named
on every reap (`branches_kept`), with `git -C <repo> branch -D <branch>` to drop
it once its work is abandoned: a coder spawned again for its task asks for the
same branch, checks
it out and carries on with its commits, where it used to start a new one from
the main checkout's HEAD without them. A restart of an agent whose tree was
removed makes the tree again on the branch its row recorded: *One rule for a
worktree and its branch* under [Naming](#naming).
`--all` walks every project's fleet, not just this one.

Each row is asked of its own server, at the socket its spawn recorded, so a
shell under another `TMUX_TMPDIR` reaps exactly what the spawning shell would,
and never by another server's panes. A tmux server that does not answer marks
**nothing** by default: silence is not evidence that its panes are dead — the
server may be wedged, running an older binary than the client after an
upgrade, or, for a row from before 0.8.0, which recorded no socket, alive
under another `TMUX_TMPDIR`. `--server-down` is the operator's word that the server is genuinely
gone (a reboot swept `/tmp`; `kill-server`). Even with it, rows are marked lost
only on a socket where tmux itself reports no server (`no server running on …`
or `error connecting to … (No such file or directory)`); a protocol mismatch or
a hung server still marks nothing, and so does a missing `tmux` binary. And of
the rows on such a socket, only those whose agent cannot be running, by the one
rule `stop`, `shutdown` and `uninstall` ask: a row from another boot, or one
whose socket file is there with nothing behind it. A row whose socket file was
taken from under its server in this boot (tmux says `No such file or directory`
for that too), or whose socket could not be asked, is **left live** and named,
with `SIGUSR1` as the way back to its server and `fleet shutdown --forget` as
your word that it is not running; under `--json` they are listed as `held`.
`reap` names a row whose socket another tmux server answers on the same way, with
`--server-down` or without it (see *The server was stopped outside the CLI*).

`--all` and `--project` are refused together (a usage error, exit 2, JSON under
`--json`). The claims of the rows it ends
or marks lost are released and counted (`claims_released`); a release the store
refused, or one the board could not be told about, is reported per row and the
command exits **1**, the same contract as `fleet stop` and `fleet shutdown`.

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

Sets (and clears) a named board signal, the project's `fleet-paused`, that the
manager's cycle respects: while it is on, the manager spawns nothing. Running
agents are not touched. Both take `--as SESSION`. The signal goes on the boards a
manager of the project reads now or would read next: the one its newest manager
was recorded at spawn to read, whether it still runs or not (a `restart` of it
hands its replacement that board), the project's checkout's board and the one a
spawn from here would give, and the boards its running agents read, manager or
not: a manager spawned the way its coders were reads where they do. A board only
a stopped coder, or a manager long since replaced, read is left alone. A pause signal of the project's that does not parse
on a board it would be set on is named, and nothing is set. `fleet resume` and `fleet shutdown` clear it wherever it is on under the
project's name, from `asq` under a hub or a plain shell alike, and only there, so a
resume puts no `off` in the prompts of agents on boards it was never paused on. On a board other
projects read too (the hub's under `AISQUARE_TEAM_HUB`, or the checkout's for a
project rooted in a sub-folder of its repository) it is named after the project,
`fleet-paused.<project id>`, and the manager's cycle names it: a pause, a resume
or a shutdown of one project leaves every other project's manager on that board
alone. A manager whose project could not be told (one launched by hand in a
folder nobody registered, under a hub) is told `fleet-paused.<project id>` to
fill in, with the project's ID, not its codename, beside the `<project>` its
fleet commands name.

### `ui`

```sh
aisquare ui
```

The app — what bare `asq` runs. Explicit so a script or alias can reach it, and
so `aisquare ui --help` exists. Without an interactive terminal it refuses with
the reason (`not_a_tty`) rather than starting a full-screen app into a pipe.

Press `R` for Remote control: the fleet on your phone through your own ngrok
tunnel, with the link, a QR code and the passphrase in one panel — see
[Phone control](remote.md).

---

## Accounts

*⚙ Settings ▸ Accounts* — or a click on the status line under *⚙ Settings* —
opens one page for the logins the fleet leans on: AISquare's, and each coding
agent's.

**AISquare** is on top. The card reads the session `aisquare login` stores and
shows who you are and when it expires. *Sign in* runs the same browser flow the
terminal runs — your one-time code and the link appear on the card, the browser
opens when one can reach you, and *Cancel* stops the wait — and *Sign out*
revokes the session exactly as `aisquare logout` does. A token supplied through
`AISQUARE_TOKEN` is shown as such and cannot be signed out from here.

**Claude Code** is below it: a table, one aligned row per account — ★ for the
default, the slot, its name, who is signed in, the plan, the five-hour and
seven-day windows (a bar and the percent **used**), a status with **Log in**
beside it when a login is due, then when each window resets. A value too long for its column ends in `…` (hover it for the whole
text); on a narrow page the table scrolls sideways as one, header included.
Slot **1** is the plain `claude` of your machine (whatever `CLAUDE_CONFIG_DIR`
was in the shell you started `asq` from, `~/.claude` otherwise). Every other
slot is a directory the CLI created under `~/.aisquare/claude-accounts/` and
launches by pointing `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_TMPDIR` at it — the
c1/c2/c3 shell aliases people write by hand, owned by the tool instead.

- **+ Add Claude account** makes the next free slot and opens Claude Code's own
  login in a pane on this page. Sign in there as you always do. The page
  watches the slot's directory; the moment Claude Code has written the login,
  the account is recorded, aisquare's hooks go into its `settings.json`, and the
  pane closes by itself. Close the pane without signing in, or press *Cancel
  sign-in*, and the empty slot is discarded.
- **Log in** on a row does the same for a slot that has no login, or whose
  token is missing, expired or rejected (slot 1 included) — the status beside
  it says which. Type `/login` if Claude Code does not prompt you; the pane
  waits for a *fresh* login, not the stale one on disk, then closes, and the
  row reads its usage again.
- **Remove** forgets a slot the CLI owns. Its directory is renamed beside
  itself as `<n>.removed-<stamp>` — the login, the settings and the transcripts
  kept, never listed again, deletable by hand — and the number is free for the
  next add. Slot 1 cannot be removed; sign out of it inside Claude Code.
- Usage comes from the endpoint Claude Code's own `/usage` reads, called with
  the token in the account's credentials file. The page asks at most once every
  five minutes per account. It shares one five-minute cache with the status
  line under *⚙ Settings*, `aisquare accounts usage` and `accounts list
  --usage`, so opening it again, or its minute tick, asks nothing while the
  readings are fresh.
  **↻ Refresh usage** above the table asks every account now. Beside it a dim
  line says how old the readings are: `last refreshed 3m ago`, from the oldest
  reading actually read, and `· last try failed 2m ago` when the endpoint did
  not answer for an account (`never` before the first). An account that needs a
  login counts in neither half; its own row says so. The endpoint is not a
  documented API. A try that fails with nothing to say about the login (the
  endpoint unreachable, an HTTP 5xx) keeps the account's figure on its row while
  that figure is under five minutes old, and the line says the try failed. With
  no such figure, the row's status says why (`usage: HTTP 503 …`). A token the
  endpoint refuses shows at once (`⚠ token rejected`), and an expired one says
  so (`⚠ token expired`). Nothing else on the page is affected. On macOS the
  token is read from the login Keychain, from the item Claude Code keeps for
  each account (plain and every managed slot), and from `.credentials.json` when
  there is no item. A locked Keychain, or one that refuses aisquare, is said on
  the row (`usage: the login Keychain is locked — unlock it …`) rather than
  offered a Log in. It is not asked again by itself (a locked Keychain's unlock
  dialog would come back every tick): opening the page, **↻ Refresh usage** or
  a Log in asks it again. A reset reads as a
  distance and a clock time — `resets in 3h 10m (18:00)`, `resets in 2d 4h (Tue
  02:00)` — the same string on the page and in `aisquare accounts usage`, so a
  weekly window six days out never looks like tonight.
- **Default**, **↑**/**↓** and **Disable** arrange the accounts. The row with
  the ★ is the **machine default**: what a launch runs on when nothing more
  specific says. The arrows set the **priority order** — the order the rows are
  listed in, and the order a headroom-based pick will try them in. *Disable*
  keeps a slot out of every automatic choice (its row says `disabled`) while
  `--account <slot>` still reaches it by hand; *Enable* puts it back.
- **Rename in place**: double-click an account's name (or press Enter or F2
  on its row) and type. Enter or clicking away saves, Esc cancels, an empty
  name goes back to `account N` / `plain claude`. The name is any text up to 64
  characters (`Work laptop`, emoji included), never only digits, never with an
  `@` and never with an invisible formatting character; it is
  what `aisquare accounts alias 2 "Work laptop"` sets, replaces `account 2`
  wherever the slot is named — the row, the board, `fleet ls`, the launch line
  — and is accepted, in any case, everywhere a slot number is (quote it:
  `--account "work laptop"`).
- Which account an agent runs on is decided in one order, for `fleet spawn`,
  `aisquare launch` and a manager spawning a coder alike: `--account` on the
  command line, then the role's binding (the **account** select beside each
  role in *⚙ Settings ▸ App settings*, or `aisquare team bind <role> --account`), then the
  project's default (`aisquare accounts default <slot> --project .`), then the
  machine default, which with one login only is whatever `claude` the shell
  already has — exactly what it ran on before any of this existed; with several,
  the one with room comes before it (*Headroom*, below). The
  agent's header and `fleet ls` show the slot it was resolved to.
- **Headroom.** By default — `[accounts] pick = "headroom"`, *launches pick* in
  App settings — the machine default gives way to the account with room
  (`aisquare config set accounts.pick default` keeps it): every enabled, signed-in account's
  usage is read through the usage cache, from a reading up to a minute old, so
  a burst of spawns asks the endpoint once. When only one account could be
  picked, nothing is read: it is the answer. A hand-over (below) and `doctor
  --live` ask the endpoint now. The first account in priority order under `switch_at`
  (85 % by default) is taken — or, when all are over it, the one with the most
  left. An account counts as full as the fuller of its two windows, so one
  that has spent its week is not taken for an empty five hours. Usage is the
  same best-effort endpoint as the bars, so an
  account that does not answer is skipped with a note and, when none answers,
  the machine default decides as before. Each reading is kept: the row's bar
  gains *≈ 40 min to the limit* once two readings of the same window exist.
- **A usage limit.** When an agent's turn ends on one (Claude Code's
  `StopFailure` hook, `You've hit your session limit · resets 12:30am`), its
  row turns **⏳ limited** with the reset time, a `limited` line goes on the
  board naming `aisquare fleet switch <label>`, and the manager is woken. Claude
  Code's own wait-and-continue at the reset is left running. `fleet switch`
  stops the agent as `fleet stop` would and starts it again under the same
  label and task, in its worktree as it stands (the branch it was on, any
  uncommitted work), on the account with the most headroom (`--to` names
  one), **resuming its session** from its transcript (`claude --resume
  <path>`) when that file is on disk — the resumed agent keeps its task claims
  and is told in one line to continue, and no exit is announced for it —
  `--fresh` (or a transcript that is not on disk) starts new with a hand-off
  prompt built from the board instead; that agent takes the claims over too,
  on its new session, and no exit is announced for it either. Should the
  replacement not start, the agent stays stopped, its task goes back to the
  pool, and the board says `agent_exited` after all, as *exited (0): its
  replacement did not start* (*exited (?)* when the stop had to kill it). An
  agent whose role, task (done or dropped) or binary would refuse the
  replacement is refused before it is stopped, and a task that closes while
  it is being stopped is left off the replacement.
  With `on_limit = "switch"`, the default (*on a usage limit* in App settings;
  `wait` leaves the agent parked for you or the manager), the fleet
  does this by itself when the limit lifts more than
  `wait_if_reset_within_minutes` away and another enabled, signed-in account
  is there to take it (with one account, `switch` is `wait`), in a worker
  detached from the agent's
  own hook; a hand-over that finds no headroom leaves the agent parked, its
  own wait intact, and says so on the board. One that goes ahead says on the
  board what it could not do (a first line it could not type, claims it could
  not move) in a note beside `switched`, and `switched` itself says whether
  the replacement's first line reached it. `doctor` lists parked agents; `doctor --live` warns when every account
  is over the line.
- **Folder trust.** Claude Code asks *"Is this a project you created or one you
  trust?"* the first time it starts in a folder, once **per account** — and the
  answer it highlights is *No, exit*. So a yes on account 2 left every agent on
  accounts 1, 3, 4… waiting at the question, and the first prompt typed into it
  ended the agent. Now a launch (`fleet spawn`, `restart`, `switch`, `aisquare
  launch`, `accounts run`) copies the folder's trust into the account it runs
  on when any other account has it, and says so on the receipt (*`<folder>` is
  trusted on account 2 — now on account 3 too*). Only that one entry of the
  account's `.claude.json` is written — under Claude Code's own lock, every
  other key byte for byte as it was — and never a trust nobody gave. Claude
  Code's rules decide what covers what: in a git checkout the yes is the
  repository's (its worktrees and sub-folders included), outside git a folder's
  is its sub-folders' too, and a folder above a repository does **not** cover
  it. `aisquare accounts trust [<dir>]` says yes on every account at once;
  `--sync` copies every folder any account trusts into all of them. On by
  default — *share folder trust* in App settings, `accounts.share_trust`. An
  agent that still meets the question (no account trusts the folder) reads
  **🔔 NEEDS YOU** *waiting for folder trust — approve it once, or `aisquare
  accounts trust <dir>`* in `fleet ls`, the sidebar and the Remote, and a spawn
  does not type its prompt into it: it waits for the answer, then leaves the
  prompt untyped with a note.

  Codex accounts share it the same way, among themselves: Codex keeps a yes as a
  `[projects."<folder>"]` table of `trust_level = "trusted"` in each slot's
  `config.toml`, so the table that covers the folder is appended to the slot a
  launch runs on, every other byte of the file kept, the file made if the slot
  has none. Codex's own rules decide (measured on 0.162.1): it asks only in a git
  checkout and records the repository's root; the folder's own entry decides
  first, then the root's, and nothing above them. A table that says
  `untrusted` is that account's no and is never overridden; `accounts trust`
  reports it and leaves it to you. A Claude account's yes is not a Codex
  account's, nor the reverse: each agent asks its own question.
- **One settings.json.** Opt-in — *share settings.json* in App settings,
  `accounts.share_settings` — every managed account's `settings.json`
  (permissions, hooks, model, status line) becomes a link to the plain claude's,
  so a change made on any account is made on all; the account's own file is
  kept beside it as `settings.json.own-<stamp>`. Claude Code writes through the
  link and keeps it. The logins stay apart: `.claude.json` and
  `.credentials.json` are never shared, and a `settings.json` that sets what
  decides a login (`apiKeyHelper`, `forceLoginMethod`, `forceLoginOrgUUID`, an
  `env` API key, OAuth token or cloud provider, `CLAUDE_CONFIG_DIR`) is neither
  shared nor replaced — the command says which setting stopped it. `aisquare
  accounts settings --sync` links them now, `--unshare` gives each its own copy
  back.

Nothing on this page writes into Claude Code's login: the email and plan
are read from what Claude Code recorded, and a token is never refreshed by the
CLI. Claude Code refreshes it on the account's next session. The two things the
accounts share (above) are the only writes into an account's directory.

**Codex** accounts are slots on the same ladder — one number names one login
across both agents — and everything above works for them, with four
differences ([Codex accounts](coding-agents.md#codex-accounts) has the
details):

- Slot **0** is the plain `codex` of your shell (`CODEX_HOME`, else
  `~/.codex`); a managed one lives under `~/.aisquare/codex-accounts/` and
  launches with `CODEX_HOME` pointing at it. The page shows them in a **Codex**
  section of their own, below Claude Code's, once Codex is installed or has a
  slot. **+ Add Codex account** (or `accounts add --agent codex`) runs `codex
  login` in a fresh slot in the same watched pane, and **Sign in** on a row
  runs it there; the account is recorded and its hooks connected the moment
  `auth.json` lands.
- **Usage is read offline**, from the `rate_limits` the account's own sessions
  recorded in their rollouts — never over the network — so a Codex bar is as
  fresh as that account's last turn and says how old it is.
- **Defaults are per agent**, and every rung of the launch order answers with
  an account of the role's agent: a Codex role gets a Codex slot, and naming a
  Claude slot for it (or the reverse) is refused, naming both.
- **A usage limit** is read off the end of the session's rollout when its turn
  stops (Codex has no `StopFailure`), and the row turns **⏳ limited** the same
  way. Codex 0.162.1 fires no `Stop` for a turn that ends on its limit (or on any
  other error), so every read of the fleet (`fleet ls`, the app, `fleet tell`)
  reads the end of a working Codex row's rollout too: a failed turn is recorded
  there once, **⏳ limited** for a limit, waiting with a `turn_failed` line for
  any other error. A Codex agent's `fleet switch`, by hand or by `on_limit = "switch"`, is
  **fresh** onto another account: its thread lives under the old `CODEX_HOME` and
  cannot be resumed from another, so the replacement starts with the hand-off
  prompt and takes the claims over at its first start. A target on the home the
  thread ran under resumes it. At its limit, a Codex whose operator keeps the
  *Switch to gpt-6-luna?* dialog on (`[notice] hide_rate_limit_model_nudge =
  false`) shows that dialog under the error. The automatic hand-over answers it
  with Esc (keep the current model) before its `/exit`, so the session it leaves
  ends, and `doctor` stops listing it as parked.
- **Restart resumes the thread.** `fleet restart` (and the agent view's Restart)
  relaunches a Codex agent as `codex <options> resume <thread id> -- <continue
  line>`, the thread its first hook bound, on the same account; a row whose agent
  never reached a hook, or whose session has no thread id, starts fresh with the
  hand-off prompt and the receipt says which.

Everything the page does is a command, and every reporting one takes `--json`:

```sh
aisquare accounts list                 # slots, who is signed in, plan, hooks
aisquare accounts list --usage         # …plus the two windows, percent USED (read at most every 5 min)
aisquare accounts add                  # a fresh slot; Claude Code opens for the sign-in, then exits
aisquare accounts add --agent codex    # a fresh Codex slot; `codex login` runs in it
aisquare accounts run 2                # a plain Claude Code session on account 2
aisquare accounts run 2 --model opus   # arguments after the slot go to claude
aisquare accounts run 3 login          # on a Codex slot: codex (here its login) with CODEX_HOME=slot
aisquare accounts usage                # the windows, per signed-in account; --refresh reads now
aisquare accounts remove 2             # the directory is kept as 2.removed-<stamp>
aisquare launch coder --account 2      # a board role on account 2
aisquare fleet spawn coder --account 2 # a fleet agent on account 2
aisquare fleet spawn coder --agent codex --account 3   # a Codex agent on Codex slot 3
aisquare accounts default 2            # the machine default — what a launch runs on when nothing else says
aisquare accounts default 3 --project . # this project's default (codename, name, id prefix, or . for here)
aisquare accounts default 1 --role coder # coders run on the plain claude (= team bind coder --account 1)
aisquare accounts default              # the three levels, as they stand
aisquare accounts default --clear --agent codex   # defaults are per agent
aisquare accounts alias 2 work         # a name: --account work, [work] on the board
aisquare accounts order work 3         # the priority order; the rest follow as they were
aisquare accounts move 3 top           # up · down · top · bottom
aisquare accounts disable 3            # out of automatic selection; enable puts it back
aisquare config set accounts.pick default      # spawns stay on the machine default (headroom is the default)
aisquare config set accounts.on_limit wait     # an agent that hits its limit stays parked (switch is the default)
aisquare accounts trust                # trust this folder on every account (its repository, in a checkout)
aisquare accounts trust --sync         # every folder an account trusts, on all of its agent's accounts
aisquare config set accounts.share_trust false # launches stop copying a folder's trust (on by default)
aisquare accounts settings             # what each account's settings.json is: shared, its own, none
aisquare accounts settings --sync      # link every managed account's settings.json to the plain claude's
aisquare accounts settings --unshare   # each linked account gets its own copy back
aisquare config set accounts.share_settings true # keep them linked at every launch (off by default)
aisquare fleet switch coder-auth       # move one now: resumes its session on the account with room
aisquare fleet restart manager         # an exited (or stuck) agent, back under its label, session resumed
                                       # …launched as it was first launched: the row records the
                                       # binary, permission mode, arguments and account (#144)
aisquare fleet restart coder-auth --permission-mode acceptEdits   # …another mode; later restarts keep it
aisquare fleet restart coder-auth --permission-mode workspace:ask # …or one for any agent (below)
aisquare fleet switch coder-auth --to personal --fresh   # a named account; a new session + hand-off
```

`add` and `run` hand the terminal to the agent, so they have no `--json`
form and `add` refuses outside an interactive terminal. `aisquare doctor` gains a
`claude-accounts` line naming any slot that still needs a sign-in, a
`claude-account-default` line when the default is not signed in or is disabled,
and a `claude-account-bindings` line when a role or project names an account the
machine no longer has. Accounts laid
out some other way — a wrapper, a proxy, a directory of your own — still bind
to a role as a launch profile (`aisquare team bind coder1 --env …`, see
[Several accounts, one team](orchestration.md#several-accounts-one-team)).

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
| reviewer | `auto` + `--disallowedTools=Edit Write NotebookEdit` | the flag is the role's `extra_args`: it keeps Bash, which its whole cycle runs on and `--restricted` would take, and loses the tools that edit files, so a prompt injected through the PR it reads cannot edit the tree with them. Its shell is not read-only: `sed -i`, `git push` or `gh pr merge` injected through a PR meets only its briefing and `auto` mode's check of each action, and no allowlist of commands would hold (its inline comments are `gh api` calls, as a merge is, and verifying a finding runs the PR's code). Where that matters, run it with a `gh` token that cannot write the repository's contents, which can comment but not push or merge, and protect the branch: [A reviewer that cannot push](#a-reviewer-that-cannot-push). `extra_args = []` gives it every tool. A config saved by 0.6.0 or 0.7.0 holds that release's default, `extra_args = ["--restricted"]`; exactly that list loads as today's default, once: the next save writes the file's `config_version` at its top, and from then on `--restricted` set there is yours and kept. A reviewer those releases started recorded the flag in its launch spec, and `fleet restart`, `fleet switch` and Restart replay today's arguments in its place, with `--disallowedTools=Edit Write NotebookEdit` beside them where they do not deny those tools: the spec cannot tell that release's default from a `--restricted` its spawn was given, so the replay errs toward the reviewer without them, and a fresh spawn gets whatever today's arguments give. One recorded since is replayed as recorded. Any other list stays as written |
| ui-tester | `auto` + `--chrome` | `--chrome` comes from the role, not this table: `aisquare launch ui-tester` adds it for the default `claude` binary wherever the role starts, and `--no-chrome` in `extra_args` or on the command turns it off. Its "read-only" is a briefing instruction, NOT construction — the row above says so, and the sentence in the coder/tester cell applies here too: nothing here writes or passes an allowed-tools list, so a PreToolUse allowlist (plan §3.6, not in this checkout) is what would enforce it |

The mode is passed straight through to `claude` and nothing here reads its
answer, so where `auto` is unavailable to the account the refusal appears in
that agent's own pane and the spawn does not fall back: pick another mode
yourself, per spawn (`--permission-mode acceptEdits`) or for the role
(`aisquare config set fleet.roles.coder.permission_mode acceptEdits`).
`bypassPermissions` is available and is never a default.

### One permission for every agent

`fleet.roles.<role>.permission` holds the role's permission once, for whichever
agent it runs on: `WRITE:APPROVALS`, where WRITE is `read_only`, `workspace` (the
project and its worktree) or `full`, and APPROVALS is `ask` (the human), `auto`
(the agent's own reviewer decides) or `never` (what is not allowed fails). Each
agent's adapter maps it to its own flags; the same value works as `fleet spawn
--permission-mode` and `fleet restart --permission-mode`, and *⚙ Settings ▸ App
settings* offers it per role, with what each agent would be passed under it. Set, it wins
over the agent-specific settings (`permission_mode` for Claude Code; `sandbox`,
`approval_policy`, `approve_for_me` for Codex); unset — the default, so an
existing config means what it always meant — those apply. A native flag passed for
one spawn (`--sandbox`, `--approval`, or the agent's own in its arguments) still
wins, as a per-spawn flag always does, and the restart replays the value the agent
was launched with.

| Permission | Claude Code | Codex |
| --- | --- | --- |
| `workspace:auto` | `--permission-mode auto` | `--approve-for-me`, store profile over `:workspace` |
| `read_only:auto` | `--permission-mode auto --disallowedTools=Edit Write NotebookEdit` | `--ask-for-approval on-request -c approvals_reviewer="auto_review"`, store profile over `:read-only` |
| `workspace:ask` | `--permission-mode manual` | `--ask-for-approval on-request -c approvals_reviewer="user"`, store profile over `:workspace` |
| `workspace:never` | `--permission-mode dontAsk` | `--ask-for-approval never`, store profile over `:workspace` |
| `read_only:ask` | `--permission-mode manual --disallowedTools=Edit Write NotebookEdit` | `--ask-for-approval on-request -c approvals_reviewer="user"`, store profile over `:read-only` |
| `read_only:never` | `--permission-mode dontAsk --disallowedTools=Edit Write NotebookEdit` | `--ask-for-approval never`, store profile over `:read-only` |
| `full:ask` | `--permission-mode manual` | `--ask-for-approval on-request -c approvals_reviewer="user" --sandbox danger-full-access` |
| `full:never` | `--permission-mode bypassPermissions` | `--ask-for-approval never --sandbox danger-full-access` |
| `full:auto` | `--permission-mode auto` | refused at spawn: Codex's reviewer runs only in workspace-write |

The built-in roles are `workspace:auto` and, for the reviewer, `read_only:auto`:
exactly what each agent gets with nothing set. Claude Code has no sandbox,
so its read-only is the reviewer's denial of the tools that edit files, given once
even where the role's own arguments carry it too.

Codex runs its tools in a sandbox. The **store profile** is the fleet's own Codex
permission profile: Codex's built-in `:workspace` or `:read-only`, with the aisquare
home read-only within it, all of it (the board's database, the launch records, the
census, the config, the account homes, credentials and keys, the fleet's tmux conf, the
agent registry, the model probe's cache), and one path writable: the launch's broker
inbox. Read-only too: what the tops of the launch's trees hold that a launch outside any
sandbox reads (`.claude/`, `.mcp.json`, `.codex/`, `.agents/`, `.aisquare`, `.git`),
every worktree but the agent's own (and that one too for a `read-only` seat), and the
directories tmux keeps its sockets in (the socket itself where that directory is a shared
one, `/tmp`), so a Codex tester in the project's directory cannot write the manager's
Claude Code settings, nor move the fleet's socket out of `/tmp`. A native `--sandbox`
the operator picks replaces the profile, and these paths with it. Every `aisquare` board call the agent makes there
goes, unchanged, through that broker, which its launch runs outside the sandbox: it runs
only a whitelist of board and memory commands, only as the agent's own board sessions,
only in its own tree, on its own board's tasks and in its own project's memory pool (for
reads too: `context show` and `log` included), and never a fleet operation (`spawn`, `stop`, `restart`, `switch`, `tell`, `shutdown` …), a
launch, a hook, the config or an account. So nothing that a restart, a switch, a
hand-over, `tell`, `stop`, `shutdown`, the manager's nudge, `reap` or a spawn reads can
have been written from inside a sandbox; every git aisquare runs runs no hook and no
fsmonitor of the repository's, and gbrain starts in its brain's home. The boundary is the
tree the agent may write: what the user's own tools run from it (git hooks in the tree
and git config the repository includes from it, a project venv, a subdirectory's
settings, what `aisquare project onboard` packs it with, `CLAUDE.md`) is the agent's to
write, as on Claude Code, and so is the window's `PATH`, where Codex itself and aisquare's
hooks find `git`: a launch whose `PATH` names a directory its sandbox may write (an empty
entry is the directory a process runs in) is warned; the restart and spawn checks read
the `PATH` the window will have: the role binding's where it sets one, else the fleet
server's.
Whatever its sandbox, a launch is refused, by `fleet spawn` before any window, by a
restart, switch or hand-over before it stops the agent, and by `aisquare launch`, where
that sandbox may write an aisquare that runs outside it, the launcher's own install or
what the agent's hooks run (their `hooks.json`'s program, or what the plugin's launcher
finds as its own `_find` does, where its hooks run), with its environment, as read from
Codex's system, user and managed config, its `-p` profile and its
arguments (the
workspace, `-C`, `--add-dir`, `writable_roots`, `/tmp`; none of them for a `read-only`
seat): install `aisquare-cli` outside the project (`uv tool install aisquare-cli`) and
connect the hooks with it. The agent keeps `PYTHONPATH` and `PYTHONHOME` as you set them,
for its own tools; every Python aisquare starts for it ignores them (`-E`): the window's
`aisquare launch`, its hooks as `agents connect codex` writes them, the plugin's launcher,
its broker and what the broker runs, the hand-over. A fleet Codex window
starts with `check_for_update_on_startup=false` unless your config sets it: an `Update
available` dialog in a starting pane takes an Enter as `npm install -g @openai/codex`
([the trust boundary](coding-agents.md#the-sandbox-and-the-aisquare-store)). The launch records stay as defence
in depth: `fleet spawn` writes each agent's launch record in `launch-seals/` (binary,
arguments, permission, role, agent, directory, account, and the tmux socket and pane
its window is in), the agent's own hook adds its session (transcript, thread, home), and
a replay and `fleet tell`'s queue read that record and nothing else. What `tell`, `stop`
(restart, switch, the hand-over, `shutdown`) and the manager's nudge type into or kill
is the pane the record places, by the record's agent's rules, and `shutdown` takes down
the sessions the records name; a failed turn's limit is read from the record's
transcript. One resolver (`fleet.resolve_place`) answers every such question, and
`tests/test_fleet_placement.py` fails on a tmux call that does not go through it. A row
rewritten to name another pane is told nothing and stopped where its record places it;
a row without one has its window killed by `stop` with nothing typed, only in a session
a record of its project names, and its replay, like a project whose directory in the
store is not the record's, is refused with the reason. So a Codex agent's board calls
succeed on their first, sandboxed attempt, a reviewer's verdict included, while its
worktree stays read-only
([the sandbox and the store](coding-agents.md#the-sandbox-and-the-aisquare-store)).
`ask` is the human's to answer on Codex as on Claude Code: the reviewer is pinned on
every spawn and restart, as a resumed thread keeps its own otherwise, so `fleet
restart <label> --permission-mode workspace:ask` turns an agent's `--approve-for-me`
into Codex's approval prompts in its pane. A pick in Codex's own `/permissions`
replaces the store profile until the next restart.

**`auto` behind the explainability proxy.** The classifier auto mode relies on
makes a separate, non-streaming request the proxy currently cuts short once the
session is large — and a fleet agent's very first request is already the whole
system prompt, tool schemas and skills (~140k tokens on a machine with several
MCP connectors), so every tool call is refused with *"… is temporarily
unavailable (server error), so auto mode cannot determine the safety of Bash"*
while the chat itself keeps working. `aisquare doctor` says so (`explainability
auto-mode`), the spawn receipt repeats it, and a session launched through the
proxy that has been refused three times is put in 🔔 attention with one
`auto_mode_blocked` board line (one that a restart or a switch is taking down
leaves both to its replacement, which is judged on the refusals it adds). Until
the proxy fix
([AISquare-Explainability-SDK#1144](https://github.com/AISquare-Studio/AISquare-Explainability-SDK/issues/1144)):
a non-classifier mode for the roles (`acceptEdits`, as above — for a role your
own `[fleet.roles]` leaves out, `config set` answers "unknown config key" and
the step is a `[fleet.roles.<role>]` table in the file; the doctor line names
the one your config takes) and, for an agent already running, `aisquare fleet
restart <label> --permission-mode acceptEdits` — a restart replays the mode the
agent was launched with (#144), so the role's setting reaches only the agents
spawned after it; a lighter config dir; or tracing off. Details and
the measurements:
[`connecting-your-agents-to-explainability.md`](connecting-your-agents-to-explainability.md#auto-mode-refuses-every-tool-call-behind-the-proxy).

**Every default is a default.** Everything this guide calls one — permission
mode, worktree-per-role, the escape key, the agent cap, the tmux socket, the
native-agent-teams switch, codenames, labels — is yours to change, under one
precedence rule:

> per-spawn flag  >  `[fleet]` config  >  built-in default

**No `[fleet]` setting is read from the environment**: the fleet reads this
section from the config file alone. (`AISQUARE_FLEET_AGENT` exists, but it is
not a setting — `fleet spawn` sets it on each window to name the agent's row,
and the session-start hook reads it to brief the agent on the task it was
spawned for; see *What the agent is told* under `fleet spawn`.) The environment
layer is real one level down — the model, effort
and binary a launch resolves (`AISQUARE_MODEL_<ROLE>` and friends, below) —
which is the harness's rule, not this one.

Three places to change one:

- **per spawn** — `fleet spawn … --agent claude-code --permission-mode acceptEdits --no-worktree --bin claude2`;
- **in config** — `aisquare config set fleet.max_agents_per_project 6`, or
  `aisquare config set fleet.roles.coder.permission workspace:ask` (any agent), or
  `aisquare config set fleet.roles.coder.permission_mode acceptEdits` (Claude
  Code's own words), or edit the file directly;
- **in the UI** — *⚙ Settings ▸ App settings* writes the same config, and its
  *Reset all settings to default* is `config reset`. Its *Save* writes only the
  settings changed on the page, so one changed in a shell while it was open is
  kept. A project's own *Settings* tab holds only that project's nickname.

**Your file holds your changes and nothing else.** The defaults are built in, and
`~/.aisquare/config.toml` is your layer over them: a save writes only the keys
that differ from the defaults, so a key you never set follows the built-in default
even when a release changes it. One exception: each `[fleet.roles.<role>]`
table is written whole, so a release that reads the file without the defaults
under it (0.8.0 and earlier, sharing `~/.aisquare`) starts each role as this
one does. A config 0.8.0 saved holds every setting; it loads
with the values that differ from 0.8.0's defaults kept as yours and the rest
following today's, and the next save writes it down to yours. A file 0.8.0 saves
whole again after a move back is read the same way. Moving back with
`aisquare upgrade --version 0.8.0` names what that release reads differently:
the settings the file leaves to today's defaults where 0.8.0's differ
(`accounts.pick`, `accounts.on_limit`).

```sh
aisquare config list --overrides   # what you changed, and nothing else
aisquare config reset              # every setting back to its default; asks first, --yes off a terminal
```

`config reset` moves the file aside to `config.toml.bak-<stamp>` beside it, so
moving it back undoes the reset; through a symlink, the file it points at is
copied there and emptied, and the link stays. With no file, or one that holds
no setting, it says `nothing to reset` and moves nothing.

The whole `[fleet]` section at its defaults — what you get with no `[fleet]` in
your file at all:

```toml
[fleet]
tmux_socket = "asq"                       # the private tmux server: tmux -L asq
escape_key = "f12"                        # hands focus from an agent pane back to the sidebar
max_agents_per_project = 4                # the manager (and you) cannot exceed this
worktree_dir = ".aisquare-worktrees"      # relative to the repo root; kept out of git via .git/info/exclude
disable_native_agent_teams = true         # exports CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=0 into fleet launches
max_continuations_per_hour = 30           # cap on the manager's Stop-hook continuations

[fleet.roles.manager]
# permission = "workspace:auto"           # one value for every agent (WRITE:APPROVALS); unset = the agent's own below
permission_mode = "auto"                  # any Claude Code mode; "" = pass no flag
worktree = false
extra_args = []
agent_args = {}

[fleet.roles.coder]
permission_mode = "auto"
worktree = true                           # one worktree per coder: parallel coders never share a tree
extra_args = []
agent_args = {}

[fleet.roles.tester]                      # `runner` is accepted as an alias
permission_mode = "auto"
worktree = false                          # runs in the repo root; point it at the branch yourself
extra_args = []
agent_args = {}

[fleet.roles.reviewer]
permission_mode = "auto"
worktree = true
extra_args = ["--disallowedTools=Edit Write NotebookEdit"]  # a shell, no editing tools
agent_args = {}

[fleet.roles.ui-tester]
permission_mode = "auto"
worktree = false                          # tests the running app, not a tree of its own
extra_args = []                           # --chrome is the role's own default; put "--no-chrome" here to drop it
agent_args = {}

[fleet.roles.validator]
permission_mode = "auto"
worktree = false
extra_args = []
agent_args = {}
```

A role or a key your file leaves out takes its default above; a role the table
above does not name gets `auto`, no worktree and no extra args. A config file that will not parse costs you the customisation and
never the fleet: the defaults apply and nothing refuses.

**Model, effort and binary per role stay where they already live** — one home
per concept: `aisquare team harness` (the ladder and the effort offsets),
`aisquare team bind <role> --agent claude-code --bin … --env … --arg …` (launch profiles),
`AISQUARE_MODEL_<ROLE>`, `AISQUARE_EFFORT_<ROLE>` and `AISQUARE_BIN_<ROLE>` in
the environment. A fleet launch is an `aisquare launch <role>` inside a tmux
window, so all of it applies unchanged, as does the Explainability wiring.
For an arbitrary wrapper binary, declare its family with
`aisquare team bind ROLE --agent claude-code --bin PATH` (or `--agent codex`),
or pass `--agent` on that launch. User/project defaults and an operator-exported
`AISQUARE_CODING_AGENT` do not identify arbitrary wrappers. Existing bin-only
configurations without those defaults retain Claude compatibility, including
inside AISquare-launched panes: a parent's family does not reclassify another
wrapper. This also applies to wrappers selected through `AISQUARE_AGENT_BIN`
and `AISQUARE_BIN_<ROLE>`.

Legacy `[fleet.roles.ROLE] extra_args` belong to Claude Code. Agent-specific
arguments name their owner, for example:

```toml
[fleet.roles.reviewer]
agent_args = { codex = ["--no-alt-screen"], claude-code = ["--verbose"] }
```

The selected adapter combines only its own arguments with the role's native
permission flags. Explicit arguments on a spawn still go to that launch.

**Native agent teams are off in fleet launches.** Claude Code's own
experimental *agent teams* (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`) let a
session spawn teammate sessions outside our tmux server — invisible to the
sidebar, the board and the wake-up rules — so by default the fleet exports the
variable as `0` into the sessions *it* starts and briefs the manager that
`fleet spawn` is the one way to get help. Your own `claude` sessions keep
whatever they had. `disable_native_agent_teams = false` turns this off.

### A reviewer that cannot push

The reviewer keeps its shell, so its briefing and `auto` mode's check of each
action are all that stand between a prompt injected through the PR it reads and
a `git push` or `gh pr merge`. Credentials are what can refuse them. Give the
reviewer a GitHub token that reads the code and reviews pull requests, and
cannot write the repository's contents:

1. Create a fine-grained personal access token (GitHub → Settings → Developer
   settings → Fine-grained tokens) for the repositories the fleet reviews, with
   **Contents: Read-only** and **Pull requests: Read and write** (its review and
   inline comments). Metadata is read-only and always on. If its cycle reads CI,
   add **Commit statuses** and **Actions**, both read-only. Leave out everything
   else, above all Contents: Read and write, Administration and Workflows.
2. Keep it in a file only you can read:

   ```sh
   mkdir -p ~/.config/aisquare
   (umask 077 && cat > ~/.config/aisquare/reviewer-gh-token)   # paste, then Ctrl-D
   ```

3. Bind it to the role:

   ```sh
   aisquare team bind reviewer \
     --env-file GH_TOKEN='~/.config/aisquare/reviewer-gh-token' \
     --env-file GITHUB_TOKEN='~/.config/aisquare/reviewer-gh-token'
   ```

   Every reviewer launch reads the file: `fleet spawn`, `fleet restart`,
   `fleet switch`, the UI's Restart and `aisquare launch reviewer`. The token
   goes into that session's environment alone. `gh` prefers `GH_TOKEN` to the
   login it keeps, and `GITHUB_TOKEN` stands in for one your shell exports. The
   token is never written to config.toml and never printed; the launch banner
   names the two keys. When the file is missing or empty, the reviewer is
   refused rather than started on your own credentials, and `fleet spawn` and
   `fleet restart` refuse before a window opens or a running reviewer is
   stopped. This is opt-in: without it, the reviewer runs on whatever `gh` and
   `git` find, as before.
4. **Protect the branch.** The token governs `gh`, and HTTPS pushes that go
   through `gh`'s credential helper (`gh auth setup-git`). A push over SSH, or
   through another credential helper, uses that credential instead. So protect
   `main` and your release branches on GitHub: require a pull request with an
   approving review before merging, block force pushes and deletions, and do not
   let administrators bypass the rule. Then no push or merge from the reviewer's
   shell lands on a protected branch, whatever credential it finds.

---

## Naming

Measured before it was designed: a non-git directory is its own project (a
parent holding several repos is one project); two checkouts can share a
directory name; and tmux will *create* a session named `a.b` or `a:b` but
cannot *target* it, because `.` and `:` are its separators. So the fleet has a
display name for people and a slug-safe token for machines.

**Project display name.** Its nickname when you have given it one, else the
directory's basename — `aisquare-cli` — for git repos and plain directories
alike. Two projects sharing a basename also show their parent as a dim subtitle
(`~/work/api`, `~/oss/api`).

**Nickname.** Free text on one line, up to 255 characters — `API: auth rewrite
🚀` — like naming a browser tab for what you are doing there. Emoji are fine,
joined ones (👩‍💻, 🏳️‍🌈) included; an invisible formatting character (a bidi
control, a zero-width space) is refused, as it would let one name pass for
another, and so are bytes the terminal could not decode. Double-click a
card's title (or press `F2` on it) and type: Enter or a click away saves, Esc
cancels, and an empty name brings the folder's back. The project's *Settings*
tab and `aisquare project rename "API: auth rewrite"` (`--clear` drops it) set
the same name, which the sidebar, `project list` and `fleet ls` all show.
`--project` takes it too, when no id prefix, folder name or codename matches
first. A card's **⋯** (shown on hover or on the selected card; `.` or a
right-click too) opens its menu: *Rename*, *Pin*, *Open settings* and *Remove
from fleet…*, which asks first, then forgets the project as `aisquare project
forget` does. Its folder and history are kept, and `aisquare project onboard
<root>` brings it back. A project with live agents is refused, naming them and
`fleet shutdown`, and so is a hub whose board another project's agents read,
live or stopped (a restart would start them under it again): a restart or a
switch of one would add it back.

**Codename.** Every project that enters the fleet gets an `adjective-animal`
codename — `amber-otter` — matching `^[a-z]{3,7}-[a-z]{3,7}$`, from two
hand-curated, family-friendly word lists (96 by 96, so 9,216 pairs). It is
**deterministic from the project id** — itself a stable hash of the resolved
root — walking to the next pair on a collision, so the same checkout gets the
same name on every machine and after a `context.db` loss, and two projects on
one machine never share one. It is assigned lazily, the first time the project
touches the fleet — never at `init`, so a memory-only user never sees one. The
codename is primary only where a stable token is needed: the tmux session, the
attach hint, branch names, `fleet` command arguments. A card does not show it;
the project's *Settings* tab names it (`fleet id: amber-otter — tmux session
asq-amber-otter, branches fleet/amber-otter/…`). `fleet rename` changes it; `aisquare project switch
amber-otter` and `--project amber-otter` both resolve it, and the "matches
several projects" error lists codenames, because basenames are what collide.

**Groups, pins and your own order.** The navigator is a flat list until you
arrange it: **drag** a project card onto a group header to put it in that
group, between cards to reorder it, or onto the empty space below the list to
take it out of its group; drag a group header to reorder the groups. Released
anywhere else — the main pane, a pinned card — a drag changes nothing. A pinned
card or group does not drag at all: its place is the pin order, which `p` sets
(the group picker still regroups a pinned project). Without a mouse: with the
sidebar focused, `shift+↑` / `shift+↓` move the project (or group) under the
cursor one step, `g` opens the group picker (an existing group, *New group…*,
or *Ungroup*), `p` pins or unpins, `space` folds a group (or a card), and `u`
undoes the last gesture — a toast says what was undone. `m` marks the card
under the cursor, and `shift+click` marks the card clicked where the terminal
passes it on (most keep Shift+click for their own text selection); `shift+g`
then moves every marked card, a drag of a marked card moves the unpinned ones,
and `Esc` clears the marks. Pinned projects and groups sit in a **📌 Pinned**
section at the top, in pin order; a group header carries the roll-up of its
members' agents, so a folded group still tells you something is running. A
group shares **nothing**:
context, prompts, snapshots, boards and explainability settings stay per
project, and deleting a group never deletes a project. Forgetting a project
takes it out of its group and off the pin: added again, it comes back loose, at
the end. The same layer from a shell, with the same store state:

```sh
aisquare project group create frontend web docs     # a group, with its first members
aisquare project group add frontend cli
aisquare project group remove cli
aisquare project group rename frontend ui
aisquare project group move tools --before ui
aisquare project group delete ui                    # members go back to the top level
aisquare project pin api
aisquare project unpin api
aisquare project move docs --to top --position 0    # or --to <group> [--before P | --after P]
aisquare project list --group ui                    # or --pinned; --json carries group, position, pinned
aisquare project onboard ~/work/new --group tools   # created if new
```

**Only what you add is a project.** A directory becomes a project when it is
added **on purpose**: `aisquare init`, `project onboard`, `project link`,
`project switch`, Welcome or the sidebar's `+`, `team on`, a fleet spawn (not
the automatic usage-limit hand-over), or a fact written by hand with
`context add --project`. A manager asked to add one runs the same commands.
Anywhere else a hooked Claude Code session records nothing — no project row, no
prompt, no turn row, no board — and a board write there (`note`, `task add`,
`task next --claim`, `team signal`) is refused with the command that adds it.
That is `[projects] capture = false`, the default since 0.8.1.

`aisquare config set projects.capture true` brings back what 0.8.0 did (#139):
every directory a hooked session runs in is then *captured* — registered so its
prompt history and injected memory work, but not listed — and `a` in the
sidebar shows those too, marked *captured*. With capture off, `a` is no key and
the sidebar lists no captured row, even with the toggle saved on.

Rows captured before you upgraded stay in the store, unlisted, until you drop
them: `project list --all` lists them, `doctor` counts them, and
`project prune --captured-only --older-than 0` drops every one with no context
entries (without `--older-than`, only those untouched for 30 days).
`project forget` sticks: with capture off nothing brings the row back, and with
it on the next prompt there captures it again, silently, instead of putting it
back on the list.

**Registrations you no longer want** — a deleted checkout, a throwaway worktree —
go with `aisquare project forget <name|path>`; `aisquare project prune` sweeps
roots that are gone from disk and worktrees of a repository that is itself
registered, showing the plan and asking first. The navigator only loads what is
registered, so this is also how a slow start is cured (#83).

**tmux.** Session `asq-<codename>`, made by the project's first spawn and
joined by every later one by tmux's own id for it (`$3`), which the project's
session list beside the launch records holds however long its last agent has been
gone; agents spawned before this version are joined in the session tmux says holds
theirs. A session found only under that name is not joined (a swap of two projects'
codenames in the store would put an agent into the other's), and the spawn makes
`asq-<codename>-2` instead. Window name = agent label; panes are addressed by
`%id`, sessions by `$id`, never by name, so renames are harmless: a rename tmux was
not told of strands nothing, and `fleet rename` gives each of a project's sessions a
name of its own.

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

**One rule for a worktree and its branch: the branch outlives the tree.** A
tree is disk, and goes when nothing in it is found only there (`fleet reap`);
its `fleet/…` branch is the agent's work, and goes only once the default branch
has that work (its tip, or every change it makes after a squash or rebase
merge of a pushed branch). So whatever happened to the tree, an agent that carries on —
Restart, `fleet restart`, `fleet switch`, Remote's restart — does so on its own
branch with every commit on it, and a spawn for the same task asks for the same
branch and checks it out:

| what happened | restart or switch | a new spawn for the task |
|---|---|---|
| tree and branch there | kept as it stands | reuses the tree, on the task's branch |
| tree deleted by hand, branch there | the tree made again on the branch it was on, at the same path | checks the branch out |
| pushed (to any remote branch) and reaped: tree removed, branch kept | the tree made again on that branch | checks the branch out |
| merged and reaped: tree removed, branch deleted | refused: the branch is gone | a new branch from HEAD — which holds the work when HEAD is the default branch |
| tree and branch both deleted by hand | refused: the branch is gone too | a new branch from HEAD |

The branch a restart makes the tree again on is the one the agent's row records
(`branch` in `fleet ls --json`): the tree's branch as git said it at spawn, at
`fleet stop`, and at `fleet reap` just before the tree went, on the newest row at
the tree's path; a detached HEAD (mid-rebase, say) leaves the branch recorded before.
So it is the branch the agent was last on, whatever its name: one that ran `git
checkout -b fix/auth` is restarted on `fix/auth`, not on the `fleet/…` branch it was
spawned on. A
`git worktree prune`, which every spawn runs and every role's cleanup asks for,
loses nothing, and neither does a later agent under the same label and path.
A row from before 0.8.1 records no branch, so its gone tree is refused. A refusal
says what it found, before anything is stopped and without writing anything, and
names the way on: spawn a new agent for the task. A branch another tree has
checked out by then is refused too, naming that tree.

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
 │ left : Fleet ▸ projects ▸ agents · Settings│ capture  │ session asq-amber-otter                        │
 │ right: onboard · manager · agent · board · │◄─────────│  ├─ window manager    : aisquare launch manager│
 │        doctor · AISquare · settings        │ send-keys│  ├─ window coder-auth : aisquare launch coder …│
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

**Each agent is asked of its own server.** A spawn records the socket its
server says it listens on, and every fleet command — `fleet ls` and the UI,
`tell`, `stop`, `restart`, `switch`, `reap`, `shutdown`, and the doctor — asks
the agent's server there (`tmux -S <path>`) when that is not where `tmux -L
asq` looks from the asking shell. Under another `TMUX_TMPDIR`, `-L` reaches
another server on the same name, or none, and every server numbers its panes
from `%0`, so its panes would answer for the agents' ids: a dead one there read
as a running agent's exit and ended its row. A row from before 0.8.0 recorded
no socket and is asked by name. The Manager tab's attach line names the
server the same way.

**Under a hub: one board, many fleet projects.** With `AISQUARE_TEAM_HUB` set,
every session is on the hub's board (`fleet spawn` sets the hub on each window
it starts), while each fleet row stays in the project it was spawned into,
beside its manager's. So an agent lives in two places, and each read and write
goes to one of them by one rule: **what the team reads and writes goes to the
board; what finds an agent goes to its row's project and never asks the
board.** A task is the one thing looked up on the board.

**Which board an agent reads is recorded on its row when it is spawned**
(`fleet_agent.board`, `fleet ls --json`'s `board`): the hub's when the spawner
honours one, else the board of the checkout its project's root belongs to, so a
project rooted in a sub-folder of its repository, or at a linked worktree, reads its
principal checkout's board as its sessions do. Every later question of an agent's
board asks the row, never the environment of whoever asks: `asq`, the phone or a
shell without the hub restarts, switches and reaps an agent on the board it was
spawned under. A restart or switch gives the replacement the same board, and its
window the hub the agent's own window was given, recorded at spawn with the launch
(`launch_spec.hub`). An agent spawned before 0.8.1 recorded no hub: its replacement
gets the one that gives its board, none for the checkout's, a project's root for
that project's board. A project's own board where the checkout is another's is kept
only when the agent's session registered on it, or its task is on it; otherwise it
was only a default, and the replacement reads the checkout's.

| On the session's board (the hub's, under one) | On the row's fleet project (whatever the board) |
|---|---|
| Tasks: `task add`, `next`, `claim`, `review`, `done`, `block` and `reopen`. Also the task `fleet spawn --task` gives an agent, looked up (`12`, `#12` or an id) on the board that agent will read; `switch` and `restart` check it, before they stop anything, on the board the replacement will read | Finding a session's row: the hand-over and the `/clear` that keep its claims for the id that follows, and the adoption of that id onto the row, whatever project the row is in |
| Notes, questions, results and every other feed line. A `fleet tell` filed as a note goes on the board the told agent recorded | The panes, tmux, and the row's label and state |
| The fleet's own lines, `agent_exited`, `switched` and `restarted`, on the board the row recorded, with a session or none; and the entries a replacement's hand-off prompt quotes | `--project` in every hint and cycle command, and the project `fleet spawn`, `stop`, `tell`, `switch` and `restart` act on |
| The claims a session holds, released or kept when it ends. The project's own `fleet-paused` signal, on the board its newest manager recorded, running or not, the boards its running agents recorded, and the ones a spawn would give, by the project's name there | Which manager a nudge wakes: the manager row in the writer's own row's project; for a writer with no row (the phone, a shell, an MCP client), a task update's the manager in the project of the row working the task (spawned for it, else its claimant's), else the manager that reads the board, and of several, the one whose project holds the writer's directory, the innermost when projects nest (the phone writes in the root of the project it names, and naming none, has no directory and picks none). The deferred wake of a manager's own Stop wakes its own row, and a manager no row holds (one launched by hand) wakes nobody. A manager is woken only for a write, or an exit, on the board it reads |
| What a nudge delivers: the delta of the board the woken manager reads | |

Without a hub the two are one project, so a call site that asks the wrong one
passes every test that sets none. `tests/test_fleet_service.py` runs each row
of this table under a hub whose fleet project is another
(`test_under_a_hub_each_read_and_write_goes_where_the_rule_says`).

**What the UI remembers.** The view that was open — a project, an agent, the
Settings hub or a page under it — comes back at the next launch if its row is still
there (an agent that has gone falls back to its project), and so does the
captured-directories toggle, which lists anything only while `[projects] capture`
is on; both live in the store's `ui_state` table (#144),
the theme in `state.json` because the board shares it, the navigator width
and the board's line sizes beside the theme. Nothing else is kept: the panes are
tmux's and the rows are the store's.

**When the doctor runs.** Once when `asq` starts, about the project it reopens,
and each time its page opens (*⚙ Settings ▸ Doctor*), about the project selected
last; then again on the page's *Re-run checks*, after one of its fixes, and when
Welcome connects Claude Code. Selecting a project, opening a tab or pressing `r`
(which refreshes the fleet) runs nothing, so moving around the UI stays quick.
What the last run found shows beside the sidebar's settings row (`⚙︎ Settings  ✗ 1  ⚠ 2`,
nothing when all is clear) and on the hub's Doctor link, so the run at start
is seen without opening the page. The page's header names what it checked
(`checks for <project>`, or `checks for this machine`), and while it runs the
checks again it keeps the last report on screen (`… running the checks again`).

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
hatch (`F12`), the scroll keys below, ctrl+c while text is highlighted in that
pane (it copies) and cmd+c, which is only ever the copy. Printable characters
travel as typed —
except with alt held on an ASCII letter, where the chord is the meaning (`M-p`,
so Claude Code's alt+p switches the model; alt+shift+a is `M-A`). Special keys
are translated into tmux's names (Enter, BSpace, ctrl+c → `C-c` when nothing is
selected, shift+tab → `BTab`, …), and so are alt+digit and alt+space when the
terminal reports them as chords rather than as text (`M-1`, `M-Space` — the
kitty keyboard protocol does, a legacy terminal cannot; see the limits below).
Where there is no safe name the character still travels: alt+shift+o types an
`O`, because `ESC O` is the start of an escape sequence to the program reading
it, not a chord; a key named after its character (`§`, `±`, `«` on a non-US
layout) and the numeric keypad's operators are typed even by a terminal that
names the key without reporting its text (numpad `+` is a `+`) — except the
keypad's decimal and separator keys, whose text only your layout knows, which
are named once rather than guessed. Shift on a letter beyond ASCII, from such a
terminal, types its capital (`shift+ф` is `Ф`) — wrong only on the few keys
that shift to something else, like AZERTY's `é`, which shifts to `2` — and a
letter with no case (`ש`, `क`) or a two-letter capital (`ß`) is named once
rather than guessed. A chord your tmux is too old to carry travels by an
older spelling of the same meaning where there is one — shift+enter below 3.5
is ctrl+J, Claude Code's newline either way — and is otherwise refused rather
than mistyped, with a warning that names the version you have and the one it
needs (`tmux 3.4 cannot carry ctrl+shift+enter — 3.5 or newer can`), once per
key name for each tmux server the pane shows. The one exception is a modifier
tmux cannot spell at all — Cmd (super) or hyper — which is dropped rather than
typed, and without a word, because
Cmd+V is a command for your terminal and not a request for a `v`. A key with
nothing to type is ignored the same way, whatever is held with it: a modifier
or a lock (Shift, Control, Caps Lock…), and the whole keys a terminal speaking
the kitty keyboard protocol reports only because we asked it for every key —
Menu, PrtSc, Pause, the volume and media keys, the keypad's centre. Those and
a Cmd chord are the only silent keys; anything else the pane cannot type — a
chord you could have meant, a function key past F12, a symbol it has no table
for — is named in a quiet notice instead, once per key name in that pane, and
restarting its agent or signing in again does not repeat it: `no way to type
f13 into a tmux pane`. Nothing is sent either way, since mistyping into a
running agent is the worse failure.
Paste is bracketed, so Claude Code sees one paste and not one Enter
per line. The wheel goes to whoever can use it: a program that tracks the mouse
(Claude Code's fullscreen TUI does) receives it as its own mouse event and
scrolls its transcript; a fullscreen program that does not is left alone (its own
keys scroll it — arrow keys would land in its prompt), and you are told so once;
anything else scrolls the pane's tmux history, and any key returns to live. A view
already scrolled into history always comes back with the wheel, and a pane in
tmux copy mode stays tmux's. The keyboard scrolls too: shift+PgUp / shift+PgDn
(or alt+PgUp / alt+PgDn where your terminal keeps shift's for its own
scrollback), shift+Home (the top) and shift+End (live) — through the same
decision as the wheel, so on a Claude Code pane they scroll Claude's transcript.
A pane scrolled into tmux history shows `[↑k/history]` in its top-right corner.

**Every Claude Code shortcut, and what reaches the agent.** The keys travel
outer terminal → Textual → `core.keys.translate` → `tmux send-keys` → Claude
Code, and `aisquare fleet attach` sends them through a raw tmux client instead.
The table is generated from the translation table itself and pinned by
`tests/test_keys.py` (one row per documented shortcut, plus the two-key
sequences in order); `aisquare doctor` prints the `fleet terminal` line —
which outer terminal it recognises, whether the tmux the keys cross carries
extended keys (the running fleet server's, which a tmux upgraded in place
leaves on the old version until it restarts; yours when none runs), and
whether the running fleet server still has a prefix key.

| Claude Code shortcut | Sent to tmux (3.5+) | tmux 3.2–3.4 | Outer terminal |
| --- | --- | --- | --- |
| ctrl+C / D / G / L / O / R / V / B / T / S / Z, ctrl+A / E / K / U / W / Y, ctrl+P / N, ctrl+J | `C-<letter>` | same | every terminal |
| ctrl+X then ctrl+K / ctrl+E | `C-x`, then `C-k` / `C-e` — one `send-keys` each, in order | same | every terminal |
| Esc, Esc Esc, ctrl+[ | `Escape`, `C-[` | same | every terminal (`escape-time 0`: Esc is immediate) |
| ctrl+_ (undo) | `C-_` | same | ctrl+shift+- depends on the terminal reporting it as ctrl+_ |
| alt+B / F / D / Y / P / T / O / M / V | `M-<letter>` | same | every terminal that sends alt as an ESC prefix (Linux, Windows); on macOS set Option as Meta (Esc+) in the terminal, and see the alt limits below |
| Option+Enter | `M-Enter` | same | as above |
| **shift+Enter** | `S-Enter` | `C-j` — Claude Code's newline, the same result | **only a terminal speaking the kitty keyboard protocol reports the shift** (kitty, ghostty, wezterm, foot, alacritty); VTE terminals, iTerm2 and Windows Terminal send a plain Enter and the UI never fakes it — `\` then Enter, or ctrl+J |
| shift+Tab (mode cycle) | `BTab` | same | every terminal |
| Tab, Up / Down, PgUp / PgDn, Home / End, Backspace, Delete | `Tab`, `Up`, `PPage`, `Home`, `BSpace`, `DC` | same | every terminal (shift+PgUp / alt+PgUp are the pane's own scroll keys) |
| `?` `!` `/` `@` `[` `\` and every printable key | the character, literally | same | every terminal |
| F1–F12 | `F1`…`F12` | same | F12 is the escape hatch (`[fleet] escape_key`) and never reaches the agent; F1 opens the UI's command palette only from the sidebar |
| ctrl+P | `C-p` — Textual's palette is on F1 in this UI, so ctrl+P is Claude Code's history | same | every terminal |
| ctrl+Z | `C-z` — suspends Claude Code *inside the pane*; type `fg` there to resume | same | every terminal |
| ctrl+D | `C-d` — exits Claude Code; the pane reads 💤 exited and offers Restart | same | every terminal |
| `[` in the transcript viewer | the character — inside tmux "native scrollback" is the pane's 50 000-line history, which shift+PgUp already reads | same | every terminal |

**A Codex pane** gets the same keys, in Codex's own spelling where it has one
(`AgentAdapter.key_spellings`, measured on Codex 0.162.1 under tmux 3.7c):
**shift+Enter is sent as ctrl+J on every tmux**, because Codex submits the prompt on
`S-Enter` and takes `C-j` as its newline (alt+Enter is a newline too). Esc
interrupts a running turn (`esc to interrupt`), shift+Tab (`BTab`) cycles Codex's
Default and Plan modes, and ctrl+C reaches Codex as it is.

**tmux pitfalls the fleet engineers around.** *Prefix:* the private server runs
with `prefix None`, so in `fleet attach` ctrl+B is Claude Code's
background-tasks key rather than tmux's, and **F12 detaches the client** (the
same key that hands focus back to the sidebar in the UI; the UI's own path,
`send-keys`, never met the prefix). *Server environment:* a window inherits the
tmux *server's* environment, frozen at its first start — after a re-login the
display, the Wayland socket, the user bus, the runtime dir and the SSH agent
socket go stale, and ctrl+V image paste, notifications and agent-forwarded `gh`
fail in silence. Each spawn now sets this shell's `DISPLAY`, `WAYLAND_DISPLAY`,
`XDG_RUNTIME_DIR`, `DBUS_SESSION_BUS_ADDRESS`, `SSH_AUTH_SOCK`, `COLORTERM` and
`TERM_PROGRAM` on its window, and `fleet attach` refreshes the session's copy
through `update-environment`; agents already running keep what they had, and
`doctor` lists what is stale on the running server. The spawner's
`AISQUARE_TEAM_HUB` and `AISQUARE_EXPLAINABILITY_TARGET` (blank for none) go on
the window the same way, so the agent's `launch` joins the project, and traces
with the key and to the deployment, that the spawner and the UI's Explainability
tab resolve, not the server's.
*Passthrough:* the server keeps `set-clipboard off` and no `allow-passthrough`,
and it does not matter for
the UI — no tmux client is attached to it, so an OSC 52 copy, an OSC 8 link or
an OSC 9 notification from Claude Code has no terminal to reach; copies arrive
through the pane (Textual selection, and the paste-buffer mirror below) and
links are plain text. In `fleet attach` your own terminal is the client and
tmux's defaults apply. *Multiple clients:* `window-size manual` is deliberately
absent (it crashes tmux 3.4); the UI pins each window it shows with
`resize-window`, and a window nobody shows takes tmux's default `latest`
behaviour — an attached `fleet attach` client resizes it to its own terminal.
*Rendering:* `default-terminal tmux-256color` with truecolor overrides, so
italics, dim and 24-bit colour survive `capture-pane -e`; wide glyphs are
measured by Rich's cell widths against tmux's, which agree on emoji and CJK;
the cursor's shape is not reported by `display-message`, so the pane draws a
block. *Two-key sequences:* each key is one `send-keys` process, issued in
order on the event loop (measured ~2 ms each), so `ctrl+X ctrl+K` and vim's
`gg`/`dd` arrive in order; coalescing a tick's keys into one call is a
latency optimisation for the control-mode client (plan §3.1), not a
correctness fix. *Escape hatch:* `[fleet] escape_key` (default `f12`) is
yours to change; keep it out of Claude Code's set.

**The mouse in a Claude Code pane.** A program that tracks the mouse — Claude
Code's fullscreen renderer does — gets your clicks, drags and releases as the
mouse events it asked for, the way it already got the wheel: click in the
prompt to place Claude's cursor, click the `✕` on its diff panel, a `/model`
row or a collapsed tool result, ctrl+click a link, and drag across the
transcript to select *in Claude Code*, which copies on release by itself
(double- and triple-click are its word and line selection). Claude's own copy
runs `wl-copy`/`xclip` in the agent's environment — the tmux server's, which may
have no display — and inside tmux writes the tmux paste buffer, so after a
left-button release the UI reads tmux's buffers and mirrors one written since the
press to your clipboard (OSC 52), the same way its own copies arrive — the same
words selected twice included, and a copy that lands as late as about 0.65 s
after the release, unless you press a mouse button first. Claude Code's selection
is copy-only by design: there is no "select in the prompt and paste over it"
in Claude Code in any terminal. While such a program owns the mouse the UI's
own drag-select stands down; **shift+drag** is the one gesture that always
selects locally, in every pane, and copies on release. In a pane whose program
does not track the mouse (a shell, the classic renderer) nothing changed:
Drag to select text in a pane (double-click selects a word): it is copied to
your clipboard on release (OSC 52 — your terminal has to accept it; Windows
Terminal, kitty, wezterm, iTerm2 and foot do), and ctrl+c or cmd+c copies it
again while the highlight stands — from the pane or from the sidebar — and then
clears it (when two panes hold a highlight, the one made most recently goes
first). Only a left-button drag is a copy, so a right-click over a highlight
leaves your clipboard alone — and a right click followed by a left click is one
click, not a double click — and so does a drag somewhere else entirely while a
highlight stands. The highlight does not outlive what it means: typing or
pasting into the agent drops it, so does the agent printing something else
under it — or a `(pane gone)` notice replacing the row — and so does a click,
and ctrl+c after any of those is the agent's interrupt; a change hidden under
the `[↑k/history]` marker is not a change you can see and leaves it standing.
A triple click selects nothing, and the pane is never highlighted
whole: a drag that starts on the agent header and ends below the pane selects
nothing in it. A drag that crosses the pane's edge — begun on the agent header,
or released outside it — copies too, one character short of the same gesture
made inside the pane: the terminal library reports the crossing endpoint
without the trailing cell, and the highlight stops there too, so what you see
is what you get. What is copied is always what is shown under the highlight at
the moment you copy: cut to the columns the pane actually shows, and including
the `[↑k/history]` marker and the `(exited 0)` notice where those are what the
row displays. Blank cells past the end of a line, and blank rows at the end of a
drag, are not text and are not copied; a drag over nothing but those — the empty
rows under an agent's output — copies nothing, says "nothing to copy" and
leaves no highlight behind, so the next ctrl+c is the agent's interrupt. A
click whose pointer slips a cell before the button comes up leaves no highlight
there either, and says nothing. A
line tmux soft-wrapped is copied as one line, as tmux's own copy mode copies
it — every frame carries tmux's own wrap marks (`capture-pane
-F`, tmux 3.7 and later; an older tmux gets one line per row). Tabs are
copied as the spaces they occupy on screen, and an emoji or a wide glyph is
always highlighted and copied whole. Under an agent that is still printing that
means the text at release, not at the press — the same text you can see
highlighted.
Modifier
chords beyond ctrl and alt depend on your *outer* terminal speaking the kitty
keyboard protocol (kitty, ghostty, wezterm, foot, recent alacritty): in
VTE-based terminals and Windows Terminal, shift+enter arrives as plain enter
and the UI never fakes it — `\` then Enter inserts a newline in Claude Code
everywhere.

The alt chord's limits are the terminal's and Textual's key parser's, not this
UI's, and they were measured against the parser (`tests/test_keys.py` feeds it
the bytes a terminal sends). A legacy terminal — xterm, VTE, Windows Terminal,
tmux — sends `ESC` plus the key: `ESC p` is read as alt+p and becomes `M-p`,
but `ESC 1` … `ESC 0` and `ESC Space` are read as the glyphs `¡ ™ £ ¢ ∞ § ¶ •
ª º` and a plain space, with no alt at all, and are typed as such; `ESC b` and
`ESC f` are read as ctrl+left and ctrl+right; and ctrl+alt on a letter loses
the alt (ctrl+alt+p reaches the agent as ctrl+p). A terminal speaking the kitty
keyboard protocol (kitty, ghostty, wezterm, foot, recent alacritty) sends the
chord itself: alt+1 is `M-1`, alt+space is `M-Space`, ctrl+alt+space is
`C-M-Space` — unless it reports the text the key produced, as macOS Option
does (alt+p is a `π`), in which case the parser drops the `alt` token and the
text is typed. And Escape is what the parser has to tell a legacy alt chord
from: a letter arriving within ~100 ms of a lone Escape is read as that chord,
so pressing Esc and immediately typing `p` switches the model instead of
typing the letter.

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
recorded as ended with their exit code, vanished panes as lost, and ended
agents' worktrees that are clean and pushed are removed; uncommitted or
unpushed work is never deleted:

```sh
aisquare fleet reap
aisquare fleet reap --all
```

If `doctor` says the private server is *not running* and `reap` reconciles
nothing, the server is silent and `reap` refuses to guess. When it is genuinely
gone — you rebooted, or ran `kill-server` — say so:

```sh
aisquare fleet reap --all --server-down
```

`doctor` says instead that no tmux client reaches the server, which *may still be
running*, when the row's socket file is gone (systemd-tmpfiles or a logout
removed it while the server ran on): send that server `SIGUSR1`, which makes
tmux create its socket again. `--server-down` leaves such rows live and names
them; once you know that server is not running, `fleet shutdown --all --forget`
ends their rows on your word.

**The server was stopped outside the CLI.** Rows read `unknown (tmux
unavailable)` and `reap` reaps nothing — correctly: it cannot ask. A stopped
server leaves its socket file behind with nothing listening, which shows that it
exited: the next spawn, in any project, or asq's Accounts sign-in window, sees it
before it starts a new server at that path, and the rows on the old one read
`✗ lost` from then on, as their panes are gone (a spawn's receipt names them). A
plain `reap` records them, `restart`
brings one back under its own label, and asq's Start the coders restarts the
lost coders. A server started some other way (a hand-run `tmux -L asq`, a 0.8.0
client) takes the path without that step, and hands the old pane ids out
afresh. That server is not the old rows': every row records when its server
started, and this one started at another time, so they read `unknown (another
tmux server on its socket)` — never the state of the agent that got their id,
whose pane the UI does not show under them either. From outside, such a server
looks like one that took the path from a server that lost its socket file and
runs on, so `reap` leaves those rows and names them, `stop` and `restart`
refuse them, and `shutdown` leaves them live; once you know the old server is
gone, `fleet shutdown --forget` ends their rows on your word. (A row started before 0.8.1
recorded no start, reads `✗ lost` as before, and a plain `reap` records it.)
`shutdown` records them as lost on your word, at any point, scoped to one
project or over all of them:

```sh
aisquare fleet shutdown --project amber-otter --yes
aisquare fleet shutdown --all --yes
```

That is also how to stop everything the fleet ever started: it ends every agent
at once (so prefer `fleet stop` per agent), and it reports what it ended.
Running `tmux -L asq kill-server` by hand is what leaves the rows above wrong,
and it takes any other session on that socket with it.

**The manager shows 💤 exited and nothing brings it back.** You ended its
Claude Code (ctrl+c until it quit, or `/exit`) inside its window; tmux keeps
the dead window (`remain-on-exit`) so the last screen stays readable, and the
row stays on the sidebar as **💤 exited** for a day while that window is
there. Its row now records the exit the moment any listing sees the dead pane
— no `reap` needed — so `aisquare fleet spawn manager` is not refused any more,
and the row itself offers **Restart**: same label, role, task, worktree (as it
stands, on the branch the agent was on — whatever its task or the codename says
now) and account, and the SAME session resumed from its transcript when that file is
on disk, so the manager comes back knowing its intake, its contracts and its
coders (`--fresh` in the command, or a missing transcript, starts new with a
hand-off prompt built from the board). From a shell: `aisquare fleet restart
manager`. The project's Manager tab says *manager exited (130)* over its
**Start manager** button instead of "no manager yet". **Stop** on an exited
row removes the dead window and takes the row off the listing; so does
starting the same label again (the replacement supersedes the old window once
it is up, so the sidebar never shows two rows called manager); `doctor` names
a project whose manager exited while agents are still running
(`fleet-manager`). A restart that is refused — a coder whose task is done, an
account no longer on this machine — leaves the 💤 row and its last screen as
they were (**Stop** clears them), and a running agent whose role, task,
account or binary would refuse the restart is refused before it is stopped. A
running agent is handed over the way `fleet switch` hands one over: its task
stays claimed for the replacement, and the board says `restarted`, not
`agent_exited`, so the manager is not woken to staff that task again. Should the
replacement not start, the task goes back to the pool and the board says
`agent_exited` after all, as *exited (0): its replacement did not start*
(*exited (?)* when the stop had to kill it). A resumed
agent is typed one line telling it to carry on, as `fleet switch` types it, so
it does not sit at the idle prompt `claude --resume` opens at. An agent a
hand-over is already moving (a `fleet switch`, by hand or on a usage limit, or
another restart) is not restarted or switched again: that hand-over starts the
replacement itself. The agent view's **Stop** and **Restart** act on the row it
shows: once its label has passed to a replacement (the manager restarted that
coder), they say so and leave the replacement alone.

**A panel reading "No changes this session" sits beside an agent's conversation
and will not go away.** That is Claude Code's own diff panel (fullscreen
renderer, v2.1.260+): it opens by itself once Claude edits a file in a terminal
at least 144 columns wide, and once opened it opens again on every edit, in
this session and later ones. Fleet windows are born 120 columns wide for
exactly this reason (`core.tmux.DEFAULT_WINDOW_WIDTH`) and a spawn from the UI
uses the pane's real size, so a panel should only appear if the pane itself is
that wide — or after `aisquare fleet attach` from a terminal that wide. Under an
attach the session's first window (usually the manager's) follows the
terminal's size unless the UI has shown it, and keeps that width after you
detach; every other window keeps its own size, panned in a smaller terminal and
padded in a larger one. To close one: wait until the agent is idle
(`⏸ waiting`) and type `/diff` into the pane — typed while Claude is working it
is queued as a message, which is why it seemed to do nothing. Or click the `✕`
in the panel's header: the pane forwards the click to Claude Code (#148; see
*The mouse in a Claude Code pane*).

**An agent is stuck on a permission prompt.** Its row shows **🔔 NEEDS YOU**
and the terminal rings. Nothing nudges it and nothing answers for it: click the
agent, click into the pane, and answer as you would in the agent's own
terminal — or `aisquare fleet attach` and answer there. If prompts are routine
for that project, the role's `permission_mode` is the knob — per spawn
(`--permission-mode`) or in `[fleet.roles.<role>]`; there is no project
allowlist in this checkout. `bypassPermissions` exists and is never a default.
A Codex agent rings only for a request you answer: under `--approve-for-me`
Codex's own reviewer answers, with no prompt in the pane and no bell.

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

- **Other terminal agents.** Claude Code and Codex both run the fleet now
  ([coding agents](coding-agents.md)). Another terminal agent (Gemini CLI, …)
  is one adapter and its fixtures
  ([Adding a terminal agent](coding-agents.md#adding-a-terminal-agent)), with
  no change to the fleet.
- **Auto-merge on green** as an opt-in policy. In v1 a human merges (plan §3.5).
- **Remote or multi-machine fleets** — the cloud roadmap is unchanged.
- **A tmux-less backend** for hosts without tmux (plan §3.1, option A).
