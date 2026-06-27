"""End-to-end CLI behaviour for the project group (and the workspace alias)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Default to a neutral temp working directory; tests may chdir elsewhere."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return workdir


def _json(output: str) -> Any:
    return json.loads(output)


def test_info_describes_the_cwd_project(runner: CliRunner, work_dir: Path) -> None:
    result = runner.invoke(app, ["--json", "project", "info"])
    assert result.exit_code == 0, result.output
    assert _json(result.stdout)["root"] == str(work_dir.resolve())


def test_list_empty_then_registered(runner: CliRunner) -> None:
    empty = runner.invoke(app, ["project", "list"])
    assert "No projects registered yet" in empty.stdout
    runner.invoke(app, ["context", "add", "a note", "--project"])  # registers the cwd project
    listed = runner.invoke(app, ["--json", "project", "list"])
    assert len(_json(listed.stdout)) == 1


def test_switch_pins_the_active_project_across_directories(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha, beta = tmp_path / "alpha", tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    monkeypatch.chdir(alpha)
    runner.invoke(app, ["context", "add", "alpha note", "--project"])
    monkeypatch.chdir(beta)
    runner.invoke(app, ["context", "add", "beta note", "--project"])

    switched = runner.invoke(app, ["project", "switch", "alpha"])
    assert switched.exit_code == 0, switched.output
    assert "switched to alpha" in switched.stdout

    # The pin wins over cwd (still in beta): info and context scope to alpha.
    info = runner.invoke(app, ["--json", "project", "info"])
    assert _json(info.stdout)["root"].endswith("alpha")
    listed = runner.invoke(app, ["--json", "context", "list"])
    texts = {entry["text"] for entry in _json(listed.stdout)}
    assert texts == {"alpha note"}


def test_switch_unknown_fails(runner: CliRunner) -> None:
    result = runner.invoke(app, ["project", "switch", "ghost"])
    assert result.exit_code == 1
    assert "no project matches" in result.output


def test_switch_ambiguous_fails(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "x" / "app", tmp_path / "y" / "app"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    for directory in (first, second):
        monkeypatch.chdir(directory)
        runner.invoke(app, ["context", "add", "note", "--project"])
    result = runner.invoke(app, ["project", "switch", "app"])
    assert result.exit_code == 1
    assert "matches multiple projects" in result.output


def test_link_adds_a_repo_to_the_active_project(runner: CliRunner) -> None:
    result = runner.invoke(app, ["project", "link", "git@github.com:acme/app.git"])
    assert result.exit_code == 0, result.output
    info = runner.invoke(app, ["--json", "project", "info"])
    assert _json(info.stdout)["linked_repos"] == ["git@github.com:acme/app.git"]


def test_onboard_seeds_from_ecosystem_markers(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = tmp_path / "pyproj"
    proj.mkdir()
    (proj / "pyproject.toml").touch()
    monkeypatch.chdir(proj)

    seeded = runner.invoke(app, ["--json", "project", "onboard"])
    assert seeded.exit_code == 0, seeded.output
    texts = [entry["text"] for entry in _json(seeded.stdout)]
    assert any("Python project" in text for text in texts)

    # Already onboarded: a second run without --refresh seeds nothing.
    again = runner.invoke(app, ["--json", "project", "onboard"])
    assert _json(again.stdout) == []

    listed = runner.invoke(app, ["--json", "context", "list"])
    assert any("Python project" in e["text"] for e in _json(listed.stdout))


def test_workspace_alias_works(runner: CliRunner, work_dir: Path) -> None:
    result = runner.invoke(app, ["--json", "workspace", "info"])
    assert result.exit_code == 0, result.output
    assert _json(result.stdout)["root"] == str(work_dir.resolve())
