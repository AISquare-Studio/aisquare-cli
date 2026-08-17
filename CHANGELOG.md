# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`docs/planner-findings-loop.md` — the find→fix loop, and the one thing
  that blocks it.** The write half is done: a traced session opens a Run keyed
  by an id the board also knows, so a finding can be traced back to the
  session, the role and the task that was open at the time. The read half needs
  a read-scoped credential, and the page makes that a five-minute unblock
  rather than a morning of discovery — it carries the falsified hypotheses (the
  403 is not about which studio is pinned), the exact env names to add, the
  gateway routes confirmed to exist, and the loop step to paste into the
  planner's prompt. The loop is driven from our own `joins.jsonl` rather than
  by polling the gateway, because `runs` has no `since` and we already know
  every Run we started. A test pins the page's field table against what
  `record_join` actually writes, in both directions, and is verified to fail
  when a row is renamed.
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
- **The correlation spine: one session, one Run, one key.** Tracing already
  sent an `X-Pipeline-Id`, but it was a random UUID — so a gateway Run and the
  board row for the very same session had nothing in common, and the two
  datasets could not be joined at all. The board keys a session by the id the
  *agent* reports, which means the launcher is the only place the two can be
  made equal: it now mints the id, starts the agent on it
  (`claude --session-id <uuid>`), and traces under that same id.
  - Applies to `aisquare launch`, `aisquare team spawn --exec`, **and** the
    printed `team spawn` command — the default, and the one a human actually
    pastes. The printed form takes its id from the same run-time `eval`, so it
    is still fresh per paste; nothing is ever burned into the banner.
  - Only when tracing is enabled **and** the wiring actually traced. With
    tracing off (the default) the argv is byte-identical to before, and an
    untraced fallback is exactly the launch you would have got anyway — an id
    pinned on a launch with no Run to join is risk bought for nothing.
  - Skipped, loudly, where it cannot be honoured: `--continue` and a bare
    `--resume` name a session that does not exist yet, and an agent that is
    not `claude` (or an install named after it) may not know the flag. Those
    still trace, unjoined, with the reason on stderr — a flag the agent
    rejects would cost the launch, and nothing may. `AISQUARE_PIN_SESSION_ID=0`
    opts out entirely.
  - A `--session-id` or `--resume <id>` you passed yourself is read, never
    doubled: your id is already the board's.
  - Every traced launch appends one JSON line to
    `~/.aisquare/explainability/joins.jsonl` — session id, agent name,
    pipeline id, started at — so board events can be joined to Runs without
    dashboard access. Unwritable log ⇒ a warning, never a failed launch.
- **`config.redaction.level` finally does something, and what it does is keep
  a pasted credential off the network.** The setting has existed since the
  first release with nothing reading it — so `strict` changed no behaviour
  anywhere, which is worse than having no setting, because an operator who set
  it believed they were protected. It is now honoured on the explainability
  shipping path: prompts and board events are scrubbed on their way into the
  spool, before anything is written to a file whose purpose is to be uploaded.
  - `off` ships as typed. `standard` (the default) removes credentials — vendor
    token shapes (`sk-`, `ghp_`, `glpat-`, `xox*-`, `AKIA`, `AIza`), JWTs, PEM
    private-key blocks, `Authorization`/`Bearer` values, `NAME=value` where the
    name says secret, and `user:pass@host` in a URL. `strict` adds identity:
    email addresses, and `/home/<user>` → `~`.
  - `standard` deliberately keeps file paths, hostnames and ports. A pasted key
    is an incident; a path is the substance of an engineering prompt, and
    redacting those by default would gut the dataset in exchange for a risk
    nobody has articulated. An over-match is a sentence the dataset cannot
    learn from, so a test pins that ordinary prose comes back byte-identical.
  - An assignment keeps its key name (`EXPLAINABILITY_API_KEY=[redacted]`), and
    every removal is marked — a silent scrub is indistinguishable from a user
    who typed nothing.
  - **Local capture is untouched.** `aisquare log` and the board row keep
    exactly what was typed; this is about what crosses the network, and
    rewriting someone's own history would make it useless for the debugging it
    exists to support.
  - The `init` consent line now names the level, so whoever says yes learns
    what leaves the machine.
