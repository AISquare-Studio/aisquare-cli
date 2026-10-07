# Memory

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

Registrations accumulate — every directory a command once ran in is one, and
the fleet UI loads state for all of them. `aisquare project forget` drops one
(its context, prompt history and board rows stay in the store, hidden, until
the root is registered again; `--purge` deletes them and the snapshot too), and
`aisquare project prune` sweeps the stale ones: roots that no longer exist, and
worktrees of a repository that is itself registered. It shows the plan and asks
before dropping anything; off a terminal it is a dry run unless `--yes`. Both
refuse a project that has live fleet agents.

```sh
aisquare project forget ../old-checkout       # one registration; --purge deletes its history too
aisquare project prune --missing --worktrees  # the stale ones, after a confirmation (or --yes)
```

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
