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
from aisquare.core.store import ContextStore, store_session
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


def _stored_session(store: ContextStore, ref: str) -> TeamSession:
    session = store.get_session(ref)
    assert session is not None
    return session


def _stored_task(store: ContextStore, ref: str) -> TeamTask:
    task = store.get_task(ref)
    assert task is not None
    return task


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


def _put_session(
    store: Any, sid: str, project_id: str, *, idle_min: int, role: str = "coder"
) -> None:
    """Register a session whose last heartbeat was ``idle_min`` minutes ago."""
    seen = datetime.now(tz=UTC) - timedelta(minutes=idle_min)
    store.upsert_session(
        TeamSession(id=sid, project_id=project_id, role=role, started_at=seen, last_seen_at=seen)
    )


def test_prune_retires_ghosts_and_frees_long_dead_claims(work_dir: Path) -> None:
    from aisquare.services import team as team_service

    project = team_project(work_dir)
    with store_session() as store:
        store.ensure_project(project)
        # Past the CLAIM clock (4h), not merely past the presence clock (30m).
        _put_session(store, CODER, project.id, idle_min=5 * 60)
        _put_session(store, PLANNER, project.id, idle_min=1)  # warm
        task, _ = store.upsert_task(_task(project.id, "orphaned", "orphaned"))
        lease = datetime.now(tz=UTC) + timedelta(minutes=60)
        assert store.claim_task(task.id, CODER, lease)  # the ghost holds it (doing)

    report = team_service.prune_sessions()

    assert [entry.id for entry in report.pruned] == [CODER]  # only the ghost
    assert report.released_total == 1
    with store_session() as store:
        assert _stored_session(store, CODER).ended_at is not None  # ghost retired
        assert _stored_session(store, PLANNER).ended_at is None  # warm session spared
        freed = _stored_task(store, task.id)
        assert freed.status == "todo" and freed.claimed_by is None  # claim back in the pool


def test_prune_retires_a_quiet_session_but_keeps_its_claim(work_dir: Path) -> None:
    """#49 — presence and ownership retire on different clocks.

    For an agent, thirty minutes of silence is not idleness; it is one long
    tool call — a multi-PR review, a build, a fan-out of subagents. Retiring
    presence too eagerly is self-healing (the next heartbeat re-registers the
    session); releasing its claim is not, because a second agent then picks up
    work the first is still doing and both push.
    """
    from aisquare.services import team as team_service

    project = team_project(work_dir)
    with store_session() as store:
        store.ensure_project(project)
        _put_session(store, CODER, project.id, idle_min=90)  # quiet, not dead
        task, _ = store.upsert_task(_task(project.id, "mid-flight", "mid-flight"))
        lease = datetime.now(tz=UTC) + timedelta(minutes=60)
        assert store.claim_task(task.id, CODER, lease)

    report = team_service.prune_sessions()

    assert [entry.id for entry in report.pruned] == [CODER]  # presence still goes
    assert report.released_total == 0  # ...but the lane does NOT
    with store_session() as store:
        assert _stored_session(store, CODER).ended_at is not None
        held = _stored_task(store, task.id)
        assert held.status == "doing" and held.claimed_by == CODER


def test_prune_release_claims_orphans_at_the_presence_threshold(work_dir: Path) -> None:
    """The opt-in for a caller that KNOWS the sessions are dead."""
    from aisquare.services import team as team_service

    project = team_project(work_dir)
    with store_session() as store:
        store.ensure_project(project)
        _put_session(store, CODER, project.id, idle_min=90)
        task, _ = store.upsert_task(_task(project.id, "orphaned", "orphaned"))
        lease = datetime.now(tz=UTC) + timedelta(minutes=60)
        assert store.claim_task(task.id, CODER, lease)

    report = team_service.prune_sessions(release_claims=True)

    assert report.released_total == 1
    with store_session() as store:
        freed = _stored_task(store, task.id)
        assert freed.status == "todo" and freed.claimed_by is None


