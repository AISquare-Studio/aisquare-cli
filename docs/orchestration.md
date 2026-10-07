# Orchestration

**You do not need this to use aisquare.** Everything in [Memory](memory.md) works on its own.
Read on only when you want several agent sessions working one problem at once.

Sessions are per **terminal**, not per account — a single `claude` install
runs the whole team:

```sh
pipx install 'aisquare-cli[tui]'         # the live board wants the TUI extra
aisquare agents connect claude-code
cd your/repo

aisquare launch planner                  # terminal 1 — you talk to this one
aisquare launch coder                    # terminal 2
aisquare launch coder                    # terminal 3 — as many as you like
aisquare launch runner                   # terminal 4 — verifies the coders' work
aisquare board -w                        # terminal 5 — you, watching live
```

`aisquare launch <role>` opts the repo in, registers the session, and hands
off to `claude` — arguments after the role are forwarded, so `aisquare launch
coder --model opus` does what it looks like. (The underlying mechanism is the
`AISQUARE_ROLE` environment variable; `AISQUARE_ROLE=coder claude` still works
if you prefer it, and is what you need when launching an agent other than
`claude` without `--command`.)

Every session is told its id, its teammates, and its **role's work cycle**
automatically — no standing prompts to paste:

- **planner** — turns your intent into contract-carrying tasks on the shared
  board (objective, why, acceptance criteria, boundaries); told to "fix"
  something, it writes the tasks for it rather than editing code itself
- **coder** — starts on the task it was spawned for (the board says
  **ASSIGNED TO YOU**), then loops `task next --claim` → work → `task review`;
  blocks instead of guessing when a task has no usable contract
- **runner** — the adversarial verifier: runs the full check the acceptance
  criteria name, tries to make the change fail, then `task done` with
  evidence or `task reopen --reason "what failed"` — and the feedback rides
  back to whichever coder picks the task up next
- **validator** — gates the assembled deliverable once, before handoff
  (final accountability review, severity-ordered findings)
- **ui-tester** — verifies anything a user sees in a real browser (Claude in
  Chrome, the Chrome DevTools MCP or a Playwright MCP, whichever the window
  has) and measures instead of eyeballing; names the branch and URL it
  verified, and reopens rather than passes a UI task it could not open in a
  browser. Launched with `--chrome` by the role itself, so the tool is not one
  operator's alias. Its briefing ASKS it to be read-only; nothing in this
  checkout enforces that (no allowed-tools list is passed)

### The model harness: each role on the right model

Roles are tiered onto a model *ladder*, strongest first, with availability
verified and automatic fallback — planner/validator want `fable`
(enterprise) and fall back to `opus`, then `sonnet`, when the account
doesn't serve it; coder/runner run on `sonnet`, the measured sweet spot for
agentic work. Launch a role through the harness and it resolves the ladder
for you:

```sh
aisquare team spawn planner            # prints: AISQUARE_ROLE=planner claude --model fable --effort high
aisquare team spawn coder --exec       # or replace this terminal with the session
aisquare team harness                  # the whole role→model matrix + how it resolves now
```

Availability is *probed*, never assumed — `claude --model` silently
substitutes the default when a known model isn't available to the account,
so the harness verifies the reply's `modelUsage` before trusting a rung, and
caches that verdict per account for a day (`--refresh` re-checks after an
entitlement changes). The probe runs isolated: it never executes the current
repo's hooks or MCP servers, and never joins the board.

Resolution is fail-open and, deliberately, only *demotes on proof*: a
genuine substitution walks down the ladder, while an outage, an expired
login, or an unrecognised reply keeps the requested model and labels the pick
`[unverified]` rather than quietly downgrading your planner. Nothing here
ever blocks a launch. Pin a role outright with `AISQUARE_MODEL_<ROLE>`
(works for custom roles too); disable probes with `AISQUARE_HARNESS_PROBE=0`.

**Effort is dynamic, not frozen.** `high` is the base — the documented default
for most work — and each role carries a predefined *offset* rather than a
hardcoded level, so the shape holds wherever you set the base:

| base | planner / coder / runner | validator (+1) |
| --- | --- | --- |
| `low` | low | medium |
| `high` *(default)* | high | xhigh |
| `xhigh` | xhigh | max |

