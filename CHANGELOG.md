# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `aisquare team prune` — retire ghost sessions and return their orphaned
  claims to the pool (#18).
- `aisquare serve --stdio` gains an idle deadline — `--close-after SECONDS`
  (env `AISQUARE_SERVE_CLOSE_AFTER`, flag wins; default 300; `0` = run
  forever): the daemon exits 0 on its own once no client message has arrived
  for that long, so clients killed mid-handshake can no longer strand
  orphaned daemons (#19). Pipe-EOF still exits immediately; HTTP mode is
  unaffected. This retires the `pkill`/`xargs` workarounds from #19.
- `AISQUARE_DB_BUSY_MS` — busy-timeout knob for the context store (default
  5000), so tests can wedge the store without waiting out the full timeout.
- **Delivery self-check (#22)** — the pull side of #20's receipts.
  `aisquare team verify RECEIPT` re-proves a write by seq or event id
  (prefix ok): found on your board → the event, exit 0; missing → exit 1
  (`not_found`, with a `hint` naming the board that holds it when it lives
  elsewhere). `aisquare team log` grows combinable filters — `--by`,
  `--mine` (with `--as`), `--since 15m|2h|ISO`, `--since-seq` (cursor
  semantics), `--kind`, `--task`. MCP parity: `team_log` gains
  `by_session` (literal `me` supported) and a new eighth `verify` tool.
  The injected session protocol now points at the receipt → verify loop.
- **First-class signals (#23)** — named board states instead of prose
  tokens. `aisquare team signal NAME VALUE --as SID` sets (single-token
  name/value), `team signal NAME` reads (value, set_by, set_at, seq),
  `team signals` lists; all with `--json`. Every set emits a `signal`-kind
  event whose payload carries structured `name`/`value`/`prev`/`set_by`
  fields — watchers filter `team log --kind signal --since-seq N` and key
  on fields, never text, so "NOT READY" prose can no longer trip a `ready`
  watcher. State lives in the existing `team_meta` table (no migration),
  the pipe event and state blob commit atomically, and sets follow the
  #20 receipt/read-back contract (`team verify` works on signal seqs).
  MCP: one combined `signal(name, value?)` tool — nine tools total.
- Sessions record **which agent config dir (account) they run under**, derived
  from the transcript path in the hook payload (so it works whether or not the
  agent exports `CLAUDE_CONFIG_DIR` to hook subprocesses). The board and the
  `board -w` TUI label sessions with the account name once more than one is in
  play, making a rate-limited account's terminals identifiable at a glance.
  Schema v9 adds `team_session.account`.
- `aisquare launch <planner|coder|runner>` — starts an agent session already
  attached to the project's team board, replacing the `AISQUARE_ROLE=coder
  claude` env-var-prefixed launch. Validates the role, opts the repo in
  explicitly, then `exec`s the agent so signals, job control and the TTY are
  unchanged. Extra arguments are forwarded (`aisquare launch coder --model
  opus`); `--command` launches an agent other than `claude`. The
  `AISQUARE_ROLE` variable still works.
- `aisquare launch --account <dir>` — run a role under one of several parallel
  agent installs by pointing at its config directory (sets
  `CLAUDE_CONFIG_DIR`). Fails on a directory that does not exist, since a typo
  would otherwise start a fresh unauthenticated profile. Shell aliases like
  `claude1` cannot be passed to `--command` — aliases are not executables —
  so `--account` is the supported route for multi-account setups.

### Fixed

- **Parallel agent installs are now tracked per config directory.** The
  registry recorded a bare agent name, so `agents list` and `doctor` only ever
  inspected `$CLAUDE_CONFIG_DIR` or `~/.claude`. With several accounts
  connected, a sibling install whose hooks had been removed still reported a
  healthy `✓ claude-code: Claude Code connected`. `agents.json` now records
  every connected directory; `doctor` checks them all and names the ones
  missing hooks, and `agents list` gains a `HOOKS IN` column. Disconnecting one
  directory no longer marks the agent disconnected while others remain hooked.
  Registries in the old format are migrated on read.

### Changed

- **Store-error honesty (#20 hardening).** Write receipts quote the board's
  `project_id` instead of its directory name (names collide across
  checkouts). `store_locked` now means genuinely retryable lock/busy
  contention only; other database failures (no such table, readonly, disk
  full, corruption) surface as a distinct `store_error` — both carry the
  real cause in a `detail` field under `--json`, and nothing tracebacks.
  `note --task` rejects a task from another project's board (the guard
  `--needs` already had), the store's setup-retry budget scales with
  `AISQUARE_DB_BUSY_MS` (no more 15s floor on a wedged fresh database), and
  the knob clamps at SQLite's 32-bit ceiling so oversized values can no
  longer silently disable the busy handler.
- Roadmap commands are now **hidden from `--help`**: `auth`, `login`,
  `logout`, `whoami`, `sync`, `connectors`, `capture`, `policy`, `enforce`,
  `open`, `upgrade`, `uninstall`. They remain registered and still report the
  not-implemented contract (exit 70) when invoked — only the listing changes.
  `aisquare --help` lists only entries that work.
- The README is split into **Part 1 — Memory (start here)** and **Part 2 —
  Orchestration (advanced)**, with an explicit note that orchestration is
  optional, so the light half of the product no longer reads as heavy.

### Fixed

- Unknown subcommands fail loudly instead of silently (#21): the usage error
  now carries a did-you-mean over the failing group's real verbs (root and
  alias groups included), and when `--json` was parsed before the failure the
  error arrives as one JSON object on stdout (`unknown_command` with
  `did_you_mean`, or `usage` for unknown options) with exit code 2 — so a
  typo can no longer masquerade as an empty result in pipelines. A `--json`
  trailing the typo falls back to the human path by design; lead with
  `--json` for guaranteed machine-readable errors.

- **Team writes cannot lie about success (#20).** `--as`-attributed commands
  (`note`, `task add`, `task next`, …) now deliver to the acting *session's*
  board — never the cwd's — warning loudly when the two disagree. Every
  event-emitting write is read back through a fresh store connection before
  `✓` is printed; the `✓` line carries a receipt (`seq N on <board>`) and
  `--json` output gains a top-level `delivered: true` (plus `warning` on a
  board mismatch). Unconfirmed writes exit 1 with `delivery_unconfirmed` (the
  payload's `ref` names the write), and a locked store maps to a clean
  `store_locked` error instead of a traceback. A failure can leave a
  durable-but-unconfirmed write — check `aisquare log` for the reported ref
  before retrying, or a retried note/claim may duplicate work.

### Fixed

- The global output flags — `--json`, `--verbose`/`-v`, `--quiet`/`-q`,
  `--no-color` and `--profile NAME` — are accepted anywhere on the command
  line: before or after the subcommand, on every command including nested
  groups. Boolean flags OR across positions (duplicates are idempotent);
  `--profile`'s last occurrence wins. `--version` stays root-only (#24).
- **Git worktrees now share their principal repository's context pool.** A
  linked worktree's `.git` is a *file*, so the marker walk in
  `workspace.find_project_root` stopped inside the worktree and handed it its
  own project id — a feature branch checked out beside the repo saw an empty
  context pool, even though team traffic (which already asked
  `git rev-parse --git-common-dir`) correctly shared one board. Both paths now
  use the same git-aware resolution, so several feature branches side by side
  share one context pool, one snapshot and one board — which is what the README
  already promised.
## [0.2.0] - 2026-07-07

### Added

- **Agent Orchestrator** — shared working memory for parallel Claude Code sessions on one
  problem (planner / coders / runner). Sessions register automatically through
  hooks; each prompt delivers a compact delta of what teammates did. Works with
  a single Claude account (sessions are per-terminal) or several installs.
  - Shared tasks: idempotent `task add` (safe to re-emit), **atomic**
    single-winner `claim`, `next --role --claim` for looped worker sessions,
    the `review` → `done` / `reopen --reason` verification cycle, and
    dependencies (`--needs`) so `next` only hands out ready work.
  - `note` / `board` / `team` groups; role work-cycles auto-injected per
    session (planner/coder/runner) — no standing prompts to paste.
  - Live session states on the board — working / waiting for input /
    needs-you — driven by the new `Stop` and `Notification` hooks.
  - `board --watch`: an interactive TUI (`[tui]` extra) — task table +
    bot-style live feed + click-for-detail bar, theme browser (`t`,
    autosaved), local screenshots (`s`), feed autoscroll toggle (`a`) and a
    select-text mode (`v`/`c`). Rich full-screen fallback without the extra.
  - **Long-term memory (gbrain)**: durable events (decisions, results, task
    outcomes, reopen feedback) distill into a per-project gbrain brain via a
    detached, flock-guarded worker; `recall` searches it. Never on the hot
    path; degrades silently when gbrain is absent.
  - **`serve`** (`[serve]` extra): the orchestrator as an MCP server (stdio or
    bearer-token HTTP) so remote Claude clients — e.g. a browser-debugging
    agent in the Claude desktop app — join as attributed virtual sessions.
  - Multi-repo executions via `AISQUARE_TEAM_HUB`; worktree-safe project
    identity (`git rev-parse --git-common-dir`); `agents connect --config-dir`
    for parallel `CLAUDE_CONFIG_DIR` installs.
  - **Semantic recall**: with `AISQUARE_BRAIN_EMBED=1` (and an
    `OPENAI_API_KEY`) distilled pages are embedded and `recall` uses gbrain's
    hybrid vector+keyword search, falling back to keyword when unavailable.
    The embedding schema is fixed at brain-creation time, so the knob must be
    set before the first distill; `doctor` flags a knob-vs-schema mismatch and
    points at the rebuild (`team distill --all`).
  - Env knobs (no config gating): `AISQUARE_TEAM`, `AISQUARE_ROLE`,
    `AISQUARE_TEAM_HUB`, `AISQUARE_TEAM_DELTA`, `AISQUARE_TEAM_LEASE_MIN`,
    `AISQUARE_BRAIN`, `AISQUARE_BRAIN_EMBED`, `AISQUARE_BRAIN_EMBED_MODEL`.

## [0.1.0] - 2026-06-29

First release — a portable memory layer for coding agents.

### Added

- **Context store** (local SQLite): `remember` and the `context` group — `add`,
  `list`, `show`, `edit`, `remove`, `search` (FTS5), `promote`, `import`,
  `export`, `preview` — across `user` and `project` pools, with sync-ready
  metadata (soft-delete tombstones, `updated_at`) and time-sortable,
  prefix-addressable ids.
- **`inject` / `why`** — assemble in-scope context for an agent session and
  explain the last injection.
- **Projects** — `init`, the `project` group (`info` / `list` / `switch` /
  `link`), and `onboard`, which packs a Repomix codebase snapshot (full pack +
  skeleton + per-file index).
- **Claude Code integration** — `agents` group with `connect` that installs
  `SessionStart` + `UserPromptSubmit` hooks (injecting context and capturing
  prompts), plus detection (`scan` / `list` / `status`) and `disconnect`.
- **Diagnostics & config** — `status`, `doctor` (dependency + setup health with
  fixes), the `config` group, and `log` (captured prompt history).

[Unreleased]: https://github.com/AISquare-Studio/aisquare-cli/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/AISquare-Studio/aisquare-cli/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/AISquare-Studio/aisquare-cli/releases/tag/v0.1.0