def test_prune_dry_run_reports_without_ending_anything(work_dir: Path) -> None:
    from aisquare.services import team as team_service

    project = team_project(work_dir)
    with store_session() as store:
        store.ensure_project(project)
        _put_session(store, CODER, project.id, idle_min=90)

    report = team_service.prune_sessions(dry_run=True)

    assert report.dry_run and [entry.id for entry in report.pruned] == [CODER]
    with store_session() as store:
        assert _stored_session(store, CODER).ended_at is None  # untouched


def test_prune_keep_spares_a_session(work_dir: Path) -> None:
    from aisquare.services import team as team_service

    project = team_project(work_dir)
    with store_session() as store:
        store.ensure_project(project)
        _put_session(store, CODER, project.id, idle_min=90)
        _put_session(store, PLANNER, project.id, idle_min=90)

    report = team_service.prune_sessions(keep=PLANNER)

    assert [entry.id for entry in report.pruned] == [CODER]
    with store_session() as store:
        assert _stored_session(store, PLANNER).ended_at is None  # explicitly kept
        assert _stored_session(store, CODER).ended_at is not None


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
    import sqlite3
    import threading

    from aisquare.core.store import SCHEMA_VERSION, is_locked_error, store_session

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
    # The invariant is SAFETY — no corruption, no half-applied schema, no
    # non-transient error, and a store that is usable when the storm passes.
    # A straggler that exhausts the (bounded, honest) retry budget because the
    # whole box is starved is not a store defect: open_store promises a bounded
    # wait then a clean locked error, never a lie.
    #
    # This DELIBERATELY no longer asserts that some opener won. That assertion
    # counted this test's own threads and compared them against contention that
    # is machine-wide: the retry budget is wall-clock, so on a box running
    # several suites at once every opener can be descheduled past it while the
    # lock itself was free. It made a claim about the MACHINE while reading as
    # a claim about the STORE, and it fired on a database that was merely busy.
    # "Wedged" is not something to infer from who lost the race — it is
    # something to ask the store directly, below, once nothing is competing.
    timeouts = [e for e in errors if isinstance(e, sqlite3.OperationalError) and is_locked_error(e)]
    real_errors = [e for e in errors if e not in timeouts]
    assert real_errors == [], real_errors

    # The real question, and it is load-independent: is the store COMPLETE and
    # USABLE now? A wedged store fails both of these — see
    # test_a_wedged_store_still_fails_the_race_invariant, which holds the write
    # lock for real and proves this pair still detects it.
    import sqlite3 as raw

    from aisquare.core import paths

    conn = raw.connect(str(paths.db_path()))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    # Not a retry of the race: one uncontended open, through the store's own
    # path, asking the only question the race cannot answer for itself.
    with store_session() as store:
        store.entries("user")


def test_a_wedged_store_still_fails_the_race_invariant(work_dir: Path) -> None:
    """Prove the loosened race test can still detect the fault it guards.

    Dropping "some opener won" is only safe if what remains catches a genuinely
    wedged store. So wedge one for real — a second connection holding BEGIN
    EXCLUSIVE on a fresh database, which is the shape that would leave a
    half-applied schema behind — and assert that the checks the race test now
    relies on both fail. A probe that can no longer see the fault is worse than
    a flaky one, so this is asserted rather than reasoned about.
    """
    import sqlite3

    from aisquare.core import paths
    from aisquare.core.store import store_session

    # Fail fast rather than sitting out the default budget; the store documents
    # this knob for exactly this case.
    monkey = pytest.MonkeyPatch()
    monkey.setenv("AISQUARE_DB_BUSY_MS", "50")
    try:
        paths.ensure_home()
        sqlite3.connect(str(paths.db_path())).close()
        hog = sqlite3.connect(str(paths.db_path()))
        hog.isolation_level = None
        hog.execute("BEGIN EXCLUSIVE")
        try:
            with pytest.raises(sqlite3.OperationalError), store_session() as store:
                store.entries("user")

            reader = sqlite3.connect(str(paths.db_path()))
            try:
                with pytest.raises(sqlite3.OperationalError):
                    reader.execute("PRAGMA integrity_check").fetchone()
            finally:
                reader.close()
        finally:
            hog.execute("ROLLBACK")
            hog.close()
    finally:
        monkey.undo()


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


