# Terminal coding agents

AISquare supports Claude Code and Codex through a coding-agent registry. Memory,
the task board, claims, manager continuation, CI delivery, tmux, worktrees and the
terminal UI are shared. An adapter supplies native hooks, config locations,
model options, permission options, MCP configuration and session commands.

Codex compatibility is tested against **CLI 0.153.4**. Use this version or a
newer compatible release; older releases may lack the required hooks. Cursor
remains a detection-only legacy entry. No Gemini or Antigravity adapter ships
with this change.

## Setup and selection

```sh
# Installer: --agent is also forwarded by the Windows -> WSL installer.
sh install.sh --agent codex

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

Connect merges AISquare's handlers into `CODEX_HOME/hooks.json`, preserving
other hooks and instruction files. Unreadable context files are skipped with a
note; hooks still install. Codex requires native trust: open `/hooks`
in Codex, review the definitions and trust them. Reconnect is idempotent and
does not rewrite an identical file. Changing definitions requires native
review again. AISquare never writes Codex trust records or automatically adds
a trust-bypass flag.

Status distinguishes configured hooks from observed execution. `unverified`
means the file is installed but this definition has not been observed running.
`observed` means a callback ran; a session's own trust and permission policy
still applies. Doctor reports missing hooks, stale AISquare executables,
short context timeouts and the review step, including account homes discovered
on disk. End and interrupt handlers are local and use Codex's three-second
timeout. Reconnect preserves extra headroom for the two context hooks and
restores the bounded timeouts for the other lifecycle hooks.

Selection order is explicit `--agent`, role binding, an exact known binary
override (legacy shorthand), project preference, inherited session selection,
user default, then Claude Code. An arbitrary wrapper declares its family with
`team bind ROLE --agent NAME --bin PATH` or `--agent` at launch (`NAME` is
`claude-code` or `codex`). User/project defaults and the inherited
`AISQUARE_CODING_AGENT` choice do not identify a wrapper's family. An unknown
wrapper requires a per-role or per-launch declaration before it receives
native flags, account configuration or model probes. Conflicting known
binaries and families are rejected.
Changing a default affects future launches.

Coding agents are optional for a CLI-only installation (`install.sh --no-agent`).
Doctor warns about a missing executable when an agent or launch profile has
been configured explicitly. Managed `--account` slots belong to Claude Code;
choose a Codex account through the role's `CODEX_HOME` binding instead.

The Settings tab has user and role agent choices; the spawn dialog supports a
per-launch choice. Model/account profiles continue to use `team bind`.
Settings retains custom values written by another version, so they can be
reviewed or corrected without losing them when saving unrelated changes.

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
sandbox = "workspace-write"
approval_policy = "on-request"
```

`AISQUARE_MODEL_ROLE` and `AISQUARE_EFFORT_ROLE` remain explicit role pins.
Codex accepts `minimal`, `low`, `medium`, `high`, and `xhigh`; Claude effort
aliases and model ladders are not reused. Native model availability is left to
Codex, without paid discovery probes. Claude retains its ladder, but probes
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

Use `--` before native Codex options, especially `-c` (AISquare's own `-c` is
the executable override). Native sandbox and approval policies stay separate.
Reviewers, including numbered seats such as `reviewer2`, default to read-only;
other Codex fleet roles use workspace-write. Numbered seats inherit the base
role's sandbox and approval policy per field unless configured separately.
Their worktree, Claude permission mode and extra arguments keep their own
defaults. Saved Codex sandbox/approval
settings apply only to Codex; Claude Code keeps its own permission mode.
No automatic approval bypass is added. Native subagents are disabled in fleet
sessions when `fleet.disable_native_agent_teams` is enabled.

## Identity, MCP and telemetry

Codex board IDs are scoped by agent, config home and native thread ID. A launch
token and fleet token bind hooks without guessing a newest transcript. Binding
metadata can arrive before the fleet row; resume keeps the same board identity.
Callback deduplication prevents concurrent prompt capture and repeated manager
decisions. Generated continuations have their own prompt source.

Initial fleet tasks use Codex's positional prompt. Subsequent automated input
requires a linked waiting session; otherwise `fleet tell` files a board note.
Permission and user-input hooks mark attention, tools restore working state,
and end hooks release claims. The existing fleet pause, resume, attach, stop,
reap and worktree operations remain shared. Restart a stopped role with
`fleet spawn`; resume a native thread with its explicit native ID.

Set `agents.mcp = true` to add AISquare's stdio server to launched sessions.
Install the `serve` extra first. This is ephemeral launch configuration;
unrelated servers are retained. The launch identity and AISquare configuration
environment are forwarded explicitly to Codex's MCP child. MCP tools reuse a
hook-bound session for the requested project when available. Until hooks join
(including while awaiting native trust), local clients use a provisional
identity unique to the project and launch token (or fleet token when there is
no launch token). The native join adopts its claims, history and focus, renews
its leases, and retires the provisional presence. Late tool calls resolve to
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
MCP calls, usage export, interactive fleet startup, tell, resize and stop.
Owned test hooks use Codex's automation trust flag only inside this fixture.
No production credentials or paid model calls are used. Common tests also
cover config preservation, mixed agents, a synthetic third adapter, migration,
concurrent callbacks, manager continuation, CI, MCP, redaction and SDK replay.
Live ChatGPT/API-key authentication and delivery to a deployed gateway require
release smoke tests with those services; loopback tests do not claim that
external validation.

To add another terminal agent, implement the protocol in
`core/agent_adapters/types.py`, register it in that package, and add native
event/telemetry handling where its protocol differs. Reuse the launch, board,
memory and fleet services. Add adapter contract fixtures and an opt-in native
test. Keep unsupported capabilities explicit instead of copying another
agent's flags or claiming hookless detection is integration.

Native contracts: [Codex hooks](https://learn.chatgpt.com/docs/hooks),
[configuration](https://learn.chatgpt.com/docs/config-file/config-reference),
[MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), and
[non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode).
