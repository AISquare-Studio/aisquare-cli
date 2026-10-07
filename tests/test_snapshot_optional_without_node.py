"""Codebase snapshots are optional: a machine with no Node gets memory, and no amber about it.

THE DEFECT THIS PINS. On a machine without Node -- the memory-only route, ``uv tool
install aisquare-cli`` and nothing else -- ``init``, the hooks and memory all work,
and ``doctor`` still showed three amber rows for a feature nobody asked for:
``repomix`` (install Node), ``tiktoken`` (install a token counter for the snapshot)
and ``snapshot`` with ``Pack one: aisquare project onboard``. That last hint is a
one-click button in the UI (``services/onboarding.KNOWN_FIXES``), and without a Node
to run repomix on it can never turn green.

ONE PREDICATE. ``snapshot_core.can_pack()`` answers "can this machine pack", and the
doctor rows and the line ``init`` / ``project onboard`` print when no snapshot came
back all read it, so they cannot disagree about whether the feature is off. Each
test below pins one surface on a machine with no Node, and a control with one.

``shutil.which`` is patched for the three Node names only; every other lookup
answers as it would, so ``init`` still finds what it needs. Whether this venv has
tiktoken is ambient too, so the rows are read as on a machine without it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import agents as agent_core
from aisquare.core import snapshot as snapshot_core
from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import diagnostics
from aisquare.services.onboarding import fix_commands

_NODE_TOOLS = ("node", "npx", "repomix")
_REAL_WHICH = shutil.which
_REAL_HAS_MODULE = diagnostics._has_module


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture(autouse=True)
def no_tiktoken(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        diagnostics, "_has_module", lambda name: name != "tiktoken" and _REAL_HAS_MODULE(name)
    )


def _which(node_tools: str | None) -> Callable[..., str | None]:
    """``shutil.which`` answering ``node_tools`` (a directory, or None) for the three names."""

    def which(cmd: str, mode: int = os.F_OK | os.X_OK, path: str | None = None) -> str | None:
        if cmd in _NODE_TOOLS:
            return None if node_tools is None else f"{node_tools}/{cmd}"
        return _REAL_WHICH(cmd, mode, path)

    return which


@pytest.fixture
def no_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """None of node, npx or repomix on PATH."""
    monkeypatch.setattr(shutil, "which", _which(None))


@pytest.fixture
def with_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """Node 26 with repomix and npx on PATH, without running any of them."""
    monkeypatch.setattr(shutil, "which", _which("/opt/node/bin"))
    monkeypatch.setattr(snapshot_core, "node_version", lambda: (26, 7, 0))
    monkeypatch.setattr(snapshot_core, "installed_repomix_floor", lambda binary=None: None)


def _rows(checks: Sequence[DoctorCheck]) -> dict[str, DoctorCheck]:
    return {check.name: check for check in checks}


def _onboard_buttons(checks: Sequence[DoctorCheck]) -> list[tuple[str, ...]]:
    return [fix.argv for fix in fix_commands(checks) if fix.argv[:2] == ("project", "onboard")]


@pytest.mark.usefixtures("no_node")
def test_doctor_without_node_reads_snapshots_off_with_nothing_to_click(runner: CliRunner) -> None:
    assert runner.invoke(app, ["init", "--no-onboard"]).exit_code == 0

    checks = diagnostics.doctor()
    rows = _rows(checks)

    for name in ("repomix", "snapshot", "tiktoken"):
        assert rows[name].status is CheckStatus.ok, rows[name]
        assert rows[name].fix is None, rows[name]
    assert rows["repomix"].detail == snapshot_core.OFF_DETAIL
    assert rows["snapshot"].detail == snapshot_core.OFF_DETAIL
    assert "snapshots are off" in rows["tiktoken"].detail
    assert _onboard_buttons(checks) == []


@pytest.mark.usefixtures("with_node")
def test_with_node_a_missing_snapshot_still_warns_and_keeps_its_button(runner: CliRunner) -> None:
    """The control: the same machine with a Node is unchanged."""
    assert runner.invoke(app, ["init", "--no-onboard"]).exit_code == 0

    checks = diagnostics.doctor()
    rows = _rows(checks)

    assert rows["repomix"].status is CheckStatus.ok
    assert rows["snapshot"].status is CheckStatus.warn
    assert rows["snapshot"].fix == "Pack one: aisquare project onboard"
    assert rows["tiktoken"].status is CheckStatus.warn
    assert _onboard_buttons(checks) == [("project", "onboard", "--refresh")]


@pytest.mark.usefixtures("no_node")
def test_the_memory_route_needs_no_node(
    runner: CliRunner, isolated_agent_home: Path, work_dir: Path
) -> None:
    """init with Claude Code, remember, and a session start that hands the memory back."""
    claude_dir = isolated_agent_home / ".claude"
    claude_dir.mkdir(parents=True)

    init = runner.invoke(app, ["--json", "init", "--local", "--yes", "--agent", "claude-code"])
    assert init.exit_code == 0, init.output
    assert f"Snapshot: {snapshot_core.OFF_DETAIL}." in json.loads(init.stdout)["notes"]
    settings = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
    assert set(settings["hooks"]) == {event for event, _ in agent_core._HOOKS}

    remembered = runner.invoke(app, ["remember", "--user", "I prefer pytest"])
    assert remembered.exit_code == 0, remembered.output
    payload = json.dumps({"cwd": str(work_dir)})
    started = runner.invoke(app, ["hook", "session-start"], input=payload)
    assert started.exit_code == 0, started.output
    assert "I prefer pytest" in started.stdout


@pytest.mark.usefixtures("no_node")
def test_project_onboard_without_node_says_off_not_skipped(runner: CliRunner) -> None:
    result = runner.invoke(app, ["project", "onboard"])
    assert result.exit_code == 0, result.output
    assert f"snapshot: {snapshot_core.OFF_DETAIL}" in result.stdout
    assert "skipped" not in result.stdout


@pytest.mark.usefixtures("with_node")
def test_a_repomix_that_ran_and_failed_is_not_called_off(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Node is here, so "needs Node.js" would send someone to install what they have."""

    def fails(*_args: object, **_kwargs: object) -> tuple[str, str]:
        raise subprocess.CalledProcessError(1, ["repomix"])

    monkeypatch.setattr(snapshot_core, "_run_repomix", fails)

    onboard = runner.invoke(app, ["project", "onboard"])
    init = runner.invoke(app, ["--json", "init", "--local"])

    assert onboard.exit_code == 0, onboard.output
    assert f"snapshot: {snapshot_core.FAILED_DETAIL}" in onboard.stdout
    assert "Node.js" not in onboard.stdout
    assert init.exit_code == 0, init.output
    assert f"Snapshot: {snapshot_core.FAILED_DETAIL}." in json.loads(init.stdout)["notes"]
