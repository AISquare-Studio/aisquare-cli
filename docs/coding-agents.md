# Terminal coding agents

AISquare supports Claude Code and Codex through a coding-agent registry. Memory,
the task board, claims, manager continuation, CI delivery, tmux, worktrees and the
terminal UI are shared. An adapter supplies native hooks, config locations,
model options, permission options and MCP configuration.

Codex compatibility is tested against **CLI 0.162.1**. Use this version or a
newer compatible release; older releases may lack the required hooks and flags
(`--approve-for-me`). Cursor
remains a detection-only legacy entry. No Gemini or Antigravity adapter ships
with this change.

## Setup and selection

```sh
# Installer: Codex alone, or both agents (--agent both, or --agent twice).
# Windows: $env:AISQUARE_INSTALL_AGENT = 'codex' before `irm .../install.ps1 | iex`,
# which becomes --agent inside WSL.
sh install.sh --agent codex
sh install.sh --agent both

# Or init directly:
aisquare init --local --yes --agent codex
aisquare init --local --yes --agent claude-code --agent codex

# Existing installation:
asq agents connect codex
asq agents use codex
asq agents use codex --project
asq agents status codex

asq launch coder --agent codex
asq team spawn coder --agent codex --exec
asq fleet spawn coder --agent codex --prompt 'Implement the task on the board'

# Mixed teams:
asq team bind reviewer --agent claude-code
```

