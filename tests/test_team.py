"""The team bus: sessions, idempotent tasks, atomic claims and the event pipe."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.ids import new_task_id
from aisquare.core.store import store_session
from aisquare.core.teambus import team_project
from aisquare.models import TeamSession, TeamTask

PLANNER = "aaaa1111-0000-0000-0000-000000000000"
CODER = "bbbb2222-0000-0000-0000-000000000000"


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def _start(runner: CliRunner, session_id: str, work: Path, source: str = "startup") -> Any:
    payload = json.dumps({"cwd": str(work), "session_id": session_id, "source": source})
    return runner.invoke(app, ["hook", "session-start"], input=payload)


def _prompt(runner: CliRunner, session_id: str, work: Path, text: str = "go") -> Any:
    payload = json.dumps({"cwd": str(work), "session_id": session_id, "prompt": text})
    return runner.invoke(app, ["hook", "user-prompt-submit"], input=payload)


def _task(project_id: str, title: str, key: str) -> TeamTask:
    now = datetime.now(tz=UTC)
    return TeamTask(
        id=new_task_id(),
        project_id=project_id,
        key=key,
        title=title,
        created_at=now,
        updated_at=now,
    )


# --- store primitives ---------------------------------------------------------


def test_task_add_is_idempotent_on_key(work_dir: Path) -> None:
    project = team_project(work_dir)
    with store_session() as store:
        store.ensure_project(project)
        first, created_first = store.upsert_task(_task(project.id, "wire auth", "wire-auth"))
        again, created_again = store.upsert_task(_task(project.id, "wire auth", "wire-auth"))
    assert created_first and not created_again
    assert again.id == first.id


def test_concurrent_claims_have_exactly_one_winner(work_dir: Path) -> None:
    project = team_project(work_dir)
    with store_session() as store:
        store.ensure_project(project)
        task, _ = store.upsert_task(_task(project.id, "hot potato", "hot-potato"))
    lease = datetime.now(tz=UTC) + timedelta(minutes=60)
    wins: list[str] = []
    barrier = threading.Barrier(8)

    def contender(name: str) -> None:
        barrier.wait()
        with store_session() as store:
            if store.claim_task(task.id, name, lease):
                wins.append(name)

    threads = [threading.Thread(target=contender, args=(f"s{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(wins) == 1, wins


def test_expired_lease_makes_a_task_claimable_again(work_dir: Path) -> None:
    project = team_project(work_dir)
    with store_session() as store:
        store.ensure_project(project)
        task, _ = store.upsert_task(_task(project.id, "stale claim", "stale-claim"))
        expired = datetime.now(tz=UTC) - timedelta(minutes=1)
        assert store.claim_task(task.id, "dead-session", expired)
        live = datetime.now(tz=UTC) + timedelta(minutes=60)
        assert store.claim_task(task.id, "live-session", live)
        claimed = store.get_task(task.id)
        assert claimed is not None and claimed.claimed_by == "live-session"
        # ... but a live lease cannot be stolen.
        assert not store.claim_task(task.id, "thief", live)


def test_events_since_excludes_own_events(work_dir: Path) -> None:
    project = team_project(work_dir)
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.ensure_project(project)
        for sid in (PLANNER, CODER):
            store.upsert_session(
                TeamSession(id=sid, project_id=project.id, started_at=now, last_seen_at=now)
            )
        from aisquare.core.ids import new_event_id
        from aisquare.models import TeamEvent

        for sid, text in ((PLANNER, "mine"), (CODER, "theirs")):
            store.add_team_event(
                TeamEvent(
                    id=new_event_id(),
                    project_id=project.id,
                    session_id=sid,
                    text=text,
                    created_at=now,
                )
            )
        events = store.events_since(project.id, 0, exclude_session=PLANNER)
    assert [event.text for event in events] == ["theirs"]


# --- hooks: activation, board, delta, end --------------------------------------


def test_session_start_is_silent_until_the_project_opts_in(
    runner: CliRunner, work_dir: Path
) -> None:
    result = _start(runner, PLANNER, work_dir)
    assert result.exit_code == 0
    assert result.stdout.strip() == ""


def test_env_role_activates_and_injects_the_board(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "planner")
    result = _start(runner, PLANNER, work_dir)
    assert "<aisquare-team>" in result.stdout
    assert "(role: planner)" in result.stdout
    assert "aaaa1111" in result.stdout  # the short id agents pass via --as


def test_second_session_auto_joins_an_active_project(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "planner")
    _start(runner, PLANNER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    result = _start(runner, CODER, work_dir)
    assert "<aisquare-team>" in result.stdout
    assert "planner" in result.stdout  # sees its teammate


def test_prompt_submit_delivers_teammate_delta_once(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "planner")
    _start(runner, PLANNER, work_dir)
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, CODER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    runner.invoke(app, ["task", "add", "wire auth", "--as", "bbbb2222"])

    first = _prompt(runner, PLANNER, work_dir)
    assert "<aisquare-team-delta>" in first.stdout
    assert "wire auth" in first.stdout
    second = _prompt(runner, PLANNER, work_dir)  # nothing new → nothing injected
    assert second.stdout.strip() == ""


def test_delta_can_be_muted_per_session(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "planner")
    _start(runner, PLANNER, work_dir)
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, CODER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    runner.invoke(app, ["task", "add", "wire auth", "--as", "bbbb2222"])
    monkeypatch.setenv("AISQUARE_TEAM_DELTA", "0")
    result = _prompt(runner, PLANNER, work_dir)
    assert result.stdout.strip() == ""


def test_session_end_releases_claims(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, CODER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    runner.invoke(app, ["task", "add", "wire auth", "--as", "bbbb2222"])
    listed = json.loads(runner.invoke(app, ["--json", "task", "list"]).stdout)
    runner.invoke(app, ["task", "claim", listed[0]["id"], "--as", "bbbb2222"])

    payload = json.dumps({"cwd": str(work_dir), "session_id": CODER})
    end = runner.invoke(app, ["hook", "session-end"], input=payload)
    assert end.exit_code == 0
    after = json.loads(runner.invoke(app, ["--json", "task", "list"]).stdout)
    assert after[0]["status"] == "todo"
    assert after[0]["claimed_by"] is None


def test_master_switch_disables_everything(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_TEAM", "0")
    monkeypatch.setenv("AISQUARE_ROLE", "planner")
    start = _start(runner, PLANNER, work_dir)
    assert start.stdout.strip() == ""
    board = runner.invoke(app, ["board"])
    assert board.exit_code == 1


def test_hooks_survive_garbage_payloads(runner: CliRunner) -> None:
    for command in ("session-start", "user-prompt-submit", "session-end"):
        result = runner.invoke(app, ["hook", command], input="not json")
        assert result.exit_code == 0, command


# --- commands -------------------------------------------------------------------


def test_claim_conflict_reports_the_holder(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "planner")
    _start(runner, PLANNER, work_dir)
    _start(runner, CODER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    runner.invoke(app, ["task", "add", "hot potato", "--as", "aaaa1111"])
    task_id = json.loads(runner.invoke(app, ["--json", "task", "list"]).stdout)[0]["id"]
    assert runner.invoke(app, ["task", "claim", task_id, "--as", "aaaa1111"]).exit_code == 0
    lost = runner.invoke(app, ["--json", "task", "claim", task_id, "--as", "bbbb2222"])
    assert lost.exit_code == 1
    assert json.loads(lost.stdout)["error"] == "claim_lost"


def test_note_flows_to_teammates(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "planner")
    _start(runner, PLANNER, work_dir)
    monkeypatch.setenv("AISQUARE_ROLE", "runner")
    _start(runner, CODER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    note = runner.invoke(
        app,
        ["note", "tests fail on py3.11", "--as", "bbbb2222", "--to", "planner", "--kind", "result"],
    )
    assert note.exit_code == 0, note.output
    delta = _prompt(runner, PLANNER, work_dir)
    assert "tests fail on py3.11" in delta.stdout
    assert "→ planner" in delta.stdout


def test_team_log_renders_envelopes_in_json(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "planner")
    _start(runner, PLANNER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    result = runner.invoke(app, ["--json", "team", "log"])
    envelopes = json.loads(result.stdout)
    assert envelopes and envelopes[0]["kind"] == "team.join"
    assert envelopes[0]["scope"] == "project"


# --- identity -------------------------------------------------------------------


def test_team_project_falls_back_without_git(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert team_project(plain).root == plain.resolve()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_worktrees_share_one_team_project(tmp_path: Path) -> None:
    main = tmp_path / "main"
    main.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
    }
    subprocess.run(["git", "init", "-q"], cwd=main, check=True, env=env)
    (main / "f").write_text("x")
    subprocess.run(["git", "add", "f"], cwd=main, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=main, check=True, env=env)
    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(worktree), "-b", "feature"],
        cwd=main,
        check=True,
        env=env,
    )
    assert team_project(worktree).id == team_project(main).id
