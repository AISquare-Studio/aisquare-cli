# aisquare

**Portable memory layer for coding agents.** `aisquare` installs into agents
like Claude Code and keeps their context — your preferences and each project's
conventions — persistent across sessions and machines.

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

The **codebase snapshot** (`project onboard`, or `init`) mirrors the server-side
[Repomix](https://github.com/yamadashy/repomix) packing for sync-consistency: a
full pack (`repomix --style xml`), a skeleton (`--compress`), and a per-file
index (char offsets + token counts), stored under
`~/.aisquare/projects/<id>/snapshot/`. Requires Node + repomix on PATH (run via
`npx` otherwise); if neither is present the snapshot is skipped, not fatal.

## Requirements

- Python 3.11+

## Install (development)

```sh
python3 -m venv .venv
source .venv/bin/activate
make install          # = pip install -e ".[dev]"
```

Two equivalent entry points are installed: `aisquare` and `asq`.

## Quick check

```sh
aisquare --help
aisquare --version
aisquare status              # stub → exits 70
aisquare --json status       # {"error":"not_implemented","command":"status"}
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
