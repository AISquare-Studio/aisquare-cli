# Memory

**Your agent starts every session already oriented.** `agents connect
claude-code` installs lifecycle hooks into `~/.claude/settings.json` (merged
carefully — your existing hooks are never touched). From then on, each session
begins with a directive pointing Claude at a packed snapshot of the codebase
(a structure-only skeleton, read first when it fits one Read; a per-file index
into the rest, opened on demand — orders of magnitude cheaper than grepping
around), your in-scope context entries, and the project's prompt history.

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
context, snapshots, prompt history, team state — scopes to it consistently.

**Git worktrees resolve to their principal repository**, so several feature
branches checked out side by side all share one context pool, one snapshot and
one board. Set a repo's conventions up once and every worktree of it starts
oriented; identity comes from `git rev-parse --git-common-dir`, not from
walking up to the nearest marker.

Registrations accumulate — every directory a command once ran in is one, and
the fleet UI loads state for all of them. `aisquare project forget` drops one
(its context, prompt history and board rows stay in the store, hidden, until
the root is registered again; `--purge` deletes them and the snapshot too), and
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

`aisquare project onboard` (also run by `init`) packs the codebase with
Repomix into three artifacts under `~/.aisquare/projects/<id>/snapshot/`: a
**full pack** (every file), a **skeleton** (structure + signatures — the
cheap thing agents orient from), and a **per-file index** (char offsets +
token counts, so an agent can open one file's slice of the pack instead of
all of it). The index, `index.json`, is a JSON array with one file per line, so
a Grep for a path returns that file's line and a Read can page it; a skeleton
is kept only beside the pack it was made with. Re-run with `--refresh` after big
changes.

The session-start directive says "read this FIRST" only of a skeleton (the
pack when there is no skeleton) of at most 12 000 tokens, as the snapshot
counts them: one default Read. Claude Code's Read returns a page of at most
25 000 of its own tokens, which held about 14 000 of these on a measured
skeleton. A bigger one is named with its size and "never read it from the
top": the agent finds the files it needs in the index and reads only their
slices, so a skeleton paged in whole is never carried by every later request.

A pack has to fit a **token budget**: `[snapshot] max_tokens` in
`~/.aisquare/config.toml`, 150 000 by default (the cap the server packs with).
The full pack is tried first, then a compressed one; when even that is over,
the compressed pack is kept as the **skeleton** with its per-file index and the
full pack is skipped — `onboard` and `aisquare doctor` both say `snapshot:
skeleton only: 2030000 tokens, 1234 files indexed; full pack skipped over
budget 150000 (10990000 tokens)`, and agents are oriented from it through its
index, told its size and never to read it from the top.
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

### Symbol index

Beside the snapshot, each project has a **symbol index**: where a name is
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
start names the commands in its directive (two lines) and starts an incremental
refresh in the background, at most once a minute per project, never on the hook's
own time. `aisquare code index` brings it up to date by hand: only files whose
size, time or content moved are read again, deleted files are dropped, and a
change of parser rebuilds it all. On aisquare-cli itself (500 Python files) a full
build takes about 4 s and a one-file refresh 0.03 s. It lives in
`~/.aisquare/projects/<id>/code/index.db`, apart from the memory store;
it is a cache of the checkout, so deleting it loses nothing. It is the project's
root checkout that is indexed: a coder in a fleet worktree asks the same index.

What it reads: git's file list (tracked, plus untracked files that are not
ignored), less the snapshot's ignore list above, binaries, and files over
`[code] max_file_kb` (1024). Python is parsed with the standard library, with
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
