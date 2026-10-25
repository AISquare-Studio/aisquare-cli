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
the root is registered again; `--purge` deletes them and its brain too), and
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

`aisquare project onboard` adds a folder as a project on purpose and seeds its
context pool with one fact per marker it finds at the root (`pyproject.toml`,
`package.json`, `Cargo.toml`, a `Makefile`, a `Dockerfile` and a few more). A
second run seeds nothing; `--refresh` reads the markers again and adds only the
facts that are new. It runs no packer: up to 0.8.2, `onboard` also packed a
codebase snapshot, and the first command of 0.9.0 or later deletes the old
packs under `~/.aisquare/projects/<id>/snapshot/`.
