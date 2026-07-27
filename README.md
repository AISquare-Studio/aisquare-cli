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

## What you get

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

**Your agents can work as a team.** Launch a planner you talk to, coders that
pull work from a shared task list, and a runner that verifies — each one a
plain Claude Code session in its own terminal. aisquare coordinates them:
atomic task claims, dependencies, a review cycle, per-prompt deltas of what
teammates did, and a live board TUI for you. One Claude account is enough.

## The memory layer

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

## Orchestrate a team of agents

This is the part that changes how you work. Sessions are per **terminal**,
not per account — a single `claude` install runs the whole team:

```sh
pipx install 'aisquare-cli[tui]'         # the live board wants the TUI extra
aisquare agents connect claude-code
cd your/repo

AISQUARE_ROLE=planner claude             # terminal 1 — you talk to this one
AISQUARE_ROLE=coder   claude             # terminal 2
AISQUARE_ROLE=coder   claude             # terminal 3 — as many as you like
AISQUARE_ROLE=runner  claude             # terminal 4 — verifies the coders' work
aisquare board -w                        # terminal 5 — you, watching live
```

Launching with `AISQUARE_ROLE` opts the repo in and registers the session.
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
| `AISQUARE_MODEL_<ROLE>` | pin a role's model outright (skips the harness ladder) |
| `AISQUARE_EFFORT` | base effort for spawned roles (default `high`, else inherits `CLAUDE_EFFORT`) |
| `AISQUARE_EFFORT_<ROLE>` | pin one role's effort absolutely (skips the role offset) |
| `AISQUARE_HARNESS_PROBE=0` | never probe model availability (ladders resolve optimistically) |
| `AISQUARE_BRAIN=0` | disable the long-term-memory layer |
| `AISQUARE_BRAIN_EMBED=1` | embed distilled pages for semantic recall (needs `OPENAI_API_KEY`; set before the first distill) |
| `AISQUARE_BRAIN_EMBED_MODEL` | embedding model (default `openai:text-embedding-3-large`) |
| `AISQUARE_HOME` | relocate the whole `~/.aisquare` tree |

Running several Claude installs for separate rate limits? Connect each
config dir once: `aisquare agents connect claude-code --config-dir
~/.claude2`. For executions spanning multiple repositories, set
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
│                   spawn <role> [--exec] [--probe/--no-probe] [--refresh]
│                                 [--effort LEVEL] · harness
├── task            add · list · show · next [--role R] [--status S] [--claim]
│                   claim · review [--note] · reopen --reason · done [--note]
│                   block --reason · drop · release        (all with [--as SESSION])
├── note <text> [--task T] [--to ROLE] [--kind note|decision|question|result]
├── board [-w] [-i SECONDS] · recall <query>
├── serve [--stdio | --port N --bind H] [--show-token]
└── config          list · get <key> · set <key> <value> · redaction <off|standard|strict>
```

| Global flag | Meaning |
| --- | --- |
| `-V` / `--version` | print the version and exit |
| `--json` | machine-readable JSON on stdout |
| `-v` / `-q` | verbose / quiet |
| `--no-color` | disable coloured output |
| `--profile NAME` | configuration profile |

You'll spot a few more groups in `--help` — `auth`, `sync`, `connectors`,
`capture`, `policy` — that's the cloud roadmap (sync across machines,
managed connectors). Each says so plainly when invoked rather than
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
