# Precise detection of a session's own MCP server row (option B)

> **Status: a handoff, not code.** A follow-up to #274. Its option A (task #52, landed
> on `fix/followups` at `0545dc5b`) counts every live `mcp:` row as a teammate and pins
> the cost as a known limitation. This plan describes option B: tell a session's own MCP
> server row apart from a real remote teammate's, so a solo session's own board-tool calls
> stop counting as a team. Every claim about the code below was checked at `0545dc5b`.
> Build it fresh on top of #274 once that is merged, in one PR.

## The problem

**The solo trim (#346).** A session with no role, no fleet row and no persona, on a board
with nobody else on it, is *solo*. It gets no `<aisquare-team>` block, and the MCP server's
instructions are one line (`SOLO_INSTRUCTIONS`) instead of the board's protocol
(`TEAM_INSTRUCTIONS`). "Nobody else" means `team._has_team` finds no open task, no live
fleet agent, and no live session other than the caller's own. A session is live if it was
seen within `_STALE_AFTER` (30 minutes) and has not ended.

**Two kinds of `mcp:` row.** Every MCP tool call goes through
`mcp_server._ensure_virtual_session`, which writes or revives a board session with the id
`mcp_server.client_session_id(project)`: `mcp:<client>:<first 6 of the project id>`, where
`<client>` is `AISQUARE_SERVE_CLIENT` or `remote`. Its role is `AISQUARE_SERVE_ROLE` or
`remote`. That row is written by two quite different things:

1. **A session's own MCP server.** A Claude Code session that mounts `aisquare serve
   --stdio` for its own tools (by hand: `claude mcp add`, or a project `.mcp.json`;
   aisquare's plugin ships only the six hooks, no MCP server) writes the row on its first
   board-tool call. That row is the session itself, not a teammate.
2. **A real remote teammate.** A Claude Desktop agent, or any other MCP client, over
   `aisquare serve --stdio` or `aisquare serve` (streamable HTTP). That row is a teammate
   (docs/orchestration.md, *Remote agents over MCP*).

Nothing in the row says which it is. With no client name, both are `mcp:remote:<proj>`.

**What it costs today (#45 m1, a known limitation).** A solo session that calls
`team_board()` (or any board tool) writes its own server's row, and from then on reads as
a team. Its next prompt gets the board once. A new session's start gets the team block.
The MCP server's instructions turn TEAM for a client that starts next. All of this lasts
until that row goes stale, `_STALE_AFTER` (30 minutes) after the last call. That is about
1.1k tokens per request for those 30 minutes. It is pinned by
`tests/test_team_block_needs_a_team.py::test_known_limitation_a_solo_sessions_own_board_tool_reads_as_a_team_until_it_goes_stale`,
and documented in docs/orchestration.md and the CHANGELOG's solo-session bullet.

## The hard rule

**No member of a team may ever read as solo.** A teammate read as solo gets no board, no
protocol and no instructions, and the team silently stops coordinating. The trim is
secondary: it saves tokens. When a design cannot tell, it must fail toward team. Every
check that cannot be completed (a store error, a process that cannot be inspected) means
team.

## What was tried, and why it failed

**1. #46: skip every `mcp:` row** (commit `7d42052d`, in `_has_team` through
`team.CLIENT_SESSION_PREFIX`). It fixed m1 and broke the hard rule. A hand-run session
beside a remote teammate over `serve` read as solo: no block, the SOLO line, nothing on its
prompts. Found by #47 (MAJOR-1, board seq 1718).

**2. #48: name the row after the hosting Claude Code session** (commit `d96fb8d9`). On
Claude Code 2.1.310, a stdio MCP server that Claude Code starts inherits
`CLAUDE_CODE_SESSION_ID`, and its value equals the `session_id` the session's hooks get.
With no client name set, `client_session_id` wrote `mcp:cc-<that id>:<proj>`.
`_has_team` skipped such a row (`team._speaks_for_itself`) when its host was the caller, or
had a row of its own on the board. #49 (board seq 1761) found three faults:

- **MAJOR-1: the variable is not only the MCP server's.** Claude Code exports
  `CLAUDE_CODE_SESSION_ID` to every shell it runs: its Bash tool, a `!` command, and a tmux
  or terminal started from there. An `aisquare serve` started from session S's shell
  inherits S's id. It wrote every client's row, a Desktop teammate's included, as
  `mcp:cc-S:<proj>`, and `_has_team` skipped it. Repro: S starts on the board; the serve
  process, with S's env, takes a Desktop client's `note_add`; S's next prompt has no team
  block. After S ends, a new hand-run session also reads solo beside the live Desktop
  teammate.
- **m1: the host lookup was unscoped.** `_speaks_for_itself` called
  `store.get_session(host)`, which searches every board and matches a prefix. A host with
  a row on board B hid its server row on board A. Repro: R's hooks put its row on B, R
  mounts A's `serve --stdio` and posts on A, and a hand-run session on A reads solo.
- **m2: attribution.** Each Claude Code session's own MCP calls showed up as a session of
  their own, `mcp:cc-<uuid>:<proj>` with role `remote`. Two coders on one board that each
  call `note_add` showed four sessions, and their notes read `mcp:cc-a (remote) note: …`.
  The 8-character short id kept one hex digit of the session. In the "LAST-DELTA #274
  MINORS + NITS" note.

**3. #52: option A, the current state** (commit `0545dc5b`). The owner's decision of
2026-10-24 (board seq 1819). `client_session_id` is back to `mcp:<client>:<proj>`.
`_has_team` counts every live session except the caller's own. #49's two repros are
tests (`test_a_serve_started_from_a_claude_code_shell_still_shows_its_clients_as_a_team`,
`test_a_session_on_another_board_never_hides_its_server_row_on_this_one`).

## Candidate designs

The core question: can the server *prove* that the client it serves is the session whose
id it holds, rather than infer it from an inherited variable? Each design below must keep
the hard rule when that proof is missing.

### (a) Trust the session id only under `--stdio`, and only from the parent process

Use `CLAUDE_CODE_SESSION_ID` only when the server runs over stdio and its parent process
(the client at the other end of its stdin/stdout) is that Claude Code process. A serve
started from S's shell has the shell as its parent, not Claude Code. Never for HTTP: an
HTTP server's clients are not its parent.

The hard part is the process tree. Map it for every way the server is launched:

- `aisquare` installed by `uv tool install` or `pipx`: `claude` → `aisquare` (a console
  script), possibly through `sh -c` if the command is a shell string.
- `uvx --from aisquare-cli aisquare serve --stdio`: `claude` → `uvx` → `aisquare`. uv may
  exec or spawn; measure.
- `pip install` into a venv, `python -m aisquare`: `claude` → `python`.
- Windows: console-script `.exe` trampolines add a process (`aisquare.exe` → `python.exe`).
- WSL: `wsl -e bash -lc "cd … && aisquare serve --stdio"` (docs/orchestration.md's
  Desktop example). The parent is `bash` inside WSL, and Claude Desktop is outside it.
  This is a Desktop client, and must count as a teammate.

Even when the tree is right, the server must know which ancestor is "Claude Code" (the
`claude` binary, a node process, a native build). That check is brittle across releases.

### (b) The SessionStart hook registers the session's process; the server proves it is its child

The SessionStart hook runs inside the Claude Code process tree. It records "session X
runs as process P" (P's pid, and its start time, so a recycled pid cannot match). At its
first call, the server walks up its own ancestors and looks for P. If P is found, its rows
are written as X's own.

Alternatively, a per-session nonce: the hook writes a fresh nonce for X, and the server
receives the same nonce through its environment. That only works where a launcher can
pass it (see (c)). Whether Claude Code can put a per-session value into an MCP server's
`env` (for example by expanding a variable in `.mcp.json`) is not measured: check it
first.

Trade-offs: the hook and the server race at session start (both start at once; #346's
start-order limitation is this race). A server that started first must look again later.
A shell started from X also descends from P, so an ancestry check alone repeats #49
MAJOR-1. Combine it with "my parent is P itself" or with (a)'s stdio rule.

### (c) The plugin passes a marker to its own MCP server only

Ship the aisquare MCP server in the Claude Code plugin (its own `.mcp.json`), with an
`env` entry that only that launch carries, for example `AISQUARE_SERVE_OWN=1`. A server
started by hand from a shell, or by Desktop, does not have it. Check whether a plugin's
`.mcp.json` can template the session id or a per-session value; if it cannot, the marker
says only "I am some session's own server", and the session id still comes from
`CLAUDE_CODE_SESSION_ID`, which is now trustworthy because the launch is known.

Trade-offs: it covers only plugin-mounted servers; a hand-registered `aisquare serve
--stdio` keeps today's behaviour (counted, the safe side). A user who copies the env entry
into a Desktop config would hide a teammate: document it, or make the marker useless
outside the plugin (a nonce the hook issues).

### Also in scope for B

- **Scope the host lookup to the board, with an exact id.** Never `get_session` (every
  board, prefix match). Ask this project's sessions only, by exact id
  (`team_sessions_seen_since(project_id, …)` filtered by `id ==`). #49 m1.
- **Attribution.** Show a hosted row as its host: render it as the host's short id and
  role (for example `aaaa1111 coder (mcp)`), or write MCP notes and claims as the host
  session itself, so the board shows one session per agent. #49 m2.
- **Resumed and forked sessions.** A `claude --resume` keeps the session id; a fork
  (`--fork-session`) gets a new one while the MCP server may outlive the switch. The row's
  host must follow the session the server serves now.
- **#346's start-order limitation.** `mcp_server._serves_a_team` calls `board_has_team`,
  which has no caller id, so at a client's start the session's own row (written by its
  SessionStart hook) counts as a teammate and a solo session can get the TEAM
  instructions. A server that knows its host for sure can pass the host id and close this.
- **Pre-upgrade rows and servers.** Rows written by older code, and MCP servers still
  running older code, carry no proof. They must count until they go stale.

## The test matrix

Every design must pass all of these, with the captain on and off (`AISQUARE_SUITE_CAPTAIN=on|off`):

| Case | Expected |
|---|---|
| A nameless Desktop client over `serve --stdio` | team |
| A named client (`AISQUARE_SERVE_CLIENT`), or one with a role (`AISQUARE_SERVE_ROLE`) | team |
| A client over `serve` (HTTP) | team |
| `serve` started from a Claude Code shell, with its inherited env (#49 MAJOR-1) | team |
| A Claude Code session that mounts the server without aisquare's hooks | team (its row is its only presence) |
| A host whose own row is on another board (#49 m1) | team |
| WSL: Desktop via `wsl -e … aisquare serve --stdio` | team |
| A solo session's own `team_board()` | **solo** (flips the known-limitation pin) |
| A resumed session, and a forked one | the server's rows follow the live session |
| Rows and servers from before the upgrade | team until stale |

Plus real-machine checks, one per install method (uv tool, uvx, pipx, pip in a venv), on
macOS, Linux, Windows and WSL: a real Claude Code session with the server mounted, and a
real Claude Desktop beside it.

## Where the code lives (at `0545dc5b`)

- `src/aisquare/services/team.py`: `_has_team` (line 2042), `board_has_team` (2067),
  `_solo` (2014), `member_by_env` (2004), `_STALE_AFTER` (61).
- `src/aisquare/services/mcp_server.py`: `client_session_id` (93), `_client_role` (104),
  `_ensure_virtual_session` (108), `TEAM_INSTRUCTIONS` / `SOLO_INSTRUCTIONS` (413, 424),
  `_serves_a_team` (427).
- `tests/test_team_block_needs_a_team.py`: the pin
  `test_known_limitation_a_solo_sessions_own_board_tool_reads_as_a_team_until_it_goes_stale`
  (line 305), which B must flip to solo; `test_a_serve_started_from_a_claude_code_shell_still_shows_its_clients_as_a_team`
  (429); `test_a_session_on_another_board_never_hides_its_server_row_on_this_one` (449);
  and `test_a_remote_teammate_over_serve_makes_the_board_a_team` (492, four cases), which
  must all keep passing.
- `tests/conftest.py` clears `CLAUDE_CODE_SESSION_ID` with the other ambient variables, so
  the suite is hermetic when run from inside Claude Code. Keep that.
- Prior art: #48's design at `d96fb8d9` (`hosting_session`, `_speaks_for_itself`), and
  its measurement of what Claude Code passes to a stdio MCP server.

## Decisions B must not reopen

- The hard rule above comes before the trim (#48's brief, after #47 MAJOR-1).
- Option A is the shipped behaviour until B lands: every live `mcp:` row counts (the
  owner, 2026-10-24, board seq 1819).
- A hand-run plain `agent` (`aisquare launch agent`) alone on its board is solo; a
  fleet-spawned one, or one with a persona, is a member (the owner, 2026-10-24, #46).

## Open questions for the owner

1. Is (c), shipping the MCP server in the plugin, wanted for its own sake? It is the
   simplest proof, and it changes how users mount the server.
2. Should B also change attribution (#49 m2), so a session's own MCP writes show as the
   session, or keep `mcp:<client>` rows and only stop counting them?
