# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **`~/.aisquare` is no longer treated as a project root.** `.aisquare` is
  overloaded — `<project>/.aisquare` is the opt-in project marker, but
  `~/.aisquare` is where config, the context database and the agent registry
  live. The marker walk could not tell them apart, so **every markerless
  directory under `$HOME` resolved to `$HOME`** and shared one context pool.
  It also defeated the guard written against exactly that: `serve --stdio`
  refuses to activate a directory that is not a project root *because* Claude
  Desktop launches from `$HOME`, and since `$HOME` always holds `.aisquare`
  the refusal never fired — the server silently activated the home directory
  instead. An aisquare home is now recognised by its layout and skipped as a
  marker. A hand-made `<project>/.aisquare` still works, and `.git`/`.hg`
  are untouched. Not a Windows bug, though Windows shows it most (temporary
  directories live under `%USERPROFILE%`).
- **Credentials and the serve token are now restricted on Windows.**
  `chmod(0o600)` is the whole story on POSIX and does nothing on NTFS, where
  the group/other bits have no equivalent — so the API key and the bearer
  token guarding the HTTP server stayed readable by every other account on the
  machine, with no error to say so. Both files now get an ACL that drops
  inherited entries and grants only the current user; `init` and `serve` say
  so explicitly when the restriction could not be applied, rather than
  implying a protection that is not there. POSIX behaviour is unchanged.

### Changed

- **CI runs on Windows.** The `check` job gains a `windows-latest` leg (3.12;
  the platform branches read `sys.platform` at call time, so a second
  interpreter would only re-run the same branches), and `package` builds and
  smoke-tests the wheel on both platforms. Getting there meant fixing the
  suite's own POSIX-only assumptions rather than skipping past them: the
  gbrain fake is now reachable through `PATHEXT`, the #20 bulk-delivery storm
  and the harness `eval` test are ported instead of skipped, test file reads
  no longer go through the locale codec, and the #56 tilde test sets the
  variable `expanduser` actually reads on each platform. The suite is green on
  Windows with no platform skips.

## [0.4.0rc2] - 2026-08-19

Two PRs on top of rc1. **#48 makes `aisquare` run on Windows at all** — the
package died on `import fcntl` before it could print `--version`, and four more
defects sat underneath that one; read the migration note under Fixed, because
hooks installed by rc1 carry broken quoting and need one `agents connect` to
become runnable. #56 adds the per-role launch profile, folding #52 + #54's
narrower `team.bins` into a single `team.profiles.<role>` map before it reached
a release. Windows is not in the CI matrix yet — the Windows branches read
`sys.platform` at call time and are exercised by monkeypatched tests on ubuntu,
but pre-existing POSIX-only assumptions in the suite need fixing before a
`windows-latest` job can go green.

