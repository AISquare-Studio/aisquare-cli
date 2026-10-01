"""The captain's round trip, live: a real tmux window, the real launcher, a stand-in claude (T2).

Acceptance line 3 (contract 13121): against a fixture home, a stand-in ``claude``
that reads ``--mcp-config``, starts the server it names exactly as Claude Code
would, and calls ``attention()`` — the round trip lands. Until T7's queue merges,
``attention()`` answers the stub's refusal, and that counts (rider 6 at 13102):
what is proven is the mount — the config path, the module entry, the home the
server reads, the flags that leave the captain no other tool — and the one audit
event the call leaves on the home board.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aisquare.core.config import FleetSettings
from aisquare.core.store import store_session
from aisquare.core.tmux import TmuxError, TmuxServer
from aisquare.services import fleet
from aisquare.services.captain import brain
from aisquare.services.captain import state as captain_state

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("tmux") is None,
    reason="a live tmux window (POSIX, tmux installed)",
)

STAND_IN = r"""
import json, os, pathlib, subprocess, sys

args = sys.argv[1:]
config = json.loads(pathlib.Path(args[args.index("--mcp-config") + 1]).read_text())
spec = config["mcpServers"]["captain"]
env = dict(os.environ)
env.update(spec.get("env", {}))
server = subprocess.Popen(
    [spec["command"], *spec["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env
)


def send(message):
    server.stdin.write((json.dumps(message) + "\n").encode())
    server.stdin.flush()


def answer_to(request_id):
    while True:
        message = json.loads(server.stdout.readline())
        if message.get("id") == request_id:
            return message


hello = {"protocolVersion": "2025-11-25", "capabilities": {},
         "clientInfo": {"name": "stand-in claude", "version": "0"}}
send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": hello})
answer_to(1)
send({"jsonrpc": "2.0", "method": "notifications/initialized"})
call = {"name": "attention", "arguments": {"utterance": "what is up"}}
send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": call})
answer = answer_to(2)
pathlib.Path("roundtrip.json").write_text(json.dumps({"argv": args, "answer": answer}))
print("stand-in claude: attention() answered", flush=True)
server.stdin.close()
server.wait(timeout=20)
input()
"""


@pytest.fixture
def private_fleet(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The fleet on a tmux socket of the test's own, killed whatever the test did."""
    socket = f"asq-t2-{os.getpid()}"
    monkeypatch.setattr(fleet, "settings", lambda: FleetSettings(tmux_socket=socket))
    try:
        yield socket
    finally:
        with contextlib.suppress(TmuxError):
            TmuxServer(socket).kill_server()
        with contextlib.suppress(OSError):
            TmuxServer(socket).socket_path().unlink()


@pytest.fixture
def stand_in_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "bin"
    folder.mkdir()
    script = folder / "claude"
    script.write_text(f"#!{sys.executable}\n{STAND_IN}", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ.get('PATH', '')}")
    return script


def _wait_for(path: Path, seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.is_file():
            with contextlib.suppress(ValueError):
                data = json.loads(path.read_text(encoding="utf-8"))
                assert isinstance(data, dict)
                return data
        time.sleep(0.2)
    raise AssertionError(f"the stand-in claude never answered: no {path}")


def test_a_stand_in_claude_mounts_the_captain_server_and_its_call_lands(
    private_fleet: str, stand_in_claude: Path
) -> None:
    receipt = brain.start()
    result = _wait_for(brain.brain_dir() / "roundtrip.json", 60.0)
    argv = result["argv"]
    assert argv[argv.index("--mcp-config") + 1] == str(brain.mcp_config_path())
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--tools") + 1] == "", "no built-in tool"
    assert argv[argv.index("--allowedTools") + 1] == "mcp__captain__*"
    answer = result["answer"]["result"]
    text = answer["content"][0]["text"]
    if answer.get("isError"):
        # T7's queue has not merged: the stub's refusal is the round trip landing.
        assert text.startswith("refused: the attention queue lands with T7"), text
    else:
        assert "items" in json.loads(text)
    home = captain_state.home_project()
    with store_session() as store:
        audits = store.filtered_events(home.id, kind="captain_action", since_seq=0, limit=20)
    calls = [json.loads(event.text) for event in audits]
    assert [(c["tool"], c["utterance"]) for c in calls] == [("attention", "what is up")]
    assert receipt.agent.cwd == brain.brain_dir()
