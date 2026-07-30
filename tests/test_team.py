"""The agent orchestrator: sessions, idempotent tasks, atomic claims and the event pipe."""

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
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
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
    runner.invoke(app, ["note", "kickoff", "--kind", "decision", "--as", "aaaa1111"])
    result = runner.invoke(app, ["--json", "team", "log"])
    envelopes = json.loads(result.stdout)
    assert envelopes and envelopes[-1]["kind"] == "team.decision"
    assert envelopes[-1]["scope"] == "project"


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


# --- the loop workflow: next / review / reopen ----------------------------------


def test_looped_pickup_review_and_reopen_cycle(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, CODER, work_dir)
    monkeypatch.setenv("AISQUARE_ROLE", "runner")
    _start(runner, PLANNER, work_dir)  # reuse the id as the runner session
    monkeypatch.delenv("AISQUARE_ROLE")
    runner.invoke(app, ["task", "add", "fix login flow", "--role", "coder"])

    # Coder's loop: pick up and claim the next coder task atomically.
    picked = json.loads(
        runner.invoke(
            app, ["--json", "task", "next", "--role", "coder", "--claim", "--as", "bbbb2222"]
        ).stdout
    )
    assert picked["status"] == "doing" and picked["claimed_by"] == CODER

    # Nothing left in the todo pool for another looper.
    empty = json.loads(
        runner.invoke(app, ["--json", "task", "next", "--role", "coder", "--claim"]).stdout
    )
    assert empty is None

    # Coder finishes → review; runner picks it from the review pool.
    runner.invoke(app, ["task", "review", picked["id"], "--note", "try login", "--as", "bbbb2222"])
    for_runner = json.loads(
        runner.invoke(app, ["--json", "task", "next", "--status", "review"]).stdout
    )
    assert for_runner["id"] == picked["id"]

    # Verification fails → reopen with feedback; coder's next prompt carries it.
    runner.invoke(
        app, ["task", "reopen", picked["id"], "--reason", "500 on submit", "--as", "aaaa1111"]
    )
    reopened = json.loads(runner.invoke(app, ["--json", "task", "list"]).stdout)[0]
    assert reopened["status"] == "todo" and reopened["claimed_by"] is None
    delta = _prompt(runner, CODER, work_dir)
    assert "reopened" in delta.stdout and "500 on submit" in delta.stdout


def test_next_respects_role_hints(runner: CliRunner, work_dir: Path) -> None:
    runner.invoke(app, ["task", "add", "runner-only job", "--role", "runner"])
    # A coder looper skips tasks hinted at another role...
    for_coder = json.loads(runner.invoke(app, ["--json", "task", "next", "--role", "coder"]).stdout)
    assert for_coder is None
    # ...the runner picks it up, and role-less tasks are open to everyone.
    for_runner = json.loads(
        runner.invoke(app, ["--json", "task", "next", "--role", "runner"]).stdout
    )
    assert for_runner is not None and for_runner["title"] == "runner-only job"
    runner.invoke(app, ["task", "add", "anyone can take this"])
    for_coder_now = json.loads(
        runner.invoke(app, ["--json", "task", "next", "--role", "coder"]).stdout
    )
    assert for_coder_now is not None and for_coder_now["title"] == "anyone can take this"


def test_claim_flag_rejected_outside_todo(runner: CliRunner, work_dir: Path) -> None:
    result = runner.invoke(app, ["task", "next", "--status", "review", "--claim"])
    assert result.exit_code == 2  # usage error