### Added
- **Per-role LAUNCH PROFILE — the third launch axis, and deliberately the
  dumbest one.** The ladder decides *what* model a role runs on, `--bin` (#52)
  decides *which* executable runs it, and a profile carries *whatever else* the
  operator wants on the command — verbatim. Three axes because they change for
  three different reasons; **one config map**, because they describe one role.
  - `aisquare team bind <role> [--bin CMD] [--env KEY=VALUE ...] [--arg ARG ...]`
    is the one-time setup, with `--unset KEY`, `--clear`, and a bare
    `aisquare team bind` to print the bindings. Everything a role launches with
    is stored under `team.profiles.<role>` — `bin`, `env`, `args`. #52's
    narrower `team.bins` (role → executable) was a strict subset of
    `profiles.<role>.bin`, so it is **deleted rather than deprecated**: it
    reached no release, no config file anywhere holds the key, and a
    hand-written one still loads because unknown keys are ignored. One map is
    one place to look, no precedence rule to learn, and nowhere for a `--clear`
    to leave an entry still steering the role.
  - `aisquare launch <role>` and `aisquare team spawn <role>` carry the binding
    with no flag; `--env KEY=VALUE` (repeatable) adds to or overrides it for a
    single launch. Env merges **per key**, so one variable can be changed
    without discarding its siblings; args **append**.
  - Values may use `~` and `$VAR`, expanded at launch — so one binding follows
    you across machines with different homes. An undefined `$VAR` is left
    verbatim rather than blanked, because a silently empty `CLAUDE_CONFIG_DIR`
    starts a fresh unauthenticated profile that surfaces as a login failure
    hours later instead of the typo it is.
  - **Nothing here interprets what you bind.** Parallel agent installs reached
    through shell aliases are just two env entries; a proxy, a region, or a
    wrapper's own variables work identically, without the CLI learning about
    any of them. Reaching these installs via `--bin` cannot work — an alias is
    not an executable, so `shutil.which("claude2")` is `None`.
  - `team harness` and `spawn`'s banner report which env keys a role carries
    and where each came from (keys only — the values are paths and tokens, and
    a banner is a terminal).

### Fixed
- **`aisquare` runs on Windows (#48).** `core/brain.py` imported `fcntl` at
  module scope and sits on the import path of every command, so a Windows
  install died before it could print `--version` — and fixing that exposed four
  more defects underneath, each independently breaking a feature. Five fixes,
  one commit each, POSIX behaviour unchanged throughout:
  - The brain lock goes through a platform-appropriate primitive — a
    non-blocking `msvcrt` byte-range lock there, `flock` here — behind one
    contract both backends share.
  - Hook commands are quoted for the shell that will actually run them, and
    the matcher that recognises them is the exact inverse. Those two halves
    disagreeing was a two-sided bug: `shlex.quote` wrapped every Windows path
    in single quotes `cmd.exe` has no syntax for, so no hook could launch,
    while `shlex.split` ate the path separators as escapes, so
    `hooks_installed()` always returned `False` — `doctor` reported hooks
    "missing or outdated" with all five sitting in `settings.json`, `connect`
    appended duplicates and `disconnect` could remove nothing.
  - `repomix`/`npx` run through the path `shutil.which` already resolved.
    `CreateProcess` does not apply `PATHEXT`, so a bare name raised
    `FileNotFoundError` and `project onboard` could never pack — which also
    makes `doctor` honest, since it probed with `shutil.which` alone and
    reported repomix available on a machine where packing could not work.
  - A redirected console is reconfigured to UTF-8. Windows streams fall back
    to the ANSI codepage when not attached to a console, which cannot encode
    the `✓`/`⚠`/`→` this CLI prints, so `aisquare doctor > out.txt` exited 1
    on `UnicodeEncodeError` while the same command run interactively was fine.
  - Every `subprocess.run` capturing text decodes as UTF-8 with
    `errors="replace"` rather than the locale codec, which raised
    `UnicodeDecodeError` mid-pack and silently lost repomix's token count.

  *Migration:* hooks installed by an earlier release carry the broken quoting
  and are not runnable. `doctor` now recognises them and reports them
  connected, so re-run `aisquare agents connect claude-code` once to rewrite
  them.
- **`team prune` no longer releases a quiet session's in-progress claim (#49).**
  Presence and ownership now retire on different clocks: the session row still
  goes at the threshold (30m), but its `doing` claims are only returned to the
  pool after 4h of silence. For an agent, thirty minutes of silence is not
  idleness — it is one long tool call, and nothing on the board distinguishes
  that from a crashed terminal. Retiring presence early is self-healing (the
  next heartbeat re-registers the session); releasing a claim early is not,
  because a second agent picks up work the first is still doing. Pass
  `--release-claims` to orphan claims at the presence threshold when you know
  the sessions are dead. `ContextStore.end_session` gains `release_claims`.
- **`save_config` could not write an unset optional field.** TOML has no null,
  so `tomli_w` raises `TypeError` on `None` rather than writing anything — one
  optional field left unset made the whole config file unwritable. Now dumped
  with `exclude_none`, which is also the correct round-trip: the omitted key
  reloads as the model default.
- **`aisquare launch` rejected numbered seats.** A crew running `coder1`,
  `coder2`, … in the same role could not launch: the role whitelist held
  exactly three names. It now accepts a first-class role, a numbered seat of
  one (`coder1`, `validator2`), or any role bound with `team bind` — while
  still refusing a typo like `codr`, which was the footgun the whitelist
  existed to catch.

## [0.4.0rc1] - 2026-08-07

The rc/v2026.08.08 train: everything pending folded into one release —
PRs #39 + #35 (deps/CI unblockers), #38 (shared-session-row banner,
fixes #37), #41 (worktree context + session accounts), #40 (surface cut +
`aisquare launch` + multi-account), #36 (the agent harness), and
#44 + #45 (config-gated session tracing, wired at `launch` and both
`spawn` exits). Review fixes were carried on the folds and are called out
in the bullets; **the rewritten role work-cycles under Changed are a live
behavior change** for existing planner/coder/runner sessions.

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
- **Config-gated session tracing** (`[explainability]`, default **off**) —
  with `explainability.enabled = true`, `aisquare launch` wires the session
  through the AISquare explainability proxy: `ANTHROPIC_BASE_URL` plus the
  `X-Agent-Name`/`X-Pipeline-Id` identity pair (a forwarded `--session-id`
  becomes the pipeline id, so board rows and dashboard Runs share a key).
  Every failure fails **open** — dead or wrong-mode proxy, user-owned
  `ANTHROPIC_*` vars, template typos, header-unsafe roles, even an unreadable
  config file cost the trace, never the launch. Hidden
  `aisquare explainability status|env` commands inspect the wiring; `env`
  emits `$'…'`-quoted exports so the header newline survives `eval`.
  `aisquare team spawn` joins at both exits: `--exec` wires the same env seam
  as `launch`, and the printed command is prefixed with
  `eval "$(aisquare explainability env <role>)"` so a **fresh** pipeline id
  mints per run — an id burned into the printable would be reused on every
  paste and merge those sessions into one dashboard Run.