- **The tracing boundary, written down before anyone measures against it**
  (`docs/explainability-tracing-boundary.md`). A Run is a **process**, not an
  agent: identity rides in process-level environment (`ANTHROPIC_BASE_URL` +
  `ANTHROPIC_CUSTOM_HEADERS`), so an in-process Claude Code Task subagent or
  Workflow step inherits the parent's identity verbatim and cannot carry its
  own. Per-role and per-session numbers are real and verified against staging;
  per-subagent numbers **do not exist**, and a query that appears to return one
  is reading root-level spans and attributing them to whichever subagent the
  reader assumed — a plausible number rather than an error, which is why this
  is a data-correctness note and not a docs nicety. Task fan-out is countable
  (`Tool:Agent` spans); a Workflow's is not recoverable at all. Separation
  needs a separate **process**, which is exactly what `aisquare launch` and
  `aisquare team spawn` give you. A test pins the page's mechanical claim
  against the code, so it cannot rot quietly.
  - **`aisquare explainability status` and `doctor` state the active level**,
    status directly under the spool counts — "how much am I sending" and "what
    is in it" are one question. Both surfaces render the same sentence from one
    source so they cannot drift, and both say plainly that the scrub applies to
    what LEAVES: local capture keeps what you typed. `off` renders as the
    setting it is, never as a failed check — doctor makes decisions visible, it
    does not overrule them. The setting spent its whole life being read by
    nothing, so being able to SEE it is what makes it trustworthy.
- **`aisquare explainability status` honours `--json`.** It printed human text
  under `--json` while `team status` and `explainability env` both returned
  real JSON — and this is the command a cutover gets scripted against, so every
  check in the runbook was a grep against prose. The payload carries every
  field the human view shows; `key` splits into `key_env`/`key_set` (never the
  key itself) and the spool counts nest under `shipping` as numbers. A test
  compares the two views so one cannot quietly gain a field the other lacks.

### Fixed
- **Rich was deleting bracketed text out of everything the CLI printed.** Rich
  reads `[...]` as a style tag and removes it, and almost every line this CLI
  prints interpolates data it does not control — paths, git refs, role names,
  config values, binary names, URLs, remembered context text. Two independent
  lanes hit it the same night from different directions: the serve hint reached
  users as `pip install 'aisquare-cli'` with the extra name gone, and the
  doctor's detail column ate the SDK's `[present]` so a configured key read
  exactly like a missing one. Neither raised — both printed a confident wrong
  answer, which is worse.
  - Fixed once, at the console factories, so the safe behaviour is what the
    next call site inherits rather than something ninety of them each have to
    remember. An AST scan counted **87 render sites carrying interpolated
    data**; all are covered by construction. It reaches Rich **tables** too,
    which parse cell text the same way — `aisquare context list` was mangling
    remembered entries.
  - **Deliberate styling is untouched.** `style=` arguments, `Column(style=…)`,
    `header_style` and `rich.text.Text` all bypass the markup parser. The six
    sites that styled text with inline tags now carry that styling structurally
    instead, so the data never reaches a parser — and a test asserts a styled
    line is still styled, on the ANSI Rich actually emits.
  - A test walks the package AST and fails if a `Console` is built outside the
    factories, because that is the one way the default gets bypassed.
- **A machine that never configured tracing reported a failure it did not
  have.** `aisquare explainability status` printed `probe: proxy unreachable at
  http://127.0.0.1:9090/health: <urlopen error [Errno 111] Connection refused>`
  on a stock install. Nothing was wrong with that machine: the shipped default
  points at loopback and nothing is listening, which is exactly right for an
  install that has never asked for tracing. But it read as broken, and the
  first thing anyone does with a line like that is go debug a proxy that was
  never meant to exist yet.
  - The line now distinguishes **not configured** (informational — the default
    is not consulted while tracing is off) from **configured and down**
    (unmistakably red, and still carrying its remediation, because launches
    keep working while silently going untraced). A cold `status` also stops
    dialling the default address at all: nothing to probe means nothing to wait
    for.
  - `status` and `doctor` now render **one sentence from one function**. They
    had already drifted — doctor knew to stay quiet while tracing was off and
    status did not — so the same machine read green in one surface and broken
    in the other.
  - The default `proxy_url` is unchanged and the exit-code rule is unchanged:
    non-zero only when tracing is on and the proxy would not take a session.
    The default being unreachable was never the bug; the wording was.
