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

#: What stands in for Claude Code: it is the hook's parent and the server's parent. Every wait
#: has a deadline, and it keeps a journal of the stage it is in: a hang ends as a failure that
#: names the stage, with the server's stderr and its threads' stacks, not as a timeout with
#: nothing to read (Windows CI timed out at 420 s with no word of where, run 40292807904).
STAND_IN = textwrap.dedent(
    """
    import json, os, queue, shlex, signal, subprocess, sys, threading, time

    session, work, mode, journal_path, diag = sys.argv[1:6]
    HOOK_S = REPLY_S = 120
    EXIT_S = 30
    started = time.monotonic()
    journal = open(journal_path, "a", buffering=1, encoding="utf-8")
    report = {"hung": None, "journal": []}
    err_lines = []
    group = {} if os.name == "nt" else {"start_new_session": True}

    def stage(text):
        line = f"{time.monotonic() - started:7.1f}s {text}"
        journal.write(line + "\\n")
        report["journal"].append(line)

    def kill_tree(proc):
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        proc.kill()

    def hung(what, server=None):
        report["hung"] = what
        stage("HUNG: " + what)
        if server is not None:
            report["server_alive"] = server.poll() is None
            report["server_stderr"] = "".join(err_lines)[-2000:]
            kill_tree(server)
        stacks = os.path.join(diag, "server-stacks.txt")
        if os.path.exists(stacks):
            with open(stacks, encoding="utf-8", errors="replace") as handle:
                report["server_stacks"] = handle.read()[-6000:]
        print(json.dumps(report))
        sys.stdout.flush()
        os._exit(3)

    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PID"}
    stage("hook: start")
    hook = subprocess.Popen(
        [sys.executable, "-m", "aisquare", "hook", "session-start"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=dict(env, CLAUDE_PID=str(os.getpid())), cwd=work, text=True, **group,
    )
    try:
        _, hook_stderr = hook.communicate(
            json.dumps({"cwd": work, "session_id": session, "source": "startup"}), timeout=HOOK_S
        )
    except subprocess.TimeoutExpired:
        kill_tree(hook)
        hung(f"the SessionStart hook did not finish in {HOOK_S}s")
    stage(f"hook: exit {hook.returncode}")

    serve = [sys.executable, "-m", "aisquare", "serve", "--stdio"]
    if mode == "shell" and os.name == "nt":
        serve = ["cmd", "/c", *serve]
    elif mode == "shell":
        serve = ["sh", "-c", shlex.join(serve) + "; true"]  # the shell stays above it
    server_env = dict(
        env,
        AISQUARE_STAND_IN_STACKS=os.path.join(diag, "server-stacks.txt"),
        PYTHONPATH=os.pathsep.join(p for p in (diag, env.get("PYTHONPATH", "")) if p),
    )
    stage("server: start")
    server = subprocess.Popen(
        serve, cwd=work, env=server_env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, **group,
    )
    lines = queue.Queue()

    def pump_out():
        for line in iter(server.stdout.readline, b""):
            lines.put(line)
        lines.put(None)

    def pump_err():
        for line in iter(server.stderr.readline, b""):
            err_lines.append(line.decode(errors="replace"))

    threading.Thread(target=pump_out, daemon=True).start()
    err_thread = threading.Thread(target=pump_err, daemon=True)
    err_thread.start()

    def send(message):
        server.stdin.write((json.dumps(message) + "\\n").encode())
        server.stdin.flush()

    def reply(request_id, what):
        deadline = time.monotonic() + REPLY_S
        while True:
            try:
                line = lines.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                hung(f"no answer to {what} in {REPLY_S}s", server)
            if line is None:
                return None
            message = json.loads(line)
            if message.get("id") == request_id:
                return message

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "stand-in", "version": "0"}}})
    initialized = reply(1, "initialize")
    stage("server: initialized")
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "note_add", "arguments": {"text": "a note through the server"}}})
    noted = reply(2, "the note_add call")
    stage("server: note answered")
    server.stdin.close()
    exited = True
    try:
        server.wait(timeout=EXIT_S)
    except subprocess.TimeoutExpired:
        exited = False
        kill_tree(server)
    stage(f"server: exit {server.returncode}")
    err_thread.join(5)
    report.update(
        hook=hook.returncode, hook_stderr=hook_stderr[-400:],
        initialized=initialized is not None, noted=noted, server_exited=exited,
        server_stderr="".join(err_lines)[-600:],
    )
    print(json.dumps(report))
    """
)

#: ``sitecustomize`` for the server alone: its threads' stacks every 45 s into a file the stand-in
#: reads when the server does not answer.
SERVER_STACKS = textwrap.dedent(
    """
    import faulthandler, os

    _where = os.environ.get("AISQUARE_STAND_IN_STACKS")
    if _where:
        _file = open(_where, "w")
        faulthandler.dump_traceback_later(45, repeat=True, file=_file)
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


def _kill_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True, check=False
        )
    process.kill()


def _run(work: Path, mode: str, tmp_path: Path) -> dict[str, object]:
    script = tmp_path / "stand_in.py"
    script.write_text(STAND_IN, encoding="utf-8")
    diag = tmp_path / "diag"
    diag.mkdir()
    (diag / "sitecustomize.py").write_text(SERVER_STACKS, encoding="utf-8")
    journal = tmp_path / "stand_in.log"
    process = subprocess.Popen(
        [sys.executable, str(script), SESSION, str(work), mode, str(journal), str(diag)],
        stdin=subprocess.DEVNULL,  # not the xdist worker's pipe
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=work,
        env=os.environ.copy(),
    )
    try:
        out, err = process.communicate(timeout=420)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        seen = journal.read_text(encoding="utf-8") if journal.exists() else "(no journal)"
        pytest.fail(f"the stand-in outlived every deadline it keeps; its journal:\n{seen}")
    lines = out.strip().splitlines()
    if process.returncode != 0 and lines:
        pytest.fail(json.dumps(json.loads(lines[-1]), indent=2))  # a hang: the stage and stacks
    assert process.returncode == 0, err
    report: dict[str, object] = json.loads(lines[-1])
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