- **The agent harness** — `aisquare team spawn <role>` resolves each role to
  the strongest model its ladder serves (probe-verified with a 24h cache;
  `--refresh` forgets every cached verdict, `--no-probe` trusts the ladder)
  and an effort level (session base from `AISQUARE_EFFORT`/`CLAUDE_EFFORT`
  shifted by a per-role offset; `max` and `ultracode` are first-class).
  `aisquare team harness` prints the whole roster's resolution. Sessions
  self-report model and effort from the SessionStart payload (schema v10 adds
  `team_session.model`/`effort`), and the board and TUI flag a session whose
  model falls outside its role's ladder as `⚠ off-ladder`.

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
- **The injected role work-cycles are rewritten — a live behavior change for
  every existing planner/coder/runner session**, picked up on the next prompt
  with no relaunch: the planner's tasks carry an explicit contract (objective,
  why, acceptance criteria, boundaries); a **coder blocks instead of
  guessing** when a claimed task has no usable contract (`task block` with
  what's missing, rather than inventing scope); a **runner reopens
  underspecified tasks** with `task reopen --reason` instead of rubber-
  stamping them; and a new **validator** role gates the assembled deliverable
  once before handoff. Expect formerly-silent sessions to push back on vague
  tasks — that is the feature.

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

[Unreleased]: https://github.com/AISquare-Studio/aisquare-cli/compare/v0.4.0rc2...HEAD
[0.4.0rc2]: https://github.com/AISquare-Studio/aisquare-cli/compare/v0.4.0rc1...v0.4.0rc2
[0.4.0rc1]: https://github.com/AISquare-Studio/aisquare-cli/compare/v0.2.0...v0.4.0rc1
[0.2.0]: https://github.com/AISquare-Studio/aisquare-cli/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/AISquare-Studio/aisquare-cli/releases/tag/v0.1.0
