# Reference

## How it works

`agents connect claude-code` writes six hooks into Claude Code's
`settings.json` (merged, never clobbering yours; `agents disconnect`
removes exactly them):

| Hook | What it does |
| --- | --- |
| `SessionStart` | inject context and, for a session with a team, the team briefing; mark the session `waiting` (it sits at its prompt until one is typed), except after a compaction, which keeps what the turn said |
| `UserPromptSubmit` | record the prompt in a project you added (anywhere, with `[projects] capture` on), unless `capture.enabled` is off; deliver the teammate delta; heartbeat, which stamps the session's last prompt and marks its fleet agent's first message taken ([fleet](fleet.md#the-first-message)) |
| `Stop` | mark the session waiting; renew its task leases; keep a manager going on fresh board decisions, and any other agent on the notes addressed to it ([fleet](fleet.md#fleet-tell)); record the turn's token usage off the transcript (`aisquare metrics tokens`); with `[accounts] before_limit` on and the turn over, move (or name) a fleet agent whose account reads over `switch_at` |
| `Notification` | flag **NEEDS YOU** when a prompt needs a human (permission, elicitation); other notices are feed lines, the idle notice nothing on the board (with `[accounts] before_limit` on, it is a safe point to move the agent, as a `Stop` is) |
| `SessionEnd` | release claims, mark the session gone, final distill, record the last stretch of token usage |
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
├── projects/     # per-project data — snapshot/ (Repomix pack), brain/ (gbrain)
├── cache/        # disposable (e.g. last_injection.json)
└── log/          # capture and diagnostic logs
```

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
├── agents          scan · list · status [name] · connect <name> · disconnect <name>
│                                                  [--config-dir DIR]
├── accounts        list [--usage] · add · run <slot> [… claude args] · usage [slot]
│                   (--usage and usage read a five-minute cache; --refresh reads now,
│                   except an account backed off after HTTP 429/503 — see docs/fleet.md)
│                   remove <slot>            — Claude Code accounts the CLI owns (docs/fleet.md)
│                   default [<slot|alias|email>] [--project P] [--role R] [--clear]
│                     (machine level: an alias of `move <slot> top` — rank 1 IS the default)
│                   alias <slot> <name> [--clear] · order <slot>… · move <slot> up|down|top|bottom
│                   disable <slot> · enable <slot>   — the order (its first enabled, signed-in
│                     account is the machine default), the project defaults, the names
│                   usage also shows the pace (≈ N min to the limit) — see [accounts] in config
├── team            on · status · focus <text> · role <name> · log [-n N] · distill [--all]
│                   spawn <role> [--exec] [--probe/--no-probe] [--refresh]
│                                 [--effort LEVEL] · harness
│                   bind <role> [--bin CMD] [--env KEY=VALUE]… [--arg A]…
│                               [--unset KEY] [--account A] [--clear-account] [--clear]
├── task            add · list · show · next [--role R] [--status S] [--claim]
│                   claim · review [--note] · reopen --reason · done [--note]
│                   block --reason · drop · release        (all with [--as SESSION])
│                   a task is its id, or 12 / #12 — its number on the caller's board
├── note <text> [--task T] [--to ROLE] [--kind note|decision|question|result]
├── board [-w] [-i SECONDS] · recall <query>
├── launch <role> [--command CMD] [--env KEY=VALUE]… [--account SLOT] [… agent args]
│                   role = planner|coder|runner|validator, a fleet role (manager,
│                   tester, reviewer, ui-tester), a numbered seat (coder1), or any role you
│                   have bound; env merges over `team bind`
├── serve [--stdio | --port N --bind H] [--show-token]
├── ui              the fleet UI — what bare `asq` opens at a terminal (docs/fleet.md)
├── remote          serve · needs · status · allow-write on|off · revoke · regenerate-password
│                   install-page <dist>   — the fleet on your phone (docs/remote.md)
├── fleet           spawn <role> [--label L] [--task ID] [--worktree/--no-worktree]
│                             [--permission-mode M] [--bin B] [--prompt TEXT] [--account SLOT]
│                             [-- agent args]
│                   ls [--all] · status · tell <label> <text> · stop <label> [--force]
│                   restart <label> [--fresh] [--permission-mode M]
│                   switch <label> [--to A] [--fresh] [--reason R]
│                   shutdown [--all] [--yes] [--force] [--forget] · attach
│                   reap [--all] [--server-down]
│                   rename <codename> · pause · resume
│                   (all with [--project P]; spawn · tell · restart · switch · pause · resume
│                   take [--as SESSION])
├── login [--no-browser] [--with-token] [--api-url URL] · logout · whoami
├── auth            status [--live] · token
├── config          list [--overrides] · get <key> · set <key> <value> · reset [--yes]
│                   redaction <off|standard|strict>
└── metrics         show · list · tokens  [-n N] [--session S] [--project P | --all]   (hidden)
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
| `accounts.before_limit` | `off` | before a usage limit: when a fleet agent's account reads over `switch_at` at its turn end or idle notice (never mid-turn), `switch` hands it to an account under the line, and `warn` only puts a `before_limit` line on the board. Not for an agent put on its account by name (`--account`, a role's account binding, a binding that sets `CLAUDE_CONFIG_DIR`), in a paused fleet, or moved within ten minutes ([fleet](fleet.md#accounts)) |
| `accounts.wait_if_reset_within_minutes` | `15` | with `on_limit` or `before_limit` set to `switch`, wait instead when the limit lifts (or the windows over the line reset) this soon |
| `accounts.share_trust` | `true` | copy a folder's trust into the account a launch runs on |
| `accounts.share_settings` | `false` | keep every managed account's `settings.json` a link to the plain claude's |
| `capture.enabled` | `true` | `false`: the `UserPromptSubmit` hook stores no prompt (`aisquare log` stays empty) and spools none for Explainability. The turn's metrics row (no text), the teammate delta and the CI experiment, if you turned it on, carry on. A config that cannot be read records no prompt either, with one line on stderr. |
| `fleet.busy_quiet_seconds` | `30` | how long an agent's pane may print nothing while the board says `working` before its screen is checked once: Claude Code idle at its prompt reads `waiting` (*idle (the board said working)*), as after a turn ended with Esc, which fires no hook. A pane that is busy is never captured. |
| `fleet.first_prompt` | `argv` | how an agent's first message reaches it: `argv` gives a Claude Code agent its message on its command line and types it for any other binary; `type` types it for every agent ([fleet](fleet.md#the-first-message)). |
| `fleet.prompt_ack_seconds` | `45` | how long `fleet spawn`, `restart` and `switch` wait for the agent's first turn to start, and how long a listing gives it before the agent reads `attention`: "first prompt not taken". |
| `fleet.draft_quiet_seconds` | `10` | how long a key typed into an agent's pane in `asq` keeps every fleet write — a nudge, a tell, a typed first message, the Remote's paste — out of that pane. A draft in its input box holds them off for as long as it stays. |
| `fleet.deliver_notes_at_turn_end` | `true` | an agent other than the manager is handed the notes addressed to its label or role when its turn ends, instead of at its next prompt ([fleet](fleet.md#fleet-tell)). |
| `projects.capture` | `false` | `true`: every directory a hooked session runs in gets a row, as 0.8.0 did; off, only a project you added records anything ([fleet](fleet.md#naming)). |
| `redaction.level` | `standard` | how much is scrubbed from what leaves the machine: `off`, `standard` or `strict` ([Explainability](connecting-your-agents-to-explainability.md)). |

## Architecture

The codebase is a thin Typer CLI over a service layer over one SQLite store
— `src/aisquare/cli/` parses, `src/aisquare/services/` behaves,
`src/aisquare/core/` is shared infrastructure. Tests run hermetically
against a temp `AISQUARE_HOME` (the suite passes even with every aisquare
env knob set adversarially). See [CONTRIBUTING.md](../CONTRIBUTING.md) for the
workflow.