The offset exists for one reason: the gate has to outrank the work it checks.
A flat override that dropped everything to `low` would leave the validator
weaker than the coder whose output it reviews, which is not a gate at all.

The base comes from, in order: `AISQUARE_EFFORT` → `CLAUDE_EFFORT` (what your
own Claude session is running at, which Claude Code exports) → `high`. So
raising your session to xhigh raises the fleet you spawn from it, with nothing
to configure. Override per launch with `aisquare team spawn coder --effort
xhigh`, or pin one role absolutely with `AISQUARE_EFFORT_<ROLE>` — both skip
the offset, because you named the level yourself. `ultracode` is accepted and
ranks as xhigh (it is xhigh plus automatic workflow orchestration). An
unusable value falls back to the base rather than being passed to the CLI,
which would silently ignore it. `aisquare team harness` prints the live base
and every derived level.

Sessions report their model back to the board, which flags any session
running off its role's ladder (`⚠ off-ladder`). That signal is advisory: the
model field is optional in Claude Code's hook payload and absent on some
surfaces (MCP teammates have none), and an in-session `/model` switch isn't
re-reported — so a missing chip means *not reported*, never *wrong*. Tiering
is enforced at launch, not policed in the store.

On every prompt, each session receives a compact delta of what teammates did
since its last turn. Nothing needs forwarding; the coordination is the
ambient state of the board.

Orchestration is **opt-in per repo** (a role launch or `aisquare team on`)
and fails open everywhere: the hooks are designed so orchestration can
never break a Claude session, even when it is broken or absent. Repos
that never opt in see nothing.

### Tasks: idempotent, atomic, dependency-aware

```sh
aisquare task add "wire auth" --role coder        # idempotent — safe to re-emit
aisquare task add "ship it" --needs tsk_01k…      # held until its dependency is done
aisquare task next --role coder --claim --as <id> # atomic claim — exactly one winner; a
                                                  # fleet agent's own assigned task comes first
aisquare task review tsk_01k… --note "how to verify" --as <id>
aisquare task reopen tsk_01k… --reason "fails on py3.11" --as <id>
aisquare note "JWT it is" --kind decision --as <id>
```

Claims are single-`UPDATE` atomic (race-tested), leased (default 120
minutes), and renewed by the session's own lifecycle hooks — so a dead
session's claims release themselves and the work gets picked up again.
`task next` only hands out tasks whose dependencies are done.

### Self-check: receipts you can re-prove

