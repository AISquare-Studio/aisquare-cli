# aisquare

[![PyPI](https://img.shields.io/pypi/v/aisquare-cli.svg)](https://pypi.org/project/aisquare-cli/)
[![Python versions](https://img.shields.io/pypi/pyversions/aisquare-cli.svg)](https://pypi.org/project/aisquare-cli/)
[![CI](https://github.com/AISquare-Studio/aisquare-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/AISquare-Studio/aisquare-cli/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Memory and orchestration for coding agents.** aisquare gives agents like
Claude Code two things they don't have out of the box: a **memory** that
persists across sessions — your preferences, each project's conventions, how
you actually prompt — and an **orchestration layer** that lets several
sessions work one problem as a coordinated team, with shared tasks, live
status, and a board you can watch.

It's a single CLI, local-first, backed by one SQLite file. No daemon, no
account, no cloud dependency. Install it, connect it to Claude Code once, and
every session after that starts oriented instead of cold.

```sh
pipx install aisquare-cli              # or: pip install aisquare-cli
cd your/repo
aisquare init                          # register the project + snapshot the codebase
aisquare agents connect claude-code    # wire aisquare into Claude Code
aisquare doctor                        # verify everything (and how to fix anything)
```

That's the whole setup. Requires **Python 3.11+**. The package is
`aisquare-cli`; the command is `aisquare` (with an `asq` alias). Codebase
snapshots use [Repomix](https://github.com/yamadashy/repomix) via Node/`npx`
when available — `aisquare doctor` tells you if it's missing, and nothing
breaks without it.

That's the whole setup — you are done. Everything below is reference.

aisquare has **two halves, and they are independent**:

| | What it is | Who it's for |
| --- | --- | --- |
| **[Part 1 — Memory](#part-1--memory-start-here)** | Your agent remembers preferences and project conventions, and starts every session oriented. | **Everyone.** Zero extra commands after setup — it just works. |
| **[Part 2 — Orchestration](#part-2--orchestration-advanced)** | Several agent sessions work one problem as a team, with a shared task board. | Opt-in, per repo. Skip it until you actually want parallel sessions. |

If you only ever read Part 1, you are using aisquare correctly.

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
Worktrees resolve to their principal repository automatically.

`aisquare project onboard` (also run by `init`) packs the codebase with
Repomix into three artifacts under `~/.aisquare/projects/<id>/snapshot/`: a
**full pack** (every file), a **skeleton** (structure + signatures — the
cheap thing agents read first), and a **per-file index** (char offsets +
token counts, so an agent can open one file's slice of the pack instead of
all of it). Re-run with `--refresh` after big changes.

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

- **planner** — turns your intent into tasks on the shared board
- **coder** — loops `task next --claim` → work → `task review`
- **runner** — verifies reviewed work → `task done`, or `task reopen
  --reason "what failed"` — and the feedback rides back to whichever coder
  picks the task up next

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

### Tuning (environment variables)

Orchestration has no config files — a handful of env knobs:

| Variable | Effect |
| --- | --- |
| `AISQUARE_ROLE` | role for this session; launching with it opts the repo in |
| `AISQUARE_TEAM=0` | master off switch — hooks and commands no-op |
| `AISQUARE_TEAM_HUB` | point sessions from several repos at one shared board |
| `AISQUARE_TEAM_DELTA=0` | mute per-prompt teammate deltas for a session |
| `AISQUARE_TEAM_LEASE_MIN` | task-claim lease in minutes (default 120) |
| `AISQUARE_BRAIN=0` | disable the long-term-memory layer |
| `AISQUARE_BRAIN_EMBED=1` | embed distilled pages for semantic recall (needs `OPENAI_API_KEY`; set before the first distill) |
| `AISQUARE_BRAIN_EMBED_MODEL` | embedding model (default `openai:text-embedding-3-large`) |
| `AISQUARE_HOME` | relocate the whole `~/.aisquare` tree |

### Several accounts, one team

Running parallel Claude installs for separate rate limits? Connect each
config dir once, then launch roles against them with `--account`:

```sh
aisquare agents connect claude-code --config-dir ~/.claude-account1
aisquare agents connect claude-code --config-dir ~/.claude-account2

aisquare launch planner                              # your default account
aisquare launch coder --account ~/.claude-account1
aisquare launch coder --account ~/.claude-account2
```

`--account` sets `CLAUDE_CONFIG_DIR` for the launched session and fails
loudly on a directory that doesn't exist — a typo would otherwise start a
fresh, unauthenticated profile. Note that shell aliases (`alias
claude1='CLAUDE_CONFIG_DIR=… claude'`) can **not** be passed to `--command`:
aliases aren't executables, so target the config directory instead.

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
├── task            add · list · show · next [--role R] [--status S] [--claim]
│                   claim · review [--note] · reopen --reason · done [--note]
│                   block --reason · drop · release        (all with [--as SESSION])
├── note <text> [--task T] [--to ROLE] [--kind note|decision|question|result]
├── board [-w] [-i SECONDS] · recall <query>
├── launch <planner|coder|runner> [--account DIR] [--command CMD] [… agent args]
├── serve [--stdio | --port N --bind H] [--show-token]
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
