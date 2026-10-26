"""No process an MCP tool starts holds the server's protocol pipe.

A stdio server's stdin is its protocol pipe: the client writes requests into it, and the server's
reader sits in a blocking read on it for as long as the server lives. A tool that runs ``git``
with ``stdin=None`` hands that pipe to the child. On every platform a child that holds the pipe
can read the client's bytes. On Windows it is worse: a process started with a pipe handle that
another thread is blocked in a synchronous read on can wait on it as it starts (every C runtime
asks the type of its standard handles), so the tool never gets its answer and the client never
gets its reply. That was the suspected cause of the real-process pair in
``test_mcp_own_row_processes.py`` timing out on Windows CI (every tool call ran ``git`` twice with
an inherited stdin); the full matrix on the fix passed it, which is a result, not a proof.

Two layers, each tested:

- **The server keeps the pipe to itself** (``mcp_server.keep_the_protocol_pipe_to_ourselves``): it
  reads from a private copy and points stdin at the null device, so no child, started by any
  site now or added later, has the pipe to inherit. The first test runs it for real, in a
  process whose stdin is a pipe, and starts a child with ``stdin=None``: the child must read
  end-of-file at once, while the process itself still gets the bytes. It runs on every platform
  CI has: on Windows it is the check that the C runtime's ``dup2`` onto fd 0 reaches the standard
  input handle ``Popen`` hands a child.
- **Each spawn a tool reaches names its stdin** (belts, and what the spies see): the spies are on
  ``Popen``, so they see every spawn a tool makes, on every platform, and fail a spawn that
  leaves ``stdin`` unset or points it at the process's own. They run the tools by condition, not
  only by name: an empty board, a task a work brief gates and a passing report backs, and a
  ready brain with ``gbrain`` on PATH, since each of those reaches spawns the others do not.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.services import briefs, mcp_server, team
from aisquare.services import command_reports as reports
from aisquare.services import fleet as fleet_service
from tests import fakebin
from tests.projects import add_project

pytestmark = pytest.mark.usefixtures("project_capture")


# --- the server keeps its protocol pipe to itself ----------------------------------------------

#: A process that is a stdio server as far as its stdin goes: it swaps the pipe away, then starts
#: a child on ``stdin=None``, as a tool's ``git`` is started, and says what each end reads.
SERVER = textwrap.dedent(
    """
    import faulthandler, json, subprocess, sys

    from aisquare.services import mcp_server

    faulthandler.dump_traceback_later(90, exit=True)  # never a test that waits for ever
    swapped = mcp_server.keep_the_protocol_pipe_to_ourselves()
    try:
        child = subprocess.run(
            [sys.executable, "-c", "import sys; sys.stdout.write(repr(sys.stdin.buffer.read()))"],
            capture_output=True, text=True, timeout=30,
        )
        child_read = child.stdout
    except subprocess.TimeoutExpired:
        child_read = "TIMEOUT: the child held the protocol pipe"
    print(json.dumps({
        "swapped": swapped,
        "child_read": child_read,
        "own_read": sys.stdin.readline(),
    }))
    """
)


def test_a_child_started_on_stdin_reads_end_of_file_while_the_server_still_gets_its_bytes(
    tmp_path: Path,
) -> None:
    """The pipe stays open until the report is in (nothing closes it before the child has read):
    a child that held it would wait for a byte that never comes, and its answer is the timeout."""
    script = tmp_path / "server.py"
    script.write_text(SERVER, encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        process.stdin.write('{"jsonrpc": "2.0", "id": 1, "method": "initialize"}\n')
        process.stdin.flush()
        out = process.stdout.readline()  # printed once the child has read, with the pipe open
        _, err = process.communicate(timeout=120)  # closes stdin: now the script may end
    finally:
        process.kill()
        process.wait()
    assert process.returncode == 0, err
    report = json.loads(out)
    assert report == {
        "swapped": True,
        "child_read": "b''",
        "own_read": '{"jsonrpc": "2.0", "id": 1, "method": "initialize"}\n',
    }, report


def test_a_stdin_that_is_not_the_process_own_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A test's stand-in, or a pipe a caller swapped in, is not the protocol pipe of a process
    that was started as a server: nothing of fd 0 is touched."""
    import io

    before = os.fstat(0)
    stand_in = io.StringIO("")
    monkeypatch.setattr(sys, "stdin", stand_in)
    assert mcp_server.keep_the_protocol_pipe_to_ourselves() is False
    assert sys.stdin is stand_in
    assert os.fstat(0).st_ino == before.st_ino


# --- every spawn a tool reaches names its stdin ------------------------------------------------


def _inherits(stdin: object) -> bool:
    """Whether a ``Popen``'s ``stdin`` is the process's own: left unset, or named as itself."""
    return stdin is None or stdin == 0 or stdin is sys.stdin or stdin is sys.__stdin__


