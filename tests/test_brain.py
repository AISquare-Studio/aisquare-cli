"""The brain layer: distiller outbox, watermark, recall — against a fake gbrain."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import brain
from aisquare.core.teambus import team_project
from aisquare.services import distill
from aisquare.services import team as team_service

_FAKE_GBRAIN = """#!/bin/sh
case "$1" in
  --version) echo "gbrain 0.42.1.0";;
  init)
    mkdir -p "$GBRAIN_HOME/.gbrain/brain.pglite"
    echo 16 > "$GBRAIN_HOME/.gbrain/brain.pglite/PG_VERSION"
    ;;
  put)
    [ "$FAKE_GBRAIN_FAIL" = "1" ] && exit 1
    mkdir -p "$GBRAIN_HOME/pages"
    cat > "$GBRAIN_HOME/pages/$(echo "$2" | tr '/' '_')"
    echo "$2" >> "$GBRAIN_HOME/puts.log"
    ;;
  search) echo "results for: $2";;
  *) exit 0;;
esac
"""


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def fake_gbrain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a scripted gbrain on PATH (keeping system dirs for git etc.)."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    script = bin_dir / "gbrain"
    script.write_text(_FAKE_GBRAIN)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    return bin_dir


def _seed_events(runner: CliRunner) -> None:
    team_service.activate()
    runner.invoke(app, ["note", "we will use JWT", "--kind", "decision"])
    runner.invoke(app, ["note", "just chatter"])  # plain notes do not distill
    runner.invoke(app, ["task", "add", "wire auth"])
    task_id = json.loads(runner.invoke(app, ["--json", "task", "list"]).stdout)[0]["id"]
    runner.invoke(app, ["task", "done", task_id, "--note", "all tests green"])


def test_drain_distills_only_durable_kinds(
    runner: CliRunner, fake_gbrain: Path, work_dir: Path
) -> None:
    _seed_events(runner)
    assert distill.drain(work_dir) == 2  # the decision + task_done; chatter skipped
    project = team_project(work_dir)
    puts = (brain.brain_home(project.id) / "puts.log").read_text().splitlines()
    assert any(slug.startswith("team/decision/") for slug in puts)
    assert any(slug.startswith("team/task-done/") for slug in puts)
    page = next(
        path
        for path in (brain.brain_home(project.id) / "pages").iterdir()
        if "decision" in path.name
    ).read_text()
    assert "we will use JWT" in page and "aisquare-team" in page
    assert distill.drain(work_dir) == 0  # watermark: nothing new on re-drain


def test_drain_holds_watermark_on_put_failure(
    runner: CliRunner, fake_gbrain: Path, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_events(runner)
    monkeypatch.setenv("FAKE_GBRAIN_FAIL", "1")
    assert distill.drain(work_dir) == 0  # first durable event failed; nothing written
    monkeypatch.delenv("FAKE_GBRAIN_FAIL")
    assert distill.drain(work_dir) == 2  # retried from the held watermark


def test_drain_without_gbrain_is_a_quiet_noop(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # no gbrain anywhere
    _seed_events(runner)
    assert distill.drain(work_dir) == 0


def test_recall_roundtrip_and_unavailable_paths(
    runner: CliRunner, fake_gbrain: Path, work_dir: Path
) -> None:
    _seed_events(runner)
    # Before any distill the brain is uninitialised → unavailable, exit 1.
    missing = runner.invoke(app, ["--json", "recall", "auth"])
    assert missing.exit_code == 1
    assert json.loads(missing.stdout)["error"] == "brain_unavailable"
    runner.invoke(app, ["team", "distill"])
    found = runner.invoke(app, ["recall", "auth"])
    assert found.exit_code == 0
    assert "results for: auth" in found.stdout


def test_distill_cli_reports_count(runner: CliRunner, fake_gbrain: Path, work_dir: Path) -> None:
    _seed_events(runner)
    result = runner.invoke(app, ["--json", "team", "distill"])
    assert json.loads(result.stdout) == {"distilled": 2}


def test_brain_master_switch(
    runner: CliRunner, fake_gbrain: Path, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_events(runner)
    monkeypatch.setenv("AISQUARE_BRAIN", "0")
    assert distill.drain(work_dir) == 0
    assert team_service.recall("auth", work_dir) is None


def test_concurrent_drain_lock_is_exclusive(fake_gbrain: Path, work_dir: Path) -> None:
    project = team_project(work_dir)
    with brain.drain_lock(project.id) as first:
        assert first
        with brain.drain_lock(project.id) as second:
            assert not second  # a running drain makes the next one skip
