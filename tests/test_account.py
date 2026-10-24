"""Sessions record which agent config dir (account) they run under.

With several parallel installs driving one board, a capped account's sessions
have to be identifiable — otherwise you can see *that* work stalled but not
which terminals to relaunch elsewhere.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.services.team import account_label, session_account
from tests.projects import add_project


@pytest.fixture
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    add_project(work)
    return work


def _start(runner: CliRunner, session_id: str, work: Path, transcript: str | None) -> Any:
    payload: dict[str, Any] = {"cwd": str(work), "session_id": session_id, "source": "startup"}
    if transcript is not None:
        payload["transcript_path"] = transcript
    return runner.invoke(app, ["hook", "session-start"], input=json.dumps(payload))


def _transcript(home: Path, account: str, session_id: str) -> str:
    return str(home / account / "projects" / "-some-repo" / f"{session_id}.jsonl")


def test_account_is_derived_from_the_transcript_path(tmp_path: Path) -> None:
    path = _transcript(tmp_path, ".claude-account1", "abc")
    assert session_account(path) == str(tmp_path / ".claude-account1")


def test_account_falls_back_to_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/from/env")
    assert session_account(None) == "/from/env"
    assert session_account("/unexpected/shape.jsonl") == "/from/env"


def test_account_is_none_when_nothing_identifies_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert session_account(None) is None


def test_account_label_is_the_directory_name() -> None:
    assert account_label("/Users/me/.claude-account1") == ".claude-account1"
    assert account_label(None) is None


def test_session_start_records_the_account(
    runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, "sess-1", work_dir, _transcript(tmp_path, ".claude-account1", "sess-1"))

    with store_session() as store:
        session = store.get_session("sess-1")
    assert session is not None
    assert session.account == str(tmp_path / ".claude-account1")


def test_the_board_names_accounts_only_when_several_are_in_play(
    runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, "sess-1", work_dir, _transcript(tmp_path, ".claude-account1", "sess-1"))

    single = runner.invoke(app, ["board"])
    assert ".claude-account1" not in single.output, "one account is just noise"

    _start(runner, "sess-2", work_dir, _transcript(tmp_path, ".claude-account2", "sess-2"))

    both = runner.invoke(app, ["board"])
    assert ".claude-account1" in both.output
    assert ".claude-account2" in both.output


def test_a_session_keeps_its_account_across_later_hooks(
    runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    transcript = _transcript(tmp_path, ".claude-account1", "sess-1")
    _start(runner, "sess-1", work_dir, transcript)
    # A later hook without a transcript path must not blank the account out.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    _start(runner, "sess-1", work_dir, None)

    with store_session() as store:
        session = store.get_session("sess-1")
    assert session is not None
    assert session.account == str(tmp_path / ".claude-account1")


def test_sessions_from_several_accounts_share_one_board(
    runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, "sess-1", work_dir, _transcript(tmp_path, ".claude-account1", "sess-1"))
    _start(runner, "sess-2", work_dir, _transcript(tmp_path, ".claude-account2", "sess-2"))

    with store_session() as store:
        sessions = store.team_sessions(team_project(work_dir).id)

    assert {session.account for session in sessions} == {
        str(tmp_path / ".claude-account1"),
        str(tmp_path / ".claude-account2"),
    }


def _fleet_row_on(work: Path, slot: int) -> str:
    """A live fleet row in ``work``'s project recorded as launched on account ``slot``."""
    from datetime import UTC, datetime

    from aisquare.core.ids import new_agent_id
    from aisquare.models import FleetAgent

    project = team_project(work)
    with store_session() as store:
        store.ensure_project(project)
        agent = store.upsert_fleet_agent(
            FleetAgent(
                id=new_agent_id(),
                project_id=project.id,
                label="coder-1",
                role="coder",
                pane_id="%1",
                cwd=work,
                account_slot=slot,
                created_at=datetime.now(tz=UTC),
            )
        )
    return agent.id


def test_a_fleet_session_is_attributed_to_the_slot_its_row_launched_on(
    runner: CliRunner,
    work_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_home: Path,
) -> None:
    """P2-W7 (#198): with sharing, a transcript can sit under a shared root (the plain
    claude's ``~/.claude``, which #198's workaround links every slot's ``projects/`` to).
    Read from the path, a session launched on slot 3 was attributed to the plain claude.
    The fleet row records the launch's account, so it is asked first."""
    from aisquare.core import claude_accounts

    for _ in range(2):
        claude_accounts.create_account()  # slots 2 and 3
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", _fleet_row_on(work_dir, 3))
    shared = fake_home / ".claude" / "projects" / "-some-repo" / "sess-9.jsonl"

    _start(runner, "sess-9", work_dir, str(shared))

    with store_session() as store:
        session = store.get_session("sess-9")
    assert session is not None
    assert session.account == str(claude_accounts.find_account(3).config_dir)  # type: ignore[union-attr]


def test_without_a_row_the_launchs_variable_comes_before_the_transcript_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The launch sets CLAUDE_CONFIG_DIR for the account it chose; a transcript under a
    shared root names another directory."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/accounts/3")
    assert session_account(_transcript(tmp_path, ".claude", "abc")) == "/accounts/3"
