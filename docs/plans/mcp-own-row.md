# Precise detection of a session's own MCP server row (option B)

> **Status: built, for 0.9.1.** A follow-up to #274. Its option A (task #52, `0545dc5b`)
> counted every live `mcp:` row as a teammate and pinned the cost as a known limitation,
> which the 0.9.0 release notes named. Option B is in: a session's own stdio MCP server
> proves whose it is and acts **as that session**, so a solo session's own board-tool calls
> no longer read as a team, and the board shows one session per agent. Everything the server
> cannot prove stays a teammate, as before. The sections from *The problem* down are the
> handoff this was built from, kept as written (repo convention); *What was built* and
> *What was measured* below say what shipped and what it rests on.
>
> **What shipped.**
>
> - `core/procs.py`: one process's parent, start token and image, from `/proc` (Linux, WSL),
>   `ps` (macOS, the BSDs) or the Toolhelp snapshot and `GetProcessTimes` (Windows).
> - `services/mcp_host.py`: the proof. The SessionStart hook records "the Claude Code process
>   `CLAUDE_PID` (pid and start token, checked to be one of the hook's own ancestors) runs
>   session X" per board, in `team_meta` as `host/<project id>/<pid>`; every start overwrites
>   it, so it names the session the process serves *now*. A stdio server walks up from its
>   parent over launchers only (`uv`, `uvx`, `pipx`, its own console script, the venv's
>   `python.exe` launcher) and, if the process it reaches is the registered one for **this
>   board** (pid and start token equal), and that session's row is there and not ended
>   (found by exact id among this board's sessions, #49 m1), it is that session's own server.
> - `services/mcp_server.py`: a proved server acts **as the host session** (see *Attribution*
>   below): `_ensure_virtual_session` returns the host's id and only touches its row. It is
>   tried for a stdio server with no `AISQUARE_SERVE_CLIENT` and no `AISQUARE_SERVE_ROLE`
>   only; HTTP never. The server's instructions at start pass the host id to
>   `team.board_has_team`, so the host's own row is not a teammate (#346's start-order case,
>   as far as the order allows: *What was measured*, 5).
> - The hook registers at session start and at the late join of a first prompt
>   (`team.hook_session_start`, `team.hook_prompt_heartbeat`). `purge_project` removes the
>   records with the board.
> - Tests: `tests/test_mcp_own_row.py` (every row of the matrix below, a fake process tree),
>   `tests/test_mcp_own_row_processes.py` (a stand-in for Claude Code, the real hook and the
>   real `aisquare serve --stdio` under it, on every platform CI runs), `tests/test_procs.py`.
>   The old pin `test_known_limitation_a_solo_sessions_own_board_tool_reads_as_a_team_until_it_goes_stale`
>   is `test_a_solo_sessions_own_board_tool_stays_solo`; the three tests the plan named as
>   "team" pass unchanged.
>
> **The owner's two open questions, decided by the manager for this build** (the owner may
> still override either):
>
> 1. *Ship the MCP server in the plugin (c)?* No, not for its own sake. How users mount the
>    server does not change. The proof is built from (a), the stdio-only rule with the parent
>    check, and (b), the hook's record of the session's process. An ancestry check alone repeats
>    #49 MAJOR-1; "my parent is that process" does not. A per-session nonce (the other half of
>    (b), and (c)'s marker) is not possible anyway: Claude Code passes `${CLAUDE_CODE_SESSION_ID}`
>    in an MCP `env` entry through *unexpanded* (*What was measured*, 4).
> 2. *Change attribution (#49 m2)?* Yes, **as the host session itself**, not a rendered
>    `aaaa1111 coder (mcp)`. A write made through the session's own server is then the same
>    write its hooks or its shell would make: one row, one author on every note, and a claim
>    taken through MCP is the claim `aisquare task done` in that session's shell finishes, and
>    `team_log(by_session='me')` reads back what the session wrote either way. The session's
>    prompt delta also stops echoing its own MCP notes back to it as a teammate's, which the
>    `mcp:` row did. A rendered label would have kept two ids for one agent and fixed only how
>    they print. The cost: a write through the server cannot tell the board it came over MCP.
>
> **What stays a teammate, on purpose** (the hard rule: no member of a team may read as solo):
> a Desktop client, a named client or one with a role (`AISQUARE_SERVE_CLIENT`,
> `AISQUARE_SERVE_ROLE`), `serve` over HTTP, a `serve` started from a shell (the Bash tool's,
> a `tmux` pane, `bash -lc "cd … && aisquare serve --stdio"`: a shell above the server is not
> a launcher, because a shell is also what the Bash tool is), WSL, a Claude Code session whose
> hooks never ran on this board, a session started before the upgrade (its hook recorded no
> process), a session on another board, a host whose row has ended, and anything that cannot be
> read: a process, the store, a record. Rows older code wrote and servers still running older
> code count until they go stale, as before.
>
> **What was not measured, and is reasoned instead.** This was built on one macOS machine,
> with Claude Code 2.1.310. Linux, Windows and WSL were not run by hand, and the interactive
> TUI was not driven (`-p`, including stream-json input, is the same binary and the same
> MCP and hook launch). Linux reads `/proc`, a documented interface; Windows is the new
> code with the most unknowns (the venv launcher, `uv`'s and pip's `.exe` trampolines, whether
> Claude Code's hook environment has `CLAUDE_PID` there): wherever one is wrong the server is
> a teammate, never a hidden one. `tests/test_mcp_own_row_processes.py` runs the real hook and
> the real server under a stand-in for Claude Code on every leg of CI, so the Windows leg is the
> first real reading of that process tree: **a red there is information about the tree, not a
> flake**. WSL: Claude Code inside WSL is Linux. Claude Desktop on Windows over
> `wsl -e bash -lc "… aisquare serve --stdio"` has a `bash` inside WSL above the server and no
> registered process anywhere above that, so it is a teammate by construction.

## What was measured

Real Claude Code **2.1.310** on macOS (Darwin 27, arm64), in an isolated `HOME` (never
`~/.claude` or `~/.aisquare`), against a stub of the Messages API on `127.0.0.1` that scripts a
Bash call and calls to the MCP tools (`ANTHROPIC_BASE_URL`; nothing left the machine). `claude
-p`, and `-p --input-format stream-json` for `/clear`. Servers: a probe MCP package installed
each way (`uv tool install`, `pipx install`, `pipx run`, `uv tool run`, `pip` into a venv as a
script and as `python -m`), and the real `aisquare serve --stdio` from this branch. uv 0.11.6.

| What | Measured |
|---|---|
| 1. The server's parent | The Claude Code process itself, for `uv tool install`, `pipx install`, `pipx run` (it execs), a venv script, `python -m`, and `sh -c "<one command>"` (macOS `sh` execs). `uv tool run` (`uvx`) leaves **`uv`** between: claude → `uv` → server. `bash -lc "cd x && cmd"` leaves `bash`. The Bash tool's shell (`zsh -c`) is a child of the same process, so anything it starts is a grandchild. A server's stdin is a socket (macOS). A server registered with `claude mcp add` (user scope), one from a project `.mcp.json` and one from `--mcp-config` are all started this way. |
| 2. Environment | A server gets `CLAUDE_CODE_SESSION_ID`, `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_MESSAGING_SOCKET` (`/tmp/cc-socks/<claude pid>.sock`), `CLAUDE_PROJECT_DIR`. The SessionStart hook also gets **`CLAUDE_PID`**, the pid of the Claude Code process, and its own parent *is* that process (the hook's `sh -c` ran the command in place). The Bash tool has the same environment as the hook, `CLAUDE_PID` and `CLAUDE_CODE_SESSION_ID` included, which is why neither proves anything by itself. |
| 3. `/clear`, `--resume`, `--fork-session` | `/clear`: the **same process and servers**, a new session id (hook source `clear`, preceded by SessionEnd), and the server's environment keeps the **old** id. `--resume`: the same id in a new process, new servers (source `resume`). `--resume --fork-session`: a new id, a new process (source `fork`). |
| 4. A per-session value in `env` | `"X": "${CLAUDE_CODE_SESSION_ID}"` in an MCP server's `env` arrives as that literal text. No nonce can be passed to a server through its configuration. |
| 5. Hook against server at a fresh start | In 6 of 6 runs the server had read the board **1.2 to 3.2 s before** the hook registered the process (the server asked 0.9 to 2.0 s after its own start; the hook's record landed at +1.2, +1.5, +1.9, +2.1, +2.9, +3.2 s after that). So at a fresh start the server cannot yet prove its host, and counts every live row: with a stale own row of the previous process (a `--resume` after a crash) the instructions are the team's, as before. A server that starts later (an MCP reconnect, a second server in the same Claude Code) proves it and passes the host. Waiting for the hook was not built: it would hold every MCP start up to 3 s for a one-line saving in a rare case. |

Checked for real with the branch's `aisquare`: a session's own server (`aisquare serve --stdio`
and through `uv tool run`) wrote its `note_add` as the session, with one row on the board and no
`mcp:` row; after `/clear` the same server's next note was the **new** session's; and a `serve`
started from the Bash tool, with the session's `CLAUDE_CODE_SESSION_ID` and `CLAUDE_PID` in its
environment, wrote `mcp:remote:<project>` beside the session: a team.

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

## Where the code lives (at `0545dc5b`; what moved, and the new files, are in the status block)

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
- Option A was the shipped behaviour until B landed: every live `mcp:` row counts (the
  owner, 2026-10-24, board seq 1819). It still is for every server that cannot prove it is
  a session's own.
- A hand-run plain `agent` (`aisquare launch agent`) alone on its board is solo; a
  fleet-spawned one, or one with a persona, is a member (the owner, 2026-10-24, #46).

## Open questions for the owner (decided for this build: see the status block)

1. Is (c), shipping the MCP server in the plugin, wanted for its own sake? It is the
   simplest proof, and it changes how users mount the server.
2. Should B also change attribution (#49 m2), so a session's own MCP writes show as the
   session, or keep `mcp:<client>` rows and only stop counting them?