Every successful write prints a receipt (`✓ … · seq N on <board>`; under
`--json`, `delivered: true` plus the event's `seq`). The pull side is yours
any time:

```sh
aisquare team verify 42                     # is seq 42 really on this board? exit 0/1
aisquare team verify evt_01k… --as <id>     # by event id (prefix ok), session's board
aisquare team log --mine --as <id>          # read back your own recent writes
aisquare team log --by aaaa1111 --since 15m --kind decision   # filters compose
```

A receipt that lives on a *different* board is an honest not-found — with a
hint naming the board that actually holds it. Remote MCP agents get the same
pair: `verify(receipt)` and `team_log(by_session="me")`.

### Signals: named states, never substring matching

Prose is a terrible protocol — a watcher grepping `READY` fires on a note
saying "NOT READY". Signals are first-class named board states:

```sh
aisquare team signal fold-ready on --as <id>   # set (single-token name/value)
aisquare team signal fold-ready                # read: value, who set it, when, seq
aisquare team signals                          # list all
aisquare team log --kind signal --since-seq N --json   # a watcher's poll loop
```

Every set emits a `signal` event whose `--json` payload carries structured
`name` / `value` / `prev` / `set_by` fields — consumers key on fields, never
on text, so negations can't false-trigger. Sets follow the write contract
(receipt + read-back; `team verify <seq>` works on signal receipts), and the
MCP `signal(name, value?)` tool gives remote agents the same pair.

Still matching free text somewhere? At minimum anchor the pattern
(`^ready$`), match whole tokens (`\bready\b` misses `NOT READY` only if you
also reject preceding negations), and treat any hit inside a longer sentence
as suspect — then switch to signals, which is the whole point of them.

### The live board (`aisquare board -w`)

An interactive [Textual](https://textual.textualize.io/) TUI with the
`[tui]` extra (full-screen Rich fallback without it): every session with a
live state chip — **▶ working**, **⏸ waiting for input**, **🔔 NEEDS YOU**
(with a terminal bell) — the open tasks, and a bot-style feed of everything
the team does. Click any task or feed line for its full detail.

| Key | Action |
| --- | --- |
| `d` | flip to the done/dropped archive — when it closed, who closed it |
| `o` | open the author session's transcript at that exact moment |
| `t` | theme browser — applies live, autosaves |
| `a` | toggle feed autoscroll |
| `v` / `c` | select-text mode (frozen, mouse-selectable feed) / copy |
| `s` | save an SVG screenshot to `~/.aisquare/screenshots/` |
| `b` | show/hide the board pane |
| `r` / `q` | refresh now / quit |

### Long-term memory (optional, via gbrain)

Durable events — decisions, results, task outcomes, reopen feedback —
distill into a per-project brain by a detached worker, never on the hot
path. `aisquare recall "what did we decide about auth?"` searches it across
sessions and weeks; `aisquare team distill --all` backfills; `aisquare
doctor` reports brain health.

This layer needs the AISquare **gbrain** CLI on `PATH` (a separate,
optional tool — *not* the unrelated `gbrain` package on public npm) and is
silently skipped when absent. Everything else works without it.

**Semantic recall**: embeddings are off by default (no surprise network
calls). Export `AISQUARE_BRAIN_EMBED=1` plus an `OPENAI_API_KEY` **before
the first distill** and `recall` becomes hybrid vector + keyword search. The
embedding schema is fixed at brain creation — to upgrade an existing brain,
remove `~/.aisquare/projects/<id>/brain` and re-run
`AISQUARE_BRAIN_EMBED=1 aisquare team distill --all`. `doctor` flags
knob-vs-schema mismatches in both directions.

### Remote agents over MCP (`aisquare serve`)

The same board, tasks, and notes — exposed as an MCP server so Claude
clients that aren't local terminals can join: a browser-debugging agent in
the Claude desktop app, for instance. Remote callers act as attributed
virtual sessions; their tasks and notes hit everyone's board and deltas
like any teammate's.

```sh
pipx install 'aisquare-cli[serve]'
aisquare serve                   # streamable HTTP on 127.0.0.1:8747, bearer-token auth
aisquare serve --show-token      # connection details for the client
aisquare serve --stdio           # stdio transport (Claude Desktop launches it)
```

`--bind` decides more than the interface. The three loopback spellings
(`127.0.0.1`, `localhost`, `::1`) keep the MCP transport's Host/Origin
validation; any other bind — `0.0.0.0`, a LAN address — runs with the bearer
token as the only gate, and that token is a long-lived credential sent in
clear over plain HTTP on every request. `serve` says so on stderr when you do
it. Use a trusted network or a TLS-terminating proxy.

Running `serve` in a repo is the explicit opt-in for that project (it
announces itself); the stdio transport refuses to run from directories that
aren't a project, so a desktop client can't accidentally adopt your home
directory. Claude Desktop on Windows + WSL2 works either over the HTTP URL
(Windows reaches WSL2 via localhost) or as a registered stdio server:

```json
{"mcpServers": {"aisquare-team": {"command": "wsl", "args": ["-e", "bash", "-lc",
  "cd /path/to/your/repo && aisquare serve --stdio"]}}}
```

An idle stdio server closes itself after 300s without a client message
(`--close-after`, env `AISQUARE_SERVE_CLOSE_AFTER`) so abandoned daemons
never linger; persistent clients like the Claude Desktop config above should
set `AISQUARE_SERVE_CLOSE_AFTER=0` (run forever) in their launch command.
The clock counts **inbound** messages only — it assumes request/response
traffic, so a deadline shorter than your slowest tool call would cut a
client mid-wait (at the 300s default no current tool comes anywhere close).

### Tuning (environment variables)

Orchestration has no config files — a handful of env knobs:

| Variable | Effect |
| --- | --- |
| `AISQUARE_ROLE` | role for this session; launching with it opts the repo in |
| `AISQUARE_TEAM=0` | master off switch — hooks and commands no-op |
| `AISQUARE_TEAM_HUB` | point sessions from several repos at one shared board |
| `AISQUARE_TEAM_DELTA=0` | mute per-prompt teammate deltas for a session |
| `AISQUARE_TEAM_LEASE_MIN` | task-claim lease in minutes (default 120) |
| `AISQUARE_MODEL_<ROLE>` | pin a role's model outright (skips the harness ladder) |
| `AISQUARE_EFFORT` | base effort for spawned roles (default `high`, else inherits `CLAUDE_EFFORT`) |
| `AISQUARE_EFFORT_<ROLE>` | pin one role's effort absolutely (skips the role offset) |
| `AISQUARE_HARNESS_PROBE=0` | never probe model availability (ladders resolve optimistically) |
| `AISQUARE_BRAIN=0` | disable the long-term-memory layer |
| `AISQUARE_BRAIN_EMBED=1` | embed distilled pages for semantic recall (needs `OPENAI_API_KEY`; set before the first distill) |
| `AISQUARE_BRAIN_EMBED_MODEL` | embedding model (default `openai:text-embedding-3-large`) |
| `AISQUARE_HOME` | relocate the whole `~/.aisquare` tree |

### Several accounts, one team

Running parallel Claude Code logins for separate rate limits? The CLI owns them
for you. **Slot 1** is the plain `claude` of your machine. Every account you
**add** is a numbered slot with its own config directory under
`~/.aisquare/claude-accounts/`, signed in through Claude Code's own login and
launched by number — the c1/c2/c3 shell aliases, without the aliases:

```sh
aisquare accounts list             # who is signed in where, plan, hooks
aisquare accounts add              # a fresh slot: Claude Code opens, you sign in, it is recorded
aisquare accounts run 2            # a plain session on account 2 (what a c2 alias did)
aisquare accounts usage            # the 5-hour and weekly windows, per account
aisquare accounts remove 2         # forget it; the directory is kept as 2.removed-<stamp>

aisquare launch coder --account 2  # a board role on account 2
aisquare fleet spawn coder --account 2
```

The same page lives in `asq` under **Accounts**: the AISquare sign-in on top
(a card that runs the same browser flow as `aisquare login`), then every Claude
account with its usage bars, **+ Add Claude account** — the login opens in a
pane right there and closes by itself the moment it lands — and **Remove**.
`aisquare doctor` gets a `claude-accounts` line naming any slot that still needs
a sign-in.

**Choosing one.** Several accounts are only useful if a launch knows which to
run on, so an account can be the **default**, have a **name**, and sit in a
**priority order** — arranged from the Accounts page (★ *Default*, ↑/↓,
*Disable*) or from the command line:

```sh
aisquare accounts default 2               # the machine default: every launch runs on slot 2
aisquare accounts default 3 --project .   # …except this project, which runs on slot 3
aisquare accounts default 1 --role coder  # …and coders, who run on the plain claude
aisquare accounts default                 # show the three levels as they stand
aisquare accounts alias 2 work            # name it: --account work, [work] on the board
aisquare accounts order work 3            # the priority order (what headroom-based picking will try first)
aisquare accounts move 3 up               # one step at a time
aisquare accounts disable 3               # never picked automatically; --account 3 still works
aisquare team bind coder --account work   # the same role binding, from the team side
```

A launch — `aisquare launch`, `fleet spawn`, the manager spawning a coder —
resolves the account in exactly one order: `--account` on the command line, then
the role's binding, then the project's default, then the machine's default. With
none of those set it runs on whatever `claude` the shell already has, exactly as
before, so a machine that never ran `accounts default` notices nothing. One
resolver answers for every surface, so the fleet UI and a hand-typed launch can
never disagree about which login an agent gets; `accounts list` stars the
default and lists the slots in priority order; `doctor` warns when the default is
not signed in or is disabled, and when a binding names an account the machine no
longer has.

**Spending several accounts.** With more than one login the interesting
questions are *where is there room* and *what happens when one runs out*. Both
are `[accounts]` settings (on the Settings tab, or `aisquare config set`):

```sh
aisquare config set accounts.pick headroom   # spawns take, in priority order, the first account
                                             # under switch_at % of its 5-hour window — or the one
                                             # with the most room when all are over it
aisquare config set accounts.switch_at 85    # the line (default 85 %)
aisquare config set accounts.on_limit switch # when an agent hits its limit, move it (default: wait)
aisquare fleet switch coder-auth             # move one by hand: same label, task and worktree,
                                             # on the account with headroom, resuming its session
aisquare fleet switch coder-auth --to work --fresh   # a named account, and a fresh session with a
                                             # hand-off prompt built from the board
```

When an agent's turn ends on a usage limit — Claude Code's `StopFailure` hook,
`You've hit your session limit · resets 12:30am` — its row shows **⏳ limited**
with the reset time, the manager is woken with the one command that moves it,
and Claude Code's own wait-and-continue at the reset is left in place. With
`on_limit = switch` the fleet hands the agent over on its own: it stops the
agent, starts it again on the account with the most headroom, and resumes the
same session by its transcript (`claude --resume <path>`), unless the limit
lifts within `wait_if_reset_within_minutes` (a reset ten minutes away is cheaper
than a cold start). Every usage reading is kept, so `accounts usage` and the
Accounts page can say *≈ 40 min to the limit* at the current pace, and
`doctor --live` warns when every account is over the line.

An account is two variables, `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_TMPDIR`, set
for the launch and nothing else. Slot 1 sets neither: it is whatever `claude`
already is in the shell you launch from. The CLI never writes into Claude
Code's own files — it reads the email and plan Claude Code recorded, and the
usage numbers come from the endpoint Claude Code's `/usage` reads, best effort:
if that endpoint changes, a row says `usage unavailable` and nothing else
breaks.

Accounts laid out some other way — a wrapper script, a proxy, a directory you
made yourself — still bind to a role as a **launch profile**: a binary, a set
of env vars and extra args, carried through verbatim.

```sh
aisquare agents connect claude-code --config-dir ~/.claude-account1
aisquare team bind coder1 \
  --env CLAUDE_CONFIG_DIR='$HOME/.claude-account1' \
  --env CLAUDE_CODE_TMPDIR='$HOME/.cache/claude-account1'
aisquare launch coder1             # bound above — nothing to retype
```

`~` and `$VAR` expand at launch, so one binding follows you across machines
with different homes, and an undefined variable is left as written rather than
blanked — a silently empty `CLAUDE_CONFIG_DIR` starts a fresh unauthenticated
profile that reads as a login failure hours later instead of the typo it is.
Set **both** variables: `CLAUDE_CONFIG_DIR` alone shares the *default* scratch
directory with every other account, which looks isolated right up until two
parallel sessions collide in temp. `--account` sets both for you.

For a one-off, `aisquare launch <role> --env KEY=VALUE` merges over the
binding per key. Shell aliases (`alias claude1='CLAUDE_CONFIG_DIR=… claude'`)
can **not** be passed to `--command` — an alias is not an executable — but an
alias is only env vars around a binary, which is exactly what `--env` sets.

Each session records **which config dir it runs under**, and the board labels
sessions with it once more than one account is in play — `account 2` for a
slot the CLI owns, the directory name for anything else:

```
sessions:
  - a1b2c3d4 coder [account 2] — 2m ago
  - e5f6a7b8 coder [.claude-account1] — 1m ago
```

So when one account hits its limit you can see exactly which terminals to
relaunch elsewhere. Because claims are leased and released on `SessionEnd`,
a killed session hands its task straight back to the pool — relaunching under
another account picks the work up with full context from the board.

All accounts share one `~/.aisquare` — one context store, one board, one task
list. Sessions are per **terminal**, not per account, so several accounts
simply mean several rate-limit pools driving one team. `agents list` and
`doctor` report every connected directory separately, so a sibling install
whose hooks went missing is named rather than hidden behind a healthy ✓.

For executions spanning multiple repositories, set
`AISQUARE_TEAM_HUB=/path/to/hub` in every session; git worktrees already
share their principal repo's board automatically.
