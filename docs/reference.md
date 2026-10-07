# Reference

## How it works

`agents connect claude-code` writes five hooks into Claude Code's
`settings.json` (merged, never clobbering yours; `agents disconnect`
removes exactly them):

| Hook | What it does |
| --- | --- |
| `SessionStart` | inject orientation: snapshot pointers, context, team briefing |
| `UserPromptSubmit` | capture the prompt; deliver the teammate delta; heartbeat |
| `Stop` | mark the session waiting; renew its task leases |
| `Notification` | flag **NEEDS YOU** when a prompt needs a human (permission, elicitation); other notices are feed lines, the idle notice nothing |
| `SessionEnd` | release claims, mark the session gone, final distill |

Every hook is **fail-open**: any error is swallowed and the session
continues untouched. State lives in one SQLite database (WAL mode,
concurrency-tested against racing parallel sessions):

```
~/.aisquare/
├── context.db    # context entries, projects, prompt history, tasks, events, sessions
├── config.toml   # typed configuration
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
├── project (workspace)  info · list · switch · link · onboard [--refresh] · forget <id|path> [--purge]
│                   prune [--missing] [--worktrees] [--purge] [--yes]
├── agents          scan · list · status [name] · connect <name> · disconnect <name>
│                                                  [--config-dir DIR]
├── accounts        list [--usage] · add · run <slot> [… claude args] · usage [slot]
│                   remove <slot>            — Claude Code accounts the CLI owns (docs/fleet.md)
│                   default [<slot|alias|email>] [--project P] [--role R] [--clear]
│                   alias <slot> <name> [--clear] · order <slot>… · move <slot> up|down|top|bottom
│                   disable <slot> · enable <slot>   — the default, the order, the names
│                   usage also shows the pace (≈ N min to the limit) — see [accounts] in config
├── team            on · status · focus <text> · role <name> · log [-n N] · distill [--all]
│                   spawn <role> [--exec] [--probe/--no-probe] [--refresh]
│                                 [--effort LEVEL] · harness
│                   bind <role> [--bin CMD] [--env KEY=VALUE]… [--arg A]…
│                               [--unset KEY] [--account A] [--clear-account] [--clear]
├── task            add · list · show · next [--role R] [--status S] [--claim]
│                   claim · review [--note] · reopen --reason · done [--note]
│                   block --reason · drop · release        (all with [--as SESSION])
├── note <text> [--task T] [--to ROLE] [--kind note|decision|question|result]
├── board [-w] [-i SECONDS] · recall <query>
├── launch <role> [--command CMD] [--env KEY=VALUE]… [--account SLOT] [… agent args]
│                   role = planner|coder|runner|validator, a fleet role (manager,
│                   tester, reviewer, ui-tester), a numbered seat (coder1), or any role you
│                   have bound; env merges over `team bind`
├── serve [--stdio | --port N --bind H] [--show-token]
├── ui              the fleet UI — what bare `asq` opens at a terminal (docs/fleet.md)
├── fleet           spawn <role> [--label L] [--task ID] [--worktree/--no-worktree]
│                             [--permission-mode M] [--bin B] [--prompt TEXT] [--account SLOT]
│                             [-- agent args]
│                   ls [--all] · status · tell <label> <text> · stop <label> [--force]
│                   restart <label> [--fresh] [--permission-mode M]
│                   switch <label> [--to A] [--fresh] [--reason R]
│                   shutdown [--all] [--yes] [--force] · attach · reap [--all] [--server-down]
│                   rename <codename> · pause · resume
│                   (all with [--project P]; spawn · tell · restart · switch · pause · resume
│                   take [--as SESSION])
├── login [--no-browser] [--with-token] [--api-url URL] · logout · whoami
├── auth            status [--live] · token
├── config          list · get <key> · set <key> <value> · redaction <off|standard|strict>
└── metrics         show · list  [-n N] [--session S] [--project P | --all]   (CI test bed; hidden)
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

`sync`, `connectors`, `capture`, `policy` / `enforce`, `open` and
`uninstall` are the cloud roadmap (sync across machines, managed connectors). They are **hidden from `--help`**
so the listed surface is only what actually works, but they still run and
still say plainly that they are not implemented (exit code 70) rather than
half-working. Follow along in
[issues](https://github.com/AISquare-Studio/aisquare-cli/issues).

## Architecture

The codebase is a thin Typer CLI over a service layer over one SQLite store
— `src/aisquare/cli/` parses, `src/aisquare/services/` behaves,
`src/aisquare/core/` is shared infrastructure. Tests run hermetically
against a temp `AISQUARE_HOME` (the suite passes even with every aisquare
env knob set adversarially). See [CONTRIBUTING.md](../CONTRIBUTING.md) for the
workflow.