# --- #20: session-board delivery, read-back receipts, store_locked ---------------


def _flat(text: str) -> str:
    """Console output with all wrapping collapsed, for substring asserts."""
    return " ".join(text.split())


def test_attributed_write_delivers_to_the_session_board_not_cwd(
    runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.services import team as team_service

    monkeypatch.setenv("AISQUARE_ROLE", "planner")
    _start(runner, PLANNER, work_dir)
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, CODER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # the session's cwd wandered off its board

    result = runner.invoke(app, ["note", "routed home", "--as", "bbbb2222"])

    assert result.exit_code == 0, result.output
    assert "delivered to" in result.stderr  # the mismatch warning, loudly
    # The note landed on the SESSION's board, not the cwd board...
    on_board = team_service.log_events(work_dir)
    assert any(e.kind == "note" and e.text == "routed home" for e in on_board)
    assert all(e.text != "routed home" for e in team_service.log_events(elsewhere))
    # ...so its audience actually receives it on their next prompt.
    delta = _prompt(runner, PLANNER, work_dir)
    assert "routed home" in delta.stdout


def test_next_task_reads_the_session_board_from_a_foreign_cwd(
    runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, CODER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    runner.invoke(app, ["task", "add", "board A work", "--role", "coder", "--as", "bbbb2222"])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # a looper iterating from another checkout
    picked = json.loads(
        runner.invoke(
            app, ["--json", "task", "next", "--role", "coder", "--claim", "--as", "bbbb2222"]
        ).stdout
    )
    assert picked is not None and picked["title"] == "board A work"  # not an empty read
    assert picked["delivered"] is True


def test_needs_resolve_against_the_session_board_from_a_foreign_cwd(
    runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Direction (a) of the #20 --needs flip: a dependency that lives on the
    # SESSION's board must be accepted even when cwd resolves elsewhere
    # (the old cwd-board comparison rejected exactly this).
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, CODER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    runner.invoke(app, ["task", "add", "prerequisite on A", "--as", "bbbb2222"])
    needed = json.loads(runner.invoke(app, ["--json", "task", "list"]).stdout)[0]["id"]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    result = runner.invoke(
        app, ["--json", "task", "add", "dependent", "--needs", needed, "--as", "bbbb2222"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["created"] is True and payload["needs"] == [needed]


def test_needs_on_the_cwd_board_is_rejected_for_a_foreign_session(
    runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Direction (b): a dependency that lives on the CWD's board while the
    # acting session belongs elsewhere is cross-board contamination — reject.
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, CODER, work_dir)  # session registered on board A
    monkeypatch.delenv("AISQUARE_ROLE")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    runner.invoke(app, ["team", "on"])  # board B exists, with its own task
    runner.invoke(app, ["task", "add", "local to B"])
    on_b = json.loads(runner.invoke(app, ["--json", "task", "list"]).stdout)[0]["id"]
    result = runner.invoke(
        app, ["--json", "task", "add", "dependent", "--needs", on_b, "--as", "bbbb2222"]
    )
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["error"] == "invalid_needs"


def test_json_write_carries_delivered_flag_and_mismatch_warning(
    runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, CODER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    result = runner.invoke(app, ["--json", "note", "hello", "--as", "bbbb2222"])
    payload = json.loads(result.stdout)
    assert payload["delivered"] is True
    assert "delivered to" in payload["warning"]
    assert payload["kind"] == "team.note"  # envelope fields unchanged
    assert payload["payload"]["text"] == "hello"


def test_write_receipts_carry_seq_and_board(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, CODER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")

    board_id = team_project(work_dir).id
    note = runner.invoke(app, ["note", "receipt me", "--as", "bbbb2222"])
    # The receipt quotes the unambiguous project_id, not the collidable name.
    assert "· seq" in _flat(note.output) and board_id in _flat(note.output)

    added = runner.invoke(app, ["task", "add", "with receipt", "--as", "bbbb2222"])
    assert added.exit_code == 0 and "· seq" in _flat(added.output)
    # The idempotent duplicate still confirms the row — a receipt without a seq.
    dup = runner.invoke(app, ["task", "add", "with receipt", "--as", "bbbb2222"])
    assert "already tracked" in dup.output and f"· on {board_id}" in _flat(dup.output)

    picked = json.loads(
        runner.invoke(
            app, ["--json", "task", "next", "--role", "coder", "--claim", "--as", "bbbb2222"]
        ).stdout
    )
    assert picked["delivered"] is True and picked["status"] == "doing"
    reviewed = json.loads(
        runner.invoke(
            app, ["--json", "task", "review", picked["id"], "--note", "check", "--as", "bbbb2222"]
        ).stdout
    )
    assert reviewed["delivered"] is True
    # A pure read hands back no receipt and no delivered flag.
    peeked = json.loads(runner.invoke(app, ["--json", "task", "next", "--status", "review"]).stdout)
    assert "delivered" not in peeked


def test_wedged_store_fails_loudly_with_no_success_marker(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3 as raw

    from aisquare.core import paths

    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, CODER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    monkeypatch.setenv("AISQUARE_DB_BUSY_MS", "50")  # fail in 50ms, not 5s
    blocker = raw.connect(str(paths.db_path()))
    try:
        blocker.execute("BEGIN IMMEDIATE")  # hold the write lock: the store is wedged
        as_json = runner.invoke(app, ["--json", "note", "did this land?", "--as", "bbbb2222"])
        assert as_json.exit_code == 1
        assert "✓" not in as_json.output + as_json.stderr  # NO success marker, anywhere
        payload = json.loads(as_json.stdout)
        assert payload["error"] == "store_locked"
        assert "locked" in payload["detail"]  # the real cause reaches --json
        human = runner.invoke(app, ["note", "did this land?", "--as", "bbbb2222"])
        assert human.exit_code == 1
        assert "✓" not in human.output + human.stderr
        assert "Traceback" not in human.output + human.stderr
        # The MAPPED message must be on stderr — a raw OperationalError would
        # also exit 1 with no ✓ under CliRunner's exception catch, so only
        # this assert proves the store_locked mapping ran (review, #20).
        assert "context store busy" in _flat(human.stderr)
    finally:
        blocker.rollback()
        blocker.close()


def test_vanished_write_reports_delivery_unconfirmed(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.core.store import SqliteStore

    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, CODER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    # The pathological case behind #20: the commit "succeeded" but a fresh
    # connection cannot corroborate the row — success must not be reported.
    monkeypatch.setattr(SqliteStore, "get_event", lambda self, event_id: None)
    result = runner.invoke(app, ["--json", "note", "ghost", "--as", "bbbb2222"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "delivery_unconfirmed"
    assert payload["ref"].startswith("evt_")  # names the write to go verify
    assert "✓" not in result.output + result.stderr


def test_busy_timeout_env_knob(work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from aisquare.core.store import SqliteStore, open_store

    monkeypatch.setenv("AISQUARE_DB_BUSY_MS", "123")
    store = open_store()
    try:
        assert isinstance(store, SqliteStore)
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 123
    finally:
        store.close()


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


# --- #22: delivery self-check — log filters + team verify -----------------------


def test_note_verify_round_trip(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, CODER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    note = json.loads(runner.invoke(app, ["--json", "note", "prove me", "--as", "bbbb2222"]).stdout)
    seq = note["payload"]["seq"]

    by_seq = runner.invoke(app, ["team", "verify", str(seq)])
    assert by_seq.exit_code == 0, by_seq.output
    assert "delivered" in _flat(by_seq.output) and "prove me" in _flat(by_seq.output)

    by_prefix = runner.invoke(app, ["--json", "team", "verify", note["payload"]["id"][:12]])
    payload = json.loads(by_prefix.stdout)
    assert payload["delivered"] is True and payload["payload"]["seq"] == seq


def test_verify_not_found_and_ambiguous(runner: CliRunner, work_dir: Path) -> None:
    from datetime import UTC, datetime

    from aisquare.models import TeamEvent

    runner.invoke(app, ["team", "on"])
    missing = runner.invoke(app, ["--json", "team", "verify", "999999"])
    assert missing.exit_code == 1
    payload = json.loads(missing.stdout)
    assert payload["error"] == "not_found" and payload["ref"] == "999999"
    assert "hint" not in payload  # nothing to point at — a pure miss

    project = team_project(work_dir)
    with store_session() as store:
        for suffix in ("one", "two"):
            store.add_team_event(
                TeamEvent(
                    id=f"evt_zz{suffix}",
                    project_id=project.id,
                    text=suffix,
                    created_at=datetime.now(tz=UTC),
                )
            )
    ambiguous = runner.invoke(app, ["--json", "team", "verify", "evt_zz"])
    assert ambiguous.exit_code == 1
    assert json.loads(ambiguous.stdout)["error"] == "ambiguous_id"


def test_verify_wrong_board_hints_where_the_event_lives(
    runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, CODER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    note = json.loads(runner.invoke(app, ["--json", "note", "on A", "--as", "bbbb2222"]).stdout)
    seq = str(note["payload"]["seq"])

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    runner.invoke(app, ["team", "on"])  # the cwd board (B) is a different board
    wrong = runner.invoke(app, ["--json", "team", "verify", seq])
    assert wrong.exit_code == 1
    payload = json.loads(wrong.stdout)
    assert payload["error"] == "not_found"
    assert payload["hint"] == work_dir.name  # honest miss, but says who has it
    human = runner.invoke(app, ["team", "verify", seq])
    assert f"exists on board {work_dir.name}" in _flat(human.stderr)
    # With --as, the session's board wins over cwd — the receipt verifies.
    ok = runner.invoke(app, ["team", "verify", seq, "--as", "bbbb2222"])
    assert ok.exit_code == 0, ok.output


def test_log_filters_compose(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    monkeypatch.setenv("AISQUARE_ROLE", "planner")
    _start(runner, PLANNER, work_dir)
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, CODER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    runner.invoke(app, ["note", "from planner", "--as", "aaaa1111"])
    runner.invoke(app, ["note", "coder note", "--as", "bbbb2222"])
    runner.invoke(app, ["note", "coder decision", "--kind", "decision", "--as", "bbbb2222"])

    mine = json.loads(
        runner.invoke(app, ["--json", "team", "log", "--mine", "--as", "bbbb2222"]).stdout
    )
    texts = [e["payload"]["text"] for e in mine]
    assert "coder note" in texts and "from planner" not in texts

    by = json.loads(runner.invoke(app, ["--json", "team", "log", "--by", "aaaa1111"]).stdout)
    assert [e["payload"]["text"] for e in by] == ["from planner"]

    narrowed = runner.invoke(
        app, ["--json", "team", "log", "--mine", "--as", "bbbb2222", "--kind", "decision"]
    )
    assert [e["payload"]["text"] for e in json.loads(narrowed.stdout)] == ["coder decision"]

    everything = json.loads(runner.invoke(app, ["--json", "team", "log"]).stdout)
    first_seq = everything[0]["payload"]["seq"]
    after = json.loads(
        runner.invoke(app, ["--json", "team", "log", "--since-seq", str(first_seq)]).stdout
    )
    assert after and all(e["payload"]["seq"] > first_seq for e in after)

    recent = json.loads(runner.invoke(app, ["--json", "team", "log", "--since", "15m"]).stdout)
    assert len(recent) >= 3
    future = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()
    nothing = json.loads(runner.invoke(app, ["--json", "team", "log", "--since", future]).stdout)
    assert nothing == []

    added = json.loads(
        runner.invoke(app, ["--json", "task", "add", "filter me", "--as", "bbbb2222"]).stdout
    )
    tasked = json.loads(runner.invoke(app, ["--json", "team", "log", "--task", added["id"]]).stdout)
    assert tasked and all(e["payload"]["task_id"] == added["id"] for e in tasked)


def test_log_filter_guardrails(runner: CliRunner, work_dir: Path) -> None:
    runner.invoke(app, ["team", "on"])
    bare_mine = runner.invoke(app, ["--json", "team", "log", "--mine"])
    assert bare_mine.exit_code == 1
    assert json.loads(bare_mine.stdout)["error"] == "missing_session"
    both = runner.invoke(app, ["team", "log", "--mine", "--by", "aaaa1111", "--as", "aaaa1111"])
    assert both.exit_code == 2  # usage error: mutually exclusive
    garbage = runner.invoke(app, ["team", "log", "--since", "banana"])
    assert garbage.exit_code == 2  # BadParameter, not a stack trace
    unknown_author = runner.invoke(app, ["--json", "team", "log", "--by", "ffff9999"])
    assert unknown_author.exit_code == 1
    assert json.loads(unknown_author.stdout)["error"] == "not_found"


# --- #20 hardening: store-error honesty + guards --------------------------------


def test_busy_timeout_fallbacks_and_int32_clamp(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.core.store import SqliteStore, open_store

    def timeout_of() -> int:
        store = open_store()
        try:
            assert isinstance(store, SqliteStore)
            value: int = store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
            return value
        finally:
            store.close()

    monkeypatch.setenv("AISQUARE_DB_BUSY_MS", "abc")
    assert timeout_of() == 5000
    monkeypatch.setenv("AISQUARE_DB_BUSY_MS", "-5")
    assert timeout_of() == 5000
    # SQLite parses the pragma with 32-bit atoi: 2**31 would wrap to 0 and
    # silently DISABLE the busy handler — the clamp keeps it at the max.
    monkeypatch.setenv("AISQUARE_DB_BUSY_MS", "2147483648")
    assert timeout_of() == 2147483647


def test_fresh_db_wedge_fails_within_the_knob_budget(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The setup-retry deadline derives from AISQUARE_DB_BUSY_MS (3x), not a
    # hardcoded 15s floor: knob=50 on a wedged FRESH db must fail fast.
    import sqlite3 as raw
    import time as clock

    from aisquare.core import paths
    from aisquare.core.store import open_store

    monkeypatch.setenv("AISQUARE_DB_BUSY_MS", "50")
    paths.ensure_home()
    blocker = raw.connect(str(paths.db_path()))
    try:
        blocker.execute("BEGIN IMMEDIATE")  # the WAL switch/migration will starve
        started = clock.monotonic()
        with pytest.raises(raw.OperationalError):
            open_store()
        elapsed = clock.monotonic() - started
    finally:
        blocker.rollback()
        blocker.close()
    assert elapsed < 5, f"took {elapsed:.1f}s — the hardcoded floor is back"


def test_store_error_is_distinct_from_store_locked(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Non-lock database failures must not be dressed up as retryable.
    import sqlite3 as raw

    from aisquare.core.store import SqliteStore

    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, CODER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")

    def broken(self: SqliteStore, event: object) -> object:
        raise raw.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(SqliteStore, "add_team_event", broken)
    result = runner.invoke(app, ["--json", "note", "x", "--as", "bbbb2222"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "store_error"
    assert "readonly" in payload["detail"]  # the real cause reaches --json
    human = runner.invoke(app, ["note", "x", "--as", "bbbb2222"])
    assert human.exit_code == 1
    assert "Traceback" not in human.output + human.stderr
    assert "context store error" in _flat(human.stderr)


def test_note_task_ref_must_live_on_the_target_board(
    runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same cross-board contamination --needs already rejects.
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, CODER, work_dir)  # session on board A
    monkeypatch.delenv("AISQUARE_ROLE")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    runner.invoke(app, ["team", "on"])  # board B, with its own task
    runner.invoke(app, ["task", "add", "task on B"])
    on_b = json.loads(runner.invoke(app, ["--json", "task", "list"]).stdout)[0]["id"]
    poisoned = runner.invoke(app, ["--json", "note", "poison", "--task", on_b, "--as", "bbbb2222"])
    assert poisoned.exit_code == 1
    payload = json.loads(poisoned.stdout)
    assert payload["error"] == "invalid_task" and payload["ref"] == on_b
    # Same-board refs still attach fine.
    monkeypatch.chdir(work_dir)
    runner.invoke(app, ["task", "add", "task on A", "--as", "bbbb2222"])
    on_a = json.loads(runner.invoke(app, ["--json", "task", "list"]).stdout)[0]["id"]
    ok = runner.invoke(app, ["--json", "note", "fine", "--task", on_a, "--as", "bbbb2222"])
    assert ok.exit_code == 0 and json.loads(ok.stdout)["delivered"] is True


def test_receipts_on_team_on_focus_and_prune(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_on = json.loads(runner.invoke(app, ["--json", "team", "on"]).stdout)
    assert first_on["delivered"] is True  # first activation emits + confirms
    board_id = first_on["activated"]
    again = json.loads(runner.invoke(app, ["--json", "team", "on"]).stdout)
    assert "delivered" not in again  # already active: no event, no receipt

    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, CODER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    focus = runner.invoke(app, ["team", "focus", "receipts", "--as", "bbbb2222"])
    assert focus.exit_code == 0 and "· seq" in _flat(focus.output)
    focus_json = json.loads(
        runner.invoke(app, ["--json", "team", "focus", "again", "--as", "bbbb2222"]).stdout
    )
    assert focus_json["delivered"] is True

    with store_session() as store:
        _put_session(store, PLANNER, board_id, idle_min=90)  # a ghost to retire
    pruned = runner.invoke(app, ["team", "prune"])
    assert pruned.exit_code == 0 and "· seq" in _flat(pruned.output)
    with store_session() as store:
        _put_session(store, "cccc9999-0000-0000-0000-000000000000", board_id, idle_min=90)
    pruned_json = json.loads(runner.invoke(app, ["--json", "team", "prune"]).stdout)
    assert pruned_json["delivered"] is True


def test_idempotent_dup_add_reports_delivery_unconfirmed_when_row_vanishes(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.core.store import SqliteStore

    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _start(runner, CODER, work_dir)
    monkeypatch.delenv("AISQUARE_ROLE")
    first = runner.invoke(app, ["--json", "task", "add", "dup me", "--as", "bbbb2222"])
    assert json.loads(first.stdout)["delivered"] is True
    # The dup branch confirms via a fresh get_task read-back — starve it.
    monkeypatch.setattr(SqliteStore, "get_task", lambda self, ref: None)
    dup = runner.invoke(app, ["--json", "task", "add", "dup me", "--as", "bbbb2222"])
    assert dup.exit_code == 1
    payload = json.loads(dup.stdout)
    assert payload["error"] == "delivery_unconfirmed"
    assert payload["ref"].startswith("tsk_")
    assert "✓" not in dup.output + dup.stderr


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


def test_the_quiet_board_teaches_launch_not_the_env_var_incantation(
    runner: CliRunner, work_dir: Path
) -> None:
    """`aisquare launch coder` replaced the AISQUARE_ROLE=... prefix as the
    documented way in; an empty board is exactly where newcomers read the hint."""
    runner.invoke(app, ["team", "on"])

    board = runner.invoke(app, ["board"])

    assert "aisquare launch" in board.output
    assert "AISQUARE_ROLE=" not in board.output
