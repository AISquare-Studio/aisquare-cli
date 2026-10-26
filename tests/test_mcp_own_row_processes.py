"""A session's own MCP server, proved by the real process tree.

``tests/test_mcp_own_row.py`` builds the trees it means; this module lets the machine build one.
A stand-in for Claude Code (a Python process) runs the real SessionStart hook with
``CLAUDE_PID`` set to its own pid, and then starts the real ``aisquare serve --stdio`` as its
child, as Claude Code does, and takes a note through it. The same stand-in starts a second
server through a shell that stays above it (``sh -c`` / ``cmd /c``): the shape of the
``bash -lc "cd … && aisquare serve --stdio"`` command, and of a ``serve`` run from the Bash
tool, whose parent is a shell and not Claude Code.

It runs on every platform CI has, which is the point of it: Windows's process tree (the
venv's ``python.exe`` launcher, and whatever the platform keeps above a server) and Linux's
``/proc`` are read for real here, and only here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.store import store_session
from aisquare.core.workspace import current_project

pytest.importorskip("mcp", reason="the [serve] extra is not installed")
pytestmark = pytest.mark.usefixtures("project_capture")

SESSION = "cccc3333-0000-0000-0000-000000000000"

#: What stands in for Claude Code: it is the hook's parent and the server's parent.
STAND_IN = textwrap.dedent(
    """
    import json, os, shlex, subprocess, sys

    session, work, mode = sys.argv[1:4]
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PID"}
    hook = subprocess.run(
        [sys.executable, "-m", "aisquare", "hook", "session-start"],
        input=json.dumps({"cwd": work, "session_id": session, "source": "startup"}),
        env=dict(env, CLAUDE_PID=str(os.getpid())),
        cwd=work, text=True, capture_output=True, timeout=240,
    )
    serve = [sys.executable, "-m", "aisquare", "serve", "--stdio"]
    if mode == "shell" and os.name == "nt":
        serve = ["cmd", "/c", *serve]
    elif mode == "shell":
        serve = ["sh", "-c", shlex.join(serve) + "; true"]  # the shell stays above it
    server = subprocess.Popen(
        serve, cwd=work, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def send(message):
        server.stdin.write((json.dumps(message) + "\\n").encode())
        server.stdin.flush()

    def reply(request_id):
        while True:
            line = server.stdout.readline()
            if not line:
                return None
            message = json.loads(line)
            if message.get("id") == request_id:
                return message

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "stand-in", "version": "0"}}})
    initialized = reply(1)
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "note_add", "arguments": {"text": "a note through the server"}}})
    noted = reply(2)
    server.stdin.close()
    try:
        server.wait(timeout=60)
    except subprocess.TimeoutExpired:
        server.kill()
    print(json.dumps({
        "hook": hook.returncode, "hook_stderr": hook.stderr[-400:],
        "initialized": initialized is not None, "noted": noted,
        "server_stderr": server.stderr.read().decode(errors="replace")[-600:],
    }))
    """
)


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    monkeypatch.chdir(root)
    for name in ("AISQUARE_ROLE", "AISQUARE_FLEET_AGENT", "AISQUARE_PERSONA"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("AISQUARE_SERVE_CLIENT", raising=False)
    monkeypatch.delenv("AISQUARE_SERVE_ROLE", raising=False)
    return root


def _run(work: Path, mode: str, tmp_path: Path) -> dict[str, object]:
    script = tmp_path / "stand_in.py"
    script.write_text(STAND_IN, encoding="utf-8")
    done = subprocess.run(
        [sys.executable, str(script), SESSION, str(work), mode],
        capture_output=True,
        text=True,
        timeout=420,
        cwd=work,
        env=os.environ.copy(),
    )
    assert done.returncode == 0, done.stderr
    report: dict[str, object] = json.loads(done.stdout.strip().splitlines()[-1])
    return report


def _board() -> tuple[list[str], dict[str, str]]:
    with store_session() as store:
        project_id = current_project().id
        rows = sorted(s.id for s in store.team_sessions(project_id))
        notes = {
            e.text: e.session_id or ""
            for e in store.recent_events(project_id, limit=50)
            if e.kind == "note"
        }
    return rows, notes


def test_a_server_that_claude_code_starts_acts_as_its_session(
    work: Path, runner: CliRunner, tmp_path: Path
) -> None:
    assert runner.invoke(app, ["team", "on"]).exit_code == 0
    report = _run(work, "direct", tmp_path)
    assert report["hook"] == 0, report
    assert report["initialized"] is True, report
    rows, notes = _board()
    assert rows == [SESSION], (rows, report)
    assert notes == {"a note through the server": SESSION}, (notes, report)


def test_a_server_under_a_shell_that_stays_is_a_teammate(
    work: Path, runner: CliRunner, tmp_path: Path
) -> None:
    """A shell above the server is what a ``serve`` run from the Bash tool has, and what
    ``bash -lc "cd … && aisquare serve --stdio"`` leaves: Claude Code is the shell's parent
    and not the server's, so nothing proves the server is the session's own."""
    assert runner.invoke(app, ["team", "on"]).exit_code == 0
    report = _run(work, "shell", tmp_path)
    assert report["hook"] == 0, report
    assert report["initialized"] is True, report
    rows, notes = _board()
    assert SESSION in rows and any(row.startswith("mcp:remote:") for row in rows), (rows, report)
    [author] = notes.values()
    assert author.startswith("mcp:remote:"), (notes, report)
    assert SESSION not in notes.values(), "not written as the session"
