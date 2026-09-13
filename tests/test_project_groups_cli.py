"""``aisquare project group …``, ``pin`` / ``unpin``, ``move``, ``list --group/--pinned``,
``onboard --group`` (#140): every sidebar gesture has a command that leaves the same store
state, and ``project list --json`` exposes ``group``, ``position`` and ``pinned``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.store import store_session
from aisquare.models import ProjectInfo


@pytest.fixture
def projects(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    paths.ensure_home()
    ids: dict[str, str] = {}
    with store_session() as store:
        for name in ("api", "cli", "docs", "web"):
            root = tmp_path / name
            root.mkdir()
            project = store.onboard_project(ProjectInfo(id=f"prj_{name}", root=root))
            ids[name] = project.id
    monkeypatch.chdir(tmp_path / "api")
    return ids


def _listed(runner: CliRunner, *args: str) -> list[dict[str, Any]]:
    result = runner.invoke(app, ["--json", "project", "list", *args])
    assert result.exit_code == 0, result.output
    rows: list[dict[str, Any]] = json.loads(result.stdout)
    return rows


def _names(runner: CliRunner, *args: str) -> list[str]:
    return [row["name"] for row in _listed(runner, *args)]


def test_groups_pins_and_moves_through_the_cli(runner: CliRunner, projects: dict[str, str]) -> None:
    assert _names(runner) == ["api", "cli", "docs", "web"]

    created = runner.invoke(app, ["project", "group", "create", "frontend", "web", "docs"])
    assert created.exit_code == 0, created.output
    assert "✓ group frontend created with 2 project(s)" in created.stdout
    assert _names(runner) == ["web", "docs", "api", "cli"], "grouped first, then the loose ones"
    rows = {row["name"]: row for row in _listed(runner)}
    assert rows["web"]["group"] == "frontend" and rows["web"]["position"] == 0
    assert rows["docs"]["position"] == 1 and rows["api"]["group"] is None
    assert rows["api"]["pinned"] is False

    assert _names(runner, "--group", "frontend") == ["web", "docs"]
    missing = runner.invoke(app, ["--json", "project", "list", "--group", "nope"])
    assert missing.exit_code == 1 and json.loads(missing.stdout)["error"] == "not_found"

    added = runner.invoke(app, ["project", "group", "add", "frontend", "cli"])
    assert added.exit_code == 0 and _names(runner, "--group", "frontend") == ["web", "docs", "cli"]
    moved = runner.invoke(app, ["project", "move", "cli", "--before", "web"])
    assert moved.exit_code == 0, moved.output
    assert _names(runner, "--group", "frontend") == ["cli", "web", "docs"]
    out = runner.invoke(app, ["project", "move", "docs", "--to", "top", "--position", "0"])
    assert out.exit_code == 0 and _names(runner) == ["cli", "web", "docs", "api"]
    removed = runner.invoke(app, ["project", "group", "remove", "cli"])
    assert removed.exit_code == 0 and _names(runner) == ["web", "docs", "api", "cli"]

    pinned = runner.invoke(app, ["project", "pin", "api"])
    assert pinned.exit_code == 0 and _names(runner) == ["api", "web", "docs", "cli"]
    assert _names(runner, "--pinned") == ["api"]
    table = runner.invoke(app, ["project", "list"])
    assert "📌" in table.stdout and "GROUP" in table.stdout and "frontend" in table.stdout
    unpinned = runner.invoke(app, ["project", "unpin", "api"])
    assert unpinned.exit_code == 0 and _names(runner, "--pinned") == []

    renamed = runner.invoke(app, ["project", "group", "rename", "frontend", "ui"])
    assert renamed.exit_code == 0
    dup = runner.invoke(app, ["project", "group", "create", "UI"])
    assert dup.exit_code == 1 and "already exists" in dup.output
    second = runner.invoke(app, ["project", "group", "create", "tools"])
    assert second.exit_code == 0
    reordered = runner.invoke(app, ["project", "group", "move", "tools", "--before", "ui"])
    assert reordered.exit_code == 0
    listing = runner.invoke(app, ["--json", "project", "group", "list"])
    payload = json.loads(listing.stdout)
    assert [g["name"] for g in payload["groups"]] == ["tools", "ui"]
    assert [m["name"] for m in payload["groups"][1]["members"]] == ["web"]
    plain = runner.invoke(app, ["project", "group", "list"])
    assert "ui: web" in plain.stdout and "tools: —" in plain.stdout

    deleted = runner.invoke(app, ["project", "group", "delete", "ui"])
    assert deleted.exit_code == 0 and "its projects are back at the top level" in deleted.stdout
    assert _names(runner) == ["docs", "api", "cli", "web"]  # ungrouped to the end, no project lost
    assert runner.invoke(app, ["project", "group", "delete", "ui"]).exit_code == 1


def test_onboard_into_a_group_creates_it_when_new(
    runner: CliRunner, projects: dict[str, str], tmp_path: Path
) -> None:
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    result = runner.invoke(app, ["project", "onboard", str(fresh), "--group", "new-things"])
    assert result.exit_code == 0, result.output
    assert _names(runner, "--group", "new-things") == ["fresh"]
    again = runner.invoke(
        app, ["project", "onboard", str(tmp_path / "web"), "--group", "new-things"]
    )
    assert again.exit_code == 0, again.output
    assert _names(runner, "--group", "new-things") == ["fresh", "web"]
    listing = json.loads(runner.invoke(app, ["--json", "project", "group", "list"]).stdout)
    assert [g["name"] for g in listing["groups"]] == ["new-things"], "created once, reused after"