`install.sh --agent codex` installs a missing Codex with its own installer
(`curl -fsSL https://chatgpt.com/codex/install.sh | sh`, as its
[docs](https://learn.chatgpt.com/docs/codex/cli) give), run so that it asks
nothing; an installed Codex is kept, and `--upgrade-all` runs `codex update`.
It then runs `init --agent codex` and, with Codex alone, `agents use codex`, so
launches use Codex rather than the Claude Code default. Claude Code is neither
installed nor connected. The run ends on a doctor that is green but for one
row: Codex's hooks are `unverified` until you trust them, which the summary
names as the step left to you (run `codex`, sign in, open `/hooks`, trust them).

Connect merges AISquare's handlers into `CODEX_HOME/hooks.json`, preserving
other hooks and instruction files. Unreadable context files are skipped with a
note; hooks still install. Codex requires native trust: open `/hooks`
in Codex, review the definitions and trust them. Reconnect is idempotent and
does not rewrite an identical file. Changing definitions requires native
review again. AISquare never writes Codex trust records or automatically adds
a trust-bypass flag.
Connect validates and installs hooks before importing global memories. Malformed,
unreadable or non-regular native settings produce a file repair diagnostic;
existing settings and memories are preserved on that failure. Updates retain
the file's permissions and symlink target. Replacing a hard-linked file updates
that path while leaving the other hard links' contents untouched.

Status distinguishes configured hooks from observed execution. `unverified`
means the file is installed but this definition has not been observed running.
`observed` means a callback ran; a session's own trust and permission policy
still applies. Evidence identifies the definition that actually ran, so an older
open session cannot verify newly installed hooks. `aisquare upgrade` rewrites
them for the new install (see Upgrade and uninstall); review the updated hooks in
Codex. Doctor reports missing hooks, stale AISquare executables,
short context timeouts and the review step, including account homes discovered
on disk. End and interrupt handlers are local and use Codex's three-second
maximum. Reconnect preserves extra headroom for the two context hooks and
restores the bounded timeouts for the other lifecycle hooks. Events AISquare does
not install (`SubagentStart`, `PreCompact` and later additions) are ignored
without output.

Codex hooks are on by default. `hooks = false` under `[features]` (or the
deprecated alias `codex_hooks`, which `hooks` overrides wherever both are set) in
`CODEX_HOME/config.toml` or a `-p` profile file, or `--disable hooks` /
`-c features.hooks=false` in a launch, switches every hook off, the way
`"disableAllHooks": true` does for Claude Code. Doctor says so, naming the file
or the roles whose arguments do it, and `agents status` names the file; Connect
still installs the hooks and cannot change the switch.

Selection order is explicit `--agent`, role binding, an exact known binary
override (legacy shorthand), project preference, inherited launch family,
operator `AISQUARE_CODING_AGENT`, user default, then Claude Code. An arbitrary wrapper declares its family with
`team bind ROLE --agent NAME --bin PATH` or `--agent` at launch (`NAME` is
`claude-code` or `codex`). With the implicit default or a saved Claude user default, legacy
bin-only wrappers retain Claude compatibility, including inside an AISquare
pane: the parent's exported family does not reclassify another wrapper.
Non-Claude user defaults, project defaults, and operator `AISQUARE_CODING_AGENT` do not
identify an arbitrary wrapper; with those defaults, declare its family explicitly.
Conflicting known binaries and families are rejected.
Changing a default affects future launches. AISquare writes the inherited family
to `AISQUARE_LAUNCH_AGENT` (source `inherited`); it leaves the operator preference
unchanged (source `operator`). This
applies equally to plain shells, fleet windows with an empty launch ID, and
pasted commands. Printed commands for both agents use `aisquare launch`, which
creates a fresh identity each time without exporting anything into the parent shell.
Printed commands carry the resolved native arguments with `--no-bound-args` so
the launcher does not prepend the saved arguments again.
They also authorize custom role names with `--custom-role`; a plain `launch`
continues to catch role typos unless that option or a saved binding declares the role.

Coding agents are optional for a CLI-only installation (`install.sh --no-agent`).
Doctor warns about a missing executable when an agent or launch profile has
been configured explicitly. Managed `--account` slots work for both agents
(see [Codex accounts](#codex-accounts)); a role's `CODEX_HOME` binding still
works for a home laid out some other way.
A merely present, unused Codex home is advisory. `team harness` reports a bad
role with its repair hint alongside the roles that resolve successfully.
`--json agents use codex --project` returns `scope: "project"` and `project_id`.

### In asq: Welcome, Doctor, Settings and the spawn dialog

`asq` takes a Codex user from an empty home to a running manager and two coders
the way it takes a Claude Code user ([the first five
minutes](fleet.md#the-first-five-minutes)):

- **Welcome, step 2** is the agent the fleet launches for the manager: the
  selection order above, and on a machine where Codex is the only agent installed
  and nothing chose one, Codex. It shows Codex's version (read from its install,
  else `codex --version`) and binary, whether the account the manager will launch
  on is signed in (`CODEX_HOME/auth.json`, read locally), and whether AISquare's
  hooks are in that `CODEX_HOME`. *Connect* runs `agents connect codex` (with
  `--config-dir` when the manager's home is not your shell's); *Sign in* opens the
  Accounts page with `codex login` already running on that account; *Check again*
  looks again (the page also does so every few seconds). Hooks switched off
  (`features.hooks = false`) or a `hooks.json` that cannot be read are named
  instead of offering a Connect that cannot help. The trust step is Codex's own:
  the card says the hooks run once you trust them in `/hooks`, and step 3 says
  where (the manager's pane, the first time it starts). With neither agent
  installed, step 2 names both agents' install commands (Codex: its installer
  `curl -fsSL https://chatgpt.com/codex/install.sh | sh`, Homebrew on macOS, npm);
  with both installed and nothing more specific than the user default choosing, it
  offers *Use Claude Code* / *Use Codex* and saves the pick as your default
  (`agents use`).
- **Welcome, step 3** starts the manager and the coders with `fleet spawn --agent`
  on that agent (a role bound to an agent of its own keeps it), each on the account
  the account ladder picks for it. A Codex the page picked because it was the only
  one installed is saved as your default first, so the coders the manager spawns
  later run on Codex too. Nothing is typed into a new agent: Codex, like Claude
  Code, first asks whether to trust the folder.
- **Doctor** rows for each Codex home: connected (`configured`/`observed`),
  `partial` (some of AISquare's hooks, naming the missing events), not connected,
  hooks naming a stale or missing aisquare, hooks switched off, `unverified` (the
  trust step, whose fix is `/hooks` in Codex — no button can trust hooks), the
  plugin route (connected through the aisquare Codex plugin, its hooks not trusted
  yet, both routes installed — keep one — or the plugin running in place of
  `hooks.json` hooks whose aisquare is gone) and the account rows
  (`codex-accounts`, defaults, bindings, limits). *Connect* fixes are
  one click, and so is a slot's sign-in: *Sign in account N* opens the Accounts
  page with that slot's `codex login` in its pane. `aisquare --json doctor` carries
  every row.
- **Settings** edits, per role, the agent (`team.profiles.<role>.agent`), Claude
  Code's permission mode, Codex's sandbox, approval policy (`on-request` or
  `never`) and `approve_for_me`, the worktree switch and the account binding (whose
  list names any slot of another agent, which a launch would refuse). Under each
  role is the line its next `fleet spawn` passes for permission — the adapter's own
  flags (`--approve-for-me`, `--sandbox read-only --ask-for-approval on-request`,
  `--permission-mode auto`) — or the refusal the spawn would meet. A per-role
  agent-neutral permission value is the fleet-parity task's; until it lands, each
  agent keeps its own words here.
- **The spawn dialog** (`＋ spawn agent`) is `fleet spawn` with the role, agent,
  account (the launching agent's slots only) and permission (Claude Code's
  `--permission-mode`, or Codex's `--sandbox` and `--approval`), and shows the
  flags the launch will pass. It calls the same `services.fleet.spawn`.

Model/account profiles continue to use `team bind`.
Settings retains custom values written by another version, so they can be
reviewed or corrected without losing them when saving unrelated changes.
A damaged AISquare config renders defaults and an error; saving preserves the
damaged file until it has been repaired and reloaded.

### The Codex plugin

Without the installer, Codex can take aisquare's hooks from a plugin:
`codex plugin marketplace add AISquare-Studio/aisquare-cli`, then
`codex plugin add aisquare@aisquare-cli`, then trust its hooks in `/hooks`.
They run `aisquare hook codex` and stand down while `$CODEX_HOME/hooks.json`
runs aisquare's; `aisquare agents status codex` names the route (`hooks`,
`plugin` or `both`). See [The Codex plugin](codex-plugin.md).

### Upgrade and uninstall

`aisquare upgrade` re-connects Codex's hooks as it does Claude Code's: the new
install runs `agents refresh-hooks codex` for every Codex home this machine
connected and for each Codex account home under `~/.aisquare/codex-accounts`,
so the hooks name the new install. Only homes holding AISquare hooks are
rewritten; other groups, `config.toml` and `AGENTS.md` are left as they are,
and a home whose hooks run another aisquare install is left and named. An
identical definition is not rewritten; a changed one is reported with the
review Codex asks for in `/hooks`. `--version` back to a release without Codex
hooks (0.8.0 or older) leaves them, naming `agents disconnect codex` to run first.

`aisquare uninstall` takes AISquare's hook groups out of every Codex home it
finds (`~/.codex`, `~/.codex*`, `$CODEX_HOME`, connected homes, account
homes), keeping every other group. It reads each home's `config.toml` and
never rewrites it: a `[mcp_servers.<name>]` that runs aisquare is listed with
the `codex mcp remove <name>` that removes it there (with `CODEX_HOME=` set for
a home other than the one your shell's `codex` reads), and an enabled aisquare
Codex plugin with its `codex plugin remove`. `--purge` refuses while either
would start aisquare again, as it does for Claude Code's. MCP servers added only
for a launch (`agents.mcp = true`, `-c mcp_servers.aisquare…`) are never written
to a file, so there is nothing to remove.

## Codex accounts

`aisquare accounts` and the Accounts page manage Codex logins as they manage
Claude Code's ([Accounts](fleet.md#accounts)): numbered slots, an alias, a
priority order, a default, *Disable*, usage, a headroom pick and the hand-over
on a usage limit.

- **Slots.** One ladder numbers both agents' slots, so a number names one
  login: `aisquare accounts add --agent codex` takes the lowest number no
  Claude or Codex slot uses and makes `~/.aisquare/codex-accounts/<n>/` (its
  marker names `"agent": "codex"`). Existing Claude slot numbers never change.
  Slot **0** is the plain `codex` of this shell (`CODEX_HOME`, else
  `~/.codex`), listed once that home exists; slot **1** stays the plain
  `claude`. A launch on a Codex slot sets `CODEX_HOME` to it and keeps the
  shell's own as `AISQUARE_PLAIN_CODEX_HOME`, so a process under the slot still
  knows which home the plain codex is. Codex has no scratch variable of its own.
- **Sign-in.** `add --agent codex` runs `codex login` with the slot's
  `CODEX_HOME` (in the foreground; the Accounts page runs it in a tmux window
  as `aisquare accounts run <n> login`). It writes `auth.json`; once that holds
  a login, the slot is recorded and `agents connect codex --config-dir <slot>`
  installs AISquare's hooks there (Codex asks you to trust them on its first
  session). A slot nothing signed into is discarded. `accounts run <n>` starts
  `codex` on the slot.
- **Identity.** Read from the claims of the id token in `auth.json` — `email`,
  and `chatgpt_plan_type` / `chatgpt_account_id` under
  `https://api.openai.com/auth` — decoded locally and never verified, sent or
  printed. No token value is ever read beyond whether one is there. An API-key
  login shows `API key`.
- **Usage, offline.** There is no public usage endpoint and AISquare makes no
  request for Codex: each session writes a rollout
  (`$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl`) whose `token_count`
  events carry `rate_limits`, and the newest one under a home is that account's
  usage. The shorter window is shown as the session window and the longer one
  as the week; some plans have only a weekly window. A window whose reset has
  passed since that reading reads 0 %, and the reading is labelled with its
  age (`as of 3h ago`) because it is only as fresh as the account's last turn.
  A Codex reading is not kept as a usage sample, so its bar shows no
  *to the limit* trend.
- **Choosing.** `--account`, a role's `account` binding, `accounts default
  --project` and the machine default all answer with an account of the role's
  agent: a Codex role takes a Codex slot. A Claude slot named for a Codex role
  (or the reverse) is refused naming both agents. Defaults are per agent;
  `accounts default <ref>` sets that account's agent's default and
  `accounts default --clear --agent codex` clears Codex's. An email signed in
  to both agents resolves to the launching agent's account; without one it is
  refused as ambiguous.
- **Usage limits.** Codex has no `StopFailure` hook. On its `Stop` and
  `Interrupt` events AISquare reads the end of the session's rollout: a
  `rate_limit_reached_type` on the newest `token_count`, or an `error` event of
  this turn with `codex_error_info: usage_limit_reached` (or the text
  `You've hit your usage limit`), parks the row **limited** with the reset time
  of the window that is full, puts `limited` on the board and wakes the
  manager — exactly what Claude Code's limit does. With `[accounts] on_limit =
  "switch"` the hand-over moves the agent to the Codex account with the most
  headroom. A limit found when the session ends is recorded without a move.
  AISquare relies on no wait-and-continue from Codex at the reset (Claude
  Code's): a parked Codex agent that is not switched waits for its next prompt.
- **Switching is always fresh.** A Codex thread lives under the old
  `CODEX_HOME` and cannot be resumed from another, so `fleet switch` of a Codex
  agent (by hand or automatic) starts it fresh with the board-built hand-off
  prompt and moves its claims onto the new session, as `--fresh` does.
- **Doctor** lists Codex slots and their headroom beside Claude's; reading
  Codex headroom needs no network, so it does not wait for `--live`.

## Native settings

Codex resolves `--config-dir`, profile `CODEX_HOME`, ambient `CODEX_HOME`, then
`~/.codex`. Connect imports the effective global `AGENTS.override.md` or
`AGENTS.md` into the user memory pool. Project and nested instruction files
remain under Codex's native project discovery; they are never promoted to
global memory or overwritten by AISquare.

Codex keeps its native model default. Configure optional agent and role
preferences in AISquare's `config.toml`:

```toml
[agents]
default = "codex"
mcp = false

[agents.models.codex]
model = "your-model-id"
effort = "high"

[agents.models.codex.roles.reviewer]
effort = "xhigh"

[team.profiles.coder]
agent = "codex"
bin = "codex"
env = { CODEX_HOME = "/path/to/codex-account" }

[fleet.roles.coder]
approve_for_me = false        # the default is Codex's --approve-for-me
sandbox = "workspace-write"
approval_policy = "on-request"
agent_args = { codex = ["--no-alt-screen"] }
```

`AISQUARE_MODEL_<ROLE>` and `AISQUARE_EFFORT_<ROLE>` remain explicit role pins.
Codex names `minimal`, `low`, `medium`, `high`, `xhigh`, `max` and `ultra`; these
pass unchanged, and which of them a model takes is Codex's to settle (a level the
model lacks is sent as its default). The shared name `ultracode` maps to Codex's
`max`, with a note identifying the mapping: a native reasoning level, without
Claude workflow orchestration. Another level name is passed on with a note for
Codex to validate; a value that is not a level name is refused. AISquare-configured values are trimmed and effort
names ignore case; blank environment/config pins fall through to native defaults.
An explicitly empty `--effort` is a usage error. Native model availability is left to
Codex, without paid discovery probes. `--probe`, `--no-probe` and `--refresh`
explain that there is no AISquare Codex availability cache. Claude retains its ladder, but probes
now use the selected executable and effective account. Cache keys include the
resolved executable's upgrade fingerprint, login identity and provider inputs.
File-backed Claude credentials also contribute their subscription and rate-limit
tier. On macOS those entitlements live in Keychain and are not read for cache
hashing: use `team spawn ROLE --refresh` after a plan change, or wait for expiry.
Parent-session variables, OAuth refresh timestamps and hook edits do not
invalidate the 24-hour cache. `--refresh` clears the selected account's scope.
Both refresh and ordinary cache writes prune expired scopes while retaining
other accounts' valid cached verdicts.

```sh
asq fleet spawn reviewer --agent codex --sandbox read-only --approval on-request
asq launch coder --agent codex -- resume NATIVE_SESSION_ID
asq launch coder --agent codex -- exec --json 'Run the local checks'
```

Use `--` to explicitly separate native options from AISquare options. Codex
`-c KEY=VALUE` and `-cKEY=VALUE` also work before the separator, as do attached
model options such as `-mMODEL`. Legacy `-c BINARY` selects an executable;
`--command BINARY` is unambiguous, including for a path containing `=`.
Attached AISquare options retain their values, including spaces in executable
paths or environment assignments. Once a native subcommand or positional prompt
begins, all following tokens belong to the agent. For example,
`asq fleet spawn coder --agent codex exec -listfiles` forwards both native tokens;
`asq launch coder --agent codex exec -- -listfiles` preserves the native separator.
Known native option values such as `--model MODEL` and `--add-dir DIR` do not
start that boundary. For an unknown native flag whose arity is ambiguous, put
AISquare options first and use `--`; the parser refuses ambiguous mixed ownership.
An explicit separator before any native command belongs to AISquare. To pass a
literal dash-prefixed prompt directly, include the native separator too:

```sh
asq launch coder --agent codex -- -- '-migrate the schema'
```

Explicit native model/effort options override AISquare defaults without injecting
a second value. Codex validates its own `-c` values, including future reasoning
levels; the shared alias `ultracode` is rewritten to `max` in the actual native
arguments. AISquare validates its explicit `--effort` and the
effective Claude effort saved with `--arg`. One-off native passthrough is validated
by the agent, including future levels; a later native effort supersedes a saved one. `team harness` and `team spawn` report these choices with source `native`;
these are argv overrides, not availability claims. Codex parses `-migrate` as
`-m igrate`, including a space-containing suffix in a quoted token. To preserve
a configured model and send that text as a prompt, use the two separators above.
When native effort overrides AISquare's `--effort`, spawn reports the precedence.
`team harness` also reports fleet-specific arguments and model choices separately
from the defaults used by `team spawn`.
Native sandbox and approval policies stay separate.
Codex fleet roles run with `--approve-for-me` (Codex's automatic reviewer, in the
workspace-write sandbox), the counterpart of the `auto` permission mode Claude
Code fleet roles get; reviewers, including numbered seats such as `reviewer2`,
run `--sandbox read-only --ask-for-approval on-request` instead. Codex refuses
`--approve-for-me` beside `--sandbox` or `--ask-for-approval`, so a role (or a
spawn) that chooses a sandbox or an approval policy, in its settings or its
native arguments, runs without it; `approve_for_me = false` turns it off, and
`approve_for_me = true` beside a conflicting choice is refused at spawn. The
approval policies are `on-request` and `never`: Codex no longer supports
`untrusted`, `on-failure` is deprecated, and the granular table is native
config. A sandbox or policy given in the native arguments is not given twice.
Numbered seats inherit the base role's sandbox, approval policy and
`approve_for_me` per field unless configured separately.
Their worktree, Claude permission mode and extra arguments keep their own
defaults. Saved Codex sandbox/approval
settings apply only to Codex; Claude Code keeps its own permission mode.
Legacy fleet `extra_args` belong to Claude Code; `agent_args` maps each agent
name to its own extra arguments. A customized Claude role therefore cannot
forward `--restricted` or `--verbose` into a Codex fleet launch.
No approval bypass is added: `--approve-for-me` reviews each request.
Native subagents are disabled in fleet sessions when
`fleet.disable_native_agent_teams` is enabled.

## Identity, MCP and telemetry

Codex board IDs are scoped by agent, config home and native thread ID. A launch
token and fleet token bind hooks without guessing a newest transcript. Binding
metadata can arrive before the fleet row; resume keeps the same board identity.
Callback deduplication prevents concurrent prompt capture and repeated manager
decisions. Generated continuations have their own prompt source.
Failed callbacks release their owned pending claim immediately so retries can
recover. Pending claims expire after 180 seconds using a monotonic clock.
An unavailable observation cache costs readiness evidence, not lifecycle processing.
Launch bindings and retry records expire after 24 hours without session activity.
Active sessions retain their bindings and replay history. Metadata without a
historical timestamp gets a full retention period on upgrade; restored rows with
an unknown (zero) timestamp are retained. Queued insights are retained too.
Cleanup runs on all native session starts and receiver maintenance, at most once
per minute across processes sharing the store. Ordinary starts between cleanup
runs use an indexed timestamp read.

Initial fleet tasks use Codex's positional prompt. Subsequent automated input
requires a linked waiting session; otherwise `fleet tell` files a board note.
Permission and user-input hooks mark attention, tools restore working state,
and end hooks release claims. The existing fleet pause, resume, attach, stop,
reap and worktree operations remain shared; `fleet stop` types `/exit` and its
Enter apart, because Codex reads one burst of keys as a paste. Restart a stopped role with
`fleet spawn`; resume a native thread with its explicit native ID.

Set `agents.mcp = true` to add AISquare's stdio server to launched sessions.
Install the `serve` extra first. This is ephemeral launch configuration;
unrelated servers are retained. The launch identity and AISquare configuration
environment are forwarded explicitly to Codex's MCP child. MCP tools reuse a
hook-bound session for the requested project when available. Until hooks join
(including while awaiting native trust), local clients use a provisional
identity unique to the project and launch token (or fleet token when there is
no launch token). The native join adopts its claims, history and focus, renews
its leases, and retires the provisional presence, including after an idle sweep
ended that provisional row. Late tool calls resolve to
the native row. External clients retain their project-scoped virtual identity.

With both `explainability.enabled` and `explainability.ship` enabled, Codex
exports native OTLP JSON logs to a per-launch, authenticated loopback receiver on
POSIX/WSL. It spools allowlisted, redacted event metadata. Existing
`asq explainability ship` delivery replays model, tool and decision spans
through the SDK under the same run binding. User-configured native OTEL
exporters in the effective account home, selected profile, system config or
explicit `-c`/`--config` overrides are preserved; AISquare reports that it
has stood down. Unreadable or invalid native config also leaves telemetry
unchanged, with a message naming the affected layer and file. Missing files
are treated as absent. Attached native options such as `-cotel.exporter="none"`,
`-pwork` and `-p=work` are inspected too. Prompts and unselected profiles do not
disable tracing. Use a second `--` to keep a prompt beginning with an option
literal, for example `asq launch coder --agent codex -- exec -- '-pwork'`:
the first terminator belongs to AISquare, the second to Codex.
Codex [ignores `otel` in project-local config](https://learn.chatgpt.com/docs/config-file/config-advanced),
so AISquare does not search ancestor projects or other account homes for
exporters. Usage is counted from native logs once, with the observed provider
name; duplicate exports and the parallel native span stream do not add usage again.
Each queued event's dedup marker commits before the next event is processed,
so a failed batch retains its completed prefix on retry. Spool file writes do
not hold the board's database writer lock. A temporary spool failure returns
a retryable receiver response instead of acknowledging the incomplete batch.
Per-launch deduplication/provider metadata is removed when the receiver exits.
Malformed OTLP batches return 400. Temporary spool failures such as a full disk
return 503 for retry; permanent failures such as a read-only spool fail open with
200. A partial batch checkpoints its queued prefix in one metadata transaction,
without holding the board's writer lock across spool writes. Delivery remains
at least once across receiver crashes or loss of the checkpoint store.

Receivers also prune metadata idle for 24 hours and caches from older releases;
project purge removes that project's native metadata, including unjoined launches.
Queued insight records remain available for delivery. Malformed token counts
are treated as unknown (zero), so they cannot indefinitely block later records.

This transport does not change model routing, API keys or ChatGPT login. It
does not capture raw model bodies or tool arguments. Native timestamps remain
metadata; SDK replay creates spans at delivery time. Do not interpret replay
span duration as native model latency. Exporter or gateway failure leaves the
agent usable; disabled telemetry starts no receiver. Native Windows launches
remain usable without this POSIX receiver; the fleet runs in WSL.

## Validation and extending support

`make check` runs the common suite. The opt-in native fixture uses the installed
Codex binary, temporary config homes and a loopback Responses server:

```sh
AISQUARE_TEST_CODEX=1 .venv/bin/python -m pytest tests/test_codex_native.py
```

The CI Codex job installs the pinned compatible binary and runs
`python -m tests.run_codex_native` on each PR, alongside the existing Claude
and shared regression suite. This runner checks the exact binary version and
requires the native fixture to execute and pass; an all-skipped run fails.
The ambient CI jobs also export parent-agent identity and a populated
`CODEX_HOME`, which the shared test fixtures must isolate.

It covers native hook context, prompt capture, exec/resume identity, tool and
MCP calls, usage export, interactive fleet startup with `--approve-for-me`, tell,
resize, a graceful stop (Codex's own `SessionEnd` releases the claim) and the
effort levels that reach the provider.
Owned test hooks use Codex's automation trust flag only inside this fixture.
No production credentials or paid model calls are used. Common tests also
cover config preservation, mixed agents, a synthetic third adapter, migration,
concurrent callbacks, manager continuation, CI, MCP, redaction and SDK replay.
Live ChatGPT/API-key authentication and delivery to a deployed gateway require
release smoke tests with those services; loopback tests do not claim that
external validation.

To add another terminal agent, follow [Adding a terminal agent](#adding-a-terminal-agent).

Native contracts (checked against 0.162.1's `codex --help` and `codex features list`):
[Codex hooks](https://learn.chatgpt.com/docs/hooks),
[configuration](https://learn.chatgpt.com/docs/config-file/config-reference),
[MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), and
[non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode).

Native OTLP retries use deterministic spool names and durable receipts independent
of the board. A killed receiver or locked board cannot republish an already queued,
claimed, or delivered event within the seven-day receipt retention period. Shipping
still retries network delivery in the existing outbox; this does not promise exactly-once
network delivery if the gateway accepts a request but its acknowledgement is lost.
Malformed members are skipped without discarding valid neighboring records. Permanent
spool failures cost at most one write attempt per request, and `aisquare doctor` checks
spool access and reports the most recent receiver failure.

## Adding a terminal agent

`src/aisquare/core/agent_adapters/` is the one place a terminal agent (Gemini CLI,
Grok, ...) is implemented. The shared services (hooks, board, launch, fleet, doctor)
ask its adapter and never compare its name: `tests/test_no_agent_literals.py` fails on
`== "codex"`, `in (..., "claude-code")` or a `match` case on either name anywhere
outside that package. Keep unsupported capabilities explicit instead of copying
another agent's flags or claiming hookless detection is integration.

### The files

| # | File | What |
|---|------|------|
| 1 | `src/aisquare/core/agent_adapters/<agent>.py` | A class implementing `AgentAdapter` (`types.py`), with its `AgentCapabilities`. |
| 2 | `src/aisquare/core/agent_adapters/__init__.py` | One line: `register(<Agent>Adapter())`. |
| 3 | `tests/fixtures/agent_events/<id>.json` | `events`: a recorded payload for every hook event it installs, with the `AgentEvent` fields it must decode to; `version_output`/`version`; `hooks_off` (the file and text that switch its hooks off). |
| 4 | `tests/test_adapter_contract.py` | Its row in `EXPECTED_PERMISSIONS` (a coder's and a reviewer's flags). |
| 5 | `tests/test_<agent>_native.py`, `tests/run_<agent>_native.py` | The opt-in test against the real binary (`AISQUARE_TEST_<AGENT>=1`), as `test_codex_native.py` and its CI job do for Codex. |
| 6 | `docs/coding-agents.md` | Its setup and native settings. |

Nothing else, and no new `aisquare hook` subcommand: a `HookSpec` whose command is
`types.GENERIC_HOOK` installs `aisquare hook event --agent <id> --event <native event>`
(plus `--config-dir <home>` with `hook_names_home`), which decodes with the adapter.
Optional slots: numbered login slots (`accounts`, an `AgentAccounts`), the plugin route
(`plugin`, an `AgentPlugin`), and native telemetry (`capabilities.native_telemetry` with
`telemetry_args`; the receiver in `services/native_telemetry.py` reads Codex's OTel
attributes and config layers, which a second telemetry agent extends there).

### The protocol

| Slot | What it answers |
|------|-----------------|
| `id`, `label`, `binary`, `install_hint` | The board's and config's name; what the UI says; the executable (`adapter_for_binary` knows a wrapper by this name). |
| `home_env`, `home_name`, `config_dir(home, env, explicit)` | The config dir the binary reads: explicit, else `$home_env`, else `~/home_name`. |
| `version_args` | The arguments that print its version; `types.version_in` reads the output. |
| `settings_name`, `capabilities.hooks` | The hooks file in that dir, and each event installed with its subcommand, timeout and matcher. `core.agents.install_hooks`/`remove_hooks` merge it, keeping every foreign entry. |
| `hooks_off_setting`, `hooks_switch(home, settings, args)` | The setting that silences every hook, and whether the dir or the launch arguments set it. |
| `plugin` | The aisquare plugin route, or `None`. |
| `decode_event(payload)` | The payload as an `AgentEvent` of one kind: `session_start`, `prompt`, `stop`, `session_end`, `interrupt`, `attention`, `tool_done`, `turn_failed`, `notification`; `None` for one AISquare ignores. |
| `encode_reply(kind, reply)` | What the hook prints: context for `session_start`/`prompt`, a "keep going" decision for `stop`, `""` for nothing. |
| `model_args`, `resolve_model`, `native_args`, `native_choice`, `validate`, `effort_alias` | Model and reasoning effort: the flags, the configured choice, the native spelling, what a native argv already chose. |
| `permission_args(intent, role, native)` | The flags for a `PermissionIntent` (below), less what the `native` arguments already settle; `ValueError` for one it cannot express. `fleet.roles.<role>.permission` and `--permission-mode WRITE:APPROVALS` reach every agent through it (`services.fleet.permission_flags`). |
| `fleet_args(role, label, mode, ...)` | A fleet spawn's flags from the role's native permission settings, then `label_args(label)`. |
| `resume_args(native_id, transcript)` | The arguments that resume a session, put first: `fleet restart`, `fleet switch` and Restart resume the native id the first hook bound (Codex: `codex <options> resume <id> -- <continue line>`). |
| `exit_command` | What `fleet stop` types to end the session so its `SessionEnd` runs. |
| `queue_args(native_id, message)` | The arguments that queue a message for a RUNNING session's next turn (Codex: `queue --thread <id> --message <text>`), run with `home_env` set to the session's dir: what `fleet tell` does for a busy agent. `None` for none (a board note). |
| `key_spellings` | tmux key names the agent reads under another one, which the fleet UI's pane sends instead (Codex: `S-Enter` → `C-j`, its newline). |
| `role_defaults(role, binary, args)` | The flags a role needs on this agent wherever it starts (Claude Code's ui-tester `--chrome`), less what `args` says. |
| `browser_tools(home)` | The browser the agent brings itself, as the doctor's browser-tools line names it (Codex's `browser_use` feature and bundled browser plugin); `None` where the doctor finds it another way. |
| `mcp_args`, `context_files`, `disable_native_teams` | AISquare's MCP server for this session; the instruction files it reads; the arguments and environment that keep its own sub-agent teams off. |
| `telemetry_args(endpoint, authorization)` | Its OpenTelemetry exporter, with `native_telemetry`. |

`AgentCapabilities` holds the rest as flags, each documented in `types.py` with the
service that reads it: `pid_env` (the variable that hands a hook the agent's own pid,
Claude Code's `CLAUDE_PID`) or `tool_shell_env` (the variable the agent sets in its
tools' shells and never in its own hooks, Codex's `CODEX_THREAD_ID`) — how a fleet row
tells its agent's own process from a nested child, across a `/clear` or a `/resume` —
`assigns_session_id` (AISquare names the session at launch),
`requires_hook_trust`, `hook_names_home`, `strict_hooks_file`, `stop_reason_as_prompt`,
`limits_in_transcript` (no turn-failed event), `ambient_first_run`,
`reads_pane_readiness` (`fleet tell` may type into a pane that looks idle),
`resumes_across_homes`, `exports_home_env`, `own_doctor_check`, `model_ladders`,
`model_proxy`, `positional_prompt`, `first_context_file_only`, `sandbox_permissions`,
`legacy_fleet_args`, `value_options`, `switch_options`.

A `PermissionIntent` is a write scope (`read_only`, `workspace`, `full`) and an
approvals policy (`ask`, `auto`, `never`), written `workspace:auto`. A reviewer's seats
are `read_only:auto`, every other role `workspace:auto`; an explicit native flag still
wins.

| Intent | Claude Code | Codex |
|--------|-------------|-------|
| `read_only:ask` | `--permission-mode manual --disallowedTools=Edit Write NotebookEdit` | `--sandbox read-only --ask-for-approval on-request` |
| `read_only:auto` (reviewer) | `--permission-mode auto --disallowedTools=Edit Write NotebookEdit` | `--sandbox read-only --ask-for-approval on-request` |
| `read_only:never` | `--permission-mode dontAsk --disallowedTools=Edit Write NotebookEdit` | `--sandbox read-only --ask-for-approval never` |
| `workspace:ask` | `--permission-mode manual` | `--sandbox workspace-write --ask-for-approval on-request` |
| `workspace:auto` (default) | `--permission-mode auto` | `--approve-for-me` |
| `workspace:never` | `--permission-mode dontAsk` | `--sandbox workspace-write --ask-for-approval never` |
| `full:ask` | `--permission-mode manual` | `--sandbox danger-full-access --ask-for-approval on-request` |
| `full:auto` | `--permission-mode auto` | refused: `--approve-for-me` runs only in workspace-write |
| `full:never` | `--permission-mode bypassPermissions` | `--sandbox danger-full-access --ask-for-approval never` |

### What the conformance suite fails on

`tests/test_adapter_contract.py` runs every case for every registered adapter and for
`tests/fake_agent.py`, a synthetic third agent registered only there. It fails when:

- the fixture file or the `EXPECTED_PERMISSIONS` row is missing;
- an installed event has no recorded payload, or decodes to another kind or other fields;
- the adapters together no longer decode every event kind;
- installing twice doubles a hook, removing leaves the file other than it was before,
  a foreign entry is lost, or the installed command is not `hook_args`'s;
- the fixture's hooks-off switch does not make a complete install read as not connected;
- the installed command, run as `aisquare hook ...` with the recorded payloads, does not
  put the session on the board under this agent and then mark it `waiting`;
- a reply prints something for nothing, drops the context, or makes a Stop reply the
  bare reason rather than a decision;
- `version_in` misreads its version line, or `config_dir` ignores `home_env` or an
  explicit dir;
- an intent maps to neither flags nor a refusal with a reason; resume arguments are
  empty; the exit command is not a slash command; the MCP arguments do not name the
  server; nothing keeps native teams off; telemetry arguments come without the
  capability.

The fake adapter was written from this section alone. Besides the cases above it is
spawned through `services.fleet.spawn` with a stub binary, its window's `aisquare
launch` builds its argv, and `fleet stop` types its `/quit`, with no change to any
shared service.
