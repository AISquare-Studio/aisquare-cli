"""``[projects] capture`` off, the default since 0.8.1: no project row unless one is added on
purpose (#139) — the board.

The rest of the call sites are tested beside their own: the hooks in tests/test_hooks.py
and tests/test_ci_augment.py, the shell in tests/test_ui_shell.py, the fleet in
tests/test_fleet_service.py, the MCP server in tests/test_serve.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import ProjectInfo
from aisquare.services import team as team_service


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def _rows() -> list[ProjectInfo]:
    with store_session() as store:
        return store.list_projects(all=True, include_forgotten=True)


# --- the board -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["note", "hello"],
        ["task", "add", "wire auth"],
        ["task", "next", "--claim"],
        ["team", "signal", "phase", "build"],
    ],
    ids=["note", "task-add", "task-claim", "signal"],
)
def test_a_board_write_where_nothing_was_added_is_refused_and_adds_nothing(
    runner: CliRunner, work_dir: Path, argv: list[str]
) -> None:
    result = runner.invoke(app, ["--json", *argv])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["error"] == "not_added" and payload["ref"] == str(work_dir.resolve())
    assert f"add it first: aisquare project onboard {work_dir.resolve()}" in payload["detail"]
    assert _rows() == [], "a refusal writes no row"


def test_the_refusal_says_what_to_run_where_a_person_reads_it(
    runner: CliRunner, work_dir: Path
) -> None:
    result = runner.invoke(app, ["note", "hello"])
    assert result.exit_code == 1
    assert "repo is not one of your projects — add it first: aisquare project onboard" in (
        result.output
    )


def test_a_board_read_where_nothing_was_added_is_empty_and_adds_nothing(
    runner: CliRunner,
) -> None:
    for argv in (["board"], ["task", "list"], ["log"], ["task", "next"]):
        result = runner.invoke(app, ["--json", *argv])
        assert result.exit_code == 0, (argv, result.output)
    assert _rows() == []


def test_team_on_adds_the_project_and_the_board_takes_writes(
    runner: CliRunner, work_dir: Path
) -> None:
    assert runner.invoke(app, ["team", "on"]).exit_code == 0
    assert runner.invoke(app, ["note", "hello"]).exit_code == 0
    with store_session() as store:
        [added] = store.list_projects()
    assert added.id == team_project(work_dir).id


def test_with_capture_on_a_board_write_captures_the_directory_unlisted(
    runner: CliRunner, work_dir: Path, project_capture: None
) -> None:
    assert runner.invoke(app, ["note", "hello"]).exit_code == 0
    with store_session() as store:
        assert [p.id for p in store.captured_projects()] == [team_project(work_dir).id]
        assert store.list_projects() == []


def test_a_role_session_where_nothing_was_added_joins_no_board(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``AISQUARE_ROLE`` activates a board, and still adds nothing: ``launch`` adds its
    project on purpose (``team on``); a bare ``AISQUARE_ROLE=coder claude`` does not."""
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    session = "dddd4444-0000-0000-0000-000000000000"
    payload = {"cwd": str(work_dir), "session_id": session}

    start = runner.invoke(app, ["hook", "session-start"], input=json.dumps(payload))
    prompt = runner.invoke(
        app, ["hook", "user-prompt-submit"], input=json.dumps({**payload, "prompt": "go"})
    )

    assert start.exit_code == 0 and start.stdout == ""
    assert prompt.exit_code == 0 and prompt.stdout == ""
    assert _rows() == []
    with store_session() as store:
        assert store.get_session(session) is None
    team_service.activate()  # what `aisquare launch coder` does first
    start = runner.invoke(app, ["hook", "session-start"], input=json.dumps(payload))
    assert "role: coder" in start.stdout


def test_team_spawn_adds_its_project_when_it_starts_the_session_not_when_it_prints(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--exec`` is ``launch`` by another door: the session it starts joins this board,
    so it adds the project as ``launch`` does. A printed line may never be run."""
    monkeypatch.setattr("aisquare.cli.team.shutil.which", lambda _name: "/usr/bin/claude")
    monkeypatch.setattr("aisquare.cli.team.os.execvpe", lambda *_args: None)

    printed = runner.invoke(app, ["team", "spawn", "coder", "--no-probe"])
    assert printed.exit_code == 0, printed.output
    assert _rows() == []

    started = runner.invoke(app, ["team", "spawn", "coder", "--no-probe", "--exec"])
    assert started.exit_code == 0, started.output
    with store_session() as store:
        assert [p.id for p in store.list_projects()] == [team_project(work_dir).id]
