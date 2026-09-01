"""``import claude-memory``: Claude Code's auto-memory files become pool entries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app

_USER_MEMORY = """---
name: prefers-pytest
description: Prefers pytest over unittest
metadata:
  type: user
---

Always reach for pytest.
"""

_PROJECT_MEMORY = """---
name: dual-workspace-stacks
description: "Two side-by-side stacks — dev and platform"
metadata:
  type: project
---

Ports: api :80 vs :8080. Never cross the projects.
"""


@pytest.fixture()
def claude_dir(tmp_path: Path) -> Path:
    """A fake ~/.claude/projects with one slug carrying two memories + index."""
    memory = tmp_path / "claude-projects" / "-home-work" / "memory"
    memory.mkdir(parents=True)
    (memory / "prefers-pytest.md").write_text(_USER_MEMORY, encoding="utf-8")
    (memory / "dual-workspace-stacks.md").write_text(_PROJECT_MEMORY, encoding="utf-8")
    (memory / "MEMORY.md").write_text("- [x](prefers-pytest.md) — index stub\n", encoding="utf-8")
    return tmp_path / "claude-projects"


def _entries(runner: CliRunner, *argv: str) -> Any:
    result = runner.invoke(app, ["--json", *argv])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_import_maps_types_and_skips_the_index(runner: CliRunner, claude_dir: Path) -> None:
    result = runner.invoke(app, ["import", "claude-memory", "--claude-dir", str(claude_dir)])
    assert result.exit_code == 0, result.output

    entries = _entries(runner, "context", "list")
    texts = {entry["text"].splitlines()[0] for entry in entries}
    assert "Prefers pytest over unittest" in texts
    assert "index stub" not in str(entries), "MEMORY.md is an index, not a memory"
    by_head = {entry["text"].splitlines()[0]: entry for entry in entries}
    assert "type:user" in by_head["Prefers pytest over unittest"]["tags"]


def test_import_is_idempotent(runner: CliRunner, claude_dir: Path) -> None:
    first = runner.invoke(
        app, ["--json", "import", "claude-memory", "--claude-dir", str(claude_dir)]
    )
    report = json.loads(first.stdout)
    assert len(report["imported"]) == 2
    second = runner.invoke(
        app, ["--json", "import", "claude-memory", "--claude-dir", str(claude_dir)]
    )
    report = json.loads(second.stdout)
    assert report["imported"] == []
    assert len(report["skipped"]) == 2


def test_project_memories_can_target_a_stream(runner: CliRunner, claude_dir: Path) -> None:
    runner.invoke(app, ["stream", "new", "platform"])
    result = runner.invoke(
        app,
        [
            "--json",
            "import",
            "claude-memory",
            "--claude-dir",
            str(claude_dir),
            "--stream",
            "platform",
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    pools = {entry["text"].splitlines()[0]: entry["pool"] for entry in report["imported"]}
    assert pools["Prefers pytest over unittest"] == "user", "user memories stay personal"
    assert pools["Two side-by-side stacks — dev and platform"] == "stream"


def test_missing_claude_dir_is_a_clean_error(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["import", "claude-memory", "--claude-dir", str(tmp_path / "nope")])
    assert result.exit_code == 1
    assert "no Claude Code projects directory" in result.output
