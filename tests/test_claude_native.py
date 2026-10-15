"""Opt-in compatibility tests against an installed Claude Code, with no account at all.

Run AISQUARE_TEST_CLAUDE=1 pytest tests/test_claude_native.py. The agent runs under a
config dir of its own, with an API key that is not one, approved there so it asks
nothing about it, and a base URL nothing listens on: no login, no network and no
model spend. What is measured is everything before a model call: dialogs, hooks, and
what reaches the agent's prompt.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import agents, selfcli
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.config import load_config, save_config
from aisquare.core.store import store_session
from aisquare.core.tmux import TmuxError, TmuxServer
from aisquare.models import ProjectInfo
from aisquare.services import fleet, team

#: Not a key: approved in the config dir, so Claude Code asks nothing about it, and
#: never sent anywhere that answers (``ANTHROPIC_BASE_URL`` below).
_KEY = "sk-ant-api03-aisquare-native-fixture-not-a-key-000000000000000000000"

pytestmark = pytest.mark.skipif(
    os.environ.get("AISQUARE_TEST_CLAUDE") != "1", reason="opt-in native Claude Code test"
)


@dataclass
class Native:
    project: ProjectInfo
    srv: TmuxServer

    def screen(self, pane: str) -> str:
        return self.srv.run("capture-pane", "-p", "-t", pane)

    def until(self, check: Callable[[], bool], seconds: float, what: str, pane: str | None) -> None:
        deadline = time.monotonic() + seconds
        while not check():
            if time.monotonic() >= deadline:
                raise AssertionError(f"{what}:\n{self.screen(pane) if pane else ''}")
            time.sleep(0.2)

    def listed(self) -> list[Any]:
        with store_session() as handle:
            return handle.fleet_agents(self.project.id, live_only=True)

    def spawning(self, prompt: str, spawned: list[Any], failed: list[BaseException]) -> Any:
        def spawn() -> None:
            try:
                spawned.append(
                    fleet.spawn(
                        self.project, "coder", agent="claude-code", worktree=False, prompt=prompt
                    )
                )
            except BaseException as exc:  # handed to the test's own thread
                failed.append(exc)

        thread = threading.Thread(target=spawn, daemon=True)
        thread.start()
        return thread


@pytest.fixture
def native(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Native]:
    """A project whose folder Claude Code's account has not trusted, under a home and a
    config dir of their own, both removed after, and a tmux server killed after however
    far the test got (review r4 of #113, finding 15)."""
    binary = shutil.which("claude")
    assert binary, "AISQUARE_TEST_CLAUDE requires an installed Claude Code"
    outside = Path(tempfile.mkdtemp(prefix="asq-", dir="/var/tmp"))
    srv: TmuxServer | None = None
    try:
        store, config_dir = outside / "h", outside / "claude"
        config_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        (config_dir / ".claude.json").write_text(
            json.dumps(
                {
                    "hasCompletedOnboarding": True,
                    "theme": "dark",
                    "customApiKeyResponses": {"approved": [_KEY[-20:]], "rejected": []},
                }
            ),
            encoding="utf-8",
        )
        for key in tuple(os.environ):
            if key.startswith(("ANTHROPIC_", "CLAUDE", "OPENAI_", "CODEX_")):
                monkeypatch.delenv(key)
        monkeypatch.delenv("TMPDIR", raising=False)
        monkeypatch.setenv("AISQUARE_HOME", str(store))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
        monkeypatch.setenv("ANTHROPIC_API_KEY", _KEY)
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
        monkeypatch.setenv("DISABLE_TELEMETRY", "1")
        monkeypatch.setenv("DISABLE_AUTOUPDATER", "1")
        monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
        monkeypatch.setattr(agents, "_aisquare_command", lambda: shlex.join(selfcli.argv_for([])))
        agents.install_hooks("claude-code", config_dir)
        config = load_config()
        config.fleet.tmux_socket = f"asq-claude-native-{os.getpid()}"
        save_config(config)
        project = team.activate(workspace)
        srv = fleet.server(config.fleet)
        yield Native(project, srv)
    finally:
        if srv is not None:
            agents_left = _pane_processes(srv)
            with suppress(TmuxError):  # a server the test never got to start
                srv.run("kill-server")
            # Hung up, Claude Code still writes its transcript and runs its SessionEnd
            # hook, into the dir about to be removed: it is removed once they are done.
            deadline = time.monotonic() + 15
            while any(map(_alive, agents_left)) and time.monotonic() < deadline:
                time.sleep(0.1)
            with suppress(OSError):  # tmux 3.7c leaves the killed server's socket file
                srv.socket_path().unlink(missing_ok=True)
        shutil.rmtree(outside, ignore_errors=True)


def _pane_processes(srv: TmuxServer) -> set[int]:
    """Every process in the server's panes, each agent and its hooks: the panes' own, and
    their descendants where ``/proc`` lists them."""
    try:
        todo = [int(pid) for pid in srv.run("list-panes", "-a", "-F", "#{pane_pid}").split()]
    except (TmuxError, ValueError):
        return set()
    seen: set[int] = set()
    while todo:
        pid = todo.pop()
        if pid in seen:
            continue
        seen.add(pid)
        for children in Path(f"/proc/{pid}/task").glob("*/children"):
            with suppress(OSError, ValueError):
                todo.extend(int(child) for child in children.read_text().split())
    return seen


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_real_claude_code_keeps_its_folder_trust_dialog_and_gets_the_spawn_prompt_after(
    native: Native,
) -> None:
    """A folder the account has not trusted: Claude Code is in the pane's foreground at
    its folder-trust dialog, which fires no hook, and an Enter there picks its default,
    ``No, exit`` (measured on 2.1.300), so a spawn that typed its prompt once the agent
    was up exited the agent. Now the spawn waits for its SessionStart: the agent stays
    at the dialog, the human trusts the folder, and the prompt arrives after, whole."""
    prompt = "hello from the spawn\nsecond line of it"
    spawned: list[Any] = []
    failed: list[BaseException] = []

    def started() -> list[str]:
        with store_session() as handle:
            return [s.id for s in handle.team_sessions(native.project.id) if s.ended_at is None]

    def prompts() -> list[str]:
        with store_session() as handle:
            return [p.text for p in handle.recent_prompts(native.project.id, limit=20)]

    thread = native.spawning(prompt, spawned, failed)
    try:
        native.until(lambda: bool(native.listed()), 30, "the coder's row", None)
        pane = native.listed()[0].pane_id
        screen = native.screen
        native.until(lambda: "trust this folder" in screen(pane), 30, "the dialog", pane)
        mark = get_adapter("claude-code").capabilities.start_dialog_mark
        assert mark and mark in screen(pane), "the mark a start's dialogs show"
        time.sleep(3)  # well past the agent's start: the old rule typed by now
        assert started() == [], "no hook fires at the dialog"
        facts = native.srv.pane_facts(pane)
        assert facts is not None and not facts.dead, "the agent is still there"
        assert "hello from the spawn" not in screen(pane)
        assert thread.is_alive(), "the spawn waits for the session"
        native.srv.send_keys(pane, "Down")
        time.sleep(0.3)
        native.srv.send_keys(pane, "Enter")  # Yes, I trust this folder
        thread.join(timeout=30)
        assert not failed, failed
        [receipt] = spawned
        assert receipt.prompt_typed is True, receipt.notes
        native.until(lambda: prompt in prompts(), 20, "the prompt as its first message", pane)
        assert prompts().count(prompt) == 1, "once, whole"
        facts = native.srv.pane_facts(pane)
        assert facts is not None and not facts.dead
    finally:
        thread.join(timeout=5)


def test_real_claude_code_whose_start_the_board_cannot_see_gets_the_prompt_past_its_dialog(
    native: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch that names no session (here the pin turned off; ``--continue`` too) gives
    the board no start to see, and the prompt is typed once the agent is in the
    foreground (review r4 of #113, finding 4) — but not while its folder-trust dialog is
    on its screen, where the Enter would pick ``No, exit``: the dialog's ``Enter to
    confirm`` row keeps it out until the human has answered."""
    monkeypatch.setenv("AISQUARE_PIN_SESSION_ID", "0")
    spawned: list[Any] = []
    failed: list[BaseException] = []
    thread = native.spawning("hello from the spawn", spawned, failed)
    try:
        native.until(lambda: bool(native.listed()), 30, "the coder's row", None)
        assert native.listed()[0].session_id is None, "a start the board cannot see"
        pane = native.listed()[0].pane_id
        screen = native.screen
        native.until(lambda: "trust this folder" in screen(pane), 30, "the dialog", pane)
        time.sleep(4)  # past the settle the foreground rule types after
        facts = native.srv.pane_facts(pane)
        assert facts is not None and not facts.dead, "no Enter reached the dialog"
        assert "hello from the spawn" not in screen(pane)
        assert thread.is_alive(), "the spawn waits for the dialog to be answered"
        native.srv.send_keys(pane, "Down")
        time.sleep(0.3)
        native.srv.send_keys(pane, "Enter")  # Yes, I trust this folder
        thread.join(timeout=30)
        assert not failed, failed
        [receipt] = spawned
        assert receipt.prompt_typed is True, receipt.notes
        native.until(lambda: "hello from the spawn" in screen(pane), 20, "the prompt", pane)
        facts = native.srv.pane_facts(pane)
        assert facts is not None and not facts.dead
    finally:
        thread.join(timeout=5)
