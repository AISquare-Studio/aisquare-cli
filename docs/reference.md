# Reference

## How it works

`agents connect claude-code` writes six hooks into Claude Code's
`settings.json` (merged, never clobbering yours; `agents disconnect`
removes exactly them):

| Hook | What it does |
| --- | --- |
| `SessionStart` | inject context and, for a session with a team, the team briefing; mark the session `waiting` (it sits at its prompt until one is typed), except after a compaction, which keeps what the turn said |
| `UserPromptSubmit` | record the prompt in a project you added (anywhere, with `[projects] capture` on), unless `capture.enabled` is off; deliver the teammate delta; heartbeat, which stamps the session's last prompt and marks its fleet agent's first message taken ([fleet](fleet.md#the-first-message)) |
| `Stop` | mark the session waiting; renew its task leases; keep a manager going on fresh board decisions, and any other agent on the notes addressed to it ([fleet](fleet.md#fleet-tell)); record the turn's token usage off the transcript (`aisquare metrics tokens`); for a fleet manager with `fleet.quiet_compact_at` set, start the hidden `aisquare hook compact-check` worker ([fleet](fleet.md#long-lived-managers-state-file-start-pack-and-quiet-compaction)); for a fleet worker that may have finished, start the hidden `aisquare hook auto-stop` check ([fleet](fleet.md#capacity)); with `[accounts] before_limit` on and the turn over (no background work left running), start the worker that moves (or names) a fleet agent whose account reads over `switch_at`; the hook itself asks the usage endpoint nothing |
| `Notification` | flag **NEEDS YOU** when a prompt needs a human (permission, elicitation); other notices are feed lines, the idle notice nothing on the board (with `[accounts] before_limit` on, it is a safe point to move the agent, as a `Stop` is) |
| `SessionEnd` | release claims, mark the session gone, record the last stretch of token usage |
| `StopFailure` | the turn ended on an API error: a usage limit (`rate_limit`) marks the session `limited` with its reset time and remembers the limit on the account until that reset, so no pick by headroom sends an agent back to it before then; with `[accounts] on_limit = "switch"` a fleet agent is handed to another account, the line is a `limited_handing_over` event that wakes nobody, and a hand-over that cannot go ahead posts `handover_failed`, which wakes the manager; without a hand-over coming, `limited` wakes it ([accounts](orchestration.md#several-accounts-one-team)); any other error marks it `waiting`, with a feed line naming the error |

`SessionStart` and `UserPromptSubmit` get `"timeout": 120` (seconds): Claude
Code stops a hook after 60 seconds by default, and these two can wait that
long. If you declare the hooks yourself (home-manager, for example), give
those two the same timeout.

Every hook is **fail-open**: any error is swallowed and the session
continues untouched. State lives in one SQLite database (WAL mode,
concurrency-tested against racing parallel sessions):

```
~/.aisquare/
├── context.db    # context entries, projects, prompt history, tasks, events, sessions
├── config.toml   # your settings: only what differs from the built-in defaults, each fleet role whole; config_version is the format
├── fleet-tmux.conf  # the fleet's private tmux server config (regenerated, not yours)
├── state.json    # small runtime state (e.g. the pinned active project)
├── agents.json   # registry of connected agents
├── projects/     # per-project data — code/ (symbol index), explainability-key
├── reports/      # what `asq exec` saved: argv, the streams' first bytes, source hashes (0700)
├── briefs/       # copies of the artifacts brief checks recorded, per project and brief (0700)
├── cache/        # disposable (e.g. last_injection.json; statusline/: one record per Claude Code
│                 #   session carrying asq-statusline, kept a day, and fleet.json, the figure it prints)
└── log/          # capture and diagnostic logs
```

Every `[fleet]` key, with its default, is in
[fleet.md](fleet.md#permission-modes-and-every-default-you-can-change). The keys that
decide which Claude Code tools and how much of the skill listing a role launches with,
wherever it starts ([Tools and skills per role](fleet.md#tools-and-skills-per-role)):

| Key | Default | What it does |
| --- | --- | --- |
| `fleet.deny_tools` | the eight tools no role uses | denied for every role |
| `fleet.roles.<role>.deny_tools` | `[]` | denied for this role too |
| `fleet.roles.<role>.allow_tools` | `[]` | taken back out, safety list included; never a `--disallowedTools` on the line |
| `fleet.roles.<role>.skill_listing` | `full` for manager, `names` for the rest | `full`, `names` (descriptions cut) or `off` (`Skill` denied) |

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
├── upgrade [--check] [--version V] [--dry-run] [-y]   update this install in place
├── uninstall [--purge] [--dry-run] [-y]   hooks out of Claude Code, then the package
├── project (workspace)  info · list · switch · link · onboard [--refresh] · forget <id|path> [--purge]
│                   rename <nickname> [--clear]   — what the sidebar and the lists call it
│                   prune [--missing] [--worktrees] [--purge] [--yes]
│                   group parent · group route · group cap   — a parent manager over a group
│                                (docs/orchestration.md#a-manager-over-a-group)
├── code            find <name> [--kind K] [--limit N] · refs <name> [--defs] [--limit N]
│                   similar <name|path:line> [--limit N] · index [--full] [--status]
│                   — the project's symbol index (docs/memory.md#symbol-index; [code] enabled, max_file_kb, ignore)
├── agents          scan · list · status [name] · connect <name> · disconnect <name>
│                                                  [--config-dir DIR]
├── accounts        list [--usage] · add · run <slot> [… claude args] · usage [slot] [--summary]
│                   (--usage and usage read a five-minute cache; --refresh reads now,
│                   except an account backed off after HTTP 429/503 — see docs/fleet.md)
│                   remove <slot>            — Claude Code accounts the CLI owns (docs/fleet.md)
│                   default [<slot|alias|email>] [--project P] [--role R] [--clear]
│                     (machine level: an alias of `move <slot> top` — rank 1 IS the default)
│                   alias <slot> <name> [--clear] · order <slot>… · move <slot> up|down|top|bottom
│                   disable <slot> · enable <slot>   — the order (its first enabled, signed-in
│                     account is the machine default), the project defaults, the names
│                   usage also shows the pace (≈ N min to the limit) — see [accounts] in config
│                   statusline [--install [--wrap] | --uninstall] [--slot S | --all]
│                                            — Claude Code's status line, asq-statusline, opt-in
│                   sessions [--since 15m]   — every session on this machine that reported
│                   share [--status | --enable PARTS | --disable PARTS]   — memory, skills,
│                                agents, commands, claude_md shared across accounts
├── team            on · status · focus <text> · role <name> · log [-n N]
│                   spawn <role> [--exec] [--probe/--no-probe] [--refresh]
│                                 [--effort LEVEL] · harness
│                   bind <role> [--bin CMD] [--env KEY=VALUE]… [--arg A]…
│                               [--unset KEY] [--account A] [--clear-account] [--clear]
│                   show <seq>   — one board event in full (author, actor, task, details)
├── task            add <title> [--detail D | --detail-file F] [--rules R] [--needs T]
│                       [--project P --origin T]   — --origin: delegated from a parent's task
│                   list · show · next [--role R] [--status S] [--claim] [--task T]
│                   claim · review [--note] · reopen --reason · done [--note]
│                   block --reason · drop · release        (all with [--as SESSION])
│                   a task is its id, or 12 / #12 — its number on the caller's board
├── brief           create <title> -r TEXT… [--check R1=…]… [--assumption A]… [--boundary B]…
│                   list · show <brief> · link <brief> <task> -r R1…
│                   update <brief> [-r R1=…] [--check R1=…] [--add TEXT] [--title T]
│                                  [--assumption A]… [--boundary B]…
│                                  [--clear-assumptions] [--clear-boundaries]
│                   evidence <brief> R1 --task T --verdict pass|fail --summary S
│                                    (--report ID | --artifact F)
│                   finding <brief> R1 --task T --summary S [--artifact F]
│                   check <brief> [--source-root PATH]   exit 1 until every requirement is proven
│                   (all with [--as SESSION]; a brief is its id, or B1 / 1 — docs/work-briefs.md)
├── note <text> [--task T] [--to ROLE] [--kind note|decision|question|result]
├── board [-w] [-i SECONDS] [-P PROJECT]
├── launch <role> [--command CMD] [--env KEY=VALUE]… [--account SLOT] [… agent args]
│                   role = planner|coder|runner|validator, a fleet role (manager,
│                   tester, reviewer, ui-tester), a numbered seat (coder1), or any role you
│                   have bound; env merges over `team bind`
├── exec [--project P] [--task T] [--session S] [--raw] [--max-output-bytes N] -- CMD …
│                   run CMD once and save a report of how it ended (docs/command-reports.md)
├── reports         list · show <id> [--raw] [--stream stdout|stderr|both] · stats
│                   prune [--keep N] [--days N]   — read saved reports; never reruns
├── serve [--stdio | --port N --bind H] [--show-token]
├── ui              [--project P]… [--group G]…
│                   the fleet UI — what bare `asq` opens at a terminal (docs/fleet.md)
├── remote          serve · needs · status · allow-write on|off · revoke · regenerate-password
│                   install-page <dist>   — the fleet on your phone (docs/remote.md)
├── fleet           spawn <role> [--label L] [--task ID] [--worktree/--no-worktree]
│                             [--permission-mode M] [--bin B] [--prompt TEXT] [--account SLOT]
│                             [--board-context all|relevant|none] [-- agent args]
│                   ls [--all] · status · tell <label> <text>
│                   stop <label> [--force] [--as SESSION]
│                   restart <label> [--fresh] [--permission-mode M]
│                   switch <label> [--to A] [--fresh] [--reason R]
│                   shutdown [--all] [--yes] [--force] [--forget] · attach
│                   reap [--all] [--server-down] [--finished [--dry-run]]
│                   rename <codename> · pause [--group G] · resume [--group G]
│                   capacity · gate [--min-free-mb N] [--wait 15m] [-- CMD …]
│                   (all with [--project P]; spawn · tell · restart · switch · pause · resume
│                   take [--as SESSION])
├── watch           [--interval S] [--once | --detach | --stop | --status] · show [path] [--last]
│                   — one non-AI watcher for the fleet (docs/fleet.md#watching-the-fleet)
├── login [--no-browser] [--with-token] [--api-url URL] · logout · whoami
├── auth            status [--live] · token
├── config          list [--overrides] · get <key> · set <key> <value> · reset [--yes]
│                   redaction <off|standard|strict>
└── metrics         show · list · tokens  [-n N] [--session S] [--project P | --all]   (hidden)
                    ledger [--session S] [--since 2h] [--role R] [--project P | --all]   (tokens per session, counted live from transcripts)
```

Everything `aisquare --help` lists is implemented. Roadmap commands are
registered but hidden until they do something real.

| Global flag | Meaning |
| --- | --- |
| `-V` / `--version` | print the version and exit |
| `--json` | machine-readable JSON on stdout |
| `-v` / `-q` | verbose / quiet |
| `--no-color` | disable coloured output |
| `--profile NAME` | configuration profile |

### Roadmap commands

`sync`, `connectors`, `capture`, `policy` / `enforce` and `open` are the cloud
roadmap (sync across machines, managed connectors). They are **hidden from `--help`**
so the listed surface is only what actually works, but they still run and
still say plainly that they are not implemented (exit code 70) rather than
half-working. Follow along in
[issues](https://github.com/AISquare-Studio/aisquare-cli/issues).

## Configuration keys

Set with `aisquare config set <key> <value>`, read with `config get`; `config
list` prints every key. The ones that need more than their name:

| Key | Default | What it does |
| --- | --- | --- |
| `accounts.pick` | `headroom` | how a launch picks an account when nothing names one: `headroom` (the first under `switch_at`) or `default` (the machine default) |
| `accounts.switch_at` | `85` | percent USED of the fuller window (and, for an agent of that family, its family week) at which an account counts as full |
| `accounts.prefer` | `priority` | of the accounts under `switch_at`, `priority` takes the first in rank order (rank 1, the machine default, first); `reset-soonest` takes the one whose week resets first. The headroom pick's alone: with `pick = "default"` rank 1 decides |
| `accounts.on_limit` | `switch` | on a usage limit, `switch` hands a fleet agent to another account; `wait` leaves it parked |
| `accounts.before_limit` | `off` | before a usage limit: when a fleet agent's account reads over `switch_at` at its turn end or idle notice (never mid-turn, never with background work its turn left running), `switch` hands it to an account under the line, and `warn` only puts a `before_limit` line on the board, once per crossing of the line. The hooks ask the usage endpoint nothing; a detached worker does the reads. Not for the manager or the captain, an agent put on its account by name (`--account`, a role's account binding, a binding that sets `CLAUDE_CONFIG_DIR`), one in a paused fleet, a finished worker auto-stop is about to stop, or one started or looked at within ten minutes; the move waits five seconds after the turn end and leaves the agent alone if anything began since ([fleet](fleet.md#accounts)) |
| `accounts.wait_if_reset_within_minutes` | `15` | with `on_limit = "switch"`, wait instead when the limit lifts this soon; with `before_limit` on (`warn` or `switch`), neither move nor warn when the windows over the line reset this soon |
| `accounts.share_trust` | `true` | copy a folder's trust into the account a launch runs on |
| `accounts.share_settings` | `false` | keep every managed account's `settings.json` a link to the plain claude's |
| `accounts.share` | `[]` | what every account shares: `memory`, `skills`, `agents`, `commands`, `claude_md` (`plugins` is experimental); set with `aisquare accounts share --enable` ([fleet](fleet.md)) |
| `code.enabled` | `true` | build a project's symbol index at `project onboard` and `init` (in the background) and refresh it at session start ([memory](memory.md#symbol-index)) |
| `code.max_file_kb` | `1024` | a file larger than this is not indexed |
| `code.ignore` | `[]` | globs the symbol index leaves out, extending its built-in list (comma-separated on the command line); a `[snapshot] ignore` from 0.8.2 is read here and moved at the next save ([memory](memory.md#symbol-index)) |
| `capture.enabled` | `true` | `false`: the `UserPromptSubmit` hook stores no prompt (`aisquare log` stays empty) and spools none for Explainability. The turn's metrics row (no text), the teammate delta and the CI experiment, if you turned it on, carry on. A config that cannot be read records no prompt either, with one line on stderr. |
| `fleet.busy_quiet_seconds` | `30` | how long an agent's pane may print nothing while the board says `working` before its screen is checked once: Claude Code idle at its prompt reads `waiting` (*idle (the board said working)*), as after a turn ended with Esc, which fires no hook. A pane that is busy is never captured. |
| `fleet.max_agents_total` | `0` | live agents across every project, the captain excluded; `0` is no budget ([fleet](fleet.md#capacity)) |
| `fleet.min_free_mb` | `2048` | a spawn is refused below this much available memory, or at critical memory pressure; `0` turns the check off |
| `fleet.test_gate_mb` | `0` | memory `aisquare fleet gate` waits for before a heavy run; set, the tester's and validator's cycles name the gate |
| `fleet.test_gate_wait_minutes` | `15` | how long `fleet gate` waits before it exits 75 without running |
| `fleet.pane_clipboard` | `off` | `on`: a program in an agent's pane may set your clipboard with OSC 52, through tmux (a server started after the change) |
| `fleet.roles.<role>.board_context` | `none` for the reviewer, `all` for the others | what of the board an agent of this role reads: `all`, `relevant` (its own tasks, what is addressed to it, decisions) or `none`; `fleet spawn --board-context` sets it for one agent ([fleet](fleet.md)) |
| `fleet.auto_stop_finished` | `true` | stop a finished worker of a role in `auto_stop_roles` by itself, `actor` auto-stop, waking nobody ([fleet](fleet.md#capacity)). |
| `fleet.auto_stop_idle_minutes` | `5` | how long a finished worker sits idle before it is stopped. |
| `fleet.auto_stop_roles` | `["tester", "reviewer", "validator", "ui-tester"]` | the roles auto-stop may stop; add `"coder"` to opt in. Never a manager or the captain. |
| `fleet.first_prompt` | `argv` | how an agent's first message reaches it: `argv` gives a Claude Code agent its message on its command line and types it for any other binary; `type` types it for every agent ([fleet](fleet.md#the-first-message)). |
| `fleet.prompt_ack_seconds` | `45` | how long `fleet spawn`, `restart` and `switch` wait for the agent's first turn to start, and how long a listing gives it before the agent reads `attention`: "first prompt not taken". |
| `fleet.draft_quiet_seconds` | `10` | how long a key typed into an agent's pane in `asq` keeps every fleet write — a nudge, a tell, a typed first message, the Remote's paste — out of that pane. A draft in its input box holds them off for as long as it stays. |
| `fleet.roles.<role>.remote_control` | `false` | start the role's Claude Code sessions with Claude Code's own Remote Control, titled `<project> · <label>`, so claude.ai/code and the Claude app can continue them ([remote](remote.md#claude-codes-own-remote-control)); `fleet spawn --remote-control` / `--no-remote-control` overrides it per spawn, and a restart keeps the choice |
| `fleet.deliver_notes_at_turn_end` | `true` | an agent other than the manager is handed the notes addressed to its label or role when its turn ends, instead of at its next prompt ([fleet](fleet.md#fleet-tell)). |
| `fleet.quiet_compact_at` | `0` | a manager's context size, in tokens, at which it is compacted at a quiet point; `0` is off ([fleet](fleet.md#long-lived-managers-state-file-start-pack-and-quiet-compaction)). |
| `fleet.quiet_compact_delay` | `45` | seconds after a manager's quiet Stop before its compaction is checked again and typed. |
| `fleet.quiet_compact_cooldown` | `30` | minutes after a quiet compaction (or one that did not start) before another. |
| `fleet.quiet_compact_state_max_age` | `20` | minutes: a manager whose state file is older is not compacted. |
| `fleet.quiet_compact_resume` | `false` | `true`: after a quiet compaction, type a one-line resume prompt into the manager. |
| `projects.capture` | `false` | `true`: every directory a hooked session runs in gets a row, as 0.8.0 did; off, only a project you added records anything ([fleet](fleet.md#naming)). |
| `watch.interval_seconds` | `60` | how often `aisquare watch` looks ([fleet](fleet.md#watching-the-fleet)). |
| `watch.github` | `false` | `true`: `aisquare watch` and the captain's queue read each project's GitHub pull requests through `gh api`, with ETags. |
| `watch.github_every_seconds` | `120` | how often the watcher asks GitHub. |
| `watch.auto_stop` | `true` | each look also stops the workers that have finished ([fleet](fleet.md#capacity)). |
| `watch.sources` | `{}` | the operator's own commands, `[watch.sources.<name>] command = [...]`, `every_seconds = 300`, each printing JSON lines `{"id", "project", "kind", "summary"}`. |
| `redaction.level` | `standard` | how much is scrubbed from what leaves the machine: `off`, `standard` or `strict` ([Explainability](connecting-your-agents-to-explainability.md)). |
| `team.brief_blocks` | `["objective", "acceptance", "boundaries"]` | the blocks a task contract must carry as `<label>:`; `task add` warns about a missing one ([orchestration](orchestration.md#tasks-idempotent-atomic-dependency-aware)). |
| `team.brief_strict` | `false` | `true`: `task add` refuses a contract that is missing a block (exit 2). |

## Architecture

The codebase is a thin Typer CLI over a service layer over one SQLite store
— `src/aisquare/cli/` parses, `src/aisquare/services/` behaves,
`src/aisquare/core/` is shared infrastructure. Tests run hermetically
against a temp `AISQUARE_HOME` (the suite passes even with every aisquare
env knob set adversarially). See [CONTRIBUTING.md](../CONTRIBUTING.md) for the
workflow.