@pytest.fixture
def spawns(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], object]]:
    """Every process started from here on: its argv and its ``stdin`` (``None``: inherited)."""
    seen: list[tuple[list[str], object]] = []
    original = subprocess.Popen.__init__

    def spy(self: Any, args: Any, *rest: Any, **kwargs: Any) -> None:
        stdin = kwargs.get("stdin", rest[2] if len(rest) > 2 else None)
        seen.append((list(args) if isinstance(args, list) else [str(args)], stdin))
        original(self, args, *rest, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", spy)
    return seen


def _none_inherits(spawns: list[tuple[list[str], object]]) -> None:
    assert spawns, "no tool started a process: the spy sees nothing, and proves nothing"
    inherited = [argv[:4] for argv, stdin in spawns if _inherits(stdin)]
    assert inherited == [], f"started on the server's own stdin: {inherited}"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        [
            *("git", "-c", "user.email=t@t.invalid", "-c", "user.name=t"),
            *("-c", "commit.gpgsign=false", "-C", str(root), *args),
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    monkeypatch.chdir(root)
    for name in ("AISQUARE_ROLE", "AISQUARE_FLEET_AGENT", "AISQUARE_PERSONA"):
        monkeypatch.delenv(name, raising=False)
    return root


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real git checkout, added as a board, with a committed file."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "app.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "one")
    monkeypatch.chdir(root)
    for name in ("AISQUARE_ROLE", "AISQUARE_FLEET_AGENT", "AISQUARE_PERSONA"):
        monkeypatch.delenv(name, raising=False)
    add_project(root)
    monkeypatch.setattr(fleet_service, "nudge_manager", lambda *a, **k: False)
    return root


def test_no_tool_on_an_empty_board_starts_a_child_on_the_servers_stdin(
    work: Path, runner: CliRunner, spawns: list[tuple[list[str], object]]
) -> None:
    assert runner.invoke(app, ["team", "on"]).exit_code == 0
    spawns.clear()
    tools: list[Callable[[], object]] = [
        mcp_server.team_board,
        lambda: mcp_server.task_add("a task", role="coder"),
        lambda: mcp_server.task_next(role="coder", claim=True),
        lambda: mcp_server.note_add("a note"),
        mcp_server.team_log,
        lambda: mcp_server.signal("probe", "1"),
        lambda: mcp_server.code_lookup("anything"),
        mcp_server.server_instructions,
    ]
    for tool in tools:
        with contextlib.suppress(Exception):  # a refusal is an answer; what it started is asked
            tool()
    _none_inherits(spawns)


def test_finishing_a_task_a_brief_gates_starts_no_child_on_the_servers_stdin(
    checkout: Path, runner: CliRunner, spawns: list[tuple[list[str], object]]
) -> None:
    """The gate on ``task done`` reads the checkout's files to judge a passing report stale or
    not (``core.source_revision``): a seam filed as a command report's plumbing, and one a tool
    reaches only when a brief links the task."""
    assert runner.invoke(app, ["team", "on"]).exit_code == 0
    brief = briefs.create("Answer", ["answer() is 42"])
    task, _ = team.add_task("Build the answer", role="coder")
    briefs.link(brief.id, task.id, [r.id for r in brief.requirements])
    report = reports.run_command(
        [sys.executable, "-c", "print('ok')"],
        task_id=task.id,
        project_id=team.resolve_project().id,
    )
    briefs.record_evidence(
        brief.id, "R1", task_ref=task.id, verdict="pass", summary="ran it", report_id=report.id
    )
    spawns.clear()

    mcp_server.task_update(task.id, "done")

    assert team.show_task(task.id).status == "done", "the call did not reach the gate it is for"
    _none_inherits(spawns)


_FAKE_GBRAIN = textwrap.dedent(
    """
    import os, sys
    from pathlib import Path

    command = sys.argv[1] if len(sys.argv) > 1 else ""
    home = Path(os.environ.get("GBRAIN_HOME", "."))
    if command == "--version":
        print("gbrain 0.42.1.0")
    elif command == "init":
        (home / ".gbrain" / "brain.pglite").mkdir(parents=True, exist_ok=True)
        (home / ".gbrain" / "brain.pglite" / "PG_VERSION").write_text("16\\n")
        (home / ".gbrain" / "config.json").write_text('{"embedding_disabled": true}')
    elif command == "put":
        sys.stdin.buffer.read()
    elif command in ("search", "query"):
        print("results for: " + sys.argv[2])
    """
)


def test_recalling_from_a_ready_brain_starts_no_child_on_the_servers_stdin(
    work: Path,
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spawns: list[tuple[list[str], object]],
) -> None:
    """``recall`` drains the undistilled backlog first (``gbrain --version``, ``init``, ``put``)
    and then searches: the optional brain's whole command line, with a fake on PATH."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    impl = bin_dir / "fake_gbrain.py"
    impl.write_text(_FAKE_GBRAIN, encoding="utf-8")
    fakebin.executable_fake(
        bin_dir,
        "gbrain",
        posix=f'exec "{sys.executable}" "{impl}" "$@"',
        windows=f'"{sys.executable}" "{impl}" %*',
        exit_code=None,
    )
    fakebin.prepend_to_path(bin_dir, monkeypatch)
    monkeypatch.setenv("AISQUARE_BRAIN", "1")
    assert runner.invoke(app, ["team", "on"]).exit_code == 0
    mcp_server.note_add("we will use JWT", kind="decision")  # a backlog for the drain
    spawns.clear()

    answer = mcp_server.recall("jwt")

    assert "results for: jwt" in answer, "the call did not reach the brain it is for"
    commands = {argv[1] for argv, _ in spawns if len(argv) > 1 and "gbrain" in argv[0]}
    assert {"--version", "init", "put", "search"} <= commands, commands
    _none_inherits(spawns)
