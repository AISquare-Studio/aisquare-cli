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
