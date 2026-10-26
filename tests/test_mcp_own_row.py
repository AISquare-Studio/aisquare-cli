"""A session's own MCP server acts as that session; every other server is a teammate.

Option B of docs/plans/mcp-own-row.md. The hard rule comes first: no member of a team may read
as solo, so a server that cannot PROVE it is a session's own writes the ``mcp:`` row and the
board counts it. The proof is two records meeting: the SessionStart hook registers the Claude
Code process that runs the session (``CLAUDE_PID``, one of its ancestors), and the stdio
server's parent, launchers skipped, is that same process, pid and start token
(``services.mcp_host``). Each test builds the process tree it means (``tests/proc_tree.py``)
and says whose child the code under test is.

The matrix of the plan, one test or parameter per row, runs with the captain on and off
(``AISQUARE_SUITE_CAPTAIN``).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import procs
from aisquare.core.store import store_session
from aisquare.core.workspace import current_project
from aisquare.models import TeamSession
from aisquare.services import mcp_host, mcp_server
from aisquare.services import team as team_service
from tests import proc_tree
from tests.proc_tree import Tree

pytestmark = pytest.mark.usefixtures("project_capture")

SOLO = "cccc3333-0000-0000-0000-000000000000"
OTHER = "dddd4444-0000-0000-0000-000000000000"
THIRD = "ffff6666-0000-0000-0000-000000000000"
CODER_A = "aaaa1111-0000-0000-0000-000000000000"
CODER_B = "bbbb2222-0000-0000-0000-000000000000"
CLEARED = "99991111-0000-0000-0000-000000000000"

#: What the platforms show as the image of the processes in the chains below.
CLAUDE = "/Users/dev/.local/share/claude/versions/2.1.310"
SHELL = "/bin/zsh"
UV = "/Users/dev/.local/bin/uv"


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    (work / ".git").mkdir()
    monkeypatch.chdir(work)
    for name in (
        "AISQUARE_ROLE",
        "AISQUARE_FLEET_AGENT",
        "AISQUARE_PERSONA",
        "AISQUARE_SERVE_CLIENT",
        "AISQUARE_SERVE_ROLE",
    ):
        monkeypatch.delenv(name, raising=False)
    return work


@pytest.fixture
def tree(monkeypatch: pytest.MonkeyPatch) -> Tree:
    return proc_tree.install(monkeypatch)


def _hook(
    runner: CliRunner, event: str, payload: dict[str, str], *, env: dict[str, str] | None = None
) -> str:
    result = runner.invoke(app, ["hook", event], input=json.dumps(payload), env=env)
    assert result.exit_code == 0, result.output
    return result.stdout


def _start(
    runner: CliRunner,
    session_id: str,
    work: Path,
    *,
    source: str = "startup",
    tree: Tree | None = None,
    parent: int | None = None,
    claude: int | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> str:
    """A session start; with ``tree``, ``parent`` and ``claude``, as the hook of the Claude Code
    process ``claude`` runs it: ``CLAUDE_PID`` in its environment, ``parent`` its parent."""
    payload = {"cwd": str(work), "session_id": session_id, "source": source}
    if tree is None or claude is None or monkeypatch is None:
        return _hook(runner, "session-start", payload)
    with tree.child_of(parent if parent is not None else claude, monkeypatch):
        return _hook(runner, "session-start", payload, env={"CLAUDE_PID": str(claude)})


def _end(runner: CliRunner, session_id: str, work: Path, reason: str = "exit") -> None:
    _hook(runner, "session-end", {"cwd": str(work), "session_id": session_id, "reason": reason})


def _prompt(runner: CliRunner, session_id: str, work: Path) -> str:
    payload = {"cwd": str(work), "session_id": session_id, "prompt": "go"}
    return _hook(runner, "user-prompt-submit", payload)


def _team_on(runner: CliRunner) -> None:
    assert runner.invoke(app, ["team", "on"]).exit_code == 0


def _claude_session(
    runner: CliRunner,
    tree: Tree,
    monkeypatch: pytest.MonkeyPatch,
    session_id: str,
    work: Path,
    *,
    source: str = "startup",
    via_shell: bool = False,
) -> int:
    """A Claude Code process, and its SessionStart hook for ``session_id`` (through the
    ``sh`` the plugin's launcher runs under, with ``via_shell``). Returns the process."""
    claude = tree.spawn(CLAUDE)
    parent = tree.spawn("/bin/sh", claude) if via_shell else claude
    _start(
        runner,
        session_id,
        work,
        source=source,
        tree=tree,
        parent=parent,
        claude=claude,
        monkeypatch=monkeypatch,
    )
    return claude


def _rows() -> list[str]:
    with store_session() as store:
        return sorted(s.id for s in store.team_sessions(current_project().id))


def _notes() -> dict[str, str]:
    """Each note's text by the session that wrote it."""
    with store_session() as store:
        events = store.recent_events(current_project().id, limit=100)
    return {e.text: e.session_id or "" for e in events if e.kind == "note"}


def _own_server_of(tree: Tree, monkeypatch: pytest.MonkeyPatch, parent: int, text: str) -> None:
    """The MCP server whose parent is ``parent`` takes a note."""
    with tree.child_of(parent, monkeypatch):
        mcp_server.note_add(text)


def _instructions(tree: Tree, monkeypatch: pytest.MonkeyPatch, parent: int) -> str:
    with tree.child_of(parent, monkeypatch):
        return mcp_server.server_instructions()


# --- the row that flips: a solo session's own board tool ------------------------------------


@pytest.mark.parametrize(
    "shape",
    ["a console script", "uvx", "a windows trampoline", "uvx and a trampoline", "a venv launcher"],
)
def test_a_solo_sessions_own_board_tool_stays_solo(
    runner: CliRunner,
    work_dir: Path,
    tree: Tree,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    """The row of the plan's matrix that flips the old known limitation (#45 m1). A solo
    session that calls its own server's ``team_board`` used to write ``mcp:remote:<project>``,
    which every other reader counted as a teammate: its next prompt got the board, a new
    server got the team's instructions, for half an hour. Its own server acts as the session,
    so there is no row, and so no team, whichever way the server was launched."""
    _team_on(runner)
    claude = _claude_session(runner, tree, monkeypatch, SOLO, work_dir)
    parent = claude
    if shape == "uvx":
        parent = tree.spawn(UV, claude)
    elif shape == "a windows trampoline":
        monkeypatch.setattr(sys, "argv", [r"C:\tools\Scripts\aisquare.exe", "serve", "--stdio"])
        parent = tree.spawn(r"C:\tools\Scripts\aisquare.exe", claude)
    elif shape == "uvx and a trampoline":
        monkeypatch.setattr(sys, "argv", [r"C:\cache\Scripts\aisquare.exe", "serve", "--stdio"])
        parent = tree.spawn(r"C:\cache\Scripts\aisquare.exe", tree.spawn(r"C:\bin\uvx.exe", claude))
    elif shape == "a venv launcher":
        # Windows: Scripts\python.exe starts the base interpreter as its child, and the child
        # reports the launcher's path as its sys.executable.
        monkeypatch.setattr(sys, "executable", r"C:\work\.venv\Scripts\python.exe")
        redirector = tree.spawn(r"C:\work\.venv\Scripts\python.exe", claude)
        tree.table[os.getpid()] = procs.ProcInfo(redirector, "t-me", r"C:\Python312\python.exe")
        parent = redirector

    with tree.child_of(parent, monkeypatch):
        mcp_server.team_board()
        mcp_server.note_add("checking the login page")
    assert _rows() == [SOLO], "no mcp: row exists"
    assert _notes() == {"checking the login page": SOLO}, "written as the session itself"
    assert _prompt(runner, SOLO, work_dir) == "", "no board on its next prompt"
    assert _instructions(tree, monkeypatch, parent) == mcp_server.SOLO_INSTRUCTIONS

    # A second session still sees it as the teammate it is, and the first is briefed then.
    assert "<aisquare-team>" in _start(runner, OTHER, work_dir)
    assert "<aisquare-team>" in _prompt(runner, SOLO, work_dir)


def test_its_calls_keep_the_session_alive_and_leave_its_state_alone(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call is proof of life, like a heartbeat; it does not say the session is working."""
    _team_on(runner)
    claude = _claude_session(runner, tree, monkeypatch, SOLO, work_dir)
    with store_session() as store:
        store.touch_session(SOLO, state="waiting")
        before = store.get_session(SOLO)
    assert before is not None
    later = before.last_seen_at + timedelta(minutes=20)
    monkeypatch.setattr("aisquare.core.store._now_iso", lambda: later.isoformat(), raising=True)
    _own_server_of(tree, monkeypatch, claude, "still here")
    with store_session() as store:
        after = store.get_session(SOLO)
    assert after is not None
    assert after.last_seen_at > before.last_seen_at
    assert after.state == "waiting"


# --- the rows that stay a team ---------------------------------------------------------------


def test_a_nameless_desktop_client_over_stdio_is_a_team(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Desktop starts the server itself: its parent is Desktop, which no hook ever
    registered, wherever it runs."""
    _team_on(runner)
    _claude_session(runner, tree, monkeypatch, SOLO, work_dir)
    desktop = tree.spawn("C:\\Program Files\\Claude\\Claude.exe")
    _own_server_of(tree, monkeypatch, desktop, "checking the login page")

    assert any(row.startswith("mcp:remote:") for row in _rows())
    assert "<aisquare-team>" in _prompt(runner, SOLO, work_dir)
    _end(runner, SOLO, work_dir)
    assert _instructions(tree, monkeypatch, desktop) == mcp_server.TEAM_INSTRUCTIONS
    assert "<aisquare-team>" in _start(runner, OTHER, work_dir)


@pytest.mark.parametrize(
    "client",
    [
        {"AISQUARE_SERVE_CLIENT": "desktop"},
        {"AISQUARE_SERVE_ROLE": "browser-debugger"},
        {"AISQUARE_SERVE_CLIENT": "desktop", "AISQUARE_SERVE_ROLE": "browser-debugger"},
    ],
    ids=["a named client", "a role set", "both"],
)
def test_a_client_that_names_itself_is_a_team_whatever_started_it(
    runner: CliRunner,
    work_dir: Path,
    tree: Tree,
    monkeypatch: pytest.MonkeyPatch,
    client: dict[str, str],
) -> None:
    """Said in the server's environment, a name or a role is believed over the process tree:
    even a server that is a registered session's own child is the teammate it was told it is."""
    _team_on(runner)
    claude = _claude_session(runner, tree, monkeypatch, SOLO, work_dir)
    for name, value in client.items():
        monkeypatch.setenv(name, value)
    _own_server_of(tree, monkeypatch, claude, "checking the login page")
    for name in client:
        monkeypatch.delenv(name)

    assert any(row.startswith("mcp:") for row in _rows())
    assert "<aisquare-team>" in _prompt(runner, SOLO, work_dir)
    assert _instructions(tree, monkeypatch, claude) == mcp_server.TEAM_INSTRUCTIONS


def test_a_client_over_http_is_a_team(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An HTTP server's clients are not its parent, whoever its parent is: here the very
    process that registered the session."""
    tree = proc_tree.install(monkeypatch, stdio=False)
    _team_on(runner)
    claude = _claude_session(runner, tree, monkeypatch, SOLO, work_dir)
    _own_server_of(tree, monkeypatch, claude, "from a phone")

    assert any(row.startswith("mcp:remote:") for row in _rows())
    assert "<aisquare-team>" in _prompt(runner, SOLO, work_dir)
    assert mcp_server.build_server(team=True).instructions == mcp_server.TEAM_INSTRUCTIONS


@pytest.mark.parametrize(
    "launch",
    ["a shell", "uv from a shell", "a login shell string", "a tmux pane"],
)
def test_a_serve_started_from_a_claude_code_shell_is_a_team(
    runner: CliRunner,
    work_dir: Path,
    tree: Tree,
    monkeypatch: pytest.MonkeyPatch,
    launch: str,
) -> None:
    """#49 MAJOR-1. Claude Code exports ``CLAUDE_CODE_SESSION_ID`` and ``CLAUDE_PID`` to every
    shell it runs, and the shell is a child of the very process that registered the session.
    A ``serve`` started there serves some other client (here a Desktop teammate), and its
    parent is the shell, not that process: no proof, a teammate. An ancestry check alone
    would have hidden it."""
    _team_on(runner)
    claude = _claude_session(runner, tree, monkeypatch, SOLO, work_dir)
    shell = tree.spawn(SHELL, claude)
    parent = {
        "a shell": shell,
        "uv from a shell": tree.spawn(UV, shell),
        "a login shell string": tree.spawn("/bin/bash", shell),
        "a tmux pane": tree.spawn(SHELL, tree.spawn("/opt/homebrew/bin/tmux", shell)),
    }[launch]
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SOLO)  # inherited by the serve process
    monkeypatch.setenv("CLAUDE_PID", str(claude))
    _own_server_of(tree, monkeypatch, parent, "checking the login page")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    monkeypatch.delenv("CLAUDE_PID")

    assert any(row.startswith("mcp:remote:") for row in _rows())
    assert "<aisquare-team>" in _prompt(runner, SOLO, work_dir)
    _end(runner, SOLO, work_dir)
    assert _instructions(tree, monkeypatch, parent) == mcp_server.TEAM_INSTRUCTIONS
    assert "<aisquare-team>" in _start(runner, OTHER, work_dir)


def test_a_session_that_mounts_the_server_without_the_hooks_is_a_team(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its row is its only presence: no hook ever registered its process, so nothing says the
    server is its own, and the board counts it."""
    _team_on(runner)
    _claude_session(runner, tree, monkeypatch, SOLO, work_dir)
    unhooked = tree.spawn(CLAUDE)  # another Claude Code, with no aisquare hooks
    _own_server_of(tree, monkeypatch, unhooked, "from a session with no hooks")

    assert any(row.startswith("mcp:remote:") for row in _rows())
    assert "<aisquare-team>" in _prompt(runner, SOLO, work_dir)


def test_a_session_on_another_board_never_hides_its_server_row_on_this_one(
    runner: CliRunner, work_dir: Path, tree: Tree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#49 m1. R's hooks put its row and its process on board B, and R mounts A's
    ``serve --stdio`` and posts on A. The record is per board and the lookup is this board's,
    by exact id: A has no record of R's process, so R's server is a teammate on A."""
    other = tmp_path / "other"
    other.mkdir()
    (other / ".git").mkdir()
    monkeypatch.chdir(other)
    _team_on(runner)
    r = _claude_session(runner, tree, monkeypatch, THIRD, other)
    board_b = current_project().id
    monkeypatch.chdir(work_dir)
    _team_on(runner)
    board_a = current_project().id
    assert board_a != board_b
    with store_session() as store:
        assert list(store.list_meta(f"host/{board_b}/")) == [f"host/{board_b}/{r}"]
        assert store.list_meta(f"host/{board_a}/") == {}

    _own_server_of(tree, monkeypatch, r, "from board B's session")
    assert any(row.startswith("mcp:remote:") for row in _rows())
    assert _instructions(tree, monkeypatch, r) == mcp_server.TEAM_INSTRUCTIONS
    assert "<aisquare-team>" in _start(runner, SOLO, work_dir)


def test_a_session_whose_row_is_on_this_board_under_the_same_prefix_is_not_it(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#49 m1 again, the other half: the host is found by EXACT id among this board's sessions.
    A record naming a prefix of a real session's id (``store.get_session`` matches one) proves
    nothing."""
    _team_on(runner)
    claude = _claude_session(runner, tree, monkeypatch, SOLO, work_dir)
    with store_session() as store:
        project_id = current_project().id
        key = f"host/{project_id}/{claude}"
        record = json.loads(store.get_meta(key) or "{}")
        record["session"] = SOLO[:8]
        store.set_meta(key, json.dumps(record))

    _own_server_of(tree, monkeypatch, claude, "by a prefix")
    assert any(row.startswith("mcp:remote:") for row in _rows())


@pytest.mark.parametrize(
    "chain",
    ["a wsl shell", "a process that cannot be read", "a chain that ends at init"],
)
def test_wsl_and_unreadable_trees_are_a_team(
    runner: CliRunner,
    work_dir: Path,
    tree: Tree,
    monkeypatch: pytest.MonkeyPatch,
    chain: str,
) -> None:
    """``wsl -e bash -lc "cd … && aisquare serve --stdio"`` (docs/orchestration.md's Desktop
    example): the parent is a ``bash`` inside WSL and Claude Desktop is outside it, so no
    registered process is anywhere above. Where the tree cannot be read at all, it is the
    same: no proof, a teammate."""
    _team_on(runner)
    _claude_session(runner, tree, monkeypatch, SOLO, work_dir)
    parent = {
        "a wsl shell": tree.spawn("/usr/bin/bash", tree.spawn("/init", 1)),
        "a process that cannot be read": 987_654,
        "a chain that ends at init": tree.spawn("/usr/bin/bash", 1),
    }[chain]
    _own_server_of(tree, monkeypatch, parent, "from the windows side")

    assert any(row.startswith("mcp:remote:") for row in _rows())
    assert "<aisquare-team>" in _prompt(runner, SOLO, work_dir)
    assert _instructions(tree, monkeypatch, parent) == mcp_server.TEAM_INSTRUCTIONS


def test_a_recycled_pid_is_not_the_process_that_registered(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start token is what a pid alone cannot give: the session's process ended, its pid
    went to a new process, and a server started under THAT one is nobody's own."""
    _team_on(runner)
    claude = _claude_session(runner, tree, monkeypatch, SOLO, work_dir)
    tree.reuse(claude, "/opt/other-app/Desktop")
    _own_server_of(tree, monkeypatch, claude, "from an app that got the pid")

    assert any(row.startswith("mcp:remote:") for row in _rows())
    assert "<aisquare-team>" in _prompt(runner, SOLO, work_dir)


def test_a_server_asks_for_its_own_process_only_when_an_image_needs_it(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server's own start is one more process lookup (a ``ps`` on macOS) before the
    handshake: a plain parent, and a ``uv`` above one, are told without it."""
    claude = tree.spawn(CLAUDE)
    uv = tree.spawn(UV, claude)
    asked: list[int] = []

    def look(pid: int) -> procs.ProcInfo | None:
        asked.append(pid)
        return tree.look(pid)

    monkeypatch.setattr(procs, "inspect", look)
    for parent in (claude, uv):
        with tree.child_of(parent, monkeypatch):
            assert mcp_host.serving_process("aisquare") == procs.ProcessRef(
                claude, tree.table[claude].started
            )
    assert os.getpid() not in asked


def test_a_session_that_has_ended_is_not_a_host(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its row is the proof's last link: once the session ended, a server still running under
    its process writes as a teammate rather than revive a row that is retired."""
    _team_on(runner)
    claude = _claude_session(runner, tree, monkeypatch, SOLO, work_dir)
    _start(runner, OTHER, work_dir)
    _end(runner, SOLO, work_dir)
    _own_server_of(tree, monkeypatch, claude, "after the end")

    assert any(row.startswith("mcp:remote:") for row in _rows())
    with store_session() as store:
        row = store.get_session(SOLO)
    assert row is not None and row.ended_at is not None, "and the ended row stayed ended"


def test_a_store_that_fails_is_a_team(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every check that cannot be completed means team."""
    _team_on(runner)
    claude = _claude_session(runner, tree, monkeypatch, SOLO, work_dir)

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("store on fire")

    monkeypatch.setattr(mcp_host, "host_of", broken)
    _own_server_of(tree, monkeypatch, claude, "with the proof down")
    assert any(row.startswith("mcp:remote:") for row in _rows())
    assert _instructions(tree, monkeypatch, claude) == mcp_server.TEAM_INSTRUCTIONS


# --- resumed, forked and cleared sessions ----------------------------------------------------


def test_a_clear_moves_the_servers_rows_to_the_new_session(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/clear`` keeps the process and its MCP servers, starts a new session id (measured on
    Claude Code 2.1.310), and leaves the id in the server's environment the one it started
    with. The record is the process's, overwritten by each start: the server writes as the
    session the process serves NOW."""
    _team_on(runner)
    claude = _claude_session(runner, tree, monkeypatch, SOLO, work_dir)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SOLO)  # what the server still holds
    _own_server_of(tree, monkeypatch, claude, "before the clear")

    _end(runner, SOLO, work_dir, "clear")
    _start(
        runner, CLEARED, work_dir, source="clear", tree=tree, claude=claude, monkeypatch=monkeypatch
    )
    _own_server_of(tree, monkeypatch, claude, "after the clear")

    assert _notes() == {"before the clear": SOLO, "after the clear": CLEARED}
    assert not any(row.startswith("mcp:") for row in _rows())


def test_a_resume_starts_the_same_session_in_a_new_process(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``claude --resume`` keeps the session id and starts a new process with new servers
    (source ``resume``): the record of the old process names the session still, and the new
    one's names it too, so the new server writes as it."""
    _team_on(runner)
    first = _claude_session(runner, tree, monkeypatch, SOLO, work_dir)
    _end(runner, SOLO, work_dir)
    tree.gone(first)
    second = _claude_session(runner, tree, monkeypatch, SOLO, work_dir, source="resume")
    _own_server_of(tree, monkeypatch, second, "after the resume")

    assert _notes() == {"after the resume": SOLO}
    assert _rows() == [SOLO]


def test_a_fork_is_a_new_session_in_a_new_process(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--resume --fork-session`` (source ``fork``): a new id and a new process while the
    original may still run. Each server writes as the session of the process it was started
    by, and neither takes the other's."""
    _team_on(runner)
    original = _claude_session(runner, tree, monkeypatch, SOLO, work_dir)
    fork = _claude_session(runner, tree, monkeypatch, THIRD, work_dir, source="fork")
    _own_server_of(tree, monkeypatch, original, "by the original")
    _own_server_of(tree, monkeypatch, fork, "by the fork")

    assert _notes() == {"by the original": SOLO, "by the fork": THIRD}
    assert not any(row.startswith("mcp:") for row in _rows())


# --- attribution (#49 m2) ---------------------------------------------------------------------


def test_two_coders_that_each_call_a_board_tool_are_two_sessions_not_four(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#49 m2. With a name per client the board showed ``mcp:cc-a (remote) note: …`` beside
    the session that wrote it. Now each coder's MCP writes are that coder's: one row per agent,
    one author on every note, and its claim is the claim its own hooks and shell would hold."""
    _team_on(runner)
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    a = _claude_session(runner, tree, monkeypatch, CODER_A, work_dir)
    b = _claude_session(runner, tree, monkeypatch, CODER_B, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    with tree.child_of(a, monkeypatch):
        mcp_server.task_add("wire the login page", role="coder")
        claimed = json.loads(mcp_server.task_next(role="coder", claim=True))
        mcp_server.note_add("a is on the login page")
    with tree.child_of(b, monkeypatch):
        mcp_server.note_add("b checked the docs")
        board = mcp_server.team_board()

    assert _rows() == sorted([CODER_A, CODER_B])
    assert _notes() == {"a is on the login page": CODER_A, "b checked the docs": CODER_B}
    assert claimed["claimed_by"] == CODER_A
    assert "mcp:" not in board and "(remote)" not in board
    assert "aaaa1111 coder" in board and "bbbb2222 coder" in board


def test_a_claim_taken_through_mcp_is_the_claim_the_session_finishes_from_its_shell(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of acting as the session: ``task next --claim`` through MCP, then the board
    its next start shows (the task ``[doing @`` the session), then ``aisquare task done`` from
    its shell are one session's, with no hand-over between an ``mcp:`` row and the agent."""
    _team_on(runner)
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    claude = _claude_session(runner, tree, monkeypatch, CODER_A, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    with tree.child_of(claude, monkeypatch):
        mcp_server.task_add("wire the login page", role="coder")
        claimed = json.loads(mcp_server.task_next(role="coder", claim=True))
    task_id = claimed["id"]

    resumed = _start(
        runner,
        CODER_A,
        work_dir,
        source="resume",
        tree=tree,
        claude=claude,
        monkeypatch=monkeypatch,
    )
    assert f"{task_id} [doing @aaaa1111]" in resumed, "the board lists it under the session"

    team_service.finish_task(task_id, session_ref=CODER_A)
    with store_session() as store:
        finished = [e for e in store.recent_events(current_project().id, limit=50)]
    assert [e.session_id for e in finished if e.kind == "task_done"] == [CODER_A]
    assert _rows() == [CODER_A], "no row of an mcp: session was ever there"


def test_a_hosted_session_reads_its_own_writes_back_as_me(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    _team_on(runner)
    claude = _claude_session(runner, tree, monkeypatch, SOLO, work_dir)
    with tree.child_of(claude, monkeypatch):
        mcp_server.note_add("one of mine")
        mine = mcp_server.team_log(by_session="me")
    assert "one of mine" in mine


# --- the server's instructions, at its start (#346) ---------------------------------------------


def test_a_server_that_knows_its_host_does_not_count_the_host_s_row_as_a_teammate(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#346's start-order limitation, closed where it can be: a server started once its
    session's hook has registered (an MCP reconnect, a second server of the same Claude Code)
    knows its host for sure and passes the id, so the session's own row, written by the hook, is
    not a teammate. With another live session on the board it is a team again."""
    _team_on(runner)
    claude = _claude_session(runner, tree, monkeypatch, SOLO, work_dir)
    assert _instructions(tree, monkeypatch, claude) == mcp_server.SOLO_INSTRUCTIONS

    _start(runner, OTHER, work_dir)
    assert _instructions(tree, monkeypatch, claude) == mcp_server.TEAM_INSTRUCTIONS


def test_a_server_that_starts_before_the_hook_counts_every_live_row(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured order: the hook registers 1.2 to 3.2 s AFTER the server has read the board
    (docs/plans/mcp-own-row.md). A server whose host has a row from before (a resume after a
    crash) and no record yet cannot tell the row from a teammate's: team, the safe side."""
    _team_on(runner)
    _start(runner, SOLO, work_dir)  # the row the previous process left, not ended
    claude = tree.spawn(CLAUDE)  # the new process: its hook has not run yet
    assert _instructions(tree, monkeypatch, claude) == mcp_server.TEAM_INSTRUCTIONS

    _start(
        runner, SOLO, work_dir, source="resume", tree=tree, claude=claude, monkeypatch=monkeypatch
    )
    assert _instructions(tree, monkeypatch, claude) == mcp_server.SOLO_INSTRUCTIONS


# --- before the upgrade -----------------------------------------------------------------------


def test_rows_and_servers_from_before_the_upgrade_count_until_they_go_stale(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows written by older code, and servers still running older code, carry no proof: the
    ``mcp:remote:<project>`` row an older server wrote for a session's own calls is live and
    counted, and the session reads as a team, until the row goes stale."""
    _team_on(runner)
    claude = _claude_session(runner, tree, monkeypatch, SOLO, work_dir)
    now = team_service._now()
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=f"mcp:remote:{current_project().id.removeprefix('prj_')[:6]}",
                project_id=current_project().id,
                role="remote",
                started_at=now,
                last_seen_at=now,
            )
        )
    assert "<aisquare-team>" in _prompt(runner, SOLO, work_dir)
    assert _instructions(tree, monkeypatch, claude) == mcp_server.TEAM_INSTRUCTIONS

    later = team_service._now() + team_service._STALE_AFTER + timedelta(minutes=1)
    monkeypatch.setattr(team_service, "_now", lambda: later)
    assert _instructions(tree, monkeypatch, claude) == mcp_server.SOLO_INSTRUCTIONS, "stale: solo"


def test_a_session_started_before_the_upgrade_has_no_record_and_stays_a_teammate(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its hooks ran under the older code, which recorded no process: its new server finds
    nothing to prove itself against, and writes the ``mcp:`` row."""
    _team_on(runner)
    _start(runner, SOLO, work_dir)  # an older hook: the row, no record
    claude = tree.spawn(CLAUDE)
    _own_server_of(tree, monkeypatch, claude, "from before the upgrade")

    assert any(row.startswith("mcp:remote:") for row in _rows())
    assert "<aisquare-team>" in _prompt(runner, SOLO, work_dir)


# --- the hook's side ----------------------------------------------------------------------------


def test_the_hook_registers_the_process_it_runs_under(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``CLAUDE_PID`` is the Claude Code process, here two levels up: the hook runs under the
    ``sh`` the plugin's launcher script runs under."""
    _team_on(runner)
    claude = _claude_session(runner, tree, monkeypatch, SOLO, work_dir, via_shell=True)
    with store_session() as store:
        records = store.list_meta(f"host/{current_project().id}/")
    assert list(records) == [f"host/{current_project().id}/{claude}"]
    record = json.loads(next(iter(records.values())))
    assert record["session"] == SOLO
    assert record["started"] == tree.table[claude].started


@pytest.mark.parametrize(
    "pid",
    [None, "not-a-pid", "1", "0", "-5", "{unrelated}"],
    ids=["unset", "text", "init", "zero", "negative", "a process that is not above the hook"],
)
def test_a_pid_that_is_not_an_ancestor_registers_nothing(
    runner: CliRunner,
    work_dir: Path,
    tree: Tree,
    monkeypatch: pytest.MonkeyPatch,
    pid: str | None,
) -> None:
    """``CLAUDE_PID`` is a claim, and a hook checks it against its own ancestors: one copied
    into another shell, or from a Claude Code that does not set it, names nothing above it."""
    _team_on(runner)
    claude = tree.spawn(CLAUDE)
    unrelated = tree.spawn(CLAUDE)
    env = {} if pid is None else {"CLAUDE_PID": str(unrelated) if pid == "{unrelated}" else pid}
    with tree.child_of(claude, monkeypatch):
        _hook(
            runner,
            "session-start",
            {"cwd": str(work_dir), "session_id": SOLO, "source": "startup"},
            env=env,
        )
    with store_session() as store:
        assert store.list_meta("host/") == {}
        assert store.get_session(SOLO) is not None, "and the session started all the same"


def test_a_failing_registration_never_fails_the_session_start(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    _team_on(runner)

    def broken(pid: int) -> procs.ProcInfo:
        raise RuntimeError("ps is not here")

    monkeypatch.setattr(procs, "inspect", broken)
    claude = tree.spawn(CLAUDE)
    _start(runner, SOLO, work_dir, tree=tree, claude=claude, monkeypatch=monkeypatch)
    with store_session() as store:
        assert store.get_session(SOLO) is not None
        assert store.list_meta("host/") == {}


def test_a_session_that_joins_at_its_first_prompt_registers_too(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orchestrator was switched on after the session started: its first prompt creates
    the row, and records the process as the start would have."""
    claude = tree.spawn(CLAUDE)
    with tree.child_of(claude, monkeypatch):
        _hook(runner, "session-start", {"cwd": str(work_dir), "session_id": SOLO})
    _team_on(runner)
    with tree.child_of(claude, monkeypatch):
        _hook(
            runner,
            "user-prompt-submit",
            {"cwd": str(work_dir), "session_id": SOLO, "prompt": "go"},
            env={"CLAUDE_PID": str(claude)},
        )
    _own_server_of(tree, monkeypatch, claude, "joined late")
    assert _notes() == {"joined late": SOLO}
    assert _rows() == [SOLO]


def test_records_are_kept_per_board_and_purged_with_the_project(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    _team_on(runner)
    project_id = current_project().id
    first = _claude_session(runner, tree, monkeypatch, SOLO, work_dir)
    with store_session() as store:
        store.set_meta(f"host/{project_id}/12345", "not a record")

    second = _claude_session(runner, tree, monkeypatch, OTHER, work_dir)
    with store_session() as store:
        assert sorted(store.list_meta(f"host/{project_id}/")) == sorted(
            [f"host/{project_id}/{first}", f"host/{project_id}/{second}"]
        ), "a record that is not one goes, the two live processes' records stay"
        store.set_meta("host/prj_elsewhere/77", "{}")
        store.purge_project(project_id)
        assert list(store.list_meta("host/")) == ["host/prj_elsewhere/77"]


def _age(project_id: str, pid: int, seconds: float) -> None:
    """Make the record of ``pid`` ``seconds`` older than it is."""
    with store_session() as store:
        key = f"host/{project_id}/{pid}"
        record = json.loads(store.get_meta(key) or "{}")
        record["at"] -= seconds
        store.set_meta(key, json.dumps(record))


def _records(project_id: str) -> set[int]:
    with store_session() as store:
        return {int(key.rsplit("/", 1)[-1]) for key in store.list_meta(f"host/{project_id}/")}


OLD = mcp_host.CHECK_AFTER_SECONDS + 60
FIFTEEN_DAYS = 15 * 24 * 3600


def test_the_record_of_a_session_that_is_still_running_outlives_any_age(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manager that runs for weeks keeps its own row: another session starting on the board
    does not take the record of a process that is still there (review of #397, minor-2)."""
    _team_on(runner)
    project_id = current_project().id
    manager = _claude_session(runner, tree, monkeypatch, SOLO, work_dir)
    _age(project_id, manager, FIFTEEN_DAYS)

    _claude_session(runner, tree, monkeypatch, OTHER, work_dir)
    assert manager in _records(project_id)
    _own_server_of(tree, monkeypatch, manager, "weeks in")
    assert _notes() == {"weeks in": SOLO}
    assert _rows() == [SOLO, OTHER]


@pytest.mark.parametrize("how", ["gone", "reused"])
def test_the_record_of_a_process_that_is_gone_goes_once_it_is_old(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    _team_on(runner)
    project_id = current_project().id
    old = _claude_session(runner, tree, monkeypatch, SOLO, work_dir)
    _age(project_id, old, OLD)
    if how == "gone":
        tree.gone(old)
    else:
        tree.reuse(old, CLAUDE)  # the pid is another process's now

    fresh = _claude_session(runner, tree, monkeypatch, OTHER, work_dir)
    assert _records(project_id) == {fresh}


def test_a_young_record_is_kept_without_a_look_at_its_process(
    runner: CliRunner, work_dir: Path, tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every start would otherwise run a process lookup per record of the board."""
    _team_on(runner)
    project_id = current_project().id
    young = _claude_session(runner, tree, monkeypatch, SOLO, work_dir)
    tree.gone(young)
    asked: list[int] = []

    def look(pid: int) -> procs.ProcInfo | None:
        asked.append(pid)
        return tree.look(pid)

    monkeypatch.setattr(procs, "inspect", look)
    fresh = _claude_session(runner, tree, monkeypatch, OTHER, work_dir)
    assert young not in asked
    assert _records(project_id) == {young, fresh}


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        "[]",
        json.dumps({"session": SOLO}),
        json.dumps({"session": 5, "started": "t", "at": 1}),
        json.dumps({"session": SOLO, "started": "t", "at": "now"}),
        json.dumps({"session": SOLO, "started": "t", "at": True}),
    ],
)
def test_a_record_that_is_not_one_proves_nothing(
    runner: CliRunner,
    work_dir: Path,
    tree: Tree,
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    _team_on(runner)
    claude = _claude_session(runner, tree, monkeypatch, SOLO, work_dir)
    with store_session() as store:
        store.set_meta(f"host/{current_project().id}/{claude}", raw)
    _own_server_of(tree, monkeypatch, claude, "on a bad record")
    assert any(row.startswith("mcp:remote:") for row in _rows())