def test_v3_database_migrates_to_v4(work_dir: Path) -> None:
    import sqlite3 as raw_sqlite

    from aisquare.core import paths

    # Build a v3-era database by hand: v1+v2+v3 without the v4 rebuild.
    from aisquare.core.store import (
        _MIGRATIONS,
        SCHEMA_VERSION,
        open_store,
    )

    paths.ensure_home()
    conn = raw_sqlite.connect(str(paths.db_path()))
    for script in _MIGRATIONS[:3]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 3")
    now = datetime.now(tz=UTC).isoformat()
    conn.execute(
        "INSERT INTO team_task (id, project_id, key, title, status, created_at, updated_at) "
        "VALUES ('tsk_old', 'prj_x', 'legacy', 'legacy task', 'doing', ?, ?)",
        (now, now),
    )
    conn.commit()
    conn.close()

    store = open_store()  # migrates to v4
    try:
        survivor = store.get_task("tsk_old")
        assert survivor is not None and survivor.status == "doing"
        store.set_task_status("tsk_old", "review")  # new status is legal post-rebuild
    finally:
        store.close()
    check = raw_sqlite.connect(str(paths.db_path()))
    try:
        assert check.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        check.close()


def test_team_hub_pins_every_directory_to_one_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub = tmp_path / "studio-unified"
    repo_a = tmp_path / "backend"
    repo_b = tmp_path / "ui"
    for directory in (hub, repo_a, repo_b):
        directory.mkdir()
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub))
    assert team_project(repo_a).id == team_project(repo_b).id == team_project(hub).id
    assert team_project(repo_a).root == hub.resolve()


def test_running_session_auto_joins_on_prompt_after_activation(
    runner: CliRunner, work_dir: Path
) -> None:
    # Session starts BEFORE the orchestrator is active: silent, unregistered.
    assert _start(runner, CODER, work_dir).stdout.strip() == ""
    # The orchestrator activates afterwards (e.g. someone ran `team on`).
    runner.invoke(app, ["team", "on"])
    # Its next prompt registers it and delivers the full board + protocol.
    first = _prompt(runner, CODER, work_dir)
    assert "<aisquare-team>" in first.stdout
    assert "You are team session bbbb2222" in first.stdout
    # After that, it behaves like any registered session (quiet when quiet).
    assert _prompt(runner, CODER, work_dir).stdout.strip() == ""


