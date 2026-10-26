"""No process an MCP tool starts inherits the server's stdin.

A stdio server's stdin is its protocol pipe: the client writes requests into it, and the server's
reader sits in a blocking read on it for as long as the server lives. A tool that runs ``git``
with ``stdin=None`` hands that pipe to the child. On every platform a child that holds the pipe
can read the client's bytes. On Windows it is worse: a process started with a pipe handle that
another thread is blocked in a synchronous read on can wait on it as it starts (the MSYS and
Cygwin runtimes ``git`` is built on query a standard handle's type on the way up), so the tool
never gets its answer and the client never gets its reply. That was the suspected cause of the
real-process pair in ``test_mcp_own_row_processes.py`` timing out on Windows CI (every tool call
ran ``git`` twice with an inherited stdin); the suspicion is not measured on Windows.

The spies here are on ``Popen``, so they see every spawn a tool makes, on every platform, and
fail a spawn that leaves ``stdin`` unset.
"""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.services import mcp_server

pytestmark = pytest.mark.usefixtures("project_capture")


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    monkeypatch.chdir(root)
    for name in ("AISQUARE_ROLE", "AISQUARE_FLEET_AGENT", "AISQUARE_PERSONA"):
        monkeypatch.delenv(name, raising=False)
    return root


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


def test_no_tool_starts_a_child_on_the_servers_stdin(
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
        lambda: mcp_server.recall("anything"),
        lambda: mcp_server.code_lookup("anything"),
        mcp_server.server_instructions,
    ]
    for tool in tools:
        with contextlib.suppress(Exception):  # a refusal is an answer; what it started is asked
            tool()

    assert spawns, "no tool started a process: the spy sees nothing, and proves nothing"
    inherited = [argv[:4] for argv, stdin in spawns if stdin is None]
    assert inherited == [], f"started on the server's own stdin: {inherited}"