- **Model probes, gbrain and the detached distiller inherited the launching
  session's tracing identity.** Identity is process-level — it rides in
  `ANTHROPIC_BASE_URL` and `ANTHROPIC_CUSTOM_HEADERS` — and a child gets the
  parent's environment unless told otherwise. So `team spawn`'s availability
  probe, which runs a real `claude -p` per alias, posted a Run wearing
  whichever role happened to be probing: junk data in the dataset, attributed
  to a teammate who never asked a question. Fixed at the source rather than
  leaning on the proxy's junk-run suppression, because the traffic is ours not
  to send. gbrain gets the same treatment — its own env builder already guards
  `ANTHROPIC_API_KEY`, which is the tell that an Anthropic path exists — as
  does the detached `team distill` worker, which outlives the process that
  started it and could otherwise attach to a Run that had already ended.
  Credentials and `PATH` still travel; the strip is only the identity.
- **Every process this CLI starts now carries a written tracing ruling, and it
  is enforced.** `core/spawn.py` holds the inventory — all eleven
  `subprocess`/`exec` call sites, each `traced` or `excluded` with a reason —
  and a guard test walks the package's AST on every run, failing when a call
  site exists that the registry has not ruled on. A docstring inventory drifts
  silently the first time someone adds a `subprocess.run`; this one fails the
  build. Recorded alongside it: Claude Code subagents and Workflow agents run
  *in-process* and inherit their session's environment verbatim, so they
  collapse into the parent's identity. Process is the identity boundary, and
  no launcher change can move it.
- **A proxy URL the agent cannot parse is now refused before it can reach
  one.** `ANTHROPIC_BASE_URL` is the one value in this wiring that costs a
  *launch* rather than a trace: the agent parses it before it can report
  anything, so a malformed one dies at the first request with `API Error:
  Invalid URL` and exit 1. `wire_session` now checks the value it is about to
  set — scheme and host, nothing about reachability, which is still the
  probe's job — and launches untraced with the reason instead. The check is
  deliberately independent of the probe: the probe *happened* to reject an
  unparseable URL as "unreachable", which is both a misleading message (it
  blames the network for a typo in config) and an accident a caller with its
  own `prober` sails straight past. Refused, never repaired — a value we
  invented is a value nobody configured.
- **A corrupt `ANTHROPIC_BASE_URL` already in your environment is now named
  before it kills the launch.** That one is *not* ours to remove — overriding
  the operator's routing is forbidden, and we cannot know it is wrong for them
  — so we still stand down. But the agent is about to fail with a message that
  points nowhere near the cause, so the stand-down now says which value it
  deferred to and that it will not work. Stale shells from before the quoting
  fix are exactly this case.
- **The launcher was about to write a variable the SDK routes on.** Our
  identity marker was called `AISQUARE_AGENT_NAME` — which the Explainability
  SDK already reads as the registered routing identity, and which operators
  set in their own env file. This module even had a constant for it already,
  beside the gateway URL and the API key. Setting it from the launcher would
  have silently overridden the operator's routing, the exact thing the
  reserved-var guard refuses to do for `ANTHROPIC_*`. The marker is now
  `AISQUARE_TRACE_AGENT_NAME`, unambiguously ours, and a test pins that the
  two are different and that the SDK's variable is never written.
- **The run-key marker is named for what it holds.**
  `AISQUARE_SESSION_ID` became `AISQUARE_PIPELINE_ID`. The old name is what
  let a careful reader key spans on it as though it were the board's session
  id — which it is not on any launch that could not be pinned, so those spans
  opened a second Run beside the model traffic. Renamed in the same commit as
  `core.insights.RUN_KEY_ENV_VAR`, which duplicates it to stay off the heavy
  import path; the drift test between them guarantees the pair moves together.
