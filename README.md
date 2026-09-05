# aisquare

[![PyPI](https://img.shields.io/pypi/v/aisquare-cli.svg)](https://pypi.org/project/aisquare-cli/)
[![Python versions](https://img.shields.io/pypi/pyversions/aisquare-cli.svg)](https://pypi.org/project/aisquare-cli/)
[![CI](https://github.com/AISquare-Studio/aisquare-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/AISquare-Studio/aisquare-cli/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**One terminal UI over every project and every coding agent you have running.**
Type `asq` and you get a full-screen, mouse-driven view: your projects on the
left, and on the right a **manager** agent you task in prose — it plans, spawns
coders, testers and reviewers, and loops until the goal is met. Click any of
them and you are inside its *real* Claude Code session, typing at it directly.
Nothing is relayed or re-rendered as a chat.

Underneath, agents get a **memory** that persists across sessions — your
preferences, each project's conventions — so every session starts oriented
instead of cold.

It's a single CLI, local-first, backed by one SQLite file. No daemon, no
account, no cloud dependency.

## Install

```sh
pipx install aisquare-cli              # or: pip install aisquare-cli
```

Requires **Python 3.11+**. The package is `aisquare-cli`; the command is
`aisquare`, with `asq` as the short alias.

The UI runs agents inside a private tmux server, so you also need **tmux 3.2+**
(3.5+ recommended — that is where shift+enter reaches the agent):

```sh
sudo apt install tmux        # Debian / Ubuntu
sudo dnf install tmux        # Fedora / RHEL
brew install tmux            # macOS
tmux -V                      # 3.2 or newer
```

Agents run on **[Claude Code](https://claude.com/claude-code)** (`claude`
2.1.x), so install that too if you haven't. On Windows, run everything inside
WSL2. `git` is used for the per-agent worktrees; `gh` is optional and only
needed if you want agents opening and reviewing PRs. Codebase snapshots use
[Repomix](https://github.com/yamadashy/repomix) via Node/`npx` when available —
`aisquare doctor` tells you if it's missing, and nothing breaks without it.

## Start the GUI

```sh
aisquare agents connect claude-code    # once — wires the hooks the UI reads state from
asq                                    # open the UI
```

That's the whole setup. From inside the UI:

1. **Click `+` beside Fleet** and point it at a directory. It registers the
   project and runs a health check in the background, streaming the log — you
   never leave the UI. The project appears in the navigator on the left.
2. **Click the project**, then press *Start manager*. Its live Claude Code
   session fills the pane. **Type your goal in prose**, exactly as you would to
   any Claude session.
3. **Watch the agents appear** under the project, each with a role icon
   (🧭 manager · 🔨 coder · 🧪 tester · 👀 reviewer · 🛡 validator) and a live
   state chip — **▶ working**, **⏸ waiting**, **🔔 NEEDS YOU**, **💤 exited**.
   Click one to see and drive its session.
4. **Press `F12`** to hand focus back to the sidebar — it's the one key a pane
   never swallows. There, `t` picks a theme and `q` quits. **The agents keep
   running**; reopen `asq` and it re-attaches to what it finds.

The manager never writes code and never merges — a human does that.

Everything the UI does is also a plain command, and every one takes `--json`:

```sh
aisquare fleet ls                      # this project's agents and their live state
aisquare fleet attach                  # the same session in raw tmux, full fidelity
aisquare doctor                        # is everything wired? (and how to fix anything)
```

Scripts never meet a full-screen app: bare `aisquare` in a pipe, or under
`TERM=dumb`, prints usage and exits 2 exactly as before, and under `--json` it
prints one usage object so a `jq` pipeline gets JSON rather than a help page.

**[The fleet guide](docs/fleet.md)** has the roles in full, the
`aisquare fleet …` command reference and every default you can change.

## The rest of aisquare

The UI is a view over two halves, and they are **independent** — neither needs
the UI, and you can use either on its own:

| | What it is | Who it's for |
| --- | --- | --- |
| **[Part 1 — Memory](#part-1--memory-start-here)** | Your agent remembers preferences and project conventions, and starts every session oriented. | **Everyone.** Zero extra commands after setup — it just works. |
| **[Part 2 — Orchestration](#part-2--orchestration-advanced)** | Several agent sessions work one problem as a team, with a shared task board. | Opt-in, per repo. Skip it until you actually want parallel sessions. |

If you only ever read Part 1, you are using aisquare correctly.

**Optionally, on top of either:** send your sessions to an
[AISquare Explainability](https://aisquare.studio) workspace, so every session
becomes a Run you can read back — prompts, tool calls, tokens and cost, plus your
own prompts and board events. Off unless you ask for it, and it never blocks a
launch: if anything in the path is down the session starts untraced and says so.
See **[Connecting your agents to Explainability](docs/connecting-your-agents-to-explainability.md)**.

---

# Part 1 — Memory (start here)

**Your agent starts every session already oriented.** `agents connect
claude-code` installs lifecycle hooks into `~/.claude/settings.json` (merged
carefully — your existing hooks are never touched). From then on, each session
begins with a directive pointing Claude at a packed snapshot of the codebase
(structure-only skeleton first, full contents on demand — orders of magnitude
cheaper than grepping around), your in-scope context entries, and the
project's prompt history.

**Your agent remembers what you tell it.** `aisquare remember "prefer pytest
over unittest"` persists across every session and every project. Context
lives in two pools — `user` (follows you everywhere) and `project` (scoped to
one repo) — full-text searchable, exportable, and injected consistently.

### The five commands that matter

```sh
aisquare remember "prefer pytest over unittest"   # sticks everywhere
aisquare context add "run make check" --project   # sticks in this repo only
aisquare context list                             # what's in scope here
aisquare context search pytest                    # full-text search
aisquare doctor                                   # is everything wired?
```

Nothing else in this document is required reading.

## The memory layer in full

```sh
aisquare remember "prefer pytest over unittest" --user --tag testing
aisquare context add "run make check before pushing" --project
aisquare context list              # user pool + the active project's pool
aisquare context search pytest     # full-text search (SQLite FTS5)
aisquare context show a3f2         # ids are git-style prefix-addressable
aisquare context edit a3f2         # opens in $EDITOR
aisquare context promote a3f2      # project entry → user pool
aisquare context export out.md     # markdown or --format json
aisquare context import notes.md   # seed from Markdown bullets or JSON
aisquare context preview           # exactly what agents will be shown
aisquare inject                    # emit the context block (and record it)
aisquare why                       # explain the last injection
aisquare log                       # your captured prompt history, per project
```

The **active project** is whichever repo contains your working directory, or
the one you pin with `aisquare project switch <name>`. Everything —
context, snapshots, prompt history, team state — scopes to it consistently.

**Git worktrees resolve to their principal repository**, so several feature
branches checked out side by side all share one context pool, one snapshot and
one board. Set a repo's conventions up once and every worktree of it starts
oriented; identity comes from `git rev-parse --git-common-dir`, not from
walking up to the nearest marker.

`aisquare project onboard` (also run by `init`) packs the codebase with
Repomix into three artifacts under `~/.aisquare/projects/<id>/snapshot/`: a
**full pack** (every file), a **skeleton** (structure + signatures — the
cheap thing agents read first), and a **per-file index** (char offsets +
token counts, so an agent can open one file's slice of the pack instead of
all of it). Re-run with `--refresh` after big changes.

A pack has to fit a **token budget**: `[snapshot] max_tokens` in
`~/.aisquare/config.toml`, 150 000 by default (the cap the server packs with).
The full pack is tried first, then a compressed one; when even that is over,
the compressed pack is kept as the **skeleton** with its per-file index and the
full pack is skipped — `onboard` and `aisquare doctor` both say `snapshot:
skeleton only: 2030000 tokens, 1234 files indexed; full pack skipped over
budget 150000 (10990000 tokens)`, and agents are oriented from it as usual.
The budget gates only the full pack: an agent is handed paths and opens slices
through the index, never a whole pack in a prompt. To keep the full pack too,
raise the budget or leave more out, then re-pack:

```sh
aisquare config set snapshot.max_tokens 300000   # raise the budget for a repo you know is big
aisquare config set snapshot.ignore '**/fixtures/**,docs/generated/**'   # leave generated trees out
aisquare project onboard --refresh               # re-pack; a plain onboard only reuses the verdict
```

`[snapshot] ignore` takes Repomix glob patterns (comma-separated on the command
line) and **extends** the built-in list rather than replacing it:
`node_modules`, `.venv`/`venv`, `.git`, `__pycache__`, `dist`, `build`,
`coverage`, `.aisquare-worktrees`, `*.worktrees`, and any nested git repository
or worktree found below the root — another project's checkout is never packed
into this one. The repo's own `.gitignore` and a `.repomixignore` at the repo
root apply on top, read by Repomix itself. A smaller pack is also a cheaper one
for every agent that reads it.

---

# Part 2 — Orchestration (advanced)

**You do not need this to use aisquare.** Everything above works on its own.
Read on only when you want several agent sessions working one problem at once.

Sessions are per **terminal**, not per account — a single `claude` install
runs the whole team:

```sh
pipx install 'aisquare-cli[tui]'         # the live board wants the TUI extra
aisquare agents connect claude-code
cd your/repo

aisquare launch planner                  # terminal 1 — you talk to this one
aisquare launch coder                    # terminal 2
aisquare launch coder                    # terminal 3 — as many as you like
aisquare launch runner                   # terminal 4 — verifies the coders' work
aisquare board -w                        # terminal 5 — you, watching live
```

`aisquare launch <role>` opts the repo in, registers the session, and hands
off to `claude` — arguments after the role are forwarded, so `aisquare launch
coder --model opus` does what it looks like. (The underlying mechanism is the
`AISQUARE_ROLE` environment variable; `AISQUARE_ROLE=coder claude` still works
if you prefer it, and is what you need when launching an agent other than
`claude` without `--command`.)

Every session is told its id, its teammates, and its **role's work cycle**
automatically — no standing prompts to paste:

- **planner** — turns your intent into contract-carrying tasks on the shared
  board (objective, why, acceptance criteria, boundaries)
- **coder** — loops `task next --claim` → work → `task review`; blocks
  instead of guessing when a task has no usable contract
- **runner** — the adversarial verifier: runs the full check the acceptance
  criteria name, tries to make the change fail, then `task done` with
  evidence or `task reopen --reason "what failed"` — and the feedback rides
  back to whichever coder picks the task up next
- **validator** — gates the assembled deliverable once, before handoff
  (final accountability review, severity-ordered findings)

### The model harness: each role on the right model

Roles are tiered onto a model *ladder*, strongest first, with availability
verified and automatic fallback — planner/validator want `fable`
(enterprise) and fall back to `opus`, then `sonnet`, when the account
doesn't serve it; coder/runner run on `sonnet`, the measured sweet spot for
agentic work. Launch a role through the harness and it resolves the ladder
for you:

```sh
aisquare team spawn planner            # prints: AISQUARE_ROLE=planner claude --model fable --effort high
aisquare team spawn coder --exec       # or replace this terminal with the session
aisquare team harness                  # the whole role→model matrix + how it resolves now
```

Availability is *probed*, never assumed — `claude --model` silently
substitutes the default when a known model isn't available to the account,
so the harness verifies the reply's `modelUsage` before trusting a rung, and
caches that verdict per account for a day (`--refresh` re-checks after an
entitlement changes). The probe runs isolated: it never executes the current
repo's hooks or MCP servers, and never joins the board.

Resolution is fail-open and, deliberately, only *demotes on proof*: a
genuine substitution walks down the ladder, while an outage, an expired
login, or an unrecognised reply keeps the requested model and labels the pick
`[unverified]` rather than quietly downgrading your planner. Nothing here
ever blocks a launch. Pin a role outright with `AISQUARE_MODEL_<ROLE>`
(works for custom roles too); disable probes with `AISQUARE_HARNESS_PROBE=0`.

**Effort is dynamic, not frozen.** `high` is the base — the documented default
for most work — and each role carries a predefined *offset* rather than a
hardcoded level, so the shape holds wherever you set the base:

| base | planner / coder / runner | validator (+1) |
| --- | --- | --- |
| `low` | low | medium |
| `high` *(default)* | high | xhigh |
| `xhigh` | xhigh | max |

The offset exists for one reason: the gate has to outrank the work it checks.
A flat override that dropped everything to `low` would leave the validator
weaker than the coder whose output it reviews, which is not a gate at all.

The base comes from, in order: `AISQUARE_EFFORT` → `CLAUDE_EFFORT` (what your
own Claude session is running at, which Claude Code exports) → `high`. So
raising your session to xhigh raises the fleet you spawn from it, with nothing
to configure. Override per launch with `aisquare team spawn coder --effort
xhigh`, or pin one role absolutely with `AISQUARE_EFFORT_<ROLE>` — both skip
the offset, because you named the level yourself. `ultracode` is accepted and
ranks as xhigh (it is xhigh plus automatic workflow orchestration). An
unusable value falls back to the base rather than being passed to the CLI,
which would silently ignore it. `aisquare team harness` prints the live base
and every derived level.

Sessions report their model back to the board, which flags any session
running off its role's ladder (`⚠ off-ladder`). That signal is advisory: the
model field is optional in Claude Code's hook payload and absent on some
surfaces (MCP teammates have none), and an in-session `/model` switch isn't
re-reported — so a missing chip means *not reported*, never *wrong*. Tiering
is enforced at launch, not policed in the store.

On every prompt, each session receives a compact delta of what teammates did
since its last turn. Nothing needs forwarding; the coordination is the
ambient state of the board.

Orchestration is **opt-in per repo** (a role launch or `aisquare team on`)
and fails open everywhere: the hooks are designed so orchestration can
never break a Claude session, even when it is broken or absent. Repos
that never opt in see nothing.

### Tasks: idempotent, atomic, dependency-aware

```sh
aisquare task add "wire auth" --role coder        # idempotent — safe to re-emit
aisquare task add "ship it" --needs tsk_01k…      # held until its dependency is done
aisquare task next --role coder --claim --as <id> # atomic claim — exactly one winner
aisquare task review tsk_01k… --note "how to verify" --as <id>
aisquare task reopen tsk_01k… --reason "fails on py3.11" --as <id>
aisquare note "JWT it is" --kind decision --as <id>
```

Claims are single-`UPDATE` atomic (race-tested), leased (default 120
minutes), and renewed by the session's own lifecycle hooks — so a dead
session's claims release themselves and the work gets picked up again.
`task next` only hands out tasks whose dependencies are done.

### Self-check: receipts you can re-prove

Every successful write prints a receipt (`✓ … · seq N on <board>`; under
`--json`, `delivered: true` plus the event's `seq`). The pull side is yours
any time:

```sh
aisquare team verify 42                     # is seq 42 really on this board? exit 0/1
aisquare team verify evt_01k… --as <id>     # by event id (prefix ok), session's board
aisquare team log --mine --as <id>          # read back your own recent writes
aisquare team log --by aaaa1111 --since 15m --kind decision   # filters compose
```

A receipt that lives on a *different* board is an honest not-found — with a
hint naming the board that actually holds it. Remote MCP agents get the same
pair: `verify(receipt)` and `team_log(by_session="me")`.

### Signals: named states, never substring matching

Prose is a terrible protocol — a watcher grepping `READY` fires on a note
saying "NOT READY". Signals are first-class named board states:

```sh
aisquare team signal fold-ready on --as <id>   # set (single-token name/value)
aisquare team signal fold-ready                # read: value, who set it, when, seq
aisquare team signals                          # list all
aisquare team log --kind signal --since-seq N --json   # a watcher's poll loop
```

Every set emits a `signal` event whose `--json` payload carries structured
`name` / `value` / `prev` / `set_by` fields — consumers key on fields, never
on text, so negations can't false-trigger. Sets follow the write contract
(receipt + read-back; `team verify <seq>` works on signal receipts), and the
MCP `signal(name, value?)` tool gives remote agents the same pair.

Still matching free text somewhere? At minimum anchor the pattern
(`^ready$`), match whole tokens (`\bready\b` misses `NOT READY` only if you
also reject preceding negations), and treat any hit inside a longer sentence
as suspect — then switch to signals, which is the whole point of them.

### The live board (`aisquare board -w`)

An interactive [Textual](https://textual.textualize.io/) TUI with the
`[tui]` extra (full-screen Rich fallback without it): every session with a
live state chip — **▶ working**, **⏸ waiting for input**, **🔔 NEEDS YOU**
(with a terminal bell) — the open tasks, and a bot-style feed of everything
the team does. Click any task or feed line for its full detail.

| Key | Action |
| --- | --- |
| `d` | flip to the done/dropped archive — when it closed, who closed it |
| `o` | open the author session's transcript at that exact moment |
| `t` | theme browser — applies live, autosaves |
| `a` | toggle feed autoscroll |
| `v` / `c` | select-text mode (frozen, mouse-selectable feed) / copy |
| `s` | save an SVG screenshot to `~/.aisquare/screenshots/` |
| `b` | show/hide the board pane |
| `r` / `q` | refresh now / quit |

### Long-term memory (optional, via gbrain)

Durable events — decisions, results, task outcomes, reopen feedback —
distill into a per-project brain by a detached worker, never on the hot
path. `aisquare recall "what did we decide about auth?"` searches it across
sessions and weeks; `aisquare team distill --all` backfills; `aisquare
doctor` reports brain health.

This layer needs the AISquare **gbrain** CLI on `PATH` (a separate,
optional tool — *not* the unrelated `gbrain` package on public npm) and is
silently skipped when absent. Everything else works without it.

**Semantic recall**: embeddings are off by default (no surprise network
calls). Export `AISQUARE_BRAIN_EMBED=1` plus an `OPENAI_API_KEY` **before
the first distill** and `recall` becomes hybrid vector + keyword search. The
embedding schema is fixed at brain creation — to upgrade an existing brain,
remove `~/.aisquare/projects/<id>/brain` and re-run
`AISQUARE_BRAIN_EMBED=1 aisquare team distill --all`. `doctor` flags
knob-vs-schema mismatches in both directions.

### Remote agents over MCP (`aisquare serve`)

The same board, tasks, and notes — exposed as an MCP server so Claude
clients that aren't local terminals can join: a browser-debugging agent in
the Claude desktop app, for instance. Remote callers act as attributed
virtual sessions; their tasks and notes hit everyone's board and deltas
like any teammate's.

```sh
pipx install 'aisquare-cli[serve]'
aisquare serve                   # streamable HTTP on 127.0.0.1:8747, bearer-token auth
aisquare serve --show-token      # connection details for the client
aisquare serve --stdio           # stdio transport (Claude Desktop launches it)
```

Running `serve` in a repo is the explicit opt-in for that project (it
announces itself); the stdio transport refuses to run from directories that
aren't a project, so a desktop client can't accidentally adopt your home
directory. Claude Desktop on Windows + WSL2 works either over the HTTP URL
(Windows reaches WSL2 via localhost) or as a registered stdio server:

```json
{"mcpServers": {"aisquare-team": {"command": "wsl", "args": ["-e", "bash", "-lc",
  "cd /path/to/your/repo && aisquare serve --stdio"]}}}
```

An idle stdio server closes itself after 300s without a client message
(`--close-after`, env `AISQUARE_SERVE_CLOSE_AFTER`) so abandoned daemons
never linger; persistent clients like the Claude Desktop config above should
set `AISQUARE_SERVE_CLOSE_AFTER=0` (run forever) in their launch command.
The clock counts **inbound** messages only — it assumes request/response
traffic, so a deadline shorter than your slowest tool call would cut a
client mid-wait (at the 300s default no current tool comes anywhere close).

### Tuning (environment variables)

Orchestration has no config files — a handful of env knobs:

| Variable | Effect |
| --- | --- |
| `AISQUARE_ROLE` | role for this session; launching with it opts the repo in |
| `AISQUARE_TEAM=0` | master off switch — hooks and commands no-op |
| `AISQUARE_TEAM_HUB` | point sessions from several repos at one shared board |
| `AISQUARE_TEAM_DELTA=0` | mute per-prompt teammate deltas for a session |
| `AISQUARE_TEAM_LEASE_MIN` | task-claim lease in minutes (default 120) |
| `AISQUARE_MODEL_<ROLE>` | pin a role's model outright (skips the harness ladder) |
| `AISQUARE_EFFORT` | base effort for spawned roles (default `high`, else inherits `CLAUDE_EFFORT`) |
| `AISQUARE_EFFORT_<ROLE>` | pin one role's effort absolutely (skips the role offset) |
| `AISQUARE_HARNESS_PROBE=0` | never probe model availability (ladders resolve optimistically) |
| `AISQUARE_BRAIN=0` | disable the long-term-memory layer |
| `AISQUARE_BRAIN_EMBED=1` | embed distilled pages for semantic recall (needs `OPENAI_API_KEY`; set before the first distill) |
| `AISQUARE_BRAIN_EMBED_MODEL` | embedding model (default `openai:text-embedding-3-large`) |
| `AISQUARE_HOME` | relocate the whole `~/.aisquare` tree |

### Several accounts, one team

Running parallel Claude installs for separate rate limits? Connect each
config dir once, then **bind** each seat to the environment it launches with:

```sh
aisquare agents connect claude-code --config-dir ~/.claude-account1
aisquare agents connect claude-code --config-dir ~/.claude-account2

aisquare team bind coder1 \
  --env CLAUDE_CONFIG_DIR='$HOME/.claude-account1' \
  --env CLAUDE_CODE_TMPDIR='$HOME/.cache/claude-account1'
aisquare team bind coder2 \
  --env CLAUDE_CONFIG_DIR='$HOME/.claude-account2' \
  --env CLAUDE_CODE_TMPDIR='$HOME/.cache/claude-account2'

aisquare launch planner            # your default account
aisquare launch coder1             # bound above — nothing to retype
aisquare launch coder2
```

A binding is a **launch profile**: a binary, a set of env vars and extra args,
carried through verbatim. `~` and `$VAR` expand at launch, so one binding
follows you across machines with different homes, and an undefined variable is
left as written rather than blanked — a silently empty `CLAUDE_CONFIG_DIR`
starts a fresh unauthenticated profile that reads as a login failure hours
later instead of the typo it is.

Set **both** variables. `CLAUDE_CONFIG_DIR` alone gives a session the right
credentials and the *default* scratch directory, silently shared with every
other account; it looks correctly isolated right up until two parallel sessions
collide in temp.

For a one-off, `aisquare launch <role> --env KEY=VALUE` merges over the
binding per key. Shell aliases (`alias claude1='CLAUDE_CONFIG_DIR=… claude'`)
can **not** be passed to `--command` — an alias is not an executable — but an
alias is only env vars around a binary, which is exactly what `--env` sets.

Each session records **which config dir it runs under**, and the board labels
sessions with it once more than one account is in play:

```
sessions:
  - a1b2c3d4 coder [.claude-account1] — 2m ago
  - e5f6a7b8 coder [.claude-account2] — 1m ago
```

So when one account hits its limit you can see exactly which terminals to
relaunch elsewhere. Because claims are leased and released on `SessionEnd`,
a killed session hands its task straight back to the pool — relaunching under
another account picks the work up with full context from the board.

All accounts share one `~/.aisquare` — one context store, one board, one task
list. Sessions are per **terminal**, not per account, so several accounts
simply mean several rate-limit pools driving one team. `agents list` and
`doctor` report every connected directory separately, so a sibling install
whose hooks went missing is named rather than hidden behind a healthy ✓.

For executions spanning multiple repositories, set
`AISQUARE_TEAM_HUB=/path/to/hub` in every session; git worktrees already
share their principal repo's board automatically.

## How it works

`agents connect claude-code` writes five hooks into Claude Code's
`settings.json` (merged, never clobbering yours; `agents disconnect`
removes exactly them):

| Hook | What it does |
| --- | --- |
| `SessionStart` | inject orientation: snapshot pointers, context, team briefing |
| `UserPromptSubmit` | capture the prompt; deliver the teammate delta; heartbeat |
| `Stop` | mark the session waiting; renew its task leases |
| `Notification` | flag **NEEDS YOU** on the board (permission prompts, idle) |
| `SessionEnd` | release claims, mark the session gone, final distill |

Every hook is **fail-open**: any error is swallowed and the session
continues untouched. State lives in one SQLite database (WAL mode,
concurrency-tested against racing parallel sessions):

```
~/.aisquare/
├── context.db    # context entries, projects, prompt history, tasks, events, sessions
├── config.toml   # typed configuration
├── fleet-tmux.conf  # the fleet's private tmux server config (regenerated, not yours)
├── state.json    # small runtime state (e.g. the pinned active project)
├── agents.json   # registry of connected agents
├── projects/     # per-project data — snapshot/ (Repomix pack), brain/ (gbrain)
├── cache/        # disposable (e.g. last_injection.json)
└── log/          # capture and diagnostic logs
```

Ids everywhere are time-sortable and prefix-addressable (git-style: any
unambiguous prefix works). Every command takes a global `--json` flag for
machine-readable output — global flags go before the command:
`aisquare --json task list`.

## Command reference

```
aisquare
├── init [path] [--api-key K] [--local] [--agent A]… [--no-onboard] [--reinit] [-y]
├── remember <text> [--user|--project] [--tag T]…
├── context (ctx)   add · list · show · edit · remove · search · preview
│                   promote · import · export · —  your persistent memory
├── inject · why · log · status · doctor
├── project (workspace)  info · list · switch · link · onboard [--refresh]
├── agents          scan · list · status [name] · connect <name> · disconnect <name>
│                                                  [--config-dir DIR]
├── team            on · status · focus <text> · role <name> · log [-n N] · distill [--all]
│                   spawn <role> [--exec] [--probe/--no-probe] [--refresh]
│                                 [--effort LEVEL] · harness
│                   bind <role> [--bin CMD] [--env KEY=VALUE]… [--arg A]…
│                               [--unset KEY] [--clear]
├── task            add · list · show · next [--role R] [--status S] [--claim]
│                   claim · review [--note] · reopen --reason · done [--note]
│                   block --reason · drop · release        (all with [--as SESSION])
├── note <text> [--task T] [--to ROLE] [--kind note|decision|question|result]
├── board [-w] [-i SECONDS] · recall <query>
├── launch <role> [--command CMD] [--env KEY=VALUE]… [… agent args]
│                   role = planner|coder|runner|validator, a fleet role (manager,
│                   tester, reviewer), a numbered seat (coder1), or any role you
│                   have bound; env merges over `team bind`
├── serve [--stdio | --port N --bind H] [--show-token]
├── ui              the fleet UI — what bare `asq` opens at a terminal (docs/fleet.md)
├── fleet           spawn <role> [--label L] [--task ID] [--worktree/--no-worktree]
│                             [--permission-mode M] [--bin B] [--prompt TEXT] [-- agent args]
│                   ls [--all] · status · tell <label> <text> · stop <label> [--force]
│                   attach · reap [--all] · rename <codename> · pause · resume
│                   (all with [--project P]; spawn · tell · pause · resume take [--as SESSION])
└── config          list · get <key> · set <key> <value> · redaction <off|standard|strict>
```

Everything `aisquare --help` lists is implemented. Roadmap commands are
registered but hidden until they do something real.

| Global flag | Meaning |
| --- | --- |
| `-V` / `--version` | print the version and exit |
| `--json` | machine-readable JSON on stdout |
| `-v` / `-q` | verbose / quiet |
| `--no-color` | disable coloured output |
| `--profile NAME` | configuration profile |

### Roadmap commands

`auth` / `login` / `logout` / `whoami`, `sync`, `connectors`, `capture`,
`policy` / `enforce`, `open`, `upgrade` and `uninstall` are the cloud roadmap
(sync across machines, managed connectors). They are **hidden from `--help`**
so the listed surface is only what actually works, but they still run and
still say plainly that they are not implemented (exit code 70) rather than
half-working. Follow along in
[issues](https://github.com/AISquare-Studio/aisquare-cli/issues).

## Development

```sh
git clone https://github.com/AISquare-Studio/aisquare-cli && cd aisquare-cli
python3 -m venv .venv && source .venv/bin/activate
make install          # editable install + dev tools
make check            # exactly what CI runs: ruff, format check, mypy strict, pytest
```

The codebase is a thin Typer CLI over a service layer over one SQLite store
— `src/aisquare/cli/` parses, `src/aisquare/services/` behaves,
`src/aisquare/core/` is shared infrastructure. Tests run hermetically
against a temp `AISQUARE_HOME` (the suite passes even with every aisquare
env knob set adversarially). See [CONTRIBUTING.md](CONTRIBUTING.md) for the
workflow.

## License

[MIT](LICENSE)
