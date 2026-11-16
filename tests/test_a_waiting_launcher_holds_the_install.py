"""A session started with ``aisquare launch`` that waits on its agent refuses the upgrade and
the uninstall, named (review r40 of #113, finding 7).

Where the platform has no exec (Windows), the launcher runs the agent as its child and waits
(``cli.launch._exec``): the tool's ``python.exe``, its ``.pyd`` files and ``aisquare.exe``
stay open for the whole session, and a ``uv tool install --force`` or ``uv tool uninstall``
beside it fails on files in use. ``running_asqs`` counted asq UIs alone. Each waiting
launcher now records itself (``core.launchers``), and the upgrade (its hand-run command on
Windows) and the uninstall name it and say to end it first, as they do for an asq.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import launchers
from aisquare.services import install_route, lifecycle
from tests.test_lifecycle_upgrade import (  # noqa: F401 - fixtures, by name
    Machine,
    Tool,
    _facts,
    _one_object,
    machine,
    tool,
)

ME = f"aisquare launch of claude (pid {os.getpid()})"


def test_a_launcher_is_running_while_it_waits_and_leaves_nothing_after() -> None:
    assert launchers.waiting_launchers() == ()
    with launchers.waiting("claude"):
        assert [launcher.label for launcher in launchers.waiting_launchers()] == [ME]
        assert lifecycle.running_launchers() == (ME,)
    assert launchers.waiting_launchers() == ()
    assert list(launchers.registry_dir().iterdir()) == [], "its record and its lock are gone"


def test_a_record_whose_lock_is_free_is_a_launcher_that_ended(tmp_path: Path) -> None:
    """Killed, a launcher leaves its files, and the OS its lock: not one, whatever its pid."""
    folder = launchers.registry_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "0123456789ab.json").write_text(
        json.dumps({"pid": os.getpid(), "agent": "claude", "started_at": "2026-11-05T00:00:00"}),
        encoding="utf-8",
    )
    (folder / "0123456789ab.lock").write_text("", encoding="utf-8")

    assert launchers.waiting_launchers() == ()


def test_the_uninstall_refuses_while_a_launcher_waits_and_names_it() -> None:
    with launchers.waiting("claude"):
        refusal = lifecycle.uninstall_plan().refusal

    assert refusal is not None and refusal.error == "launcher_running"
    said = " ".join(str(refusal).split())
    assert (
        f"a session started with `aisquare launch` is waiting on its agent on this home ({ME})"
        in said
    )
    assert "removing them fails on files in use" in said
    assert "End it first (leave the agent), then run `aisquare uninstall` again." in said
    assert lifecycle.uninstall_plan().refusal is None, "control: once it has ended"


def test_windows_hand_run_upgrade_names_the_launcher_to_end_first(
    runner: CliRunner,
    tool: Tool,  # noqa: F811
    machine: Machine,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    windows = _facts(tool.prefix, platform="win32")
    monkeypatch.setattr(install_route, "facts", lambda: windows)

    with launchers.waiting("claude"):
        held = runner.invoke(app, ["upgrade", "--yes"])
    alone = runner.invoke(app, ["upgrade", "--yes"])

    said = " ".join(held.stderr.split())
    assert held.exit_code == 1 and "uv tool install --force" in said
    assert f"waiting on its agent on this home ({ME})" in said
    assert (
        "replacing them fails on files in use. End it first (leave the agent), then run it." in said
    )
    assert "aisquare launch" not in alone.stderr, "control: none waiting, none named"
    assert machine.installs == []


def test_an_upgrade_aisquare_runs_is_refused_too(
    runner: CliRunner,
    tool: Tool,  # noqa: F811
    machine: Machine,  # noqa: F811
) -> None:
    with launchers.waiting("claude"):
        plan = _one_object(runner.invoke(app, ["--json", "upgrade"]).stdout)
        refused = runner.invoke(app, ["--json", "upgrade", "--yes"])

    assert plan["launchers"] == [ME], plan
    assert refused.exit_code == 1 and _one_object(refused.stdout)["error"] == "launcher_running"
    assert machine.installs == []


def test_the_windows_launcher_is_recorded_for_as_long_as_its_agent_runs(
    capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_exec`` itself, with an agent that asks the registry while it runs (on windows-latest
    in CI as it runs there; elsewhere its Windows branch, the platform faked in ``launch``)."""
    from types import SimpleNamespace

    from aisquare.cli import launch

    if sys.platform != "win32":  # windows-latest runs it as it is; elsewhere, its branch
        monkeypatch.setattr(launch, "sys", SimpleNamespace(platform="win32"))
    agent = [
        sys.executable,
        "-c",
        "from aisquare.core import launchers as w; print([x.label for x in w.waiting_launchers()])",
    ]
    previous = signal.getsignal(signal.SIGINT)
    try:
        with pytest.raises(SystemExit) as ended:
            launch._exec(sys.executable, agent, dict(os.environ))
    finally:
        signal.signal(signal.SIGINT, previous)

    assert ended.value.code == 0
    named = f"aisquare launch of {Path(sys.executable).stem} (pid {os.getpid()})"
    assert repr([named]) in capfd.readouterr().out
    assert launchers.waiting_launchers() == (), "gone once its agent has ended"
