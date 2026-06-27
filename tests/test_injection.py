"""Context assembly (inject/preview) and the why explanation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.injection import build_block, load_last, record_injection
from aisquare.models import ContextEntry, Pool, ProjectInfo

PROJECT = ProjectInfo(id="prj_demo", root=Path("/tmp/demo"), linked_repos=[])


@pytest.fixture(autouse=True)
def project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return workdir


def _json(output: str) -> Any:
    return json.loads(output)


def _entry(text: str, pool: Pool, project_id: str | None = None) -> ContextEntry:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return ContextEntry(
        id=f"ctx_{text}",
        pool=pool,
        project_id=project_id,
        text=text,
        created_at=now,
        updated_at=now,
    )


# --- unit: block assembly ---------------------------------------------------


def test_build_block_groups_by_pool() -> None:
    entries = [
        _entry("prefer tabs", "user"),
        _entry("run make check", "project", PROJECT.id),
    ]
    block = build_block(entries, PROJECT)
    assert "## Your preferences" in block
    assert "- prefer tabs" in block
    assert "## Project: demo" in block  # from the root dir name
    assert "- run make check" in block


def test_build_block_empty() -> None:
    block = build_block([], PROJECT)
    assert "_No saved context yet._" in block


# --- unit: injection record -------------------------------------------------


def test_record_injection_round_trips() -> None:
    entries = [_entry("a", "user"), _entry("b", "project", PROJECT.id)]
    record = record_injection(entries, PROJECT)
    assert record.user_count == 1
    assert record.project_count == 1
    assert load_last() == record


def test_load_last_is_none_without_a_record() -> None:
    assert load_last() is None


# --- CLI: preview / inject / why --------------------------------------------


def test_preview_shows_in_scope_context(runner: CliRunner) -> None:
    runner.invoke(app, ["context", "add", "prefer tabs", "--user"])
    result = runner.invoke(app, ["context", "preview"])
    assert result.exit_code == 0, result.output
    assert "## Your preferences" in result.stdout
    assert "- prefer tabs" in result.stdout


def test_inject_emits_the_block(runner: CliRunner) -> None:
    runner.invoke(app, ["context", "add", "use uv", "--user"])
    result = runner.invoke(app, ["inject"])
    assert result.exit_code == 0, result.output
    assert "- use uv" in result.stdout


def test_inject_json_wraps_the_block(runner: CliRunner) -> None:
    runner.invoke(app, ["context", "add", "use uv", "--user"])
    result = runner.invoke(app, ["--json", "inject"])
    assert result.exit_code == 0, result.output
    assert "- use uv" in _json(result.stdout)["block"]


def test_why_without_injection(runner: CliRunner) -> None:
    result = runner.invoke(app, ["why"])
    assert result.exit_code == 0, result.output
    assert "No context has been injected yet" in result.stdout


def test_inject_then_why_reports_counts(runner: CliRunner) -> None:
    runner.invoke(app, ["context", "add", "global pref", "--user"])
    runner.invoke(app, ["context", "add", "local pref", "--project"])
    runner.invoke(app, ["inject"])
    result = runner.invoke(app, ["--json", "why"])
    assert result.exit_code == 0, result.output
    record = _json(result.stdout)
    assert record["user_count"] == 1
    assert record["project_count"] == 1
    assert len(record["entry_ids"]) == 2


def test_preview_does_not_record(runner: CliRunner) -> None:
    runner.invoke(app, ["context", "add", "global pref", "--user"])
    runner.invoke(app, ["context", "preview"])
    result = runner.invoke(app, ["why"])
    assert "No context has been injected yet" in result.stdout  # preview is side-effect-free