- **Every agent below the first was launching under its PARENT's identity.**
  A traced session's environment carries the wiring that traced it, so
  `aisquare launch` run from inside one hit the "not overriding your routing"
  guard, reported *untraced* — and then handed the child the parent's
  `X-Pipeline-Id` anyway, because standing down leaves the inherited variables
  in place. So the child was not untraced at all: its traffic was filed into
  the parent's Run under the parent's role. That is the whole shape of the
  morning's collective-intelligence work — agents spawning agents — and it
  would have produced one Run wearing one identity for an entire tree.
  A parent's identity is now disowned before the child wires its own, at both
  launch seams. Only ever *ours*: a gateway the operator exported has no
  marker beside it, is not ours, and still makes us stand down untouched.
- **A role bound to a wrapper is now joined, not just traced.** The
  session→Run join moved off the launcher and onto the hook that runs *inside*
  the agent — the one place that holds both halves, since Claude Code hands it
  the board session id and the launcher left the pipeline id in the
  environment. It needs nothing from the binary, so a wrapper that has never
  heard of `--session-id` joins exactly like the default agent. Pinning the id
  with `--session-id` survives as a strict extra for the one program verified
  to accept it, narrowed from "anything named claude*" to exactly `claude`,
  because since #57 an unknown flag can be a dead launch and the hook seam
  already guarantees the join. One row per session, both halves always real.
- **`aisquare launch` ignored the active target's overrides.**
  `explainability enable --target prod --proxy-url …` writes per target, and
  the wiring only ever read the top level — so a launch silently used the
  wrong proxy while reporting success, which is worse than config that is
  plainly absent. Both launch seams now fold the active target down first, and
  a broken target definition costs the override rather than the launch.
- **A pruned-but-alive session stayed invisible while its write path kept
  working** (#47). A live session whose wakeup cadence stretched past the stale
  threshold got retired by `team prune` — and then never came back, because
  only `SessionStart` cleared `ended_at` while every subsequent proof of life
  (prompt heartbeat, end of turn, permission prompt) went through writes that
  did not. Meanwhile its notes landed with verifiable receipts, `team role`
  succeeded and its claims held, so `board`, `team status`, `watch` and
  `doctor` — all of which read liveness as `ended_at IS NULL` — showed nothing
  while the session worked on. Operators read row-absence as death: on the
  board that filed this, one healthy session was pruned on a cadence artifact
  and then presumed dead a second time *because* the severed row masked its own
  recovery. `end_session` had documented the repair all along ("a wrongly
  retired presence row is repaired by the session's next heartbeat"); now it
  happens. A heartbeat is evidence and prune's retirement was an inference from
  silence, so the evidence wins — and the restore keeps the row's role, label
  and focus rather than letting a planner rejoin as `unassigned`. Nothing
  resurrects on its own: a session that really ended stays ended, and prune
  still retires a row that has genuinely gone quiet.
- **The tracing exports were bash-only, and silently misattributed every
  session started from `/bin/sh`.** `aisquare explainability env` quoted with
  bash's `$'…'`, which dash — `/bin/sh` on Debian and Ubuntu — does not treat
  as special: the value arrived with a literal `$` in front and a literal
  backslash-n where the header separator belongs. The proxy then read one
  glued header, never saw `X-Pipeline-Id`, and filed the run under its default
  identity — the exact misattribution that command exists to prevent. Now
  POSIX single-quoted, which carries a real newline in `sh`, `bash` and `zsh`
  alike. The old test pinned the *quoting syntax*, so it passed while the
  premise was false; it now pins the round trip through a real `/bin/sh`.
- **Two spawn commands pasted into one terminal merged into a single Run.**
  The first `eval` exports `ANTHROPIC_*` into the shell, so the second one
  correctly refused to clobber what looks like the operator's own routing —
  and the second agent inherited the first's `X-Pipeline-Id` verbatim. Two
  sessions, one Run, silently; and this is the up-arrow flow, run every time
  an agent exits. The printed command now clears the previous paste's tracing
  first, keyed on a marker only our own wiring sets, so a real operator
  gateway still stops the trace exactly as before.
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

[Unreleased]: https://github.com/AISquare-Studio/aisquare-cli/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/AISquare-Studio/aisquare-cli/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/AISquare-Studio/aisquare-cli/releases/tag/v0.1.0
