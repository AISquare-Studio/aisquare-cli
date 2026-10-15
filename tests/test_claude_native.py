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
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import agents, selfcli
from aisquare.core.config import load_config, save_config
from aisquare.core.store import store_session
from aisquare.services import fleet, team

#: Not a key: approved in the config dir, so Claude Code asks nothing about it, and
#: never sent anywhere that answers (``ANTHROPIC_BASE_URL`` below).
_KEY = "sk-ant-api03-aisquare-native-fixture-not-a-key-000000000000000000000"


@pytest.mark.skipif(
    os.environ.get("AISQUARE_TEST_CLAUDE") != "1", reason="opt-in native Claude Code test"
)
def test_real_claude_code_keeps_its_folder_trust_dialog_and_gets_the_spawn_prompt_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder the account has not trusted: Claude Code is in the pane's foreground at
    its folder-trust dialog, which fires no hook, and an Enter there picks its default,
    ``No, exit`` (measured on 2.1.300), so a spawn that typed its prompt once the agent
    was up exited the agent. Now the spawn waits for its SessionStart: the agent stays
    at the dialog, the human trusts the folder, and the prompt arrives after, whole."""
    binary = shutil.which("claude")
    assert binary, "AISQUARE_TEST_CLAUDE requires an installed Claude Code"
    outside = Path(tempfile.mkdtemp(prefix="asq-", dir="/var/tmp"))
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
    prompt = "hello from the spawn\nsecond line of it"
    spawned: list[Any] = []
    failed: list[BaseException] = []

    def spawn() -> None:
        try:
            spawned.append(
                fleet.spawn(project, "coder", agent="claude-code", worktree=False, prompt=prompt)
            )
        except BaseException as exc:  # handed to the test's own thread
            failed.append(exc)

    def screen(pane: str) -> str:
        return srv.run("capture-pane", "-p", "-t", pane)

    def until(check: Any, seconds: float, what: str, pane: str | None) -> None:
        deadline = time.monotonic() + seconds
        while not check():
            if time.monotonic() >= deadline:
                raise AssertionError(f"{what}:\n{screen(pane) if pane else ''}")
            time.sleep(0.2)

    def started() -> list[str]:
        with store_session() as handle:
            return [s.id for s in handle.team_sessions(project.id) if s.ended_at is None]

    def prompts() -> list[str]:
        with store_session() as handle:
            return [p.text for p in handle.recent_prompts(project.id, limit=20)]

    thread = threading.Thread(target=spawn, daemon=True)
    try:
        thread.start()

        def listed() -> list[Any]:
            with store_session() as handle:
                return handle.fleet_agents(project.id, live_only=True)

        until(lambda: bool(listed()), 30, "the coder's row", None)
        pane = listed()[0].pane_id
        until(lambda: "trust this folder" in screen(pane), 30, "the folder-trust dialog", pane)
        time.sleep(3)  # well past the agent's start: the old rule typed by now
        assert started() == [], "no hook fires at the dialog"
        facts = srv.pane_facts(pane)
        assert facts is not None and not facts.dead, "the agent is still there"
        assert "hello from the spawn" not in screen(pane)
        assert thread.is_alive(), "the spawn waits for the session"
        srv.send_keys(pane, "Down")
        time.sleep(0.3)
        srv.send_keys(pane, "Enter")  # Yes, I trust this folder
        thread.join(timeout=30)
        assert not failed, failed
        [receipt] = spawned
        assert receipt.prompt_typed is True, receipt.notes
        until(lambda: prompt in prompts(), 20, "the prompt as its first message", pane)
        assert prompts().count(prompt) == 1, "once, whole"
        facts = srv.pane_facts(pane)
        assert facts is not None and not facts.dead
    finally:
        if srv.has_session(fleet.session_name(fleet.ensure_codename(project).codename or "")):
            srv.run("kill-server")
        thread.join(timeout=5)