def test_role_cycles_are_injected_automatically(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    coder = _start(runner, CODER, work_dir)
    assert "Your standing cycle (coder)" in coder.stdout
    assert "task next --role coder --claim --as bbbb2222" in coder.stdout
    monkeypatch.setenv("AISQUARE_ROLE", "runner")
    run = _start(runner, PLANNER, work_dir)
    assert "Your standing cycle (runner)" in run.stdout
    assert "task next --status review" in run.stdout


def test_watch_frame_adapts_events_to_terminal_height(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.cli.watch import board_frame as _board_frame

    monkeypatch.setenv("AISQUARE_ROLE", "planner")
    _start(runner, PLANNER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    for index in range(20):
        runner.invoke(app, ["note", f"update number {index}"])
    tall = str(_board_frame(height=40, width=100))
    short = str(_board_frame(height=12, width=100))
    assert "aisquare board" in tall and "planner" in tall
    assert tall.count("update number") > short.count("update number")
    assert "update number 19" in short  # newest events always survive the cut


def test_watch_rejects_json_mode(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--json", "board", "--watch"])
    assert result.exit_code == 2


def test_session_state_tracks_working_waiting_attention(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, CODER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")

    def state() -> str:
        with store_session() as store:
            session = store.get_session(CODER)
            assert session is not None
            return session.state

    _prompt(runner, CODER, work_dir)
    assert state() == "working"
    payload = json.dumps({"cwd": str(work_dir), "session_id": CODER})
    runner.invoke(app, ["hook", "stop"], input=payload)
    assert state() == "waiting"
    notif = json.dumps(
        {"cwd": str(work_dir), "session_id": CODER, "message": "permission needed for Bash"}
    )
    runner.invoke(app, ["hook", "notification"], input=notif)
    assert state() == "attention"
    from aisquare.services import team as team_service

    events = team_service.log_events(work_dir)
    assert any(e.kind == "attention" and "permission needed" in e.text for e in events)
    _prompt(runner, CODER, work_dir)  # answering brings it back to working
    assert state() == "working"


def test_session_churn_stays_off_the_feed(runner: CliRunner, work_dir: Path) -> None:
    # /clear cycles and ephemeral `claude -p` children produce start/end
    # churn; presence belongs to the board panel, never the feed.
    runner.invoke(app, ["team", "on"])
    for index in range(3):
        sid = f"eeee{index}{index}{index}{index}-0000-0000-0000-000000000000"
        _start(runner, sid, work_dir)
        runner.invoke(
            app,
            ["hook", "session-end"],
            input=json.dumps({"cwd": str(work_dir), "session_id": sid}),
        )
    from aisquare.services import team as team_service

    kinds = {event.kind for event in team_service.log_events(work_dir)}
    assert "join" not in kinds and "end" not in kinds
    assert kinds == {"activate"}


def test_release_cannot_resurrect_finished_tasks(runner: CliRunner, work_dir: Path) -> None:
    runner.invoke(app, ["team", "on"])
    runner.invoke(app, ["task", "add", "one and done"])
    task_id = json.loads(runner.invoke(app, ["--json", "task", "list"]).stdout)[0]["id"]
    runner.invoke(app, ["task", "claim", task_id])
    runner.invoke(app, ["task", "done", task_id])
    done = json.loads(runner.invoke(app, ["--json", "task", "show", task_id]).stdout)
    assert done["claimed_by"] is None  # terminal statuses clear the claim
    released = runner.invoke(app, ["task", "release", task_id])
    assert released.exit_code == 1  # done → release refused, no zombie todo
    still = json.loads(runner.invoke(app, ["--json", "task", "show", task_id]).stdout)
    assert still["status"] == "done"
    # reopen IS the deliberate resurrection path.
    reopened = runner.invoke(app, ["task", "reopen", task_id, "--reason", "regressed"])
    assert reopened.exit_code == 0
    assert (
        json.loads(runner.invoke(app, ["--json", "task", "show", task_id]).stdout)["status"]
        == "todo"
    )


def test_cross_project_needs_is_rejected(
    runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.invoke(app, ["team", "on"])
    runner.invoke(app, ["task", "add", "task in project A"])
    foreign = json.loads(runner.invoke(app, ["--json", "task", "list"]).stdout)[0]["id"]
    other = tmp_path / "other-repo"
    other.mkdir()
    monkeypatch.chdir(other)
    runner.invoke(app, ["team", "on"])
    result = runner.invoke(app, ["task", "add", "dependent", "--needs", foreign])
    assert result.exit_code == 1, result.output  # rejected loudly, not starved silently


def test_attention_events_dedupe_and_stay_out_of_deltas(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "planner")
    _start(runner, PLANNER, work_dir)
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, CODER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    notif = json.dumps({"cwd": str(work_dir), "session_id": CODER, "message": "waiting"})
    for _ in range(4):  # Claude re-notifies while parked
        runner.invoke(app, ["hook", "notification"], input=notif)
    from aisquare.services import team as team_service

    kinds = [e.kind for e in team_service.log_events(work_dir)]
    assert kinds.count("attention") == 1  # transition-only, not per notice
    runner.invoke(app, ["note", "real work item", "--as", "bbbb2222"])
    delta = _prompt(runner, PLANNER, work_dir)
    assert "real work item" in delta.stdout
    assert "attention" not in delta.stdout  # human-board signal, not agent context


def test_concurrent_first_opens_migrate_safely(work_dir: Path) -> None:
    import threading

    from aisquare.core.store import SCHEMA_VERSION, store_session

    errors: list[Exception] = []
    barrier = threading.Barrier(6)

    def opener() -> None:
        barrier.wait()
        try:
            with store_session() as store:
                store.entries("user")
        except Exception as exc:  # pragma: no cover - the assertion is below
            errors.append(exc)

    threads = [threading.Thread(target=opener) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    import sqlite3 as raw

    from aisquare.core import paths

    conn = raw.connect(str(paths.db_path()))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_concurrent_notifications_emit_one_attention_event(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, CODER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    import threading

    from aisquare.services import team as team_service

    barrier = threading.Barrier(4)

    def notify() -> None:
        barrier.wait()
        team_service.hook_notification(CODER, work_dir, "permission needed")

    threads = [threading.Thread(target=notify) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    kinds = [e.kind for e in team_service.log_events(work_dir)]
    assert kinds.count("attention") == 1  # atomic transition: one event, not four


def test_task_id_tail_resolves(runner: CliRunner, work_dir: Path) -> None:
    runner.invoke(app, ["team", "on"])
    runner.invoke(app, ["task", "add", "find me by tail"])
    task_id = json.loads(runner.invoke(app, ["--json", "task", "list"]).stdout)[0]["id"]
    shown = json.loads(runner.invoke(app, ["--json", "task", "show", task_id[-8:]]).stdout)
    assert shown["id"] == task_id  # boards display the tail; the tail must resolve


def test_needs_error_blames_the_needs_ref(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, CODER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    result = runner.invoke(
        app, ["--json", "task", "add", "x", "--needs", "tsk_nope", "--as", "bbbb2222"]
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["ref"] == "tsk_nope"  # not the valid --as session


def test_old_format_mcp_sessions_are_retired_by_v8(work_dir: Path) -> None:
    import sqlite3 as raw

    from aisquare.core import paths
    from aisquare.core.store import _MIGRATIONS, SCHEMA_VERSION, open_store

    paths.ensure_home()
    conn = raw.connect(str(paths.db_path()))
    for script in _MIGRATIONS[:7]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 7")
    now = "2026-07-06T00:00:00+00:00"
    conn.execute(
        "INSERT INTO team_session (id, project_id, started_at, last_seen_at) "
        "VALUES ('mcp:remote', 'prj_x', ?, ?), ('mcp:remote:abc123', 'prj_x', ?, ?)",
        (now, now, now, now),
    )
    conn.commit()
    conn.close()
    store = open_store()
    try:
        # get_session prefix-matches, so check exact ids at the SQL level.
        exact = raw.connect(str(paths.db_path()))
        rows = {
            row[0]
            for row in exact.execute("SELECT id FROM team_session WHERE id LIKE 'mcp:%'").fetchall()
        }
        exact.close()
        assert rows == {"mcp:remote:abc123"}  # phantom retired, new format kept
    finally:
        store.close()
    check = raw.connect(str(paths.db_path()))
    try:
        assert check.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        check.close()


def test_terminal_events_returns_the_latest_closer_after_reopen(work_dir: Path) -> None:
    from aisquare.services import team as team_service

    team_service.activate(work_dir)
    task, _ = team_service.add_task("re-closed task", cwd=work_dir)
    team_service.finish_task(task.id, note="first close")
    team_service.reopen_task(task.id, reason="regressed")
    team_service.finish_task(task.id, note="second close")
    from aisquare.core.orchestrator import team_project
    from aisquare.core.store import store_session

    with store_session() as store:
        terminal = store.terminal_events(team_project(work_dir).id)
    # One entry per task, and it is the LATEST close (highest seq), not the first.
    assert task.id in terminal
    latest = terminal[task.id]
    all_done = [e for e in team_service.log_events(work_dir) if e.kind == "task_done"]
    assert latest.seq == max(e.seq for e in all_done)


# --- shared session row (issue #37) -------------------------------------------


def _start_tp(runner: CliRunner, session_id: str, work: Path, transcript: str) -> Any:
    payload = json.dumps(
        {
            "cwd": str(work),
            "session_id": session_id,
            "source": "startup",
            "transcript_path": transcript,
        }
    )
    return runner.invoke(app, ["hook", "session-start"], input=payload)


def _prompt_tp(runner: CliRunner, session_id: str, work: Path, transcript: str) -> Any:
    payload = json.dumps(
        {
            "cwd": str(work),
            "session_id": session_id,
            "prompt": "go",
            "transcript_path": transcript,
        }
    )
    return runner.invoke(app, ["hook", "user-prompt-submit"], input=payload)


def test_two_agents_on_one_session_row_are_warned(work_dir: Path) -> None:
    """The collision issue #37 was filed for: two live agents, one identity.

    Without this, both agents read the same short id as their own, every event
    either writes is stamped with it, and attribution is unrecoverable
    afterwards. transcript_path is the only per-agent value the row carries and
    upsert_session overwrites it last-writer-wins, so nothing else can tell.
    """
    runner = CliRunner()
    runner.invoke(app, ["team", "on"])
    sid = "5c7beebf-a5c2-4ae0-a6b4-342f284947cd"

    first = _start_tp(runner, sid, work_dir, "/transcripts/agent-a.jsonl")
    assert "session-collision" not in first.output, "the first agent has nobody to collide with"

    second = _start_tp(runner, sid, work_dir, "/transcripts/agent-B.jsonl")
    assert "aisquare-session-collision" in second.output
    assert "/transcripts/agent-a.jsonl" in second.output, "must name the OTHER transcript"
    assert "/transcripts/agent-B.jsonl" in second.output, "and this one, so they can be told apart"
    assert "Nothing has been reassigned" in second.output, "warn, never reroute"


def test_the_same_agent_reconnecting_is_not_a_collision(work_dir: Path) -> None:
    """Same id, same transcript: a /clear or resume, not a second agent.

    A banner here would fire on ordinary restarts and get tuned out, which
    costs more than the warning is worth.
    """
    runner = CliRunner()
    runner.invoke(app, ["team", "on"])
    sid = "11111111-2222-3333-4444-555555555555"

    _start_tp(runner, sid, work_dir, "/transcripts/same.jsonl")
    again = _start_tp(runner, sid, work_dir, "/transcripts/same.jsonl")
    assert "session-collision" not in again.output


def test_a_stale_row_with_a_new_transcript_is_a_resume_not_a_collision(work_dir: Path) -> None:
    """The false-positive guard, and the reason the check is time-boxed.

    Reusing a session id days later with a fresh conversation is normal. Only a
    row heartbeat from a DIFFERENT transcript INSIDE the freshness window means
    another agent is live on it right now.
    """
    runner = CliRunner()
    runner.invoke(app, ["team", "on"])
    sid = "99999999-8888-7777-6666-555555555555"
    _start_tp(runner, sid, work_dir, "/transcripts/old.jsonl")

    with store_session() as store:
        session = store.get_session(sid)
        assert session is not None
        # Age the row through the public upsert rather than reaching into the
        # connection: ON CONFLICT sets last_seen_at from the incoming row, so this
        # is the supported way to say "this session was last seen long ago".
        # Same transcript as the original, so the staleness is the only variable.
        store.upsert_session(
            TeamSession(
                id=sid,
                project_id=session.project_id,
                role=session.role,
                started_at=session.started_at,
                last_seen_at=datetime.now(tz=UTC) - timedelta(minutes=31),
                cursor=session.cursor,
                transcript_path="/transcripts/old.jsonl",
            )
        )

    resumed = _start_tp(runner, sid, work_dir, "/transcripts/new.jsonl")
    assert "session-collision" not in resumed.output


def test_the_warning_survives_an_empty_teammate_delta(work_dir: Path) -> None:
    """A heartbeat with no teammate traffic returns "" -- the banner must not go with it.

    The quiet case is exactly when interleaved claims do their damage, so a
    warning that only rides along with unrelated delta traffic would be missing
    for as long as it mattered most.
    """
    runner = CliRunner()
    runner.invoke(app, ["team", "on"])
    sid = "abcdabcd-1111-2222-3333-444444444444"
    _start_tp(runner, sid, work_dir, "/transcripts/agent-a.jsonl")

    quiet = _prompt_tp(runner, sid, work_dir, "/transcripts/agent-B.jsonl")
    assert "aisquare-session-collision" in quiet.output
