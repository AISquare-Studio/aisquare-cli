"""The fleet's bulk restart and the captain's hands on a real Codex, live, on loopback only.

Run ``AISQUARE_TEST_CODEX=1 pytest tests/test_codex_native_fleet_captain.py``, in the
pattern of ``tests/test_codex_native.py`` (its loopback provider, ``_Asking``, which records
every request): no account, login, provider credential or model spend.

- ``fleet restart --all --managers`` with a Codex manager (SCOPE #113 0.8.2, A9): the
  manager is stopped through its own ``/exit`` and resumes its thread through
  ``resume_args`` (``codex resume <id>``), its first turn in the request after the
  restart; its Codex coder is left as it was.
- The captain's ``press`` (A1c): a Codex coder on ``workspace:ask`` asks to run a command
  outside its sandbox, its approval view in a fleet window, and ``press yes``
  (``services.captain.actions``, as its Actions server runs it) reads that view by Codex's
  own screens and sends ``1``, and the command runs; at the next approval ``press no``
  sends Esc, and it does not. Read with Claude Code's shapes, as the captain read every
  pane before, the view showed no prompt and ``press yes`` was refused.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import agents, selfcli
from aisquare.core.agent_adapters.types import store_dir, store_read_only
from aisquare.core.config import load_config, save_config
from aisquare.core.store import store_session
from aisquare.core.tmux import TmuxServer
from aisquare.models import ProjectInfo
from aisquare.services import fleet, team
from aisquare.services.captain import actions
from aisquare.services.captain.errors import Refused
from tests.test_codex_native import _Asking, _sandbox_refused, _user_texts

#: Codex's own options for a live fleet window: the hooks the test home installed run
#: untrusted (no ``/hooks`` review in a test), and no alternate screen, so ``capture-pane``
#: reads what it draws.
NATIVE = ["--dangerously-bypass-hook-trust", "--no-alt-screen"]


@dataclass
class Fleet:
    """A fleet on a private tmux server, its Codex on a loopback provider."""

    project: ProjectInfo
    server: TmuxServer
    requests: list[dict[str, Any]]
    outside: Path

    def screen(self, pane: str) -> str:
        return self.server.run("capture-pane", "-p", "-t", pane)

    def until(self, check: Callable[[], Any], seconds: float, what: str, pane: str) -> Any:
        deadline = time.monotonic() + seconds
        while not (found := check()):
            if time.monotonic() >= deadline:
                raise AssertionError(f"{what}:\n{self.screen(pane)}")
            time.sleep(0.3)
        return found


@contextlib.contextmanager
def _codex_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Iterator[Fleet]:
    binary = shutil.which("codex")
    assert binary, "AISQUARE_TEST_CODEX requires an installed Codex"
    refused = _sandbox_refused(binary, tmp_path)
    if refused is not None:
        pytest.skip(f"Codex's sandbox cannot start a command on this host: {refused}")
    outside = Path(tempfile.mkdtemp(prefix="asq-", dir="/var/tmp"))
    store, native_home, workspace = outside / "h", outside / "c", outside / "w"
    native_home.mkdir()
    workspace.mkdir()
    socket = f"asq-{name}-{os.getpid()}"
    with ThreadingHTTPServer(("127.0.0.1", 0), _Asking) as provider:
        provider.requests = []  # type: ignore[attr-defined]
        thread = threading.Thread(target=provider.serve_forever, daemon=True)
        thread.start()
        (native_home / "config.toml").write_text(
            'model = "local-fixture"\nmodel_provider = "local"\n'
            '[model_providers.local]\nname = "Local fixture"\nwire_api = "responses"\n'
            f'base_url = "http://127.0.0.1:{provider.server_port}/v1"\n'
            "requires_openai_auth = false\n"
            f'[projects.{json.dumps(str(workspace))}]\ntrust_level = "trusted"\n',
            encoding="utf-8",
        )
        for key in tuple(os.environ):
            if key.startswith(("OPENAI_", "ANTHROPIC_", "CODEX_")):
                monkeypatch.delenv(key)
        monkeypatch.delenv("TMPDIR", raising=False)
        monkeypatch.setenv("AISQUARE_HOME", str(store))
        monkeypatch.setenv("CODEX_HOME", str(native_home))
        monkeypatch.setattr(
            agents, "_aisquare_command", lambda *args, **kwargs: shlex.join(selfcli.argv_for([]))
        )
        agents.install_hooks("codex", native_home)
        config = load_config()
        config.fleet.tmux_socket = socket
        save_config(config)
        project = team.activate(workspace)
        for kept in store_read_only(store_dir()):
            kept.parent.mkdir(parents=True, exist_ok=True)
        (store / "codex-accounts").mkdir(exist_ok=True)
        try:
            yield Fleet(project, fleet.server(config.fleet), provider.requests, outside)  # type: ignore[attr-defined]
        finally:
            subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, check=False)
            provider.shutdown()
            thread.join(timeout=2)
            shutil.rmtree(outside, ignore_errors=True)


@pytest.mark.skipif(os.environ.get("AISQUARE_TEST_CODEX") != "1", reason="opt-in native Codex test")
def test_restart_all_managers_resumes_a_real_codex_manager_and_leaves_its_coder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    with _codex_fleet(tmp_path, monkeypatch, "bulk") as live:

        def waiting(row: Any) -> None:
            live.until(lambda: fleet.status_of(row).state == "waiting", 60, "its turn", row.pane_id)

        # One at a time: two first starts on one fresh CODEX_HOME race its database's first
        # migration ("table queued_items already exists", 0.162.1).
        manager = fleet.spawn(
            live.project, "manager", agent="codex", worktree=False, prompt="hello manager",
            agent_args=NATIVE,
        ).agent  # fmt: skip
        waiting(manager)
        coder = fleet.spawn(
            live.project, "coder", agent="codex", worktree=False, prompt="hello coder",
            agent_args=NATIVE,
        ).agent  # fmt: skip
        waiting(coder)
        before = len(live.requests)
        with store_session() as store:
            bound = store.get_fleet_agent(manager.id)
        assert bound is not None and bound.session_id is not None, "bound at its first hook"

        done = runner.invoke(app, ["fleet", "restart", "--all", "--managers"])

        assert done.exit_code == 0, done.output
        assert "manager" in done.output and "stopped and restarted — resumed" in done.output
        assert "coder" not in done.output.replace("coders", ""), "--managers: the manager alone"
        with store_session() as store:
            row = store.fleet_agent_by_label(live.project.id, "manager", live_only=True)
            kept = store.get_fleet_agent(coder.id)
        assert row is not None and row.id != manager.id and row.agent == "codex"
        assert row.session_id == bound.session_id, "its board session, resumed"
        assert kept is not None and kept.ended_at is None, "its coder is left running"
        restarted = fleet._restart_prompt(row)

        def resumed_turn() -> list[str] | None:
            # Its turn's request, not the title Codex asks for after each turn.
            turns = (_user_texts(request) for request in live.requests[before:])
            return next((texts for texts in turns if restarted in texts), None)

        resumed = live.until(resumed_turn, 60, "the resumed turn", row.pane_id)
        assert "hello manager" in resumed, "the thread it began, resumed by `codex resume`"
        assert "hello coder" not in resumed


@pytest.mark.skipif(os.environ.get("AISQUARE_TEST_CODEX") != "1", reason="opt-in native Codex test")
def test_a_restart_of_a_real_codex_row_given_a_persona_since_says_its_seam_kept_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4 (``codex resume`` keeps the ``developer_instructions`` its thread began with, L1's
    and L2's measurement): a coder spawned with no persona and given one since is restarted,
    resuming its thread, and the receipt says the persona reaches it through the briefing
    alone."""
    with _codex_fleet(tmp_path, monkeypatch, "seam") as live:
        coder = fleet.spawn(
            live.project, "coder", agent="codex", worktree=False, prompt="hello seam",
            agent_args=NATIVE,
        ).agent  # fmt: skip
        live.until(lambda: fleet.status_of(coder).state == "waiting", 60, "its turn", coder.pane_id)
        fleet.attach_persona(live.project, coder.label, "skeptic")
        before = len(live.requests)

        receipt = fleet.restart(live.project, coder.label)

        assert receipt.resumed is True, receipt.notes
        assert any(
            note.startswith("persona skeptic: Codex resumes the thread with the system prompt")
            for note in receipt.notes
        ), receipt.notes
        restarted = fleet._restart_prompt(receipt.started)

        def resumed_turn() -> dict[str, Any] | None:
            requests = live.requests[before:]
            return next((r for r in requests if restarted in _user_texts(r)), None)

        turn = live.until(resumed_turn, 60, "the resumed turn", receipt.started.pane_id)
        assert "hello seam" in _user_texts(turn), "its own thread, resumed"


