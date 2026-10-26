# Memory

**Your agent starts every session already oriented.** `agents connect
claude-code` installs lifecycle hooks into `~/.claude/settings.json` (merged
carefully — your existing hooks are never touched). From then on, each session
begins with your in-scope context entries, less any preference a `CLAUDE.md`
Claude Code loads already holds (it would be in front of the agent twice), and
your prompts are captured for `aisquare log`. A session with a team also gets
the board and its protocol; a solo one does not.

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

Prompts are recorded only in a project you added (`aisquare project onboard`,
`init`, Welcome or `+` in `asq`, a fleet spawn): a hooked session anywhere else
keeps nothing. `[projects] capture = true` records them in every directory, as
0.8.0 did ([fleet](fleet.md#naming)).

`aisquare config set capture.enabled false` stops recording prompts
everywhere: `aisquare log` stays empty and nothing is spooled for
Explainability. Teammate updates still arrive, and each turn still gets its
metrics row, without the text. A config file that cannot be read records no
prompt either, and says so on stderr.

The **active project** is whichever repo contains your working directory, or
the one you pin with `aisquare project switch <name>`. Everything —
context, prompt history, team state — scopes to it consistently.

**Git worktrees resolve to their principal repository**, so several feature
branches checked out side by side all share one context pool and one board. Set a repo's conventions up once and every worktree of it starts
oriented; identity comes from `git rev-parse --git-common-dir`, not from
walking up to the nearest marker.

Registrations accumulate — every directory a command once ran in is one, and
the fleet UI loads state for all of them. `aisquare project forget` drops one
(its context, prompt history and board rows stay in the store, hidden, until
the root is registered again; `--purge` deletes them and its data directory too), and
`aisquare project prune` sweeps the stale ones: roots that no longer exist, and
worktrees of a repository that is itself registered. It shows the plan and asks
before dropping anything; off a terminal it is a dry run unless `--yes`. Both
refuse a project that has live fleet agents (`prune` keeps it and says so), and
a hub whose board another project's fleet agents read: running ones, and stopped
ones a restart would start under it again. `forget` names them, and what frees
it. A `prune` that drops the reading project too, its agents all stopped, drops
the hub with it: every hold is decided before anything goes, so the plan and
the sweep agree.

```sh
aisquare project forget ../old-checkout       # one registration; --purge deletes its history too
aisquare project prune --missing --worktrees  # the stale ones, after a confirmation (or --yes)
```

A project you reuse piece of work after piece of work can **start fresh**
instead: `aisquare project fresh` (alias `reset`) stops its fleet, removes the
agents' worktrees, clears its board (tasks, notes, sessions, signals and its work
briefs, with the evidence they keep under `~/.aisquare/briefs/<id>/`) and its
fleet rows, deletes the manager's state (`~/.aisquare/projects/<id>/manager/`,
its `state.md`), so task and brief numbers start at #1 again and the new manager
starts blank, and starts that manager. It keeps the registration and its
settings and the symbol index (`~/.aisquare/projects/<id>/code`). It does not
delete the command reports (`~/.aisquare/reports/`), but with their briefs gone
they are no longer pinned as evidence (unless another board's brief cites one
too): retention takes them as it takes any other
([command reports](command-reports.md)). Its context entries, prompt log and
fleet branches stay unless `--memory`, `--prompts` or `--branches`. Work in a
worktree or branch that no remote has refuses it unless `--discard-unpushed`.
`--dry-run` shows the plan.

```sh
aisquare project fresh --dry-run              # what it would clear
aisquare project fresh --memory --yes         # clear the context entries too, without asking
```

`aisquare project onboard` adds a folder as a project on purpose and seeds its
context pool with one fact per marker it finds at the root (`pyproject.toml`,
`package.json`, `Cargo.toml`, a `Makefile`, a `Dockerfile` and a few more). A
second run seeds nothing; `--refresh` reads the markers again and adds only the
facts that are new. It runs no packer: up to 0.8.2, `onboard` also packed a
codebase snapshot, and the first command of 0.9.0 or later that opens the
store deletes the old packs under `~/.aisquare/projects/<id>/snapshot/`.

### Symbol index

Each project has a **symbol index**: where a name is
defined, who uses it, and what already looks like it. An agent asks it before it
greps, and before it writes a helper:

```sh
aisquare code find spawn                 # definitions: exact, then case-insensitive, prefix, name parts
aisquare code refs nudge_manager --defs  # every use, its definitions first
aisquare code similar parse_duration     # like-named definitions: run it before writing a helper
aisquare code index --status             # what the index holds, and whether it is behind the checkout
```

Every command takes `--json`, and the MCP server (`aisquare serve`) offers the
same as one tool, `code_lookup(query, mode, limit)`, once the project has an
index. `similar` also takes `path:line` of a definition.

The index is built when a project is onboarded (`init`, `project onboard`,
`--refresh` rebuilds it), in the background: the onboard returns at once, and the
doctor's `code index` row says "building" until it is ready. A query that comes
first (`code find`, `refs`, `similar`) builds the index itself, or says to try
again while the background build still holds it. Where one exists, each session
start starts an incremental refresh in the background, at most once a minute per
project, never on the hook's own time; it adds no line to the session's context. `aisquare code index` brings it up to date by hand: only files whose
size, time or content moved are read again, deleted files are dropped, and a
change of parser rebuilds it all. On aisquare-cli itself (500 Python files) a full
build takes about 4 s and a one-file refresh 0.03 s. It lives in
`~/.aisquare/projects/<id>/code/index.db`, apart from the memory store;
it is a cache of the checkout, so deleting it loses nothing. It is the project's
root checkout that is indexed: a coder in a fleet worktree asks the same index.

What it reads: git's file list (tracked, plus untracked files that are not
ignored), less an ignore list, binaries, and files over `[code] max_file_kb`
(1024). The ignore list is built in — `node_modules`, `.venv`/`venv`, `.git`,
`__pycache__`, `dist`, `build`, `coverage`, `.aisquare-worktrees`, `*.worktrees`,
and any nested git repository or worktree found below the root, so another
project's checkout is never indexed into this one — and `[code] ignore`
**extends** it (globs, comma-separated on the command line):

```sh
aisquare config set code.ignore '**/fixtures/**,docs/generated/**'
```

Up to 0.8.2 that list was `[snapshot] ignore`; a config that still has it is read
as `[code] ignore`, and moved there at the next save. Python is parsed with the standard library, with
nothing to install. TypeScript and JavaScript need the optional extra:

```text
pip install --upgrade "aisquare-cli[code]"
```

which brings tree-sitter and the two grammars' wheels, each with its compiled
grammar and its own tags query: nothing is downloaded when the index is built,
and nothing leaves the machine. Other languages are listed as not indexed
(`index --status`, the doctor's `code index` row). `[code] enabled = false`
stops the build at onboard and on a first query; `aisquare code index` still
builds one by hand.

For definitions and references through a language server instead, Claude Code
has official code-intelligence plugins per language (for Python,
`pyright-lsp@claude-plugins-official`, with the server binary on `PATH`).
Plugins are installed per Claude config directory, so each account slot the
fleet uses needs it once.
