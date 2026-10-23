"""A tester spawned for one review verifies that one and stops; spawns run on the ladder.

#129 (a): `task next` had no `--task`, so a tester spawned for a task took its own first
and then "Repeat" drained the shared pool: two testers spawned for two reviews raced
for the rest. The manager now spawns one tester per task at its review, and that
tester's cycle names the task and stops after its verdict. The validator stays the one
final gate.

#129 (b): `fleet spawn` never asked `harness.resolve_model`, so every agent ran on the
account's default model, where `team spawn` launched the role's ladder.
"""

from __future__ import annotations

import json
import re
import shlex
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import harness
from aisquare.models import TeamSession
from aisquare.services import team as team_service
from tests.cycles import unscoped
from tests.projects import add_project

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)
    add_project(root)
    return root


def _in_review(work: Path, *titles: str) -> list[str]:
    ids = []
    for title in titles:
        task, _ = team_service.add_task(title, role="coder", cwd=work)
        team_service.review_task(task.id, note="ready")
        ids.append(task.id)
    return ids


def test_task_next_task_returns_that_task_and_nothing_else(work: Path) -> None:
    older, mine = _in_review(work, "older review", "my review")
    picked = team_service.next_task(status="review", task_ref=mine)
    assert picked is not None and picked.id == mine, "not the oldest: the one named"
    assert team_service.next_task(status="review", task_ref=f"#{picked.number}") == picked

    todo, _ = team_service.add_task("still todo", role="coder", cwd=work)
    assert team_service.next_task(status="review", task_ref=todo.id) is None, "not in review"
    unnamed = team_service.next_task(status="review")
    assert unnamed is not None and unnamed.id == older, "without --task: the pool, oldest first"


def test_task_next_task_on_the_command_line(work: Path) -> None:
    _older, mine = _in_review(work, "older review", "my review")
    runner = CliRunner()
    result = runner.invoke(app, ["--json", "task", "next", "--status", "review", "--task", mine])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["id"] == mine
    missing = runner.invoke(app, ["task", "next", "--status", "review", "--task", "#999"])
    assert missing.exit_code != 0


@pytest.mark.parametrize("role", ["tester", "runner", "ui-tester"])
def test_a_tester_spawned_for_a_task_verifies_that_one_and_stops(role: str) -> None:
    pooled = " ".join(harness.role_cycle(role, "abcd1234", fleet=unscoped))
    assert "Repeat." in pooled and "--task" not in pooled

    own = " ".join(harness.role_cycle(role, "abcd1234", fleet=unscoped, task="12"))
    assert "`aisquare task next --status review --task 12 --as abcd1234`" in own
    assert "Repeat." not in own
    assert "After that one verdict, tell the user and stop: take no other task." in own


def test_a_spawned_for_testers_first_command_survives_the_shell(work: Path) -> None:
    """Review of #338: `--task #12` reads in a shell as `--task` and a comment, so the
    tester's first command lost its task and its `--as`. The briefing names the task bare."""
    task, _ = team_service.add_task("verify me", role="coder", cwd=work)
    assert task.number is not None
    me = TeamSession(
        id="abcd1234-0000", project_id=task.project_id, role="tester", started_at=NOW,
        last_seen_at=NOW,
    )  # fmt: skip
    cycle = " ".join(team_service._role_cycle(me, None, task))
    command = re.search(r"`(aisquare task next --status review --task [^`]+)`", cycle)
    assert command is not None, cycle
    words = shlex.split(command.group(1), comments=True)
    assert words[words.index("--task") + 1] == str(task.number) and "--as" in words


def test_the_manager_spawns_one_tester_per_task_and_the_validator_gates_once() -> None:
    manager = " ".join(harness.role_cycle("manager", "abcd1234", fleet=unscoped))
    assert "Spawn one tester per task at its task_review:" in manager
    assert "`aisquare fleet spawn tester --task <id> --as abcd1234`" in manager
    assert "the validator stays the single final gate" in manager