@pytest.mark.skipif(os.environ.get("AISQUARE_TEST_CODEX") != "1", reason="opt-in native Codex test")
def test_a_hand_off_fork_of_a_real_codex_coder_is_a_new_thread_with_its_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A7: the Fork of a Codex teammate is ``codex fork <id>``. The new agent's first turn
    carries the source's conversation and the fork's one line, under a thread and a board
    session of its own, and the source runs on."""
    with _codex_fleet(tmp_path, monkeypatch, "fork") as live:
        source = fleet.spawn(
            live.project, "coder", agent="codex", worktree=False, prompt="hello source",
            agent_args=NATIVE,
        ).agent  # fmt: skip
        live.until(
            lambda: fleet.status_of(source).state == "waiting", 60, "its turn", source.pane_id
        )
        with store_session() as store:
            bound = store.get_fleet_agent(source.id)
        assert bound is not None and bound.session_id is not None
        before = len(live.requests)

        receipt = fleet.hand_off(live.project, source.label)

        assert receipt.resumed is True, receipt.notes
        assert any(
            note.startswith("forked as Codex forks a session: `fork ") for note in receipt.notes
        )
        fork = receipt.started.agent

        def forked_turn() -> list[str] | None:
            turns = (_user_texts(request) for request in live.requests[before:])
            line = f"You are a fork of {source.label}"
            return next((texts for texts in turns if any(line in t for t in texts)), None)

        turn = live.until(forked_turn, 60, "the fork's first turn", fork.pane_id)
        assert "hello source" in turn, "the source's conversation, carried by `codex fork`"
        live.until(
            lambda: fleet.status_of(fork).state == "waiting", 60, "the fork's turn", fork.pane_id
        )
        with store_session() as store:
            forked, kept = store.get_fleet_agent(fork.id), store.get_fleet_agent(source.id)
        assert forked is not None and forked.session_id not in (None, bound.session_id)
        assert kept is not None and kept.ended_at is None, "the source runs on"


@pytest.mark.skipif(os.environ.get("AISQUARE_TEST_CODEX") != "1", reason="opt-in native Codex test")
def test_the_captain_presses_yes_and_no_at_a_real_codex_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _codex_fleet(tmp_path, monkeypatch, "captain") as live:
        first = live.outside / "approved-by-the-captain"
        coder = fleet.spawn(
            live.project, "coder", agent="codex", worktree=False,
            permission_mode="workspace:ask", prompt=f"ESC touch {first}", agent_args=NATIVE,
        ).agent  # fmt: skip
        pane, label = coder.pane_id, coder.label

        def asking() -> bool:
            return "Would you like to run" in live.screen(pane)

        def press(key: str) -> dict[str, Any]:
            """The captain's press, retried while the view is not drawn yet: Codex records
            the call before its hooks run and it draws its view."""
            deadline = time.monotonic() + 20
            while True:
                try:
                    said: dict[str, Any] = json.loads(
                        actions.press(live.project.id, label, key, utterance=f"press {key}")
                    )
                    return said
                except Refused as exc:
                    assert time.monotonic() < deadline, f"{exc}\n{live.screen(pane)}"
                    time.sleep(0.5)

        live.until(asking, 60, "Codex's approval view", pane)

        yes = press("yes")

        assert (yes["sent"], yes["answered"]) == ("1", True), yes
        assert f"touch {first}" in yes["prompt"], "the view's words name its command"
        live.until(first.exists, 30, "the command the captain approved ran", pane)

        second = live.outside / "refused-by-the-captain"
        live.until(lambda: fleet.status_of(coder).state == "waiting", 40, "its turn's end", pane)
        assert fleet.tell(live.project, label, f"ESC touch {second}").delivered
        live.until(asking, 40, "the next approval view", pane)

        no = press("no")

        assert (no["sent"], no["answered"]) == ("Escape", True), no
        live.until(lambda: not asking(), 20, "the approval view gone", pane)
        time.sleep(2)
        assert not second.exists(), "a No runs nothing"
