# The fleet

`asq` with no arguments opens one view over every project you have registered,
the **manager** agent running in each, and the agents that manager spawns. Every
agent is a *real* Claude Code session — started in the background inside a
private tmux server, monitored, and surfaced in the UI exactly as it would look
in its own terminal. Nothing is relayed or re-rendered as a chat; click an agent
and you are typing into the session itself.

Three ideas carry the whole feature:

- **One view.** Left: a navigator — `Fleet ▸ projects ▸ agents`, then under a
  line *⚙ Settings* (App settings, Accounts, Doctor, *Fleet*'s *Restart all…*
  and *Stop all…*, *Remote control* and *Captain*) with a dim status line: the
  AISquare sign-in and the Claude accounts' headroom as two dots (`?` explains
  the colours; a click opens Accounts). Right: whatever you clicked — onboarding
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
| **`claude`** | every fleet role runs on Claude Code | 2.1.x — `--session-id`, `--permission-mode`, `--effort` |
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
   the left and a Welcome page on the right that walks you to a fleet in three
   steps: pick a project, connect Claude Code (it names the install command
   when Claude Code is missing), then start the manager and two coders. Press
   `w` in the sidebar to come back to it. Step 2 is ✓ once Claude Code is
   connected, signed in or not (it asks when it first starts), and *Sign in*
   stays a Shift+Tab back from step 3 until you are. It opens Accounts, and
   once that sign-in lands Welcome is shown again with the keyboard on the next
   step, *Start manager* when the sign-in was all it waited for; a sign-in that
   lands from nowhere in particular names the next step under the notice
   instead ("Next: open alpha and press Start manager"). No script ever meets a
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
   *Start manager* and the manager's live Claude Code session fills the pane.
   Type your goal to it in prose, exactly as you would to any Claude session. It
   writes contract-carrying tasks, spawns coders, a tester, a ui-tester for
   anything a user sees, and a reviewer as the
   work needs them, reopens what fails, calls a validator once everything is
   done, and posts `READY: <PRs + evidence>` when its gate passes. It never
   writes code and never merges — a human does (Phase 5). The project's
   **Personas** tab lists the personas it can use, previews exactly what an
   agent is briefed with, and imports, edits, exports, removes and validates
   them (`docs/personas.md`).

   Once the manager has spawned a team, a line under its info line counts it
   per role and state, in the sidebar's icons and chips: `🔨 2▶  🧪 1⏸  👀 1🔔
   · board for detail`. It is read from the same refresh as the sidebar, so it
   costs nothing; click it for the **Board** tab. With only the manager
   running, there is no line.

   **3a. Reading the board.** The **Board** tab's top line says what needs
   you: `needs you: 🔔 1 waiting · ❓ 2 questions · 👀 1 in review · ⛔ 1
   blocked`, each count a button to the first thing it counts, or "nothing
   needs you — the manager is working; talk to it on the Manager tab". 🔔 is a
   pane waiting on a key and ❓ a question to you: press it, and the agent's
   pane opens, where you answer. 👀 is a card waiting on a reviewer agent, or
   on you on its PR; ⛔ is blocked, and its detail says on what. The counts are
   the captain's attention queue's (`docs/captain.md`), read from the board's
   own tick; stale agents are left to the captain. Nothing on the board is
   typed into: you talk to the manager, and it writes the tasks. With none
   open, the table says so: "No open tasks. The manager writes them when you
   give it a goal on the Manager tab."
4. **Watch the agents appear**, indented under the project, each with a role
   icon (🧭 manager · 🔨 coder · 🧪 tester · 🌐 ui-tester · 👀 reviewer · 🛡 validator) and a
   state chip — **▶ working**, **⏸ waiting** (a fresh agent reads waiting until its
   first prompt starts a turn, and one whose pane has been quiet for
   `busy_quiet_seconds` with Claude Code idle at its prompt reads *idle (the board
   said working)*: a turn ended with Esc fires no hook), **🔔 NEEDS YOU** (with a terminal
   bell), **⏳ limited** (parked on a Claude usage limit, with the reset time),
   **💤 exited(N)**, **✗ lost**. **Click an agent** and you see its real
   session; click into the pane and every key you type goes to it. The header
   carries a **Stop** button whenever the agent still has a process, and on a
   **💤 exited** row, where it removes the dead window tmux kept for the last
   screen and takes the row off the listing — a **✗ lost** row shows none — and
   it asks first: *Stop* sends `/exit` and kills the window after the grace
   period, *Force* skips the `/exit`, *Cancel* does nothing at all. A refusal
   stays in the dialog with its reason and the agent keeps running (`fleet stop`
   below: tmux that cannot confirm the pane died leaves the row live); a stop
   toasts `✓ stopped <label> (<id>)`, the list re-reads and the view returns to
   the project. Beside it, **Restart** starts the agent again under its label —
   same role, task, worktree, account and persona; its session is resumed from
   the transcript when that is on disk (`fleet restart` below) — and the new row
   is shown if this view is still the one on screen. Both ignore a click in the
   second after they appear or move.
   **`＋ Spawn agent`**, at the top of the sidebar under *Fleet* (one for the whole
   list, not one per project), opens the **Spawn dialog**, headed with the project's
   name and codename. Its first field is **project**: every listed project, in the
   sidebar's order, preselected to the card or the agent's project you have selected,
   else the first; changing it re-reads what depends on the project (git, a running
   manager, teammates, tasks, personas, the prefilled label) and keeps what you typed
   for the rest. With no project yet it says so and opens nothing. Under it come three
   fields: **role** (`coder` preselected; first in the list *(none) — general-purpose Claude*, a
   plain Claude Code session with no standing cycle or mission, `fleet spawn
   agent` below; then the fleet's roles plus any role bound with `team bind`;
   `manager` is greyed out while one runs), **hand off from** (a teammate to
   fork or take over) and **account**. Account opens on no account at all, the
   spawn's own default, and says what that runs on and why: `(default: rank 1 ·
   work)`, `(default: headroom · work)`, `(default: bound to tester · account
   2)` or `(default: project default · work)`, what `accounts order`, `team
   bind` and `accounts default --project` decide for the role picked and the
   model it runs (a `--model` in Extra agent args, else the Model field; read
   again when either changes; the captain's dialog the same for the captain). It
   reads `(this shell's)` only when no account is signed in, so the spawn leaves
   this shell's `CLAUDE_CONFIG_DIR` as it is. A headroom pick is shown from the
   readings the status line already has; the spawn picks again when it runs.
   Everything else is folded into
   **Advanced** — click its title, or `Tab` to it and press `Enter`. It opens
   by itself when the dialog opens with a binary or persona already filled in,
   and whenever one of its fields is why *Spawn* is disabled. In it, first who
   runs it: *Pick…* opens the target picker (a bound teammate or an account
   fills role, binary and account) and binary. Then as whom:
   **persona** — `(none)` or one of the project's personas, the role's
   `[fleet.roles.<role>].persona` preselected, its description shown under it,
   and *Import…* beside it to bring one in (`docs/personas.md`).
   Then label (prefilled `<role>-1`, or `<role>-<task short id>` once a task is
   picked; 🎲 offers `<role>-<adjective>-<animal>`; a label that breaks the rule
   disables *Spawn* and shows the rule), task (the project's open tasks),
   worktree (disabled with "not a git repository" outside one), *Claude Remote
   Control* (Claude Code's own, at the role's `remote_control`; it follows the role
   until you touch it), permission mode, **model** and **effort** (opened on your
   last pick, whatever the role, or on what a role's settings pin for that role;
   blank is the account's own default, and clearing a field makes that your pick
   too: [The roles](#the-roles)),
   extra agent args (split like a shell would; a quoting error is shown inline)
   and a first prompt. A field left as it opened means the role's default,
   exactly as an omitted `fleet spawn` flag does; picking `(none)` over a role
   that has a default persona spawns without one. A refusal —
   the agent limit, a binary not on your `PATH` — stays in the dialog with its
   reason; a spawn toasts its receipt and notes and opens the new agent's pane.
   `Esc` cancels, except while a spawn is already running: that one cannot be
   taken back, so the dialog waits for its answer, and *Pick…* and *Import…*
   are disabled until it has it. *Pick… ▸ + New account* leaves for the
   Accounts page's sign-in, and once it lands the dialog opens again as it was
   typed (project, label, task, first prompt, persona, binary), on the new account; the
   persona picker's *+ New account* comes back the same way, the new account
   highlighted. A sign-in that does not land reopens nothing, and one toast says
   so; going to another page first drops the way back.
5. Selecting an agent gives its pane the keyboard at once — type, and it reaches
   Claude Code. **Press `F12`** to hand focus back to the sidebar (it is the one key the pane
   never forwards; configurable). With the sidebar focused: `t` picks a theme,
   `x` opens the Stop dialog for the selected agent — the same rule as the
   button, so on a project row or a **✗ lost** agent it does nothing — `q` quits
   the UI, and the agents keep running.
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
tier above the work it gates. Those ladders are what `team spawn` prints; **`fleet spawn`
runs the model and effort you last chose** (0.9.1; 0.9.0 passed the ladder's top rung, so
a manager always started on Fable at high effort over the default you saved with
`/model`). Each of the two is decided on its own, in this order: a `--model` or
`--effort` in the spawn's own arguments (`fleet spawn manager --model opus --effort
xhigh`, or the Spawn dialog's Model and Effort fields and Extra agent args); then the
role's settings, its `[fleet.roles.<role>] extra_args` or its binding's `--arg`, which is
where a role is customised; then your last pick; and with none, no flag at all, so Claude
Code runs the account's own saved default, as before 0.9.0. A flag the spawn names
replaces the role's, the binding's included, so Claude Code is never given two. An empty
one (`--model ""`) is refused: leave it out instead, and clear the dialog's field to
forget your pick.

**Your last pick is one per home**, for every role and persona alike. Only your own choices
write it: a spawn of yours that names one (from asq's dialog or your own shell; a cleared
dialog field forgets it, unless the role's settings pin that flag, whose pin then runs and
whose blank says so), and the built-in `/model` or `/effort` you run inside any fleet agent,
which its next turn end finds in Claude Code's own record of the command: the frame Claude
Code writes for a local command (its caveat, the command, its answer, each the last one's
child), which no text can make, so a typed prompt, a board note or a compaction summary
that quotes one counts for nothing, and a command Claude Code never answered for nothing
either. Only an answer that says the command succeeded is a choice: `Set model to `Opus
5.5`` (`claude-opus-5-5`, or what you typed after `/model`), the 2.1.311 picker's `… with
`xhigh` effort`, which sets the effort too, and `Set effort level to high`. The picker's
`Default (recommended)` forgets the model pick and `/effort auto` the effort pick: the
account's default from then on. A choice "for this session only" runs on in that agent
and is no pick, and so does ultracode switched on or off for the session (`Ultracode on
(this session only)`; the `/effort` picker's Tab with a saved level saves the level and
runs ultracode). Leaving a picker (`Kept model as …`, `Cancelled`) sets nothing, and
neither does a command Claude Code refused (`Model 'sonet' not found`, `Invalid argument`):
a typo never becomes your pick. Only commands stamped after the agent's row was bound to
its transcript count (its creation, or the moment it moved to another file): a fork or a
`/branch` does not re-read the history it copied, nor a `/resume` or a `fleet switch` the
conversation it took up.
**Change the model at the prompt, not mid-turn**: a `/model` typed while a turn runs
switches the agent but Claude Code 2.1.311 writes no record of it, so it is no pick and a
restart starts the model the agent had before (a known limit; aisquare guesses nothing
from the replies). **Ultracode on top of another level replays as that level**: launched
at ultracode, `/effort high` runs `high · ultracode`, but `claude --effort` takes one
value, so a restart passes the level you set last (`--effort high`) and never raises the
cost above what you chose (a known limit, as decided). A `/model` or `/effort` you run
after an agent's last turn counts at a restart, a switch, a hand-over or a fork before its
next one, which read it first. **Nothing else is evidence**: a model or an effort that
merely differs
from the last turn's, as a custom command's or a skill's `model:` or `effort:` frontmatter
makes it for ONE turn, writes no pick and moves nothing, whoever typed it. The reader
knows Claude Code 2.1.310's and 2.1.311's words and frames; an answer in other words, or a
`/model` record outside the frame, writes nothing, says so on stderr once, and doctor
shows it as `fleet model pick` until an answer reads as a success again (text a
teammate quotes can show there too, harmlessly). The captain writes the pick only from
its own call path (the
captain on, its own row; its spawns take no model), never from an `--as captain` an agent
typed. These guards keep cooperating agents from writing your pick by mistake; they are
no wall against a process that sets `AISQUARE_FLEET_AGENT` or the store itself. A spawn
an agent makes (`fleet spawn … --as <session>`, or from a fleet agent's own shell, such
as a manager's) runs its flags for that one agent and leaves your pick alone. Nor may an
agent hand another a `/model` or `/effort` as a message (one
that opens with it, after spaces, line breaks or invisible format characters): its `fleet
tell` is refused and pointed at `fleet restart <label> --model/--effort`, which runs that
agent alone on it and is no pick either, and its spawn whose first message would open with
one is refused before anything starts and pointed at the spawn's own `--model`/`--effort`
(with a task, the first message opens with its kickoff, and goes through). Yours and the
captain's go through. Only the agent's own session counts: a `claude -p` run from its shell
is not the agent. A role's settings are that role's alone and never write the pick.

A restart, a `fleet switch`, a hand-over and a fork as the same role start what the agent
runs: its launch's model and effort (what its session start reported, its own `--effort`,
else its first plain turn's), as a recorded `/model` or `/effort` alone moved them, the
account default it started on included, on whatever account it moves to. That wins over
the recorded flag and over a binding's pin too (`aisquare launch` drops the binding's flag
beside it), and the recorded flag stays where nothing moved it (an alias like `opus` and a
mode alias like `opusplan` as written). A fork as another role is a new spawn of that role:
its settings, else your pick. None of them changes your pick. Any model or effort Claude
Code takes is kept as written (`fable`, `claude-opus-5-5`, `opus[1m]`, `opusplan`; `low` to
`max`, `ultracode`); another binary gets neither flag. `fleet ls --json` shows the flags a
spawn added in each agent's `launch_spec` (`model`, `effort`) and its `command`. The board's
`⚠ off-ladder` chip stands down for a session on a model you chose for it: your pick, its
spawn's args or its role's settings.

**One tester per task.** The manager spawns a tester for each task that reaches review:
`fleet spawn tester --task <id>` (a ui-tester for a task titled "UI: …"). Its cycle takes
that task alone (`aisquare task next --status review --task <id>`), and after its verdict
it stops rather than draining the rest of the review pool. The validator stays the one
final gate over the assembled deliverable.

| Fleet role | Repo role | Job in the loop | Runs in |
| --- | --- | --- | --- |
| **manager** | `planner` + fleet authority | intake → contracts → `fleet spawn` → steer → report. One per project. Never codes — a direct "fix this" becomes a coder spawn — never merges. | the repo root |
| **coder** | `coder` | implements one task to its acceptance criteria; pushes and opens the PR | its own git worktree |
| **tester** | `runner` (`tester` is the fleet's name for it; `runner` still works everywhere) | adversarial verification: runs the *full* check the contract names, tries to break the change, then `task done` with evidence or `task reopen` with the reason | the repo root. It gets no worktree of its own and **nothing moves it into the coder's** — so whoever spawns it names the branch or the tree to check, in the tester's `--prompt` or a later `fleet tell`, or its "full check" runs against an unchanged root |
| **reviewer** | new | reads the PR as the stranger who will maintain it; findings on the PR via `gh pr review`. Launched without the tools that edit files (Edit, Write and NotebookEdit: the role's own deny, wherever it starts, see [Tools and skills per role](#tools-and-skills-per-role)) and with its shell: Claude Code's `--restricted` removes the Bash that `gh pr diff`, `gh pr review` and its `aisquare` cycle need (0.6.0 and 0.7.0 launched it so, and it could do none of them). Its shell could still push or merge: its briefing forbids both and `auto` mode checks each action, but only the credentials it runs with can refuse them (see the table below) | its own worktree |
| **validator** | `validator` | one final gate over the assembled deliverable before the manager says READY | the repo root |
| **ui-tester** | new | verifies tasks titled `UI: …` in a **real browser** — Claude in Chrome, the Chrome DevTools MCP or a Playwright MCP, whichever this window has — and measures (screenshots, computed sizes, console, network) instead of eyeballing; `task done` names the branch or commit and the URL it verified, or `task reopen` carries a screenshot. With no browser tool it runs the non-browser checks and reopens the task as "not browser-verified", never passes it. **Asked to be read-only, not made read-only** — its briefing says never edit and never push, and nothing enforces that (see the permission-mode table below: no allowed-tools list is written or passed, and `--restricted` would remove the Bash its own `task done`/`task reopen` need). Launched with `--chrome` by the role itself (`RoleProfile.default_args`), on any machine, unless `--no-chrome` is given | the repo root. Like the tester it gets no worktree and **nothing moves it into the coder's** — worse here, because it fails silently: it opens the URL, sees a working page and measures the PRE-change build honestly. So whoever spawns it names the branch or tree and the URL in its `--prompt` (the manager's loop does), and its verdict says which build it measured |

No role at all is `agent`: `aisquare fleet spawn agent` (the Spawn dialog's
*(none) — general-purpose Claude*) starts a plain Claude Code session, labelled
`agent-1` and on, that joins the board like any agent but is briefed with no
standing cycle and no mission, and runs on your last pick like every other spawn. `aisquare launch agent` is the same outside the fleet.

The manager talks to its agents only through the board — tasks, notes, signals
— and `fleet tell` nudges. When a sub-agent writes a result to the board, the
manager is woken (a fixed one-line nudge typed into its pane, only while it is
*waiting*) and reads the update on its next turn; when the manager stops with
new board events pending, its `Stop` hook lets it continue. No daemon, no poll
loop, and it works with the UI closed (Phase 5).

**What wakes the manager.** A review, a done, a blocked or reopened task, a result,
a question, an agent on its usage limit, anything addressed `--to manager`, and an
agent that exited badly: a non-zero status, a kill or a loss (no status), an exit
that released a claim it still held, or a hand-over whose replacement did not start.
Everything else — a clean exit, a switch, a restart, a claim, a note, a decision — is
news: it waits past the manager's cursor and rides along with the next wake or
prompt, so it costs no turn of its own. What the manager did itself never wakes it:
its own `fleet stop` names it as the actor (`coder-auth exited (0) — stopped by
manager`). Each event reaches an agent as one line, `#<seq> <who> <kind>[ #<task>]:
<first line>`, with `… → aisquare team show <seq>` when there is more.

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
aisquare fleet spawn coder --persona skeptic
aisquare fleet spawn reviewer --board-context all   # what of the board it reads
aisquare fleet spawn coder --bin claude2 --prompt "start from the failing test" -- --model opus
```

Starts an agent in the project's tmux session — a window running
`aisquare launch <role> …` — with the role's permission flags and a session id
minted *before* launch, records it, and prints a receipt:
`✓ spawned coder-auth (agt_…) → asq-amber-otter %7`, ending `· persona skeptic`
when the agent runs as one. Anything the receipt should tell you — a label that
had to be suffixed, a worktree or branch that already existed, an agent that did
not come up before its prompt was typed, a persona written for other roles —
follows as a `⚠` line.

| Flag | Meaning | Default |
| --- | --- | --- |
| `<role>` | `manager`, `coder`, `tester`, `ui-tester`, `reviewer`, `validator`, or any role you have bound with `team bind` | — |
| `--label L` / `-l L` | the agent's label (see [Naming](#naming)) | `<role>-<task short id>` with `--task`, else `<role>-<n>` |
| `--task ID` | the board task this agent is for (id or prefix); with no `--prompt`, its first message is a kickoff that names the task as its ASSIGNED TO YOU block does | none |
| `--worktree` / `--no-worktree` | run in its own git worktree | the role's setting: on for coder and reviewer |
| `--permission-mode M` | Claude Code permission mode | the role's setting: `auto` |
| `--bin B` | the agent executable | the role's binding, else `claude` |
| `--prompt TEXT` | the agent's first message: on Claude Code's command line, typed for any other binary ([the first message](#the-first-message)); after the kickoff with `--task`. Typed into Claude Code (`[fleet] first_prompt = "type"`), it goes in once its input box is drawn, past its first-run Security notes screen (Enter) | none |
| `--persona NAME` | the persona the agent runs as ([docs/personas.md](personas.md)): passed to `launch`, briefed once at session start, recorded on the row | the role's `[fleet.roles.<role>].persona`, else none |
| `--remote-control` / `--no-remote-control` | start it with Claude Code's own Remote Control, titled `<project> · <label>`, to continue from claude.ai/code or the Claude app ([remote](remote.md#claude-codes-own-remote-control)); recorded on the row, so a restart and a switch keep it; `fleet ls --json` reports it as `remote_control` | the role's `[fleet.roles.<role>].remote_control`, else off |
| `--board-context all\|relevant\|none` | what of the board it reads: `relevant` keeps its own tasks, what is addressed to its role or label, and decisions; `none` gives no recent updates at start and no per-prompt deltas, its task, protocol and cycle unchanged. Set on its window as `AISQUARE_BOARD_CONTEXT` and recorded, so `restart` and `switch` keep it. A manager reads `all`, and a narrower one is refused | the role's `[fleet.roles.<role>].board_context`: `none` for the reviewer, `all` for the rest |
| `--as SESSION` | the acting session — a manager passes its own, so the row records who spawned it | `user` |
| `-- <agent args>` | everything after the options goes to the agent, as with `aisquare launch` | — |

Refused, with the reason in the message: a second `manager`, more agents than
`max_agents_per_project`, more live agents across every project than
`max_agents_total`, less available memory than `min_free_mb` or memory pressure
at critical (see [Capacity](#capacity); `--json` errors `capacity_project`,
`capacity_fleet`, `capacity_memory`), `--worktree` in a project that is not a git
repository, an unknown role, a `--task` that is already done or dropped, a
persona the project does not have (the known names are listed; a stale
`[fleet.roles.<role>].persona` default names its key). All of these are checked
before any window exists. A known role is one of `aisquare launch`'s roles, a
numbered seat of one (`coder2`), or a role bound with `team bind` — the same
rule `aisquare launch` applies, public as `services.fleet.role_ok`.

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
dropped: nothing is said about it afterwards, and the row no longer names the
task — the label and the branch keep the task's short id. The
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

### The first message

Every first message the fleet gives an agent — `--prompt`, the kickoff of a
`--task` spawn, the line `fleet restart` and `fleet switch` start a replacement
with, a fresh hand-off, the usage-limit hand-over's — either starts a turn, or
`fleet ls` says it did not.

- **A Claude Code agent gets it on its command line**, as Claude Code's own
  first turn (`claude -- "<message>"`, and `claude --resume <transcript> --
  "<message>"` for a replacement). Nothing is typed, so there is no Enter to
  lose: an Enter that reaches Claude Code before it draws its input box is
  dropped while the text stays in the box, which left agents idle with their
  first message unsent. The message waits behind Claude Code's folder-trust
  question too, and runs once the folder is trusted. It travels in a file only
  you can read (`$AISQUARE_HOME/run/prompts/<agent>.txt`), named to `aisquare
  launch` with `--first-prompt-file` and deleted as the launch reads it, so the
  row's recorded launch never holds it and a restart replays no old message.
- **Any other binary has it typed**, and so does every agent with `[fleet]
  first_prompt = "type"`. For Claude Code that means once its input box is
  drawn: the text is pasted, checked in the box, sent with Enter, and Enter is
  pressed again only while the text is provably still waiting there — never into
  a box that is empty, holds anything else, or has a dialog over it.
- **The board tracks it.** The agent's row reads `pending` until its
  `UserPromptSubmit` says a turn started (`acked`). `fleet spawn`, `restart` and
  `switch` wait up to `[fleet] prompt_ack_seconds` (45) for that and print it
  (`✓ first message taken`); their `--json` adds `prompt_delivery` (`argv` or
  `typed`) and `prompt_acked`. A message still pending past that wait, with the
  agent at an idle input box, is not going to be taken: the agent reads
  `attention` — "first prompt not taken", with the `fleet tell` and `fleet
  restart` commands that take it on, each naming the project — and the board
  gets one `prompt_unsent`, which wakes the manager. An agent held at Claude Code's
  first-run Security notes past that wait is not going to take it either: it reads
  `attention` — "Claude Code's first-run Security notes screen — press Enter in its
  window" — with the same one `prompt_unsent`, since a `fleet tell` typed there is lost.
  `aisquare doctor`'s `fleet-delivery` row counts the last week's: acknowledged N
  of M, and how long the agents took to start their first turn.

Both keys are in [the configuration keys](reference.md#configuration-keys).

### `fleet ls` / `fleet status`

```sh
aisquare fleet ls
aisquare fleet ls --all
aisquare fleet status --project amber-otter
```

One row per agent — label, role, state chip, `(worktree)`, `· <persona>`, the detail behind
the state, the pane id — under a header naming the project, its codename and
its tmux session. `ls` shows live agents; `--all` (`-a`) includes the ones that
have ended. `status` is the same data, always live only.

State is **derived, never stored**: a fresh board session row wins (working ·
waiting · attention · limited — the last one trusted until the reset it names
has passed, since a parked agent fires no hook); otherwise tmux's facts (a dead
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
line says what is at its prompt or on its way). The note goes on the board the
agent reads, the one its next prompt reads, whatever hub or fleet window the
command runs in. Never interrupts an
agent that is working or sitting on a permission prompt. Takes `--as SESSION`.
Under `--json`, `typed` is `yes`, `no`, `unsent` or `maybe` beside `delivered`:
`unsent` when the text is at its prompt and tmux refused its Enter (press Enter in
its pane to send it), `maybe` when tmux did not answer in time with the text or its
Enter, which may still arrive. Neither is to be told again: read the agent's pane.

**Never over a draft.** Nothing the fleet types goes into a pane while its input
box holds something a human typed and has not sent, or while someone has typed
into the pane in `asq` within `[fleet] draft_quiet_seconds` (10). That covers a
tell, the manager's nudge, a typed first message and the Remote's paste.
Claude Code's own dim words in an empty box (its placeholder, its suggestion
after a turn) are not a draft. A tell held back this way is filed as a note and
says why ("a draft is in its input box, which nothing types over"). A draft
that appears in the instant the tell is pasted, a human typing between the check
and the paste, stops it before its Enter: the text stays in the box with the
draft, unsent, and the tell says so ("a draft appeared in its input box while
the text was being typed; ours is in the box with it, unsent, and nothing was
sent") and is filed as a note. A manager's
nudge waits, and goes out once the box is clear: `asq` retries it as it
refreshes, and the next board write does too. If the human sends the draft
first, that prompt hands the manager the same board update, and the nudge is
dropped.

**A note reaches a busy agent at the end of its turn.** A note addressed to an
agent's label or role (`fleet tell` to a busy agent, `aisquare note --to
<label|role>`) is handed to it by its `Stop` hook when the turn it was working
on ends, the way the manager is woken: the board update as the reason it keeps
going. It used to wait for the agent's next prompt, which an agent that had
finished might never get. It is guarded like the manager's wake-up: not on a
turn that is already continuing from a stop hook, and at most `[fleet]
max_continuations_per_hour` times an hour. Only an addressed note does this;
the rest of the board rides along with one, and waits for the next prompt
otherwise. `[fleet] deliver_notes_at_turn_end = false` turns it off.

`aisquare persona attach <name> --to <label>` types a persona's briefing into a
waiting agent the same way, and keeps the persona on the agent's row so a `/clear`
or a restart briefs it again ([docs/personas.md](personas.md)). A busy agent gets
no note in the briefing's place: the board names the persona and never carries its
body, and the agent's own next prompt hands it the briefing, for it alone. The
receipt reads `typed` or `noted`.

### `fleet stop`

```sh
aisquare fleet stop coder-auth
aisquare fleet stop coder-auth --force
aisquare fleet stop coder-auth --as <session>   # who is stopping it
```

The board's `agent_exited` names who stopped the agent — `--as`, else the session of
the fleet window you run it in (a manager's), else `user` — with the status, `--force`
and the claims it released in its details (`aisquare team show <seq>`). A manager's own
stop wakes no one; a clean stop by anyone is news for the manager's next wake, and a
kill or a failed exit wakes it.

Sends `/exit`, waits a grace period, then kills the window. The agent's own
`SessionEnd` hook releases its task claims on the way out; whatever the ended
session still held (a killed process fires no hook) is released here and named
in the output — `🔓 N claimed task(s) released back to the board`, each task
listed under it, `claims_released` under `--json` — so the work the next agent
inherits is visible. A release the store refused — or one the board could not
be told about — is reported, never swallowed: the row is down, the operator is
told which claims stayed with the ended session, and the command **exits 1**,
the same contract `fleet shutdown` and `fleet reap` keep for the same fact.
`--force` skips the graceful exit. In `asq` this is the agent view's **Stop**
button and `x` on the selected sidebar row — the same command behind one
confirmation, `--force` included.

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

### `fleet restart --all` / `fleet stop --all`

```sh
aisquare fleet restart --all
aisquare fleet restart --all --managers
aisquare fleet stop --all
aisquare fleet stop --all --managers
```

These restart or stop every running agent in every project at once, and a restart
also brings back every agent that **died**: one that ended for a reason nobody gave
it — killed (an outside SIGTERM, exit 143), crashed, its tmux server gone, a reboot.
An agent aisquare stopped on purpose (`fleet stop`, *Stop all*, `fleet shutdown`) is
not brought back; the store records how each agent ended (`stopped` or `died`), and
`fleet stop` of a 💤 agent's window marks it stopped too, as does `fleet stop` of a
dead agent with no window left (its tmux server gone), which then says it only
marked it. `--managers` acts on the
managers alone and leaves their teams. *⚙ Settings ▸ Fleet* in `asq`
does the same through the same service function (below). An agent is running when
it has a process: ▶ working, ⏸ waiting, 🔔 needs you, ⏳ limited, or · unknown. Each
is restarted or stopped the way `fleet restart` and `fleet stop` do it for one agent.
A restart hands a running agent over and resumes its session from its transcript
when that is on disk. Each agent is acted on by its row, never by its label, so a
label that passed to another agent meanwhile never makes that agent the one
stopped. Some agents are left alone, and each is named with the reason:

- for a stop, an exited (💤) or lost agent: `exited`, `lost` — there is nothing to
  stop;
- for a restart, a dead agent that was not killed: one stopped on purpose (`stopped
  on purpose`), one that exited 0 (`ended by itself (exit 0)`: someone typed `/exit`
  in its window), one that died more than 7 days ago, and an older row of a label
  that was started again since. A row that ended before aisquare recorded how is
  read by its exit status: a signal or an error is a death, no status is left;
- the agent running the command: `this session`. That is the row
  `AISQUARE_FLEET_AGENT` names, and with `restart --as SESSION` also that session's
  row, so a manager that runs `restart --all` isn't stopped half-way through it;
- a row held as another tmux server's (`held: …`), whose pane id names another agent;
- for a restart, an agent whose task is done or dropped (`task is done`), which
  `fleet restart` would refuse;
- for a restart, an agent of a project you forgot (`its project was forgotten`).
  A `project forget` that races a spawn can leave a running agent on it. Its
  replacement would add the project back, so the restart leaves it, and
  `stop --all` stops it, as `fleet shutdown --all` does;
- for a stop, an agent that ended after the list was read and before its turn:
  `already exited`. Each agent is looked at again just before it is acted on, so
  one that exited or was stopped meanwhile is left, and its 💤 window stays. A
  restart looks again too: one that died meanwhile is started again as a dead one
  is, and one that exited 0 or was stopped on purpose meanwhile is left, by the
  rule above.
  One that another command ends after this run has sent it `/exit` is not left
  alone: it fails, and a restart says no replacement was started.

The agents are taken one at a time. A stop takes each project's manager first; a
restart takes it last, after its team. A restarted manager — by `restart --all`,
`--managers`, *Restart all…* or a plain `fleet restart manager` — is told, at the end
of its first message (resumed or fresh), which of its team died and are still dead,
each with its state, its task and the exact command that brings it back
(`aisquare fleet restart --project <codename> <label>`), so after a crash
*Managers only* is enough: the managers bring their teams back. Each one is printed
as it is done, under its project's name, and the count comes last. A restart's notes
follow its line, as `fleet restart` prints them, with ⚠ on the ones that say what
didn't happen. A stop's follow it as `fleet stop` prints them: the `fleet reap` that
removes the worktree it left, and each claim it put back, by title:

```text
alpha
  ✓ manager                  manager    stopped and restarted — resumed its session
  · coder-2                  coder      skipped — task is done
beta
  ✗ coder-web                coder      failed — cannot restart 'coder-web': …
restarted 1 · skipped 1 · failed 1
```

A dead agent's line says so (`✓ coder-1 … was dead (exit 143), restarted — resumed
its session`), and the count splits the two: `restarted 4 (3 running, 1 dead) ·
skipped 0 · failed 0`.

One agent's failure is only its own line; the rest are still restarted or stopped.
The command **exits 1** when any agent failed, a project's agents couldn't be read,
or a stop left claims behind (the same as `fleet stop`). `--json` prints one
object: `receipts`, each with `project`, `agent`, `outcome` (`done`, `skipped` or
`failed`), `why` and `was` (`running` or `dead`, as the list was read), plus, for one that was done, the fields `fleet restart --json`
or `fleet stop --json` prints for that agent (a stop's `agent` is then the row as
the stop ended it). The object also has `unread`, `summary`, `ok` and `interrupted`. Interrupted with
Ctrl-C, it **exits 130**, and it still says how far it got: the agents done so far
(the JSON object under `--json`), the count, and the agent it was on, as `fleet
shutdown` does. The agents after it are left as they were.

`--all` means every project's fleet, so it is refused with `--project` (as
`reap --all` is) and with a label. `--managers` is refused without `--all`. With
`--all`, `--fresh`, `--permission-mode` and `--force` are refused too, because each
is for one agent. Without `--all`, both commands work as described above. Unlike
`fleet shutdown`, which takes a project's whole tmux session down, these keep the
sessions and the server, and a restart brings each agent back.

**In `asq`: *⚙ Settings ▸ Fleet*.** *Restart all…* and *Stop all…* each open a
dialog. It lists the agents it will act on, grouped by project (the project's name,
then `label · role · state` for each agent). The agents it leaves are listed dim,
with the reason. A dead agent a restart brings back is listed apart, `💤 exited (143)
— will be started again`. A *Managers only* switch redraws the list and the count, and
the confirm button names the count (*Restart 4 managers*, *Stop 9 agents*), with the
dead apart when there are any (*Restart 5 agents (4 running, 1 dead)*). *Cancel*
has the focus, and Escape cancels too. When nobody would be acted on, the dialog
says *nothing to restart* (or *nothing to stop*) and its confirm button stays
greyed. The list is read when the dialog opens, and read again on confirm. Confirm
acts only on the agents the dialog showed: one that ended meanwhile is skipped as
*already exited*, and one started meanwhile is left alone. A project whose agents
can't be read on confirm is left alone too, and the summary names it and why. The work runs off the UI
thread, with both buttons greyed until it ends, and `q` refused: a quit can't stop
it, and would leave the terminal blank until the rest were done. Each agent gets a toast as it is
done (`✓ restarted manager (alpha) — resumed its session`, `✓ stopped coder-1 (beta)`).
Then one toast sums it up (`restarted 4 · skipped 1 · failed 0`) and names who was
skipped or failed, and why. A restart from here never moves your view. Like an agent
view's Stop and Restart, both buttons and the dialog's confirm ignore a mouse click
in the second after they appear or move. An ignored click on the confirm button
leaves the focus on *Cancel*.

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

What it kills is the fleet's own `asq-<codename>` sessions, never the server:
`tmux kill-server` would take down every session on that socket, and the fleet's
claim is only ever to its own (a hand-made session, or one a failed `rename`
left under an old name, is not the fleet's to destroy — and `[fleet]
tmux_socket` may point anywhere). A server with nothing left on it exits by
itself, so the next `asq` or `fleet spawn` starts a fresh one on whichever
account that shell carries.

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
full-fidelity escape hatch. The server has tmux's mouse on: the wheel scrolls
Claude Code (or tmux's history, over a program that did not ask for the mouse),
and a plain drag is tmux's own selection, copied to tmux's paste buffer. Hold
Shift (Option in macOS Terminal and iTerm2) to select in your own terminal
instead. Under `--json` it prints the command it would run
instead of running it. The session is found on the server the project's live
agents are on, at the socket their spawns recorded (`tmux -S`), the line the
project's Settings tab prints, so it works from a shell under another `TMUX_TMPDIR`. With
no live agent, the servers its ended agents were on are asked, the latest to
end first: an ended manager's window lingers there and holds the session, the
screen behind the 💤 row. Last, the socket this shell's tmux looks at, but
only while no live agent recorded where its server listens: from a shell under
another `TMUX_TMPDIR` that is another server, and its session of that name is
not the one the agents are in.

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
aisquare fleet reap --finished [--dry-run] [--json]
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

`--finished` does something else: it stops the workers that have finished (see
*Auto-stop* under [Capacity](#capacity)), each `agent_exited` saying `stopped by
auto-stop` and why. `--dry-run` lists every candidate with the reason it would be
stopped, or the condition that keeps it, and stops none. With `--json` it prints one
object per worker: `label`, `project_id`, `finished`, `reason`, `kept_because`,
`stopped`.

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
aisquare fleet pause --group <name>       # the group's members and its parent
```

`--group` pauses (or resumes) every member of a project group and its parent at once
(see [A manager over a group](orchestration.md#a-manager-over-a-group)), and under a
parent an agent's own spawn (`fleet spawn --as …`, a parent manager restarting a child) on
a paused board is refused, not only advised against. A person's spawn goes ahead, and a
project with no parent above or below it behaves as before.

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
aisquare ui --project amber-otter --project web   # only these projects
aisquare ui --group frontend                      # only this group's projects
```

The app — what bare `asq` runs. Explicit so a script or alias can reach it, and
so `aisquare ui --help` exists. Without an interactive terminal it refuses with
the reason (`not_a_tty`) rather than starting a full-screen app into a pipe.

`--project` (repeatable; codename, name or id prefix) and `--group` (repeatable;
the group's members as they stand when the app starts) list only those projects
for this session, plus the captain's section when it is on. A name that matches
nothing is refused before the app starts.

**What a tick costs.** Every two seconds the sidebar reads the fleet off the UI
thread: one store session and one `tmux list-panes -a` per server, however many
projects are listed. A project with no live agent and none that ended in the
last day is not read at all, and the members of a collapsed group are not read
either: its header counts their live agents instead, and its bells show once it
is unfolded. A card whose agents did not change is not repainted. Measured with
50 listed projects, 10 of them with 3 agents: the read went from 51 store
sessions and 20 tmux processes (~200 ms) to 1 and 1 (~11 ms), and the UI thread's
paint from ~15 ms to ~4–7 ms.

*⚙ Settings ▸ Remote control* (App settings' own *Remote* tab) turns Remote on: the fleet on your phone through
your own ngrok tunnel, with the link, a QR code and the passphrase; the first time,
it asks for your ngrok token. `R` opens the same Remote in one
panel — see [Phone control](remote.md). *⚙ Settings ▸ Captain* (the box at the top
of *App settings ▸ Agent settings*) switches the
experimental captain on and off with no restart; `AISQUARE_EXPERIMENTAL_CAPTAIN`,
when set, wins over it ([docs/captain.md](captain.md)).

---

## Accounts

*⚙ Settings ▸ Accounts* — or a click on the status line under *⚙ Settings* —
opens one page for the two logins the fleet leans on.

**AISquare** is on top. The card reads the session `aisquare login` stores and
shows who you are and when it expires. *Sign in* runs the same browser flow the
terminal runs — your one-time code and the link appear on the card, the browser
opens when one can reach you, and *Cancel* stops the wait — and *Sign out*
revokes the session exactly as `aisquare logout` does. A token supplied through
`AISQUARE_TOKEN` is shown as such and cannot be signed out from here.

**Claude Code** is below it: a table, one aligned row per account in priority
order — **↑**/**↓** first, then the slot, its name, who is signed in, the
five-hour and seven-day windows (a bar and the percent **used**, or one `!`
across the two when there is no reading to show) with **Log in** beside them
when a login is due, when each window resets, and the plan last. Hover the `!`
for what is wrong and what to do about it: `not signed in` with `fix: press Log
in …`, `the login Keychain is locked` with `fix: unlock the login keychain …`,
`could not reach the usage endpoint (URLError)` with `fix: check the network or
HTTPS_PROXY`. Where nothing done on this machine helps (an HTTP 5xx, an answer
aisquare cannot read), it says so: `fix: none from here, the next read tries
again by itself`. A value too long for its column ends in `…` (hover it for the whole
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
  token is missing, expired or rejected (slot 1 included) — the `!` beside it
  says which on hover. Type `/login` if Claude Code does not prompt you; the pane
  waits for a *fresh* login, not the stale one on disk, then closes, and the
  row reads its usage again. On a row that reads fine the same button says
  **Log in again**: the same flow, for a fresh login on a working slot, without
  `aisquare accounts run <slot>` and `/login` by hand. It does not clear a rate
  limit (below). `/login` typed in an agent's own fleet pane works too: on macOS
  the login goes to the Keychain, which each slot is read from (0.8.1, #263).
  Where the Keychain refuses the write, as in an SSH session, Claude Code keeps it
  in the slot's `.credentials.json` (mode 0600) instead.
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
  endpoint unreachable, an HTTP 5xx, a rate limit) keeps the account's last good
  figure on its row, whatever its age: the session cell says how old it is
  (`▮▮▯▯▯ 40% · 12m`) and its tooltip why the newer try read nothing (`reading
  from 12m ago · rate-limited until 14:32 …`); the line above says the try
  failed. A window of that reading that has reset since shows as `reset 1h ago`,
  never as the old figure. Only an account that was never read shows the `!`,
  whose tooltip says why and the fix (`HTTP 503 …`); a reading's own cell tooltip
  has the pace of the session window when there is one. `aisquare accounts usage`
  and `list --usage` print the kept reading's reason under the table (`account 2:
  rate-limited until 14:32 — logging in again does not clear it; showing the
  reading from 12m ago`), and an unread account's reason and fix in its row (`the
  stored token has expired; fix: press Log in, …`). In `--json` such a reading
  carries `cause` (`token_expired`, `keychain_locked`, `rate_limited`…) and `fix`
  (`null` where nothing done here helps).
- **A rate-limited read backs off.** When the endpoint answers HTTP 429 (or 503
  with `Retry-After`), the account is not asked again until the wait it named
  ends — or, when it named none, a minute, doubling with each 429 in a row up to
  half an hour, and starting over after a good read. The wait is recorded on
  disk, so every `aisquare` process, the page, **↻ Refresh usage**, `--refresh`,
  a headroom pick and a hand-over all honour it: no request goes out for that
  account before it ends. `aisquare doctor` lists the accounts backed off and
  until when (`claude-account-backoff`). Logging in again does not clear a rate
  limit: the wait is the signed-in account's, kept across a token refresh, and only
  another account signed into the slot is asked at once. In `--json` a reading that
  is not live says `"kept": true`. If you script usage, poll no more often than the cache's five minutes
  (`aisquare accounts usage --json`, without `--refresh`): every surface shares the
  one cached reading. To hear that an agent has hit a usage limit, run
  `aisquare watch` ([Watching the fleet](#watching-the-fleet)), which reports it with
  the rest of what needs you, rather than a loop of your own around `accounts usage`.
  A token the endpoint refuses shows the `!` at once (`the token was rejected (HTTP 401)`
  on hover), and so does an expired one (`the stored token has expired`). Nothing else
  on the page is affected. On macOS the
  token is read from the login Keychain, from the item Claude Code keeps for
  each account (plain and every managed slot), and from `.credentials.json` when
  there is no item. A locked Keychain, or one that refuses aisquare, is said on
  the row's `!` (`the login Keychain is locked`, `fix: unlock the login keychain
  …`) rather than offered a Log in. It is not asked again by itself (a locked Keychain's unlock
  dialog would come back every tick): opening the page, **↻ Refresh usage** or
  a Log in asks it again. A reset reads as a
  distance and a clock time — `resets in 3h 10m (18:00)`, `resets in 2d 4h (Tue
  02:00)` — the same string on the page and in `aisquare accounts usage`, so a
  weekly window six days out never looks like tonight.
- **The fleet at a glance** (#287). Under the usage bar, one line for every
  account at once: `Fleet: 58% left now · week: 1.8 of 3 accounts left · next
  weekly reset in 2d 4h (Thu 14:00) (account 3)`. *Left now* is the mean, over
  the usable accounts, of each one's fuller window (the share a spawn has to
  work with). *Week* is account-weeks: the sum of each account's weekly window
  left, out of the accounts whose week was read. The reset is the soonest
  weekly one. The status line under *⚙ Settings* carries the same *left now*
  figure beside the count, `● 3× claude 58%`, in the colour of its dot.
  `aisquare accounts usage` ends with an `all` row (the mean of each column,
  percent USED like the rows above it) and the same line; `aisquare --json
  accounts usage --summary` prints `{"accounts": [...], "summary": {...}}`, the
  summary with `percent_left`, `week_percent_left`, `weeks_left`,
  `week_left_by_slot` and `week_resets_soonest`. Plain `--json` stays the list.
- **Claude Code's status line** (#287, opt-in). `aisquare accounts statusline
  --install` sets `asq-statusline` as the status line of every account (or
  `--slot N`), in each one's `settings.json`, only where none is set. Inside
  every session it prints one line, `acct 3 · 5h 42% · wk 61% · fleet 58% left
  (3×) · ctx 34%`: the account's five-hour and weekly use, the fleet's share left
  and the session's context, shortened from the end to fit. The fleet's share is
  shown only while its file (`fleet.json`, which `asq` rewrites every minute and
  `aisquare accounts usage` on each run) is at most five minutes old, the usage
  cache's own age; after that the part is left out, and the account's name stays.
  A status line you set yourself is never replaced: `--install --wrap` keeps it
  and prints its line first, and `--uninstall` (or `aisquare uninstall`) takes out
  only what aisquare wrote, putting yours back as it was, byte for byte, from the
  copy of `settings.json` made at install. A command that cannot be read back
  from inside ours exactly as you wrote it is not wrapped: on Windows that is one
  with a `"` in it, a quoted path under `Program Files`; `--install --wrap` leaves
  it as it is, says so, and exits 0. `aisquare accounts statusline` alone says
  what each account has. The status line runs on every message, so it imports
  the standard library only, and it reads the session's JSON and prints its line
  in UTF-8 whatever the code page of a Windows pipe (it would otherwise raise on
  the `·`). The windows show for a Pro or Max login after the
  session's first answer; an API-key session shows the rest.
- **Sessions on this machine.** Each session carrying the status line keeps a
  record under `~/.aisquare/cache/statusline/`: its id, cwd, model, account and
  the figures above, never a prompt, deleted after a day. `aisquare accounts
  sessions [--since 15m] [--json]` lists every session that reported in the
  window, hand-launched ones and ones outside any project included: account,
  short session id, cwd, model, context, five-hour use, age, and the fleet label
  when a fleet spawn started it. The Accounts page lists the same under the
  accounts. The usage cache also takes each record's reading as a sample of
  that account, so the usage endpoint is asked less often.
- **↑**/**↓** and **Disable** arrange the accounts. The arrows set the
  **priority order** — the order the rows are listed in, and the order a
  headroom-based pick will try them in. Its first enabled, signed-in account is
  the **machine default**: what a launch runs on when nothing more specific
  says. There is no separate default to pick — move an account to the top to
  make it the default. *Disable* keeps a slot out of every automatic choice (its
  name is dimmed and the button reads *Enable*) while `--account <slot>` still
  reaches it by hand; *Enable* puts it back.
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
  machine default — the first enabled, signed-in account in priority order;
  with several, the one with room comes before it (*Headroom*, below). With no
  account signed in, a launch runs on whatever `claude` the shell already has,
  exactly as before any of this existed — and so does a role whose binding sets
  `CLAUDE_CONFIG_DIR` by hand (`team bind <role> --env`): neither rank 1 nor a headroom
  pick overrides it (a hand-over still moves it). The
  agent's info line and `fleet ls` name the account it was resolved to, and its
  sidebar row shows the slot (`#2`).
- **Headroom.** By default — `[accounts] pick = "headroom"`, *launches pick* in
  App settings — the machine default gives way to the account with room
  (`aisquare config set accounts.pick default` keeps it): every enabled, signed-in account's
  usage is read through the usage cache, from a reading up to a minute old, so
  a burst of spawns asks the endpoint once. When only one account could be
  picked, nothing is read: it is the answer. A hand-over (below) and `doctor
  --live` ask the endpoint now. The first account in priority order under `switch_at`
  (85 % by default) is taken — or, when all are over it, the one with the most
  left. An account counts as full as the fuller of its two windows, so one
  that has spent its week is not taken for an empty five hours. The endpoint's
  per-family weeks count too, for an agent of that family: Claude Code's Opus
  and Sonnet limits apply only to requests to that model family, so an account
  whose Opus week is spent is no room for an Opus agent and still is for a
  Sonnet one (the family is the session's model, or for a spawn the `--model`
  it runs: its own, its role's, or your last pick; with none, the account's own
  default, no family). A spent family week is no answer at all for that
  family, not even the least bad one a switch by hand would take. A window the
  endpoint marks locked reads 100 % used.
  `[accounts] prefer = "reset-soonest"` (*under the line, prefer* in App
  settings) takes, of the accounts under `switch_at`, the one whose week resets
  first, so an allowance is spent before it lapses; `priority`, the default,
  takes the first in rank order, rank 1 (the machine default) first. It orders the
  headroom pick alone, a launch's under `pick = "headroom"` and every hand-over's:
  under `pick = "default"` rank 1 decides, whatever `prefer` says (every `[accounts]` key:
  [Configuration keys](reference.md#configuration-keys)). `accounts usage` shows
  each week's reset and each family week beside it. Usage is the
  same best-effort endpoint as the bars, so an
  account that is backed off after a 429 is not asked, and an older reading kept
  on a row after any failed read is never taken for a live one: the pick uses it
  only if it falls inside the pick's own window with neither window reset since
  (and says so), else skips it with a note giving its age. The fleet's dot
  ignores a kept reading older than five minutes. An
  account that does not answer is skipped with a note and, when none answers,
  the machine default decides as before. Each reading is kept: the row's bar
  gains *≈ 40 min to the limit* once two readings of the same window exist.
- **Limits are remembered.** Every usage limit an agent hits leaves a mark on
  the account it was on until the reset the message named (*weekly limit ·
  resets Mon 12:00am*), and every pick by headroom — a spawn with `pick =
  "headroom"`, the automatic hand-over, a `fleet switch` without `--to` —
  skips that account until then, saying so: *account 3 skipped — weekly
  limit until Mon 00:00 (in 2d 4h), refused 10:42*. A hand-over or a `fleet
  switch` without `--to` never takes a marked account, its fallback rungs (the
  binding, the project default, the machine default) included: with nowhere
  else to go it refuses, and `handover_failed` says when the agent's own limit
  resets. A launch's choices are not second-guessed: `--account`, a role
  binding, a project default and `pick = "default"` still name the account, and
  when every account carries a mark a spawn falls to the machine default as it
  does when no usage can be read. `--to` always names the account. A session or weekly limit
  closes the account to every agent; an Opus or Sonnet limit to that family's.
  The endpoint may read an account under the line while it refuses, so the
  refusal is believed over the reading. A live reading that shows a family
  week spent leaves the same mark (*read* instead of *refused*), because the
  cache a spawn picks from keeps only the two shared windows.
- **A usage limit.** When an agent's turn ends on one (Claude Code's
  `StopFailure` hook, `You've hit your session limit · resets 12:30am`), its
  row turns **⏳ limited** with the reset time and a `limited` line goes on the
  board. When the automatic hand-over (below) will move it, the line is a
  `limited_handing_over` event that says *moving it to another account
  automatically* and wakes nobody; otherwise it
  names `aisquare fleet switch <label>` and the manager is woken. Claude
  Code's own wait-and-continue at the reset is left running. `fleet switch`
  stops the agent as `fleet stop` would and starts it again under the same
  label and task, in its worktree as it stands (the branch it was on, any
  uncommitted work), on the account with the most headroom (`--to` names
  one), **resuming its session** from its transcript (`claude --resume
  <path>`) when that file is on disk (a session resumed under another account
  keeps writing its original transcript while Claude Code reports a path under
  the new account that is never created, so the board keeps the file on disk,
  and the next restart or switch resumes it too) — the resumed agent keeps its task claims
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
  own wait intact, and says so with a `handover_failed` event naming `fleet
  switch` or the reset — that event, not `limited`, wakes the manager. One
  limit moves an agent once: `fleet switch` refuses a replacement the hand-over
  started within the last ten minutes (*moved 3 min ago by the usage-limit
  hand-over; `--force` moves it again*), and a successful `switched` is news on
  the next delta, not a wake-up. One that goes ahead says on the
  board what it could not do (a first line it could not type, claims it could
  not move) in a note beside `switched`, and `switched` itself says whether
  the replacement's first line reached it. `doctor` lists parked agents; `doctor --live` warns when every account
  is over the line.
- **Before the limit.** Off by default for this release: `[accounts] before_limit =
  "switch"` moves a fleet agent before its account runs out instead of after.
  When the agent's turn ends (the `Stop` hook), or Claude Code's idle notice says it has sat
  at its prompt for a minute, and its account reads at or over `switch_at` (85 % used by
  default, the line every pick counts as full), the same kind of detached hand-over as
  `on_limit = "switch"` moves it to an account under the line, resuming its session.
  The board says `switched … (before limit)` and the replacement is told it moved
  before its usage limit. It never happens mid-turn: an agent that is working, asking for
  permission or kept going by a note is left alone, and so is one whose turn left background
  work running, which the `/exit` of a move would kill — a background command, a `Monitor`,
  a background subagent or a skill forked in the background whose final notice (the one
  with a status; a Monitor's events have none) is not in its transcript yet, its
  subagents' transcripts included, until that work has reported (a `TaskStop` of it, or a
  Monitor's own timeout, ends it too) — and one
  with a message queued during its turn, which starts the next turn as this one ends. The
  move waits five seconds after the turn end, past the start-up of the hook a just-typed
  prompt runs, so that prompt is stamped on the board; then, before the stop and again once
  the agent is marked as being moved, it checks that nothing began since the turn end — no
  prompt stamped on the board, and no queued message taken, notice or reply in the
  transcript — and that the pane shows Claude Code idle at its prompt, and no background
  work. The pane is what catches a prompt typed in the last moment: Claude Code shows its
  spinner from the moment a prompt is submitted, while its hook runs and before the prompt
  reaches the board or the transcript. If anything has begun, the agent is left alone and
  its next turn end asks again. The manager and the
  captain are never moved before a limit (a limit still moves them). Nothing moves when the
  windows over the line reset within `wait_if_reset_within_minutes`, when no other account
  is under the line (the agent goes on where it is, and nobody is woken), for an agent put
  on its account by name (`--account` at the spawn or in the Spawn dialog, `team bind <role>
  --account`, or a binding that sets `CLAUDE_CONFIG_DIR` by hand), in a project `fleet
  pause` holds (on the board its agents read, the hub's under one), for a worker auto-stop
  is about to stop (it has finished: see [Capacity](#capacity); moved, it would run a whole
  turn on the new account before the stop), or for an agent started or looked at in the
  last ten minutes. The manager is woken only when a move that went
  ahead fails (`handover_failed`). `warn` makes no move: it puts one `before_limit` line on
  the board for each crossing of the line, naming the account, the account with room and
  the `fleet switch` that moves it; it is said again only once the account has read under
  the line or its windows have reset, and it is for you, not the agents (it reaches no
  teammate's prompt). No loop polls, and a turn end asks the usage endpoint nothing: the
  hook decides from what the usage cache already holds whether to look, and a worker
  detached from it does the reads, through the cache (the agent's account five minutes
  deep, a back-off honoured; the others a minute deep, as for a launch, which is what the
  move then picks from). With every other account over the line, turn ends read none of
  them. A kept reading (the last good figure of a failed read) moves nobody.
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
- **Memory, skills, agents, commands, CLAUDE.md.** Opt-in, `[accounts] share`
  (empty by default), turned on and off with `aisquare accounts share`:
  - `memory`: every launch, the plain claude's included, gets
    `--settings '{"autoMemoryDirectory": "~/.aisquare/claude-shared/memory/<project>"}'`,
    so a memory saved on one account is read on every other. The first
    `--enable memory` merges what each account already remembered there: nothing
    overwritten, a file that differs kept as `<name>.slot<N>.md`, `MEMORY.md`
    merged by line. The directory is outside every `.claude` path on purpose:
    since Claude Code 2.1.280 every auto-memory write into a linked `.claude`
    directory asks for approval nothing can give.
  - `skills`, `agents`, `commands`, `claude_md`: each managed account's entry
    becomes a link to the plain claude's (a junction on Windows, and a copy of
    `CLAUDE.md`), its own kept beside it as `<entry>.own-<stamp>`;
    `--disable` puts it back exactly. An entry already linked somewhere else is
    yours and is left alone. A launch on an account added since links it.
  - `plugins`: experimental. MCP logins and the plugin store are per account.
  - Transcripts are never shared. A linked `projects/` makes every auto-memory
    write prompt, and the shortest `cleanupPeriodDays` would sweep them all. A
    session moved to another account resumes by its transcript's path instead
    (verified on Claude Code 2.1.309:
    [cross-account resume](runbooks/cross-account-resume.md)).

  `aisquare doctor`'s `claude-accounts sharing` line says what is shared and
  warns about an account that does not share a part that is on, a link it
  did not make, or a `projects/` that is a symlink. A session is attributed to
  the account its fleet row launched it on, whatever its transcript's path says.

Nothing on this page writes into Claude Code's login: the email and plan
are read from what Claude Code recorded, and a token is never refreshed by the
CLI. Claude Code refreshes it on the account's next session. What the
accounts share (above) is the only thing written into an account's directory.

Everything the page does is a command, and every reporting one takes `--json`:

```sh
aisquare accounts list                 # slots, who is signed in, plan, hooks
aisquare accounts list --usage         # …plus the two windows, percent USED (read at most every 5 min)
aisquare accounts add                  # a fresh slot; Claude Code opens for the sign-in, then exits
aisquare accounts run 2                # a plain Claude Code session on account 2
aisquare accounts run 2 --model opus   # arguments after the slot go to claude
aisquare accounts usage                # the windows, per signed-in account, then the fleet's; --refresh reads now (a rate-limited account waits out its Retry-After)
aisquare --json accounts usage --summary # {accounts, summary}: the list beside the fleet's aggregate
aisquare accounts statusline --install # Claude Code's status line on every account, where none is set (--wrap keeps yours)
aisquare accounts statusline --uninstall # takes out only aisquare's (a wrapped one goes back)
aisquare accounts sessions --since 1h  # every session on this machine that reported in the last hour
aisquare accounts remove 2             # the directory is kept as 2.removed-<stamp>
aisquare launch coder --account 2      # a board role on account 2
aisquare fleet spawn coder --account 2 # a fleet agent on account 2
aisquare accounts default 2            # the machine default: moves slot 2 to rank 1 (= accounts move 2 top)
aisquare accounts default 3 --project . # this project's default (codename, name, id prefix, or . for here)
aisquare accounts default 1 --role coder # coders run on the plain claude (= team bind coder --account 1)
aisquare accounts default              # the three levels, as they stand
aisquare accounts alias 2 work         # a name: --account work, [work] on the board
aisquare accounts order work 3         # the priority order; the rest follow as they were
aisquare accounts move 3 top           # up · down · top · bottom
aisquare accounts disable 3            # out of automatic selection; enable puts it back
aisquare config set accounts.pick default      # spawns stay on the machine default (headroom is the default)
aisquare config set accounts.on_limit wait     # an agent that hits its limit stays parked (switch is the default)
aisquare config set accounts.before_limit switch # move an agent at its turn end once its account is over switch_at (off is the default; warn only says so)
aisquare config set accounts.prefer reset-soonest # under the line, the week that ends first (priority is the default)
aisquare accounts trust                # trust this folder on every account (its repository, in a checkout)
aisquare accounts trust --sync         # every folder any account trusts, trusted on all of them
aisquare config set accounts.share_trust false # launches stop copying a folder's trust (on by default)
aisquare accounts settings             # what each account's settings.json is: shared, its own, none
aisquare accounts settings --sync      # link every managed account's settings.json to the plain claude's
aisquare accounts settings --unshare   # each linked account gets its own copy back
aisquare config set accounts.share_settings true # keep them linked at every launch (off by default)
aisquare accounts share --status       # what every account shares: memory, skills, agents, commands, CLAUDE.md
aisquare accounts share --enable memory,skills   # one memory per project and one skills/ for every account
aisquare accounts share --disable skills         # each account gets its own skills/ back, as it was
aisquare fleet switch coder-auth       # move one now: resumes its session on the account with room
aisquare fleet restart manager         # an exited (or stuck) agent, back under its label, session resumed
                                       # …launched as it was first launched: the row records the
                                       # binary, permission mode, arguments and account (#144)
aisquare fleet restart coder-auth --permission-mode acceptEdits   # …another mode; later restarts keep it
aisquare fleet switch coder-auth --to personal --fresh   # a named account; a new session + hand-off
aisquare fleet switch coder-auth --force # again, minutes after the usage-limit hand-over moved it
```

`add` and `run` hand the terminal to Claude Code, so they have no `--json`
form and `add` refuses outside an interactive terminal. `aisquare doctor` gains a
`claude-accounts` line naming any slot that still needs a sign-in, a
`claude-account-default` line when the default is not signed in or is disabled,
and a `claude-account-bindings` line when a role or project names an account the
machine no longer has. A `role bindings` line checks every binding that points a
role at another Claude Code directory: its `CLAUDE_CONFIG_DIR` must hold aisquare's
hooks (the same "connected" Welcome asks), or its sessions never reach the board,
and its `CLAUDE_CODE_TMPDIR` must be a writable directory. Each value is expanded as
a launch expands it (`$VAR`, `~`). The line warns with the fix, quoted for your shell
(`aisquare agents connect claude-code --config-dir <dir>`, or `aisquare team bind <role>
--unset KEY`), and is absent when no binding sets either variable. Accounts laid
out some other way — a wrapper, a proxy, a directory of your own — still bind
to a role as a launch profile (`aisquare team bind coder1 --env …`, see
[Several accounts, one team](orchestration.md#several-accounts-one-team)).

---

### Adopting a session

A conversation you started by hand, `claude` in a terminal, can join the fleet
and keep going there:

```sh
aisquare fleet adopt --list            # the recent ones no fleet agent owns, newest first
aisquare fleet adopt 0636…e0 --role coder --label coder-login
```

The list shows each conversation's short id, account, directory, how long ago it
was last written and the first thing you typed (`--limit N`, `--json`). Adopting
one starts a fleet agent that resumes it (`claude --resume <transcript>`) on the
account whose directory holds its transcript and in the directory it ran in, on
that directory's project. It keeps its session id, so the board row is the
conversation's own. It is refused, with the reason, when:

- a `claude` is still running it (quit that one first: two processes would write
  one conversation);
- a fleet agent already owns it (restart that agent instead);
- its directory is no onboarded project (`aisquare project onboard <dir>` first);
- its transcript is not under one of the CLI's accounts (its slots and the plain
  claude's `~/.claude`). A Claude directory the CLI did not create is never
  adopted (decided with the owner, `docs/plans/claude-accounts.md`).

In `asq`, the Accounts page's *Adopt a conversation…* (under *Sessions on this
machine*) shows the same list: one a `claude` still runs, or whose directory is no
onboarded project, says so and cannot be picked; Enter on another adopts it.

Nothing lists or adopts conversations on its own. The manager offers the list
only when you ask what you were working on in Claude outside the CLI.

### Fresh start without losing accounts

To start over without signing every account in again:

1. `aisquare config reset` puts every setting back to its default (the old file
   is kept beside it as `config.toml.bak-<stamp>`).
2. `aisquare project prune` (or `project forget <id>`) drops the projects you are
   done with; the sidebar's *Remove from fleet…* does the same for one.
3. Leave `~/.aisquare/claude-accounts/` alone. Each slot's directory holds its
   login (on macOS, a Keychain item keyed by that exact path), and deleting
   `~/.aisquare`, or moving that directory, costs a fresh sign-in for every one.

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
| reviewer | `auto`, denied Edit, Write and NotebookEdit | the deny is the role's own, merged into the one `--disallowedTools` every role gets ([Tools and skills per role](#tools-and-skills-per-role)), so `fleet spawn`, `team spawn` and `aisquare launch reviewer` all have it, and an `extra_args` you set no longer replaces it; `allow_tools = ["Edit"]` is the one way to give a tool back. It keeps Bash, which its whole cycle runs on and `--restricted` would take, and loses the tools that edit files, so a prompt injected through the PR it reads cannot edit the tree with them. Its shell is not read-only: `sed -i`, `git push` or `gh pr merge` injected through a PR meets only its briefing and `auto` mode's check of each action, and no allowlist of commands would hold (its inline comments are `gh api` calls, as a merge is, and verifying a finding runs the PR's code). Where that matters, run it with a `gh` token that cannot write the repository's contents, which can comment but not push or merge, and protect the branch: [A reviewer that cannot push](#a-reviewer-that-cannot-push). A config saved by 0.8.0 to 0.8.2 holds that release's default, `extra_args = ["--disallowedTools=Edit Write NotebookEdit"]`, and one saved by 0.6.0 or 0.7.0 holds `["--restricted"]`; exactly that list loads as today's default (none), once: the next save writes the file's `config_version` at its top, and from then on a list set there is yours and kept. A reviewer 0.6.0 or 0.7.0 started recorded `--restricted` in its launch spec, and `fleet restart`, `fleet switch` and Restart replay today's arguments in its place, with `--disallowedTools=Edit Write NotebookEdit` beside them where they do not deny those tools: the spec cannot tell that release's default from a `--restricted` its spawn was given, so the replay errs toward the reviewer without them. One recorded by 0.8.0 to 0.8.2 leads with that release's default deny, which a restart drops, since the role's own deny holds; so `allow_tools` reaches a restarted reviewer too. Any other list stays as written |
| ui-tester | `auto` + `--chrome` | `--chrome` comes from the role, not this table: `aisquare launch ui-tester` adds it for the default `claude` binary wherever the role starts, and `--no-chrome` in `extra_args` or on the command turns it off. Its "read-only" is a briefing instruction, NOT construction — the row above says so, and the sentence in the coder/tester cell applies here too: nothing here writes or passes an allowed-tools list, so a PreToolUse allowlist (plan §3.6, not in this checkout) is what would enforce it |

The mode is passed straight through to `claude` and nothing here reads its
answer, so where `auto` is unavailable to the account the refusal appears in
that agent's own pane and the spawn does not fall back: pick another mode
yourself, per spawn (`--permission-mode acceptEdits`) or for the role
(`aisquare config set fleet.roles.coder.permission_mode acceptEdits`).
`bypassPermissions` is available and is never a default.

**`auto` behind the explainability proxy.** The classifier auto mode relies on
makes a separate, non-streaming request the proxy currently cuts short once the
session is large — and a fleet agent's very first request is already the whole
system prompt, tool schemas and skills (~140k tokens on a machine with several
MCP connectors), so every tool call is refused with *"… is temporarily
unavailable (server error), so auto mode cannot determine the safety of Bash"*
while the chat itself keeps working. `aisquare explainability doctor` says so (`explainability
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

- **per spawn** — `fleet spawn … --permission-mode acceptEdits --no-worktree --bin claude2`;
- **in config** — `aisquare config set fleet.max_agents_per_project 6`, or
  `aisquare config set fleet.roles.coder.permission_mode acceptEdits`, or edit
  the file directly;
- **in the UI** — *⚙ Settings ▸ App settings* writes the same config: its *Agent
  settings* tab holds these, and *User preferences* how `asq` shows things (`[ui]`)
  and whether it looks for a new release (`[updates]`);
  its third tab, *Remote*, is Remote control. Each tab shows the few settings a new
  user needs — *Agent settings* the *Captain (experimental)* box, how launches pick
  an account and what a usage limit does — and folds the rest under **Advanced**
  (click to open): the roles, the fleet and the rest of the accounts' rules here.
  A *Save* refused for a folded field opens it. Its *Reset all settings to default*
  is `config reset`, and the Captain box follows it, as it follows *Reload*. Its
  *Save* writes only the settings changed on the first two tabs, so one changed in a
  shell while it was open is kept; *Remote* and the Captain box act at once. A
  project's own *Settings* tab holds only that project's nickname.

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
(`accounts.pick`, `accounts.on_limit`). A move back to 0.8.2 or earlier also names
each role that release would launch with tools its safety list denies here: those
releases deny a role only what its `extra_args` say, and this one leaves the
reviewer's empty, so a reviewer there could edit the tree it reviews. The warning
gives the one `config set` that keeps the deny there. It is said only by `aisquare
upgrade`: an older install that shares `~/.aisquare` without going through it
leaves no trace this one can read.

```sh
aisquare config list --overrides   # what you changed, and nothing else
aisquare config reset              # every setting back to its default; asks first, --yes off a terminal
```

The keys that need more than their name are listed in
[reference: configuration keys](reference.md#configuration-keys).

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
max_agents_total = 0                      # live agents across every project, captain excluded; 0 = no budget
min_free_mb = 2048                        # refuse a spawn below this much available memory; 0 = off
test_gate_mb = 0                          # `fleet gate`'s floor; 0 = min_free_mb
test_gate_wait_minutes = 15               # how long `fleet gate` waits for it
auto_stop_finished = true                 # stop finished workers of these roles by themselves
auto_stop_idle_minutes = 5                # … once idle this long
auto_stop_roles = ["tester", "reviewer", "validator", "ui-tester"]  # coders opt in
pane_clipboard = "off"                    # "on": a program in a pane may set your clipboard (OSC 52)
busy_quiet_seconds = 30                   # a "working" row whose pane printed nothing this long is
                                          # checked once: an idle Claude Code prompt reads waiting
deny_tools = ["Artifact", "ArtifactComments", "Workflow", "ReportFindings",
              "ShareOnboardingGuide", "PushNotification", "ScheduleWakeup", "SendFeedback"]
                                          # every role launches without these, wherever it starts

[fleet.roles.manager]
permission_mode = "auto"                  # any Claude Code mode; "" = pass no flag
worktree = false
extra_args = []                           # fleet windows only; `launch` and `team spawn` never read them
deny_tools = []                           # added to [fleet] deny_tools and the role's own safety list
allow_tools = []                          # taken back out of that set: the one way to give back a safety-listed tool
skill_listing = "full"                    # "full" | "names" | "off"; every other role defaults to "names"

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
extra_args = []                           # its Edit/Write/NotebookEdit deny is the role's own, not this
board_context = "none"                    # judges a change cold: no recent updates, no deltas

[fleet.roles.ui-tester]
permission_mode = "auto"
worktree = false                          # tests the running app, not a tree of its own
extra_args = []                           # --chrome is the role's own default; put "--no-chrome" here to drop it

[fleet.roles.validator]
permission_mode = "auto"
worktree = false
extra_args = []
```

A role may also name a default persona — every spawn of that role runs as it
unless `--persona` names another:

```toml
[fleet.roles.coder]
persona = "minimalist"                    # checked at spawn: a name the project lacks refuses, naming this key
```

Every role table also takes `deny_tools`, `allow_tools` and `skill_listing`, shown
once under the manager: [Tools and skills per role](#tools-and-skills-per-role). A
role or a key your file leaves out takes its default above; a role the table above
does not name gets `auto`, no worktree, no extra args, the `[fleet]` deny list and
skill names only. A config file that will not parse costs you the customisation and
never the fleet: the defaults apply and nothing refuses.

**Role bindings are in App settings too** (#98). Its *Role bindings* section, the last
under *Agent settings ▸ Advanced*, lists every role `team bind` bound: its binary, its
args, and its environment, with `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_TMPDIR` shown with
their values and any other variable by name only, since a value may be a token.
- *Edit* opens the bind form on the binding as `config.toml` holds it at that moment,
  so a `team bind` run in a shell since is never written away. Each environment
  value shows as `••••` until you turn on *Show values*. A value left masked is
  saved as the one stored; type over it to change it. Its *Save* replaces the
  binary, environment and args, which `team bind <role> --clear` followed by a
  fresh `team bind` would also do. Variables bound to a file and the account stay
  as they were.
- *Unset key…* is `team bind <role> --unset KEY`.
- *Clear* (it asks first) is `team bind <role> --clear`.
- *+ New bind* makes one.

Each of these writes at once, apart from the page's *Save*, through the same writer
`team bind` uses. The section re-reads the file after each write, on *Reload*, and
whenever the page is shown again, so a binding made in a shell shows without
restarting `asq`.

### Capacity

A spawn is admitted against three limits, in this order, each checked before any
window opens:

1. **`max_agents_per_project`** (4): the project's live agents.
2. **`max_agents_total`** (0, no budget): live agents across every project, the
   captain excluded and managers counted. The refusal names each project's count.
3. **`min_free_mb`** (2048): available memory. Below it, or with the kernel's
   memory pressure at critical, the spawn is refused with both numbers; at warn
   pressure it is admitted with a note on the receipt. After a memory refusal
   the floor is `min_free_mb + 512` for five minutes, so admission does not flap
   on a machine hovering at the line. `0` turns the floor off.

Available memory is `MemAvailable` from `/proc/meminfo` on Linux, with PSI
(`/proc/pressure/memory`, `some avg10`: warn at 10, critical at 40) when the
kernel has it; on macOS it is `vm_stat`'s free + inactive + speculative pages,
and the pressure is `kern.memorystatus_vm_pressure_level`, which wins because
that figure ignores the compressor. Under WSL2 these are the VM's numbers,
capped by `.wslconfig`. Where neither can be read, admission admits and says so.

```text
aisquare fleet capacity [--json]
```

shows the headroom, the pressure, the floor in force, and the live agents
against each limit, and whether the next spawn would be admitted; `aisquare
doctor` has the same as its `fleet capacity` row. Every refusal, and every
admission that fills the budget, is a line in `~/.aisquare/fleet/admission.jsonl`
(rotated at 1 MB).

**The test-run gate.** A heavy run (the full suite, a build) can wait for memory
instead of pushing the machine into swap:

```text
aisquare fleet gate [--min-free-mb N] [--wait 15m] [--json] [-- CMD …]
```

It polls every 5 s. With the headroom it runs `CMD` and exits with its code
(with no `CMD`, exits 0); at the deadline it runs nothing and exits **75**,
`not run: <available> MB available, <N> MB needed for <wait>`. The floor is
`--min-free-mb`, else `test_gate_mb`, else `min_free_mb`; the wait is
`test_gate_wait_minutes`. Once `test_gate_mb` is set, the tester's and the
validator's cycles tell them to run heavy checks through it.

**Auto-stop of finished workers.** A tester that posted its verdict or a reviewer
whose review is in keeps a seat, its memory and a slot of `max_agents_total` until
something stops it. With `auto_stop_finished` (on by default) the fleet stops a
worker of a role in `auto_stop_roles` once it has finished. All of these must hold:

- it reads `waiting`, not `working`, `attention` or `limited`;
- it has been idle `auto_stop_idle_minutes`, by its board row and by its pane's
  output;
- it holds no claim;
- the task it was spawned for is closed, or, spawned for none, it wrote a `result`;
- nothing addressed to its label or role waits unread;
- nothing is queued that its cycle would take (the review pool for a checking
  role);
- its input box is idle and empty, so your half-typed draft is never killed with it.

Three things act on it, with no daemon. A worker's own Stop starts a short-lived
background check that waits out the idle window, asks again and stops it; a later
turn supersedes that check. `aisquare fleet reap --finished` does the same on demand
(`--dry-run` lists without stopping). And a spawn that `max_agents_total` or the
memory floor refuses stops the finished workers first and tries once more, saying
which it stopped on the receipt. The exit is an `agent_exited` with actor
`auto-stop` and the reason (`reviewer-1 exited (0) — stopped by auto-stop (it
posted its result)`). It wakes nobody: the manager reads it with its next update.
Coders are not in the default list, because a coder in review may be reopened; add
`"coder"` to opt in. A manager or the captain is never stopped.

### Watching the fleet

A manager that watches panes, CI or pull requests with its own turns (a Monitor, a
`sleep` loop, a `gh` poll) pays for its whole context on every look, and most looks
find nothing; a Monitor also expires after 30 minutes at most. `aisquare watch` is
one plain process that looks instead, and wakes a manager only for something new:

```text
aisquare watch [--interval 60] [--once] [--json]
aisquare watch --detach | --stop | --status
aisquare watch show [<path> | --last] [--json]
```

Every `[watch] interval_seconds` (60) it looks at:

- **what needs you**: the same scan as the phone's *Needs you* cards, the phone's
  dismissals honoured. That covers a permission or question dialog, an agent asking,
  a usage limit, a crashed, lost or failed agent, and a manager or fleet down;
- **stale agents**: the captain queue's rule for an agent that shows no sign of life;
- **pull requests**, with `[watch] github = true` (off by default, since it leaves the
  machine): each project's GitHub repository (its `origin`) through `gh api`, every
  `github_every_seconds` (120). Each request carries the last ETag, so an unchanged
  answer is a 304 that costs no rate limit and makes no further call. It reports
  `pr_approved`, `pr_changes_requested` and `pr_checks_done`;
- **your own sources**: `[watch.sources.<name>]` with `command = [...]` and
  `every_seconds = 300`. A command prints JSON lines `{"id", "project", "kind",
  "summary"}`; `project` is a codename or an id. aisquare ships no integration with any
  outside service: a source for an inbox is your own script. One that fails is said
  once per streak while the others carry on.

**What it delivers.** Each item has a stable id, kept in `~/.aisquare/watch/state.json`,
so the same news is never new twice and a new prompt is a new item. For each project
with something new, the full items go to `~/.aisquare/watch/<project>/<UTC stamp>.json`
(mode 600: pane excerpts stay on this machine). Then one board line of kind `watch`,
addressed to the manager, carries the counts and that path and never an excerpt:
`watch: 3 new (2 permission, 1 pr_approved): aisquare watch show <path>`. The manager
then gets one nudge. A working manager is handed the line when its turn ends, because a
line addressed to it wakes it. With `[watch] auto_stop` (on), each look also stops the
workers that have finished (see *Auto-stop* under [Capacity](#capacity)).

**One watcher per home.** `--detach` starts it in the `asq-watch` session of the fleet's
own tmux server and returns; a second refuses and names the first. `--status` says
whether one runs and how long ago it last looked, and `aisquare doctor`'s `fleet watcher`
row says the same, and warns when a running watcher has not looked for three intervals.
`--stop` ends it, and so does a `fleet shutdown` that leaves no agent live on the
machine: it watches every project, so one project's shutdown leaves it running.
`--once` looks once, delivers and exits, which suits a cron line. Beside a running
watcher's look it waits a moment, then exits 1 with `busy` (`{"error": "busy"}` under
`--json`) rather than deliver the same news twice.

### Tools and skills per role

Every tool schema and the skill listing ride every request of every agent, for the
whole session. So each role launches with only what it uses, wherever it starts:
`fleet spawn`, `team spawn` or `aisquare launch <role>` (#280). The tools a launch
denies are, in one `--disallowedTools` token, sorted:

- the role's own safety list: Edit, Write and NotebookEdit for the reviewer, none
  for the others;
- `[fleet] deny_tools`: by default eight tools no role's cycle and no aisquare
  command uses (Artifact, ArtifactComments, Workflow, ReportFindings,
  ShareOnboardingGuide, PushNotification, ScheduleWakeup, SendFeedback). The board,
  `remote` and `fleet tell` do what Artifact, PushNotification and ScheduleWakeup
  would;
- the role's `deny_tools`;
- less the role's `allow_tools`, the only way to hand back a safety-listed tool;
- plus every `--disallowedTools` already on the command line (a binding's `--arg`,
  a fleet `extra_args`, or what you type after `--`). `allow_tools` never hands
  those back. Every one of them comes out of the line and goes into the one token.

A launch at `ultracode` effort keeps `Workflow`, because ultracode is xhigh plus
workflow orchestration, and the launch says `Workflow kept: effort ultracode
orchestrates workflows`. That covers an `--effort ultracode` on the line and the
effort `team spawn` resolves (`AISQUARE_EFFORT=ultracode`). An agent put at
ultracode by Claude Code's own settings is not seen from here: give its role
`allow_tools = ["Workflow"]`.

`skill_listing` sets how much of Claude Code's skill listing a role carries:
`full` (the manager's default), `names` (everyone else's: every skill name, with
its description cut to nothing by `--settings '{"skillListingMaxDescChars": 1}'`),
or `off` (the `Skill` tool denied). A line that already passes `--settings` is
never merged with: the launch says `skill_listing withheld: --settings already
given` and runs as asked. All of it applies to Claude Code alone. A role bound to
another agent gets none of it, and the launch says so.

Measured with `/context` in fresh interactive sessions on Claude Code 2.1.309
(Opus 5.5), which is how fleet agents run:

| Session | System tools | Skills | Context at start |
| --- | --- | --- | --- |
| no flags | 21.6k | 2.5k | 26.9k |
| coder: the eight denied, skill names | 7.4k | 100 | 9.9k |
| reviewer: Edit, Write and NotebookEdit too | 6.8k | 100 | 9.4k |
| `skill_listing = "off"` | 6.8k | none | 9.3k |

Every request of the session carries that. `skillListingBudgetFraction` at its
smallest cut the listing by about 40 tokens on that release, so `names` uses
`skillListingMaxDescChars` instead. `off` takes the `Skill` tool too; no role
defaults to it, because then no agent can invoke a skill at all.

`aisquare doctor` prints one `<role> tools` row per role with what it denies and
its listing. Claude Code does not report a name it has no tool for, so a typo in
`deny_tools` denies nothing, and the row is where that shows. A restart that
changes a running agent's deny set pays one uncached first request: the tool list
is part of the cached prefix.

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
(`~/work/api`, `~/oss/api`). To see every project's directory under its name, turn
on *Always show project directory* in *⚙ Settings ▸ App settings ▸ User
preferences* (`aisquare config set ui.always_show_project_path true`). Saved there,
it shows at once; set from a shell while `asq` runs, it shows after that page's
*Reload* or at `asq`'s next start, since `asq` does not re-read the file every refresh.

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

*Start fresh…* — on the menu, just above *Remove from fleet…*, and on the
project's *Settings* tab — clears the project for its next piece of work, as
`aisquare project fresh` does: its agents stopped, their worktrees removed, its
board (with its work briefs and their evidence) and fleet rows cleared (task and
brief numbers start at #1 again), the manager's state file deleted, and a new
blank manager started. The dialog reads the plan first and shows its counts,
with three extras off by default that redraw it: *Project memory (context
entries)*, *Prompt log & metrics* and *Fleet git branches*. Work a
tree or branch holds that no remote has is listed in a second dialog, *Discard
and start fresh* or Cancel; a refused plan (a board another project's agents
read) says why and cannot be confirmed. Cancel has the focus, and Esc cancels.

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
context, prompts, boards and explainability settings stay per
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

**tmux.** Session `asq-<codename>`, always targeted exactly (`=asq-amber-otter`)
so `asq-ruby-fox` never prefix-matches an `asq-ruby-foxhound`. Window name =
agent label; panes are addressed by `%id`, never by name, so renames are
harmless. A row stores the pane id and *derives* the session name from the
current codename — a rename never strands an agent.

**Agent labels.** `manager` — always, one per project; a second is refused.
Everything else matches `^[a-z][a-z0-9-]{1,23}$`: 2–24 characters, lowercase
letters, digits and `-`, no `.`, `:` or spaces. The manager is briefed to make
labels descriptive (`coder-auth`, `tester-py311`, not `coder-2`) and told the
24-character limit. Labels are
unique per project among **live** agents — an ended agent frees its label. A
collision appends `-2`, `-3` … and the receipt names the label actually used
(`✓ spawned coder-auth-2 (asked: coder-auth)`) rather than failing: a manager
mid-plan must not stall on a name. Without `--label`, the default is
`<role>-<task short id>` when there is a `--task`, else `<role>-1`.

No label the fleet makes is longer than 24 characters. Before a suffix (`-2`,
`-3` …) or a task's short id, the label or role it follows is trimmed so the
whole fits: a live `coder-authentication-ref` (24) makes the next one
`coder-authentication-r-2`, and a role bound as `platform-security-auditor`
spawned for a task makes `platform-securi-01k9q8p3`, the short id kept whole.
A fork with no label is numbered and trimmed the same way. Only the length
is fitted: a role bound under a name the rule would refuse as a
`--label` (`data_eng`) keeps its own characters (`data_eng-1`). An over-long
`--label` is refused with a label that fits — `label
'coder-authentication-refactor' is 29 characters — at most 24; try
'coder-authentication-ref'` — so it can be retried as it stands.

**Branches and worktrees.** A coder's branch is
`fleet/<codename>/<task-short-id>-<slug>` — `fleet/amber-otter/01k9q8p3-wire-auth`:
the codename groups one fleet's branches and tells two machines apart on one
remote; the slug is the task title lowercased, runs of anything but `[a-z0-9]`
collapsed to `-`, clipped to 32 characters. A spawn without a task uses
`fleet/<codename>/<label>`. Its worktree lives at
`<repo>/.aisquare-worktrees/<label>`, excluded through `.git/info/exclude`, and
shares the principal repo's board and context for free — that is the
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
extended keys on so shift+enter and friends reach Claude Code, mouse on (so
the wheel scrolls Claude Code in `fleet attach`; the UI sends its own mouse
reports straight to the pane and is unaffected), the clipboard off unless
`[fleet] pane_clipboard` says on, monitor-activity on. One server for every
project; one session per project; one window per agent. A changed setting
applies to the next server started. Agents survive an update of aisquare:
`aisquare launch` hands each pane to the agent's own program, so no aisquare
code runs in a pane, and asq's ⬆ *Restart & update* reattaches them after it
reopens, restarting any whose pane is gone ([Updating from asq](install.md#updating-from-asq)).

**The typing lease.** When you type into an agent's pane in `asq`, it touches
`~/.aisquare/run/typing/<socket>-<server>-<pane>`, at most once a second.
`<server>` is a digest of the socket's real path, so another `asq` server at
another path, whose pane ids repeat this one's, is never held by it. For `[fleet]
draft_quiet_seconds` (10) after your last key, no fleet write types into that
pane: not a nudge, a tell, a typed first message or the Remote's paste. The
input box is read too, so a draft you leave there holds them off for as long as
it stays (see [`fleet tell`](#fleet-tell)). Typing through `tmux attach` leaves
no lease, and the input box covers it.

**Each agent is asked of its own server.** A spawn records the socket its
server says it listens on, and every fleet command — `fleet ls` and the UI,
`tell`, `stop`, `restart`, `switch`, `reap`, `shutdown`, and the doctor — asks
the agent's server there (`tmux -S <path>`) when that is not where `tmux -L
asq` looks from the asking shell. Under another `TMUX_TMPDIR`, `-L` reaches
another server on the same name, or none, and every server numbers its panes
from `%0`, so its panes would answer for the agents' ids: a dead one there read
as a running agent's exit and ended its row. A row from before 0.8.0 recorded
no socket and is asked by name. The Settings tab's attach line names the
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

The captain (experimental, [docs/captain.md](captain.md)) is the one exception, both
ways: its own row reads the home board, and its window is given the home's root,
whatever hub started it; and a seat it spawns reads that seat's own project's board,
since the captain's processes carry the home as their hub only to land the captain
itself there.

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

**Images.** Claude Code takes an image three ways, and each works in `asq`:
- **Drag a file onto the window.** Your terminal hands it to `asq` as a paste of the
  file's path, quoted the way that terminal quotes it. It goes to the agent on screen
  whether the pane or the sidebar had the keyboard, and the pane takes the keyboard
  with it. A text field or a dialog keeps its own pastes. With no agent on screen,
  nothing is sent and one toast says "nothing to paste into — open an agent first";
  with more than one live pane, it says how many and to click the one you mean, then
  paste again. Under WSL, a drop that is one Windows path (`C:\…`) of a file that
  exists becomes its Linux path through `wslpath -u`, asked off the UI thread so asq
  never waits on it; every other path reaches Claude Code exactly as the terminal
  sent it.
- **Ctrl+V** pastes an image from the clipboard (Alt+V on Windows and WSL; Cmd+V in
  iTerm2). The key reaches the pane, and Claude Code reads the clipboard itself,
  which is why each spawn carries this shell's display variables (below).
- **Type the file's path** in your message.

Claude Code's own Remote Control ([remote](remote.md#claude-codes-own-remote-control))
is another way: the Claude app can attach a photo to the session. To record what your
terminal sends on a drop or a paste (#288), start `asq` with
`AISQUARE_PASTE_LOG=/tmp/paste.log`: each paste adds one line, with its length, its
first 80 characters and the widget that had the keyboard. The log is off by default,
because a paste may be a secret.

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
*Passthrough:* the server keeps `set-clipboard off` (unless `[fleet]
pane_clipboard = "on"`, which lets a program in a pane set your clipboard with
OSC 52 in `fleet attach`) and no `allow-passthrough`, and it does not matter for
the UI. The only client the UI attaches is a control-mode client (see *The UI's
pipe to tmux* below), which has no terminal, so an OSC 52 copy, an OSC 8 link or
an OSC 9 notification from Claude Code has no terminal to reach; copies arrive
through the pane (Textual selection, and the paste-buffer mirror below) and
links are plain text. In `fleet attach` your own terminal is the client and
tmux's defaults apply. *Multiple clients:* `window-size manual` is deliberately
absent (it crashes tmux 3.4); the UI pins each window it shows with
`resize-window`, and a window nobody shows takes tmux's default `latest`
behaviour — an attached `fleet attach` client resizes it to its own terminal.
The UI's own control client attaches with `ignore-size` and is never given a
size, so it resizes nothing.
*Rendering:* `default-terminal tmux-256color` with truecolor overrides, so
italics, dim and 24-bit colour survive `capture-pane -e`; wide glyphs are
measured by Rich's cell widths against tmux's, which agree on emoji and CJK;
the cursor's shape is not reported by `display-message`, so the pane draws a
block. *Two-key sequences:* each key is one `send-keys`, sent in order down
the control client's pipe (or, polling, one process each), so `ctrl+X ctrl+K`
and vim's `gg`/`dd` arrive in order. *The UI's pipe to tmux:* the pane on screen
renders and takes input over ONE `tmux -C` client per server. It is attached to
that pane's session, and every other pane there is paused for it, so a hidden
agent sends it nothing and keeps running. It never changes which window is the
session's current one, so a `fleet attach` open on the same project stays where
you left it. tmux counts it as an attached, focused client, like a `fleet attach`
client on that project. With `focus-events on`, the agent in the project's
current window is told it has focus while `asq` shows that project, and that it
lost focus when you move to another project or quit `asq`. A key reaches the screen in about a
millisecond (it was 60 ms or more polled). On tmux older than 3.2, or if that
client cannot start or ends, the pane polls tmux as before, without a word on
screen; keys typed as it ended arrive once each, in order. If tmux will not say
which of those keys it ran, none of them is sent again and nothing typed since is
sent: the pane says `(input not confirmed)` and a warning names the agent, so check
the pane and type again what is missing. *Escape hatch:* `[fleet] escape_key` (default `f12`) is
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
left-button release the UI reads tmux's buffers and copies one written since the
press to your clipboard by the route its own copies take (*Where a copy goes*,
below) — the same words selected twice included, and a copy that lands as late
as about 0.65 s after the release, unless you press a mouse button first. Claude
Code's selection
is copy-only by design: there is no "select in the prompt and paste over it"
in Claude Code in any terminal. While such a program owns the mouse the UI's
own drag-select stands down; **shift+drag** is the one gesture that always
selects locally, in every pane, and copies on release. In a pane whose program
does not track the mouse (a shell, the classic renderer) nothing changed:
Drag to select text in a pane (double-click selects a word): it is copied to
your clipboard on release (*Where a copy goes*, below), and ctrl+c copies it
again while the highlight stands — from the pane or from the sidebar — and then
clears it (when two panes hold a highlight, the one made most recently goes
first). cmd+c does the same in a terminal that passes it on; Terminal.app and
iTerm2 keep it as their own Copy (*Copy and paste on macOS*, below). Only a
left-button drag is a copy, so a right-click over a highlight
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

**Where a copy goes.** Every copy the UI makes — a release in a pane, ctrl+c on
a standing highlight, the mirror of Claude Code's own selection, the board's
`c`, the Remote panel's link, a text field's copy — goes two ways at once. On a
local session it goes to the system clipboard through the platform's own tool:
`pbcopy` on macOS; `clip.exe` on Windows, and on WSL while interop is on; and
on Linux (WSLg included) and the BSDs `wl-copy` under Wayland, else `xclip` or
`xsel` under X. A tool that fails or hangs is not run again for a minute, so
a broken one (an `xclip` whose display has gone) doesn't hold up every copy's
toast: those copies go by OSC 52 alone. It is also sent as OSC 52, which your *outer*
terminal turns into a clipboard write if it accepts it: Windows Terminal,
kitty, ghostty, wezterm and foot do, iTerm2 only with its clipboard setting on,
and Terminal.app never. On a remote session the native tool would write the
remote machine's clipboard, not yours, so OSC 52 is the only route: over SSH
(`SSH_CONNECTION` or `SSH_TTY` set, or an `sshd` above `asq`, as under `sudo`
or `su`), in a Codespace (`CODESPACES`), and in your own tmux when it was last
attached over SSH (tmux's own `SSH_CONNECTION`, asked at each copy). Inside tmux
its answer outranks `asq`'s own SSH variables, so a tmux created over SSH and
attached at the machine since is local. GNU screen, zellij and dtach have no
such answer to give: one started at the machine and reattached over SSH keeps
writing the machine's clipboard, and says "copied". If you reattach from other
machines, run `asq` in tmux. The
toast says "copied" when the native tool took the text, or when OSC 52 was the
only route (over SSH, or with the tool missing or failing) and the terminal is
one known to take it: kitty, Ghostty and WezTerm by their own variables
(`KITTY_WINDOW_ID`, `GHOSTTY_RESOURCES_DIR`, `WEZTERM_EXECUTABLE`), Ghostty and
WezTerm also by `TERM_PROGRAM`, Windows Terminal by `WT_SESSION`, and kitty,
Ghostty, WezTerm, foot and Alacritty by the `TERM` ssh carries. In Terminal.app
(named by `TERM_PROGRAM`) it warns that the copy did not reach the clipboard,
and how to select with the terminal itself. In iTerm2 — named by
`TERM_PROGRAM`, and over SSH by the `LC_TERMINAL` ssh usually carries, though
not behind tmux or screen, which keep the one of whichever terminal started
them — it
says the text was sent to your terminal's clipboard (OSC 52) and, if paste is
empty, to turn on iTerm2's clipboard setting or hold ⌥ while you drag: whether
that setting is on, nothing outside iTerm2 can see. On any terminal nothing
names (over SSH from Terminal.app, or behind tmux or screen) it says the text
was sent to your terminal's clipboard (OSC 52), and that an empty paste means
the terminal ignores it.

**Copy and paste on macOS.** Run on your Mac, every copy in `asq` reaches the
clipboard through `pbcopy`, in any terminal. Over SSH only OSC 52 can carry it
back, so the terminal decides:

- *Terminal.app* never implemented OSC 52, so a copy over SSH does not reach
  your clipboard. Terminal.app names itself to nothing across SSH, so the toast
  says the copy was sent through OSC 52 and that an empty paste means the
  terminal ignored it. Select with the terminal itself instead (below).
- *iTerm2* accepts it only with *Settings › General › Selection › Applications
  in terminal may access clipboard* turned on. The toast cannot see that
  setting, so over SSH it says the copy was sent through OSC 52 and, if paste
  is empty, to turn the setting on or hold ⌥ while you drag.
- *Ghostty, kitty and WezTerm* accept it out of the box.

⌘C is Terminal.app's and iTerm2's own Copy, so it never reaches `asq`, and it
does not need to: a drag copies on release with no key, and ctrl+c copies a
standing highlight again. ⌘V does reach `asq`, as a bracketed paste, and
pastes into the focused pane or field. To select with the terminal itself while
`asq` has the mouse, hold Option while dragging in iTerm2, or Fn in
Terminal.app on most Macs (users report it; Apple does not document it); then
⌘C copies that selection as it would anywhere else. When you type on a Mac —
`asq` running on it, or reached from Terminal.app or iTerm2 (iTerm2 over SSH
too, by its `LC_TERMINAL`) — the `?` screen lists these keys, and the board's
copy row names `c`, not ⌘C. Through tmux or screen on another machine they
are left out: the multiplexer keeps the `LC_TERMINAL` of whichever terminal
started it, which may not be the one you attached from.

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

### Long-lived managers: state file, start pack and quiet compaction

A manager is the longest-lived session in a fleet, and every request it makes
re-sends its whole context. Three things keep that context small.

**The state file.** The manager's cycle asks it to keep its state in
`~/.aisquare/projects/<project id>/manager/state.md`, at most 4,000 characters:
goal, decisions, open items, next step. It rewrites the file when a plan step
completes and before it stops. Scripts and rule text it reuses live in files,
and it cites them by path rather than pasting them into a prompt.

**The start pack.** At every `SessionStart` (startup, resume, `/clear`, a
compaction) a manager gets its start pack instead of the plain board. The pack
holds its state file (up to its 4,000 characters, with its age), every open
task on one line each (the board shows eight), the unanswered questions, the
live agents with their task and account, how many wake events wait past its
cursor, the project's `.aisquare/rules/*.md` by path and first line, and then
the protocol and its cycle. It is never more than 20,000 characters: the task
list takes what the rest leaves, and a last line says how many tasks did not
fit.

**Quiet-point compaction** is off by default (`fleet.quiet_compact_at = 0`).
Set it to a token count (400,000 is a sensible first value) and a manager is
compacted only at a quiet point. That is when its turn ends with no decision
pending, it is not asking a question, it is not in plan mode, and it does not
need the human. Its Stop hook then starts `aisquare hook compact-check` in the
background. After `quiet_compact_delay` seconds the check asks everything again,
and it types `/compact` only if all of these hold:

- the row still reads `waiting` and its cursor has not moved;
- no wake-class event is pending;
- the context (the transcript's last request) is at least `quiet_compact_at`;
- no quiet compaction ran within `quiet_compact_cooldown` minutes;
- the state file is under `quiet_compact_state_max_age` minutes old and newer
  than the last decision the manager was woken for;
- the pane shows the manager idle at its prompt with an empty input box.

A half-typed draft of yours is never sent. The line goes through the same
verified delivery as a first message. The compaction's own `SessionStart`
confirms it, and the row reads `waiting` afterwards, so a coder's
`task review` still nudges it. If no compaction starts within 120 seconds, the
board gets one human-only line and nothing is retried before the cooldown.
Each refusal is kept, the newest, under `team_meta` key
`compact-refused:<session>`. With `quiet_compact_resume = true` the manager
is also given a one-line resume prompt after the compaction.

Claude Code's own auto-compaction (about 967K tokens on 1M-context models)
and idle compaction (after about 54 idle minutes at 200K tokens or more) are
left as they are; aisquare never lowers them. Quiet compaction runs well
before both, at a moment chosen by what the manager is doing, not by a
threshold or a timer.

---

## Upgrading

To 0.9.0 from 0.8.2: the store migrates itself from v32 to v39 on first open, and
the config file moves to version 3, once. The four steps below are all 0.9.0's: the
first messages (v36), the token diet (v37 and config version 3), capacity and setup
(v38), then work briefs (v39).

### To 0.9.0, part 1: verified first messages (store schema 36)

**The store migrates itself on first open, from v35 to v36, in one step** (from 0.8.2's
v32, in four: v33 to v35 are under [0.9.0] in the CHANGELOG). v36 adds three
nullable columns: `team_session.prompted_at` (the session's last
`UserPromptSubmit`), `fleet_agent.first_prompt` (`pending`, `acked` or `unsent`) and
`fleet_agent.first_prompt_at` (when that last changed). The step has no script of its
own: the columns are checked for by presence and added only where absent, as every
step from v15 on is, so a store that another build already carried past v35 converges
too, and so does one a pre-release build of this train stamped v34 with these columns.
Nothing is rewritten. An agent spawned before the upgrade has no first-message state,
reads as "not tracked", and is never reported as a message not taken.

**No config migration.** Every new key has a default, and a key you never set
follows it:

| Key | Default | What it changes |
| --- | --- | --- |
| `[fleet] first_prompt` | `argv` | A Claude Code agent's first message rides its command line. `type` types it instead, once the input box is drawn, and never puts it on a command line. |
| `[fleet] prompt_ack_seconds` | `45` | How long `fleet spawn`, `restart` and `switch` wait for the agent to take its first message. |
| `[fleet] busy_quiet_seconds` | `30` | How long a `working` agent's pane may stay quiet before one capture checks whether it is idle. |
| `[fleet] draft_quiet_seconds` | `10` | How long after your last keystroke in a pane the fleet waits before typing into it. |
| `[fleet] deliver_notes_at_turn_end` | `true` | A note addressed to a busy agent is handed over when its turn ends. |
| `[accounts] prefer` | `priority` | Of the accounts under the switch threshold, which one a spawn's headroom pick and a hand-over take: the first by rank (rank 1 is the machine default), or with `reset-soonest` the one whose week resets first. Under `pick = "default"` rank 1 decides. |

**What you will notice.**

- `fleet spawn`, `restart` and `switch` take up to `prompt_ack_seconds` longer and end
  with `✓ first message taken`, or say it was not.
- A just-spawned agent reads `waiting` until its first prompt, not `working`.
- `fleet switch` refuses to move an agent that was just moved, unless you add `--force`.
  One usage limit now moves an agent once.
- `capture.enabled = false` now stops prompt capture: `aisquare log` stays empty and
  nothing is spooled ([reference](reference.md#configuration-keys)).
- `aisquare doctor` gains a `fleet-delivery` row.
- The codebase snapshot is gone, and `aisquare doctor` loses its `repomix`, `snapshot`
  and `tiktoken` rows. The first command that opens the store deletes the old packs
  under `~/.aisquare/projects/<id>/snapshot/`, once, and nothing else. A `[snapshot]` section
  in `config.toml` is no longer read, except its `ignore` list, which becomes `[code]
  ignore` (the symbol index's, part 3): doctor's config row says so, and the next save
  drops the section and writes that list under `[code]`.
- **On a shared machine**, a first message on the command line can be read by other
  local users in `ps` while the agent runs. `aisquare config set fleet.first_prompt
  type` keeps it off the command line.

**Moving back** to 0.8.2 or earlier with `aisquare upgrade --version <v>` is safe. An
older build opens a v36 store as it is, without migrating it down, and never reads the
three columns. It ignores the new config keys and types first messages as it always
did. It packs a project again on `project onboard` and `init` where its Node can,
with its default `[snapshot]` settings once a save here has dropped yours.

### To 0.9.0, part 2: the token diet (store schema 37, config version 3)

**The store migrates itself on first open, from v36 to v37, in one step.** v37 adds two
nullable columns, `team_event.actor` (who caused an event: a session, `user`,
`usage-limit`, `auto-stop`) and `team_event.meta` (its details as JSON; an `agent_exited`
carries its status, `--force`, the claims it released and why). They are checked for by
presence and added only where absent. An event written before the step reads as no actor
and no details; an old `agent_exited` is read from its text, so an upgrade wakes no
manager for exits it had already seen.

**The config file moves to version 3, once.** A reviewer whose `extra_args` are exactly
0.8.2's default (`--disallowedTools=Edit Write NotebookEdit`) loses them, because that
deny is the role's own now and every launch of a reviewer has it. Every other value you
set stays as it was. New keys follow their defaults until you set them:

| Key | Default | What it changes |
| --- | --- | --- |
| `[fleet] deny_tools` | the eight tools in [Tools and skills per role](#tools-and-skills-per-role) | Every role launches without them. `[]` gives them back. |
| `[fleet.roles.<role>] skill_listing` | `full` for the manager, `names` for the others | How much of Claude Code's skill listing a role carries. |
| `[fleet.roles.<role>] deny_tools` / `allow_tools` | `[]` | Tools one role also denies, or gets back. |
| `[fleet.roles.<role>] board_context` | `none` for the reviewer, `all` for the others | What of the board an agent reads. |
| `[fleet] quiet_compact_at` | `0` (off) | A manager's context size at which it is compacted at a quiet point. |
| `[fleet] quiet_compact_delay` / `_cooldown` / `_state_max_age` / `_resume` | `45` s / `30` min / `20` min / `false` | The quiet compaction's timing. |
| `[team] brief_blocks` / `brief_strict` | `["objective", "acceptance", "boundaries"]` / `false` | The blocks `task add` checks a contract for, and whether a missing one refuses the add. |

**What you will notice.**

- An agent restarted after the upgrade launches with the merged deny token, and its first
  request is uncached once: the tool list is part of the cached prefix.
- A manager wakes for decisions, not news: a clean exit, a switch or its own `fleet stop`
  waits for its next wake. Updates arrive one line each; `aisquare team show <seq>` prints
  one in full.
- A reviewer reads no board by default. Set `fleet.roles.reviewer.board_context` to `all`
  to give it the board back.
- A manager starts from its start pack, not the board. Its cycle asks it to keep
  `~/.aisquare/projects/<id>/manager/state.md`.
- `task add` warns on stderr when a contract lacks `objective:`, `acceptance:` or
  `boundaries:`.
- `fleet spawn` passes the role's model ladder (`--model`, `--effort`), and a tester
  spawned for a task verifies that one task and stops. (0.9.1 runs your last pick
  instead, or the account's default: [The roles](#the-roles).)

**Moving back** with `aisquare upgrade --version <v>` is safe for the store: an older build
opens a v37 store as it is, without migrating it down, and never reads the two columns.
For the config, `upgrade --version` to 0.8.2 or earlier names each role that release would
launch with tools this one denies, with the `config set` that keeps them denied there.
0.8.1 and 0.8.2 read a version-3 file over their own defaults. An install that shares
`~/.aisquare` without going through `aisquare upgrade` (a second tool install, a synced
home) is not warned.

### To 0.9.0, part 3: capacity and setup (store schema 38)

**The store migrates itself on first open, from v37 to v38, in one step.** v38 adds five
nullable columns for a parent manager over a group: `project_group.parent_project_id`,
`project_group.route` and `project_group.max_agents`, and `team_task.origin_task_id` and
`team_task.origin_project_id` (a task delegated from a parent's). They are checked for by
presence and added only where absent. A group without a parent, and every task written
before the step, reads as before. The symbol index is its own SQLite file per project
(`~/.aisquare/projects/<id>/code/`), not part of the store.

**The config file keeps version 3.** Every new key follows its default until you set it:

| Key | Default | What it changes |
| --- | --- | --- |
| `[fleet] max_agents_total` | `0` (no budget) | Live agents across every project, the captain excluded. |
| `[fleet] min_free_mb` | `2048` | A spawn is refused below this much available memory. `0` turns it off. |
| `[fleet] test_gate_mb` / `test_gate_wait_minutes` | `0` / `15` | What `aisquare fleet gate` waits for before a heavy run. |
| `[fleet] auto_stop_finished` / `auto_stop_idle_minutes` / `auto_stop_roles` | `true` / `5` / every checking role | Finished workers stop by themselves; coders opt in. |
| `[fleet] pane_clipboard` | `off` | Whether a pane program may set your clipboard. |
| `[watch] interval_seconds` / `github` / `github_every_seconds` / `auto_stop` / `sources` | `60` / `false` / `120` / `true` / `{}` | `aisquare watch`, which nothing starts until you do. |
| `[code] enabled` / `max_file_kb` | `true` / `1024` | The symbol index behind `aisquare code`. |
| `[code] ignore` | `[]` | Globs the symbol index leaves out, beside its built-in list; a `[snapshot] ignore` from 0.8.2 is carried here. |
| `[accounts] share` | `[]` | What every Claude account shares. Nothing until you enable a part. |

**What you will notice.**

- On a machine with under 2 GB available, a spawn is refused and names both numbers. Lower
  `fleet.min_free_mb` (or set it to `0`) to spawn anyway. A refusal for the per-project cap
  says `capacity_project` under `--json`, where it said `fleet_error`.
- A tester, reviewer, validator or ui-tester that has finished stops by itself after five
  idle minutes, and its exit wakes nobody. `fleet.auto_stop_finished = false` keeps them.
- The fleet's tmux server turns the mouse on: in `fleet attach` a plain drag is tmux's
  selection; hold Shift (Option in macOS Terminal and iTerm2) to select in your terminal.
  It applies to a server started after the upgrade.
- `project onboard` and `init` start a symbol index in the background, and a session start
  in a project with an index refreshes it in the background, adding nothing to the
  session's context. `code.enabled = false` leaves `aisquare code index` as the only way
  to build one.
- `aisquare doctor` has new rows: `fleet capacity`, `code index`, `anthropic routing`,
  `claude-accounts sharing` and `fleet watcher`.

**Moving back** with `aisquare upgrade --version <v>` is safe for the store: an older build
opens a v39 store as it is, without migrating it down, and never reads the five columns. A
group's parent, its routes and its cap, and every task's link to its origin, are then
ignored, and come back on the next upgrade. The new config keys are ignored by an older
build. Shared accounts stay linked: run `aisquare accounts share --disable <parts>` before
moving back to put each account's own entries back.

### To 0.9.0, part 4: work briefs (store schema 39)

**The store migrates itself on first open, from v38 to v39, in one step.** v39 adds the
`brief` table and its `brief_number` index, for work briefs and the evidence gate
([work-briefs.md](work-briefs.md)). Its script is `CREATE … IF NOT EXISTS` only. A store
that a pre-release build of the briefs branches (#306, #341, #417) stamped v34, v36, v37
or v38 already holds `brief`, and converges the same way as the steps above: the presence
pass adds what a step under its stamp left out (for #417's v38, part 3's columns), and
v39 finds `brief` in place, its briefs intact. No config key is added, and a task no
brief links is done exactly as before.

**Moving back** with `aisquare upgrade --version <v>` is safe for the store: an older build
opens a v39 store as it is, without migrating it down, and never reads the `brief` table.

### To the release with usage at a glance, adopt and the captain's gate (no store migration)

**No store migration.** The status line's records and `fleet.json` are cache files
under `~/.aisquare/cache/statusline/`, and its readings go into the `claude_usage`
rows the usage cache already keeps. `fleet adopt` reads Claude Code's own transcripts
and session registry and writes nothing beside them.

**Two things change for what you already have.**

- **Task keys** (#270): a title with words outside ASCII gets a new key (`Fix вход` is
  `fix-вход`, was `fix`). Re-adding a title as the board has it still answers "already
  tracked"; the CHANGELOG's *Upgrading* note has the edge cases.
- **Pins** (#271): `project forget` and `project prune` no longer write a pin. A pin an
  earlier build wrote stays until the next forget of that project or a `project unpin`.

**New config keys**, each with a default ([reference](reference.md#configuration-keys)):

| Key | Default | What it changes |
| --- | --- | --- |
| `[fleet.roles.<role>] remote_control` | `false` | The role's Claude Code sessions start with Claude Code's own Remote Control. |
| `[captain] gate` | `true` | With the captain on, the tools that act as you need a code from your authenticator app. Set in `config.toml`, not with `config set` ([captain](captain.md)). |

**What you will notice.**

- The sidebar's status line carries a number (`● 3× claude 58%`), the Accounts page
  and `accounts usage` end with the fleet's line, and the Manager tab counts the team
  under the manager's info line.
- **With the captain on**, its owner tools are shut until you run `aisquare captain gate
  enroll` (and install the `gate` extra); `gate = false` under `[captain]` in
  `~/.aisquare/config.toml` turns the gate off (it is no `config set` key).
- `persona import --from-dir` skips a README or LICENSE (listed `skipped`) instead of
  sending it to an engine.
- No new agent label is longer than 24 characters.
- New commands: `aisquare accounts statusline`, `accounts sessions`, `fleet adopt`,
  and the `asq-statusline` program the status line runs. The status line is opt-in.

**Moving back** to 0.9.0 or earlier is safe for the store and the config: an older build
ignores the new keys and the cache files. If you installed the status line, run
`aisquare accounts statusline --uninstall` first: an older build has no
`asq-statusline`, so a status line left pointing at it fails in every session.

## Troubleshooting

**`asq` lags with many projects listed.** Each tick reads only the projects
with recent agents, and nothing of a collapsed group's members (see [`ui`](#ui)).
Fold the groups you are not watching, `aisquare project forget` the projects you
are done with, or start a session with only the ones you work in:
`aisquare ui --project a --project b` or `aisquare ui --group <name>`.

**A drag in `fleet attach` selects in tmux, not in my terminal.** The fleet's
server has the mouse on so the wheel reaches Claude Code. Hold Shift (Option in
macOS Terminal and iTerm2) while you drag to select in your terminal.

**An agent reads `attention`: "first prompt not taken".** Its first message
never started a turn: nothing acknowledged it within `[fleet] prompt_ack_seconds`
and its input box is idle. Look at its window (`aisquare fleet attach`): text
still in the box goes with an Enter there; an empty box takes the message again
with `aisquare fleet tell <label> "…"`, or `aisquare fleet restart <label>`
starts it over. Either one clears the `attention`. See [the first
message](#the-first-message).

**An agent reads `attention`: "Claude Code's first-run Security notes screen —
press Enter in its window".** A new account's first run shows those notes, and a
first message on the command line waits behind them. Press Enter in its window
(`aisquare fleet attach`); the notes' only key is Enter. If its first turn then
starts, the `attention` clears; if it sits at an empty box instead, it reads
"first prompt not taken", above.

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
configuration errors for the first attached client, and the only one the fleet
attaches, the UI's control client, does not show them,
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
coder), they say so and leave the replacement alone. A restart from the view
moves you to the new row only if that view is still on screen when the restart
ends. If you have opened another agent meanwhile, you stay there, and a toast
names the restart and its project. Both buttons ignore a mouse click that
lands within a second of the view coming up, moving or a dialog over it
closing, and say so once: that click was aimed at what was there before. An
ignored click doesn't take the keyboard focus either, so the next Enter goes
where it was going. Enter on the focused button is never ignored.

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

**What did the fleet spend in tokens?** `aisquare metrics tokens` sums what the
Stop hook recorded for each session, with its cost. `aisquare metrics ledger`
counts the same sessions live from each Claude Code transcript (the path the board
recorded at its start), with what the fleet's own traffic cost, and prints one row
per session and a total. The ledger's columns are:

- requests;
- input summed over them: fresh plus cache write plus cache read, which is also the
  context size at each request;
- the baseline, the session's first request: system prompt, tool schemas, skills;
- the peak context;
- wakes, and how many of them took no action: a Stop-hook continuation whose turn
  made no tool call but a board read (`board`, `team log|status|signals`, `task
  list|show`, `task next` without `--claim`, `fleet ls|status`, alone or behind a `cd`
  and piped through a filter; `aisquare log` is the prompt log, not the board);
- the characters of the `<aisquare-team-delta>` blocks it was given;
- compactions.

`--json` adds the rest: output, the input split, mean and latest context, wake
input, each delta's carried characters (its size times the requests after it, up
to the next compaction), compaction triggers, and tool calls by name. It prints
counts, never a prompt or a path.

```sh
aisquare metrics ledger                       # this project's sessions
aisquare metrics ledger --role manager --since 2h
aisquare --json metrics ledger --all          # every project, as JSON
```

A transcript is read a line at a time, so a large one costs no memory. The line
Claude Code is still writing is skipped and counted. A session whose transcript
was never recorded, or is gone, is a row of zeros marked `(no transcript)`.

---

## Later

- **Other agents, Codex first.** The substrate is agent-agnostic — a fleet
  window runs whatever binary the role is bound to, and `--bin` already takes
  one — so *surfacing* a Codex session needs nothing new. What Codex lacks is
  our lifecycle hooks: until an equivalent exists it would have no board state
  and no wake-ups, and its row would say `no hooks`. v1 is Claude Code only
  (plan §8.5); nothing in the schema blocks the rest.
- **Auto-merge on green** as an opt-in policy. In v1 a human merges (plan §3.5).
- **Remote or multi-machine fleets** — postponed (#293). Today a manager can
  drive another box's fleet over SSH:
  [docs/runbooks/multi-machine-ssh.md](runbooks/multi-machine-ssh.md); the
  design is in [docs/plans/multi-machine-fleet.md](plans/multi-machine-fleet.md).
- **A tmux-less backend** for hosts without tmux (plan §3.1, option A).
