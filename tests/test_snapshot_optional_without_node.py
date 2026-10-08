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
# Taken before the autouse ``no_repomix`` fixture swaps it: with no packer on PATH it
# raises from ``_repomix_base`` without starting anything, as on a machine with no Node.
_REAL_RUN_REPOMIX = snapshot_core._run_repomix

_PACK = (
    '<files>\n<file path="a.py">\nprint("a")\n</file>\n'
    '<file path="b.py">\nprint("b")\n</file>\n</files>\n'
)

#: What npm prints when ``npx`` cannot reach the registry (measured with npm 11).
_NPM_OFFLINE = (
    "npm error code ENOTFOUND\n"
    "npm error syscall getaddrinfo\n"
    "npm error network request to https://registry.npmjs.org/repomix failed, "
    "reason: getaddrinfo ENOTFOUND registry.npmjs.org\n"
)
_NPM_REASON = (
    "npm error network request to https://registry.npmjs.org/repomix failed, "
    "reason: getaddrinfo ENOTFOUND registry.npmjs.org"
)


def _packs(_root: Path, *, compress: bool, ignore: Sequence[str] = ()) -> tuple[str, str]:
    return _PACK, "Total Tokens: 42"


def _offline(*_args: object, **_kwargs: object) -> tuple[str, str]:
    raise subprocess.CalledProcessError(
        1, ["/opt/node/bin/npx", "--yes", "repomix"], output="", stderr=_NPM_OFFLINE
    )


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
def node_without_npm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Node 26 on PATH and nothing to pack with: Arch, Alpine or Debian's own nodejs."""

    def which(cmd: str, mode: int = os.F_OK | os.X_OK, path: str | None = None) -> str | None:
        if cmd == "node":
            return "/opt/node/bin/node"
        return None if cmd in _NODE_TOOLS else _REAL_WHICH(cmd, mode, path)

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr(snapshot_core, "node_version", lambda: (26, 7, 0))


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

    said = f"{snapshot_core.FAILED_DETAIL}: repomix exited 1 without saying why"
    assert onboard.exit_code == 0, onboard.output
    assert f"snapshot: {said}" in onboard.stdout
    assert "Node.js" not in onboard.stdout
    assert init.exit_code == 0, init.output
    assert f"Snapshot: {said}." in json.loads(init.stdout)["notes"]


@pytest.mark.usefixtures("node_without_npm")
def test_a_node_with_nothing_to_pack_with_is_told_what_is_missing(runner: CliRunner) -> None:
    """Not "need Node.js 22+": that Node 26 user needs npm (review of #244, finding 3)."""
    init = runner.invoke(app, ["--json", "init", "--local"])
    onboard = runner.invoke(app, ["project", "onboard"])
    rows = _rows(diagnostics.doctor())

    assert init.exit_code == 0, init.output
    assert f"Snapshot: {snapshot_core.NO_PACKER_DETAIL}." in json.loads(init.stdout)["notes"]
    assert f"snapshot: {snapshot_core.NO_PACKER_DETAIL}" in onboard.stdout
    assert rows["snapshot"].status is CheckStatus.ok and rows["snapshot"].fix is None
    assert rows["snapshot"].detail == snapshot_core.NO_PACKER_DETAIL
    assert "Node.js 22+ (optional" not in onboard.stdout + rows["snapshot"].detail
    assert rows["repomix"].status is CheckStatus.warn, "the row with the fix still warns"


def test_a_refresh_without_node_keeps_the_last_pack_and_says_so(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """Packed while a Node was on PATH, refreshed from a shell without one (review of #257).

    The installer's fnm Node is on PATH for its own run only, and an nvm Node only in
    the shells that load it. A failed pack rewrites nothing, so the last pack stays and
    session start keeps handing it to every agent. ``--refresh`` said "off", and the
    doctor's snapshot row said "ready" beside a repomix row that said "off".
    """
    monkeypatch.setattr(shutil, "which", _which("/opt/node/bin"))
    monkeypatch.setattr(snapshot_core, "node_version", lambda: (26, 7, 0))
    monkeypatch.setattr(snapshot_core, "_run_repomix", _packs)
    packed = runner.invoke(app, ["project", "onboard"])
    assert packed.exit_code == 0, packed.output
    with_node = _rows(diagnostics.doctor())

    monkeypatch.setattr(shutil, "which", _which(None))
    monkeypatch.setattr(snapshot_core, "_run_repomix", _REAL_RUN_REPOMIX)
    refreshed = runner.invoke(app, ["project", "onboard", "--refresh"])
    report = json.loads(runner.invoke(app, ["--json", "project", "onboard", "--refresh"]).stdout)
    started = runner.invoke(
        app, ["hook", "session-start"], input=json.dumps({"cwd": str(work_dir)})
    )
    rows = _rows(diagnostics.doctor())

    assert refreshed.exit_code == 0, refreshed.output
    line = refreshed.stdout.strip()
    assert line.startswith("snapshot: not refreshed — "), line
    assert "Agents still get the last pack, made " in line
    assert snapshot_core.OFF_DETAIL not in refreshed.stdout
    assert f"— {snapshot_core.NEEDS_NODE}. " in line
    assert report["snapshot"]["status"] == "ready" and report["snapshot"]["file_count"] == 2
    assert report["snapshot_note"] == line.removeprefix("snapshot: ")
    assert "packed snapshot" in started.stdout, "what the line says: agents still get it"
    assert rows["snapshot"].status is CheckStatus.ok and rows["snapshot"].fix is None
    assert "cannot be refreshed here" in rows["snapshot"].detail, rows["snapshot"].detail
    assert rows["snapshot"].detail.startswith("snapshot ready (2 files, ")
    assert rows["snapshot"].detail.endswith(f"here: {snapshot_core.NEEDS_NODE}")
    assert rows["repomix"].detail == snapshot_core.OFF_DETAIL
    # The control: where something can pack, the row is what it was.
    tokens = report["snapshot"]["token_count"]
    assert with_node["snapshot"].detail == f"snapshot ready (2 files, {tokens} tokens)"


@pytest.mark.usefixtures("with_node")
def test_a_pack_that_failed_says_why_and_sends_nobody_to_the_doctor(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """npx could not reach the registry (review of #257).

    The reason was thrown away, and the line said "run: aisquare doctor": a doctor that
    reads PATH and snapshot.json, whose repomix row says "enabled" and whose only fix
    runs the same pack again. A ``--refresh`` that fails beside a kept pack says why too.
    """
    monkeypatch.setattr(snapshot_core, "_run_repomix", _offline)
    onboard = runner.invoke(app, ["project", "onboard", "--refresh"])
    init = runner.invoke(app, ["--json", "init", "--local"])
    monkeypatch.setattr(snapshot_core, "_run_repomix", _packs)
    assert runner.invoke(app, ["project", "onboard", "--refresh"]).exit_code == 0
    monkeypatch.setattr(snapshot_core, "_run_repomix", _offline)
    kept = runner.invoke(app, ["project", "onboard", "--refresh"])

    said = f"skipped — the pack failed: {_NPM_REASON}"
    assert onboard.exit_code == 0, onboard.output
    assert f"snapshot: {said}" in onboard.stdout
    assert "doctor" not in onboard.stdout
    assert init.exit_code == 0, init.output
    assert f"Snapshot: {said}." in json.loads(init.stdout)["notes"]
    assert kept.exit_code == 0, kept.output
    assert f"snapshot: not refreshed — the pack failed: {_NPM_REASON}. " in kept.stdout
    assert "Agents still get the last pack" in kept.stdout
