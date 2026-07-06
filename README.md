# aisquare

[![PyPI](https://img.shields.io/pypi/v/aisquare-cli.svg)](https://pypi.org/project/aisquare-cli/)
[![Python versions](https://img.shields.io/pypi/pyversions/aisquare-cli.svg)](https://pypi.org/project/aisquare-cli/)
[![CI](https://github.com/AISquare-Studio/aisquare-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/AISquare-Studio/aisquare-cli/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Portable memory layer for coding agents.** `aisquare` installs into agents
like Claude Code and keeps their context — your preferences and each project's
conventions — persistent across sessions and machines.

## Quickstart

```sh
pipx install aisquare-cli              # or: pip install aisquare-cli
cd path/to/your/repo
aisquare init                          # set up ~/.aisquare + snapshot this project
aisquare agents connect claude-code    # install into Claude Code (optional)
aisquare doctor                        # optional, recommended — verify setup & deps
```

Installs two commands, `aisquare` and `asq`. The PyPI package is **`aisquare-cli`**;
the command stays `aisquare`. `pipx` is recommended (isolated, stable on PATH).
Requires **Python 3.11+**; optional **Node.js + [repomix](https://github.com/yamadashy/repomix)**
for codebase snapshots (`aisquare doctor` tells you what's missing).

> **Status: early.** The full command surface exists and parses arguments.
> Implemented and backed by a local SQLite store: `init`, `remember`, the full
> `context` group, `inject`/`why`, the `project` group (incl. a **Repomix
> codebase snapshot**), `status`/`doctor`, the `config` group, `log`, and the
> `agents` group — which **installs into Claude Code** (see
> [Claude Code integration](#claude-code-integration)). The remaining commands
> (`auth`/cloud, `capture`, `sync`, `connectors`, `policy`) are stubs: each
> prints `⚠ aisquare <command> is not implemented yet (planned: <tier>)` to
> stderr and exits `70`. Features land one service module at a time — see
> [Implementing a feature](#implementing-a-feature-stub--service).

## Implemented

```sh
aisquare init                    # set up ~/.aisquare, register & onboard this project
aisquare remember "prefer pytest over unittest" --user --tag testing
aisquare context add "run make check before pushing" --project
aisquare context list            # user pool + the active project's pool
aisquare context search pytest   # full-text search (SQLite FTS5)
aisquare context show a3f2       # by id or unambiguous prefix (git-style)
aisquare context edit a3f2       # opens the entry in $EDITOR
aisquare context promote a3f2    # move a project entry into the user pool
aisquare context remove a3f2     # soft-delete (tombstoned)
aisquare context export out.md   # export in-scope context (md or --format json)
aisquare context import notes.md # seed context from Markdown bullets or JSON
aisquare context preview         # the context block that would be injected
aisquare inject                  # emit that block (and record the injection)
aisquare why                     # explain the last injection
aisquare project list            # registered projects (active one marked *)
aisquare project switch alpha    # pin the active project (name or id prefix)
aisquare project onboard         # pack a Repomix snapshot + seed ecosystem facts
aisquare agents scan             # detect installed agents (Claude Code, …)
aisquare agents connect claude-code  # install hooks + ingest CLAUDE.md
aisquare log                     # captured prompt history for this project
aisquare status                  # health, pools, active project, agents
aisquare doctor                  # checks deps/install/hooks/snapshot + how to fix each
aisquare config set default_pool user   # read/write config (get/list/redaction)
aisquare --json context list     # machine-readable output (any command)
```

Context lives in two pools — `user` (global) and `project` — persisted in a
SQLite database at `~/.aisquare/context.db`. The **active project** is whichever
you `project switch` to (pinned in `state.json`), else the one containing your
working directory; everything scopes to it consistently. Entries carry
sync-ready metadata (`updated_at`, soft-delete tombstones) and time-sortable,
prefix-addressable ids from day one.

## Claude Code integration

`aisquare agents connect claude-code` makes aisquare an active part of Claude
Code by writing two hooks into `~/.claude/settings.json` (merged, never
clobbering your other settings; remove them with `agents disconnect`):

- **`SessionStart` → `aisquare hook session-start`** — injects a directive that
  points Claude at the codebase snapshot (skeleton first, full pack on demand)
  and the prompt history, plus your in-scope context — so Claude orients without
  burning tokens grepping for files.
- **`UserPromptSubmit` → `aisquare hook user-prompt-submit`** — captures how you
  prompt, so Claude can replay your intent (`aisquare log`).

### Team bus — parallel Claude Code sessions, one shared working memory

Run several Claude Code sessions on one problem — a planner you talk to,
coders that pick work up, a runner that verifies — and they coordinate
through a shared bus in the same SQLite store: a live session board, an
**idempotent** shared task list with **atomic claims** (lease-based, so a
dead session's claims self-release), task **dependencies**, and an
append-only event pipe delivered to each session as a compact delta on its
next prompt. Everything degrades gracefully: repos that never opt in see
nothing.

#### Quickstart (one Claude account is plenty)

Sessions are per **terminal**, not per account — a single `claude` install
runs the whole team:

```sh
pipx install 'aisquare-cli[tui]'         # or: pip install 'aisquare-cli[tui]'
aisquare agents connect claude-code      # installs the lifecycle hooks
cd your/repo

AISQUARE_ROLE=planner claude             # terminal 1 — talk to this one
AISQUARE_ROLE=coder   claude             # terminal 2
AISQUARE_ROLE=coder   claude             # terminal 3 (as many as you like)
AISQUARE_ROLE=runner  claude             # terminal 4 — verifies coders' work
aisquare board -w                        # terminal 5 — you, watching live
```

Launching with `AISQUARE_ROLE` activates the bus for that repo and registers
the session; every session is told its id, its teammates, and its
**role-specific work cycle** automatically (planner: stock the board; coder:
`task next --claim` → work → `task review`; runner: verify → `done` /
`reopen --reason`). Reopen feedback rides the pipe back to whichever coder
picks the task up next — you never forward anything.

Useful commands (agents learn these from their injected briefing):

```sh
aisquare task add "wire auth" --role coder     # idempotent — safe to re-emit
aisquare task add "ship it" --needs tsk_…      # dependency: held until ready
aisquare task next --role coder --claim --as <id>   # atomic, one winner
aisquare note "JWT it is" --kind decision --as <id>
aisquare recall "what did we decide about auth?"    # long-term memory
```

Several Claude installs (`CLAUDE_CONFIG_DIR` aliases) give sessions separate
rate limits — connect each with `aisquare agents connect claude-code
--config-dir ~/.claude2`. For executions spanning several repositories, set
`AISQUARE_TEAM_HUB=/path/to/hub` in every session so they share one bus;
git worktrees already share their principal repo's bus automatically.

#### The live board (`aisquare board -w`)

Interactive TUI (with the `[tui]` extra; falls back to a full-screen Rich
view without it): sessions with live state chips — **▶ working**,
**⏸ waiting for input**, **🔔 NEEDS YOU** (with a terminal bell) — the open
tasks, and a bot-style feed of everything the team does. Click any task or
feed line for its full detail in the bottom bar.

| Key | Action |
| --- | --- |
| `t` | theme browser — stays open, every change applies + autosaves |
| `a` | toggle feed autoscroll |
| `v` / `c` | select-text mode (frozen, mouse-selectable feed) / copy selection |
| `s` | save an SVG screenshot to `~/.aisquare/screenshots/` |
| `b` | show/hide the board pane (auto-hides on narrow terminals) |
| `r` / `q` | refresh now / quit |

#### Long-term memory (gbrain)

Durable events — decisions, results, task outcomes, reopen feedback — are
distilled into a per-project [gbrain](https://www.npmjs.com/package/gbrain)
brain by a detached worker (never on the hot path; requires `gbrain` on
PATH; initialised automatically with embeddings off; silently skipped when
absent). `aisquare recall "<question>"` searches it; `aisquare team distill`
drains on demand; `aisquare doctor` reports brain health.

#### Remote agents (MCP)

`aisquare serve` (the `[serve]` extra) exposes the same bus to Claude clients
that are not local terminal sessions — e.g. a browser-debugging agent in the
Claude desktop app:

```sh
aisquare serve                   # streamable HTTP on 127.0.0.1:8747, bearer-token auth
aisquare serve --show-token      # connection details for the client
aisquare serve --stdio           # stdio transport (Claude Desktop launches it)
```

Remote callers act as an attributed virtual session (`mcp:<client>`): their
tasks and notes hit the board and everyone's deltas like any teammate's. For
Claude Desktop on Windows + WSL2, either add the HTTP URL (Windows reaches
WSL2 via localhost) or register a stdio server in
`claude_desktop_config.json`:

```json
{"mcpServers": {"aisquare-team": {"command": "wsl", "args": ["-e", "bash", "-lc",
  "cd /path/to/your/repo && aisquare serve --stdio"]}}}
```

#### Env knobs

| Variable | Effect |
| --- | --- |
| `AISQUARE_ROLE` | role for this session; also activates the bus for the repo |
| `AISQUARE_TEAM=0` | master off switch (hooks and commands no-op) |
| `AISQUARE_TEAM_HUB` | pin sessions from several repos onto one bus |
| `AISQUARE_TEAM_DELTA=0` | mute per-prompt teammate deltas for a session |
| `AISQUARE_TEAM_LEASE_MIN` | claim lease in minutes (default 120) |
| `AISQUARE_BRAIN=0` / `AISQUARE_BRAIN_EMBED=1` | disable the gbrain layer / embed distilled pages |

The **codebase snapshot** (`project onboard`, or `init`) mirrors the server-side
[Repomix](https://github.com/yamadashy/repomix) packing for sync-consistency: a
full pack (`repomix --style xml`), a skeleton (`--compress`), and a per-file
index (char offsets + token counts), stored under
`~/.aisquare/projects/<id>/snapshot/`. Requires Node + repomix on PATH (run via
`npx` otherwise); if neither is present the snapshot is skipped, not fatal.

## Install (development)

```sh
git clone https://github.com/AISquare-Studio/aisquare-cli && cd aisquare-cli
python3 -m venv .venv
source .venv/bin/activate
make install          # = pip install -e ".[dev]"
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the dev workflow and how to implement a command.

## Quick check

```sh
aisquare --help
aisquare --version
aisquare doctor              # install/deps/integration health
asq ctx list                 # aliases work too
```

## Command tree

```
aisquare
├── init [path] [--api-key K] [--local] [--agent A]... [--no-onboard] [--reinit] [-y]
├── status · doctor · inject · sync · why · log · open
├── remember <text> [--user|--project] [--tag T]...
├── login · logout · whoami · upgrade · uninstall
├── auth        status · rotate · token
├── agents      list · connect <name> · disconnect <name> · scan · status [name]
├── connectors  list · add <name> · remove <name> · status
├── context     list · add <text> [--user|--project] [--tag T]... · show <id> · edit <id>
│   (alias ctx) remove <id> · search <query> · preview · import <file>
│               export [file] [--format md|json] · promote <id>
├── project     info · list · switch <name> · link <repo> · onboard [path] [--refresh]
│   (alias workspace)
├── team        on · status · focus <text> · role <name> · log · distill
├── task        add <title> · list · show <id> · next [--role R] [--status S] [--claim]
│               claim <id> · review <id> · reopen <id> --reason · done <id>
│               block <id> --reason · drop <id> · release <id>   (all with [--as SESSION])
├── note <text> [--task T] [--to ROLE] [--kind K] · board · recall <query>
├── serve       [--stdio | --port N --bind H] [--show-token]
├── capture     status · pause · resume · start · stop
├── config      list · get <key> · set <key> <value> · redaction <off|standard|strict>
├── policy      list
└── enforce     status · enable · disable
```

### Global flags

| Flag | Meaning |
| --- | --- |
| `-V`, `--version` | Print the version and exit |
| `-v`, `--verbose` | Verbose output |
| `-q`, `--quiet` | Suppress non-essential output |
| `--json` | Machine-readable JSON on stdout |
| `--profile NAME` | Configuration profile to use |
| `--no-color` | Disable coloured output |

Global flags go **before** the command: `aisquare --json context list`.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `2` | Usage error (bad arguments) |
| `70` | Command not implemented yet |

## Architecture

```
src/aisquare/
├── cli/          # THIN Typer layer: one module per command group.
│   │             # Parses arguments, then calls exactly one service function.
│   ├── app.py    # root app: global flags, --version, group registration
│   ├── root.py   # top-level commands (init, status, remember, ...)
│   ├── common.py # shared parsing helpers (e.g. --user/--project → pool)
│   └── <group>.py
├── services/     # SERVICE layer: one module per domain. Real behaviour goes
│   │             # here. Today every function raises the shared stub.
│   └── <domain>.py
├── core/         # shared infrastructure (already real):
│   ├── paths.py  #   ~/.aisquare layout (override with $AISQUARE_HOME)
│   ├── config.py #   typed TOML config load/save (Pydantic + tomllib/tomli-w)
│   ├── store.py  #   SQLite context store (ContextStore protocol + open_store)
│   ├── ids.py    #   ULID-style, time-sortable, prefix-addressable entry ids
│   ├── entries.py#   shared ContextEntry factory (add / import / onboard)
│   ├── workspace.py #  resolve the active project (pin in state.json, else cwd)
│   ├── injection.py #  assemble the context block + record injections (why)
│   ├── agents.py #   detect agents + install Claude Code hooks (settings.json)
│   ├── snapshot.py #  Repomix codebase pack (full + skeleton + index)
│   ├── editor.py #   launch $EDITOR for `context edit`
│   ├── state.py  #   runtime state from the global flags
│   ├── console.py#   Rich console factories honouring --no-color
│   └── stubs.py  #   stub() — the consistent not-implemented behaviour
└── models.py     # Pydantic domain models (ContextEntry, DataEnvelope, ...)
```

Flow: `cli/<group>.py` parses arguments → calls `services/<domain>.py` →
(today) `core/stubs.py:stub()` prints the not-implemented message and raises
`typer.Exit(70)`.

**What is real today:** `--help` everywhere, `--version`, global-flag parsing
into `core/state.py`, the `~/.aisquare/` layout, TOML config load/save, the
SQLite context store (`core/store.py`), and the commands wired to it — `init`,
`remember`, the full `context` group (`add`, `list`, `show`, `edit`, `remove`,
`search`, `promote`, `import`, `export`, `preview`), `inject`, `why`, the
`project` group (`info`, `list`, `switch`, `link`, `onboard`+snapshot),
`status`, `doctor`, the `config` group (`list`, `get`, `set`, `redaction`),
`log`, and the `agents` group (`scan`, `list`, `status`, `connect`+hooks,
`disconnect`). Everything else is a stub.

### `~/.aisquare/` layout

```
~/.aisquare/
├── config.toml   # typed configuration (core/config.py)
├── credentials   # API keys / tokens
├── context.db    # SQLite store: context entries, projects, captured prompts
├── state.json    # small runtime state (e.g. the pinned active project)
├── agents.json   # registry of connected agents
├── projects/     # per-project data — <id>/snapshot/ (Repomix pack + skeleton + index)
├── cache/        # disposable cached data (e.g. last_injection.json)
└── log/          # capture and diagnostic logs
```

Set `AISQUARE_HOME` to relocate the whole tree (the test suite does this).

## Implementing a feature (stub → service)

Each feature is implemented by replacing one `stub(...)` call in one service
module. The CLI wiring, argument parsing and signatures already exist. Example —
making `aisquare context add` real:

**1. Implement the service** (`src/aisquare/services/context.py`). Replace the
stub with real logic; keep the existing signature, it is already final.
Persisted state goes through the `ContextStore` from `core/store.py`. The
already-implemented `add_entry` is the worked example:

```python
def add_entry(text: str, pool: Pool | None, tags: list[str]) -> ContextEntry:
    """Add a context entry to the user or project pool."""
    resolved: Pool = pool or load_config().default_pool
    with store_session() as store:
        project_id: str | None = None
        if resolved == "project":
            project = current_project()
            store.ensure_project(project)
            project_id = project.id
        now = datetime.now(tz=UTC)
        entry = ContextEntry(
            id=new_entry_id(), pool=resolved, project_id=project_id, text=text,
            tags=tags, source="cli", created_at=now, updated_at=now,
        )
        return store.add(entry)
```

**2. Render in the CLI layer** (`src/aisquare/cli/context.py`). The CLI module
stays thin: parse, call the service, print. Honour `--json` via the runtime
state:

```python
@app.command("add")
def add(text: ..., user: ..., project: ..., tag: ...) -> None:
    """Add a context entry."""
    entry = context_service.add_entry(text, pool=resolve_pool(user, project), tags=tag or [])
    if get_state().json_output:
        typer.echo(entry.model_dump_json())
    else:
        stdout_console().print(f"✓ remembered ({entry.pool}): {entry.text}")
```

**3. Update the tests.** The walk-based test in `tests/test_stubs.py` asserts
that every leaf exits with `70`; once a command is real it will fail there —
add the command to an explicit "implemented" skip-list in that test and write
real tests for the new behaviour.

Rules of thumb:

- CLI modules never contain behaviour; services never parse CLI arguments.
- Services return data; the CLI renders it. (`stub()` printing is the one
  deliberate exception, so all unimplemented commands behave identically.)
- New shared plumbing goes in `core/`; new domain shapes go in `models.py`.

## Development

| Task | Command |
| --- | --- |
| Install (editable + dev tools) | `make install` |
| Run tests | `make test` |
| Lint | `make lint` |
| Type-check | `make typecheck` |
| Format + autofix | `make fmt` |
| All CI checks | `make check` |

Run a single test: `pytest tests/test_stubs.py -k "context add"`.
