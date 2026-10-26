"""``project fresh`` (alias ``reset``): Start fresh — clear an onboarded project's board and
fleet, and start it a blank manager, without onboarding it again.

The manager had only hand-written SQL to clear a project between pieces of work. What makes
the one supported action safe to offer, held here:

- it is scoped: every other project's rows come out byte-identical, and the project's own
  context entries, prompt log and metrics stay unless ``--memory`` / ``--prompts``;
- the board restarts at task #1 with its signals gone, and a new manager (a new session id)
  is spawned on it;
- work an agent's worktree or branch holds that no remote has refuses it, named, unless
  ``--discard-unpushed``; ``--dry-run`` changes nothing;
- it is refused from inside one of the project's own fleet agents, and while another
  project's fleet agents read its board.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import typer.main
from typer.core import TyperGroup
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import brain as brain_core
from aisquare.core import locking, paths
from aisquare.core.entries import new_entry
from aisquare.core.ids import new_agent_id, new_event_id, new_task_id
from aisquare.core.orchestrator import FLEET_AGENT_ENV_VAR, team_project
from aisquare.core.store import SqliteStore, store_session
from aisquare.core.tmux import TmuxError, TmuxServer, TmuxUnavailable
from aisquare.models import (
    FleetAgent,
    FreshPlan,
    FreshReport,
    FreshUnpushed,
    ProjectInfo,
    TeamEvent,
    TeamSession,
    TeamTask,
)
from aisquare.services import fleet as fleet_service
from aisquare.services import fresh as fresh_service
from aisquare.services import team as team_service
from tests.test_worktree import _git

_BOARD = ("team_task", "team_event", "team_session", "fleet_agent")
_NO_SESSIONS = fleet_service.ShutdownPlan()
"""What ``fleet shutdown``'s plan says of a project with no tmux session standing."""
_KEPT = ("entry", "prompt", "metric")


def _raw(sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    connection = sqlite3.connect(str(paths.db_path()))
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def _rows(project_id: str) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of ``project_id`` in each table a reset could touch, as stored."""
    return {
        table: _raw(f"SELECT * FROM {table} WHERE project_id = ? ORDER BY rowid", (project_id,))
        for table in (*_BOARD, *_KEPT)
    }


def _meta() -> dict[str, str]:
    return dict(_raw("SELECT key, value FROM team_meta"))


def _add(root: Path, codename: str) -> ProjectInfo:
    """``root`` onboarded, with a codename, as ``project onboard`` and a first spawn leave it."""
    root.mkdir(parents=True, exist_ok=True)
    with store_session() as store:
        return store.onboard_project(team_project(root), codename=codename)


def _agent(project: ProjectInfo, label: str, **update: Any) -> FleetAgent:
    return FleetAgent(
        id=new_agent_id(),
        project_id=project.id,
        label=label,
        role="manager" if label == "manager" else "coder",
        pane_id="%1",
        session_id=f"sess-{label}-{project.codename}",
        cwd=project.root,
        created_at=datetime.now(tz=UTC),
    ).model_copy(update=update)


def _seed(project: ProjectInfo, word: str) -> dict[str, str]:
    """Tasks, events, sessions, signals, session marks, an ended fleet row, an entry, a
    prompt and a metric for ``project``; its ``team_meta`` keys, by key."""
    now = datetime.now(tz=UTC)
    session = f"sess-{word}"
    with store_session() as store:
        store.upsert_session(
            TeamSession(id=session, project_id=project.id, started_at=now, last_seen_at=now)
        )
        for n in range(3):
            store.upsert_task(
                TeamTask(
                    id=new_task_id(),
                    project_id=project.id,
                    key=f"{word}-{n}",
                    title=f"{word} task {n}",
                    created_at=now,
                    updated_at=now,
                )
            )
        for n in range(2):
            store.add_team_event(
                TeamEvent(
                    id=new_event_id(),
                    project_id=project.id,
                    session_id=session,
                    text=f"{word} note {n}",
                    created_at=now,
                )
            )
        manager = store.upsert_fleet_agent(_agent(project, "manager", session_id=session))
        store.end_fleet_agent(manager.id, exit_status=0, ended_by="stopped")
        store.add(new_entry(f"{word}fact remembered", "project", project.id, [], "manual"))
        store.add_prompt(f"{word} prompt", project.id)
        keys = {
            f"signal/{project.id}/phase": json.dumps({"value": word}),
            f"nudge:{session}": now.isoformat(),
            f"continuations:{session}:2026-10-25": "1",
            f"distill_seq:{project.id}": "4",
        }
        for key, value in keys.items():
            store.set_meta(key, value)
    connection = sqlite3.connect(str(paths.db_path()))
    try:
        connection.execute(
            "INSERT INTO metric (trace_id, project_id, started_at, client_reason) "
            "VALUES (?, ?, '2026-09-01T00:00:00+00:00', 'disabled')",
            (f"trc_{word}", project.id),
        )
        connection.commit()
    finally:
        connection.close()
    return keys


@pytest.fixture
def two(tmp_path: Path) -> tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]]:
    alpha = _add(tmp_path / "alpha", "alpha-x")
    beta = _add(tmp_path / "beta", "beta-y")
    return alpha, beta, _seed(alpha, "alpha"), _seed(beta, "beta")


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, str, dict[str, Any]]]]:
    """``fleet.spawn`` faked: each call recorded, and a live manager row written with a new
    session id, as a real spawn records one."""
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def spawn(project: ProjectInfo, role: str, **kwargs: Any) -> fleet_service.SpawnReceipt:
        calls.append((project.id, role, kwargs))
        with store_session() as store:
            row = store.upsert_fleet_agent(
                _agent(project, "manager", session_id=str(uuid.uuid4()), pane_id="%9")
            )
        return fleet_service.SpawnReceipt(agent=row, asked_label=None, tmux_session="asq-x")

    monkeypatch.setattr(fleet_service, "spawn", spawn)
    yield calls


@dataclass
class Stops:
    """What the faked ``fleet.shutdown`` was asked, and the sessions its plan lists."""

    asked: list[str] = field(default_factory=list)
    sessions: list[str] = field(default_factory=list)


@pytest.fixture
def stops(monkeypatch: pytest.MonkeyPatch) -> Stops:
    """``fleet.shutdown`` (and its plan) faked: each live row of the project ended as a stop
    ends it, with the ``agent_exited`` a stop writes on the board; the plan lists
    :attr:`Stops.sessions`."""
    faked = Stops()

    def shutdown(project: ProjectInfo, **_: Any) -> fleet_service.ShutdownReport:
        faked.asked.append(project.id)
        with store_session() as store:
            live = store.fleet_agents(project.id, live_only=True)
            for row in live:
                store.end_fleet_agent(row.id, exit_status=0, ended_by="stopped")
                store.add_team_event(
                    TeamEvent(
                        id=new_event_id(),
                        project_id=project.id,
                        kind="agent_exited",
                        text=f"{row.label} exited (0)",
                        created_at=datetime.now(tz=UTC),
                    )
                )
        killed, faked.sessions = faked.sessions, []  # its sessions go with the stop
        return fleet_service.ShutdownReport(stopped=live, sessions_killed=killed)

    def shutdown_plan(project: ProjectInfo, **_: Any) -> fleet_service.ShutdownPlan:
        return fleet_service.ShutdownPlan(sessions=list(faked.sessions))

    monkeypatch.setattr(fleet_service, "shutdown_plan", shutdown_plan)
    monkeypatch.setattr(fleet_service, "project_sessions", lambda *_: list(faked.sessions))
    monkeypatch.setattr(fleet_service, "shutdown", shutdown)
    return faked


# --- the service ---------------------------------------------------------------------------


def test_a_reset_clears_the_board_and_fleet_and_keeps_memory_prompts_and_every_other_project(
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
) -> None:
    alpha, beta, alpha_keys, _ = two
    beta_before = _rows(beta.id)
    alpha_kept = {table: _rows(alpha.id)[table] for table in _KEPT}
    meta_before = _meta()

    report = fresh_service.start_fresh(alpha)

    after = _rows(alpha.id)
    assert after["team_task"] == after["team_event"] == after["team_session"] == []
    # Its only fleet row is the manager the reset spawned.
    assert [row[0] for row in after["fleet_agent"]] == [report.manager.id if report.manager else ""]
    assert {table: after[table] for table in _KEPT} == alpha_kept
    assert _rows(beta.id) == beta_before
    # Its signals and its sessions' marks went; the distiller's watermark and every other
    # key, beta's included, are as they were.
    gone = {key for key in alpha_keys if not key.startswith("distill_seq:")}
    assert _meta() == {k: v for k, v in meta_before.items() if k not in gone}
    assert report.cleared.model_dump() == {
        "tasks": 3,
        "events": 2,
        "sessions": 1,
        "meta": 3,
        "agents": 1,
        "worktrees": 0,
        "briefs": 0,
        "evidence": 0,
        "manager_state": 0,
        "entries": None,
        "brain": None,
        "prompts": None,
        "metrics": None,
        "branches": None,
    }
    with store_session() as store:  # the registration is untouched
        assert store.get_project(alpha.id) == alpha


def test_memory_and_prompts_go_too_with_their_flags_and_still_only_the_projects(
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
) -> None:
    alpha, beta, _, _ = two
    beta_before = _rows(beta.id)

    report = fresh_service.start_fresh(alpha, memory=True, prompts=True)

    after = _rows(alpha.id)
    assert after["entry"] == after["prompt"] == after["metric"] == []
    assert (report.cleared.entries, report.cleared.prompts, report.cleared.metrics) == (1, 1, 1)
    assert _rows(beta.id) == beta_before
    with store_session() as store:  # the full-text index followed the deletes
        assert store.search("alphafact", project_id=alpha.id) == []
        assert [e.project_id for e in store.search("betafact", project_id=beta.id)] == [beta.id]
    assert _raw("SELECT rowid FROM entry_fts WHERE entry_fts MATCH 'alphafact'") == []


def test_task_numbers_restart_at_one_signals_are_gone_and_a_new_manager_has_a_new_session(
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
) -> None:
    alpha, _, _, _ = two
    with store_session() as store:
        old = {row.session_id for row in store.fleet_agents(alpha.id)}
        assert sorted(store.task_numbers(alpha.id).values()) == [1, 2, 3]

    report = fresh_service.start_fresh(alpha)

    assert [(project, role) for project, role, _ in spawned] == [(alpha.id, "manager")]
    assert report.manager is not None and report.manager.session_id not in old
    now = datetime.now(tz=UTC)
    with store_session() as store:
        added, _ = store.upsert_task(
            TeamTask(
                id=new_task_id(),
                project_id=alpha.id,
                key="next",
                title="the next piece of work",
                created_at=now,
                updated_at=now,
            )
        )
        assert store.task_numbers(alpha.id) == {added.id: 1}
        assert store.list_meta(f"signal/{alpha.id}/") == {}
        assert [row.id for row in store.fleet_agents(alpha.id)] == [report.manager.id]


def test_live_agents_are_stopped_the_way_shutdown_stops_them_before_anything_goes(
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
    stops: Stops,
) -> None:
    alpha, _, _, _ = two
    with store_session() as store:
        store.upsert_fleet_agent(_agent(alpha, "coder-1"))

    planned = fresh_service.plan(alpha)
    assert planned.live_agents == ["coder-1"] and stops.asked == []

    report = fresh_service.start_fresh(alpha)

    assert stops.asked == [alpha.id]
    assert report.stopped == ["coder-1"]
    assert report.cleared.agents == 2


def test_an_agent_left_live_by_the_stop_clears_nothing(
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha, _, _, _ = two
    with store_session() as store:
        live = store.upsert_fleet_agent(_agent(alpha, "coder-1"))
    before = _rows(alpha.id)
    monkeypatch.setattr(fleet_service, "shutdown_plan", lambda *_, **__: _NO_SESSIONS)
    monkeypatch.setattr(
        fleet_service,
        "shutdown",
        lambda *_, **__: fleet_service.ShutdownReport(
            failed=[fleet_service.ShutdownRow(live, "its pane is still alive")]
        ),
    )

    with pytest.raises(fresh_service.FreshIncomplete, match=r"coder-1 \(its pane is still alive\)"):
        fresh_service.start_fresh(alpha)

    assert _rows(alpha.id) == before and spawned == []


def test_the_store_refuses_a_reset_while_a_fleet_row_is_live(
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
) -> None:
    alpha, _, _, _ = two
    with store_session() as store:
        store.upsert_fleet_agent(_agent(alpha, "coder-1"))
        before = store.reset_project(alpha.id, apply=False)
        with pytest.raises(ValueError, match="coder-1"):
            store.reset_project(alpha.id, apply=True)
        assert store.reset_project(alpha.id, apply=False) == before


# --- unpushed work -------------------------------------------------------------------------


@pytest.fixture
def unpushed(
    tmp_path: Path, spawned: list[tuple[str, str, dict[str, Any]]]
) -> tuple[ProjectInfo, Path, str]:
    """Alpha as a git checkout, and an ended coder's worktree on its fleet branch holding one
    commit no remote has."""
    alpha = _add(tmp_path / "alpha", "alpha-x")
    _git("init", "-q", "-b", "main", cwd=alpha.root)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=alpha.root)
    tree = tmp_path / "trees" / "coder-1"
    branch = "fleet/alpha-x/coder-1"
    _git("worktree", "add", "-q", "-b", branch, str(tree), cwd=alpha.root)
    (tree / "work.txt").write_text("work\n", encoding="utf-8")
    _git("add", "work.txt", cwd=tree)
    _git("commit", "-q", "-m", "work", cwd=tree)
    with store_session() as store:
        row = store.upsert_fleet_agent(
            _agent(alpha, "coder-1", cwd=tree, worktree=True, branch=branch)
        )
        store.end_fleet_agent(row.id, exit_status=0, ended_by="stopped")
    return alpha, tree, branch


def _branches(root: Path) -> list[str]:
    import subprocess

    listed = subprocess.run(
        ["git", "-C", str(root), "branch", "--format=%(refname:short)"],
        capture_output=True,
        text=True,
        check=True,
    )
    return listed.stdout.split()


def test_an_unpushed_commit_refuses_the_reset_naming_the_tree_its_branch_and_the_count(
    runner: CliRunner, unpushed: tuple[ProjectInfo, Path, str]
) -> None:
    alpha, tree, branch = unpushed
    before = _rows(alpha.id)

    refused = runner.invoke(app, ["project", "fresh", "-P", "alpha-x", "--yes"])

    assert refused.exit_code == 2, refused.output
    said = " ".join(refused.output.split())
    assert f"{tree} on {branch}: 1 commit on no remote" in said
    assert "--discard-unpushed" in said
    as_json = runner.invoke(app, ["--json", "project", "fresh", "-P", "alpha-x", "--yes"])
    assert json.loads(as_json.stdout)["error"] == "unpushed_work"
    assert _rows(alpha.id) == before and tree.is_dir()


def test_discard_unpushed_removes_the_tree_and_branches_deletes_its_branch_locally(
    runner: CliRunner,
    unpushed: tuple[ProjectInfo, Path, str],
    spawned: list[tuple[str, str, dict[str, Any]]],
) -> None:
    alpha, tree, branch = unpushed
    _git("branch", "mine", cwd=alpha.root)  # an owner's branch: never the reset's

    result = runner.invoke(
        app,
        ["--json", "project", "fresh", "-P", "alpha-x", "--yes", "--discard-unpushed"],
    )

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["worktrees_removed"] == [str(tree)] and not tree.exists()
    assert [item["branch"] for item in report["discarded"]] == [branch]
    assert branch in _branches(alpha.root)  # kept without --branches

    with store_session() as store:  # the next run, with --branches, finds it recorded
        row = store.upsert_fleet_agent(_agent(alpha, "coder-2", branch=branch))
        store.end_fleet_agent(row.id, exit_status=0, ended_by="stopped")
        for live in store.fleet_agents(alpha.id, live_only=True):
            store.end_fleet_agent(live.id, exit_status=0, ended_by="stopped")
    refused = runner.invoke(app, ["project", "fresh", "-P", "alpha-x", "--yes", "--branches"])
    assert refused.exit_code == 2 and f"branch {branch}: 1 commit on no remote" in " ".join(
        refused.output.split()
    )
    result = runner.invoke(
        app,
        [
            "--json",
            "project",
            "fresh",
            "-P",
            "alpha-x",
            "--yes",
            "--branches",
            "--discard-unpushed",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["branches_deleted"] == [branch]
    assert _branches(alpha.root) == ["main", "mine"]


# --- the CLI -------------------------------------------------------------------------------


def test_dry_run_prints_the_plan_and_changes_nothing(
    runner: CliRunner,
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
) -> None:
    alpha, _, _, _ = two
    before, meta = _rows(alpha.id), _meta()

    result = runner.invoke(app, ["project", "fresh", "-P", "alpha-x", "--dry-run", "--memory"])

    assert result.exit_code == 0, result.output
    said = " ".join(result.output.split())
    assert "clears 3 tasks · 2 notes and events · 1 session · 3 signals and marks" in said
    assert "1 fleet agent · 0 worktrees · 0 briefs · 0 brief evidence files" in said
    assert "0 manager state files · 1 context entry · 0 project brains" in said
    assert "keeps prompt log and metrics (--prompts), fleet branches (--branches)" in said
    as_json = runner.invoke(app, ["--json", "project", "fresh", "-P", "alpha-x", "--dry-run"])
    assert json.loads(as_json.stdout)["counts"]["tasks"] == 3
    assert _rows(alpha.id) == before and _meta() == meta and spawned == []


def test_off_a_terminal_without_yes_it_is_a_dry_run(
    runner: CliRunner,
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
) -> None:
    alpha, _, _, _ = two
    before = _rows(alpha.id)

    result = runner.invoke(app, ["project", "fresh", "-P", "alpha-x"])

    assert result.exit_code == 0, result.output
    assert "dry run: nothing cleared — re-run with --yes" in result.output
    assert _rows(alpha.id) == before and spawned == []


def test_at_a_terminal_it_asks_and_asks_again_before_discarding(
    runner: CliRunner,
    unpushed: tuple[ProjectInfo, Path, str],
    spawned: list[tuple[str, str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, tree, _ = unpushed
    monkeypatch.setattr("aisquare.cli.project._stdin_is_a_terminal", lambda: True)
    argv = ["project", "fresh", "-P", "alpha-x", "--discard-unpushed"]

    declined = runner.invoke(app, argv, input="y\nn\n")

    assert declined.exit_code == 0, declined.output
    assert "Start alpha fresh?" in declined.output
    assert "Throw away the unpushed work in 1 place, as listed above" in declined.output
    assert "(uncommitted changes go as they are when the reset runs)?" in " ".join(
        declined.output.split()
    )
    assert "nothing cleared" in declined.output and tree.is_dir() and spawned == []

    agreed = runner.invoke(app, argv, input="y\ny\n")

    assert agreed.exit_code == 0, agreed.output
    assert "✓ started alpha fresh — cleared" in agreed.output and not tree.exists()
    assert "✓ a new manager started: agt_" in agreed.output


def test_json_names_the_counts_cleared(
    runner: CliRunner,
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
) -> None:
    result = runner.invoke(app, ["--json", "project", "fresh", "-P", "alpha-x", "--yes"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert {k: v for k, v in report["cleared"].items() if v is not None} == {
        "tasks": 3,
        "events": 2,
        "sessions": 1,
        "meta": 3,
        "agents": 1,
        "worktrees": 0,
        "briefs": 0,
        "evidence": 0,
        "manager_state": 0,
    }
    assert report["manager"]["role"] == "manager" and report["manager_error"] is None


def test_a_manager_the_fleet_refuses_is_said_and_the_board_is_clear_all_the_same(
    runner: CliRunner,
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha, _, _, _ = two

    def refuse(*_: Any, **__: Any) -> fleet_service.SpawnReceipt:
        raise fleet_service.FleetError("tmux is not installed")

    monkeypatch.setattr(fleet_service, "spawn", refuse)

    result = runner.invoke(app, ["project", "fresh", "-P", "alpha-x", "--yes"])

    assert result.exit_code == 1, result.output
    said = " ".join(result.output.split())
    assert "✗ no manager started: tmux is not installed — start one: aisquare fleet spawn" in said
    assert _rows(alpha.id)["team_task"] == []


def test_fresh_and_reset_are_the_same_command(
    runner: CliRunner,
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
) -> None:
    root = typer.main.get_command(app)
    assert isinstance(root, TyperGroup)
    project = root.commands["project"]
    assert isinstance(project, TyperGroup)
    # One function under two names: typer wraps each registration's callback around it.
    fresh_cb, reset_cb = project.commands["fresh"].callback, project.commands["reset"].callback
    assert getattr(fresh_cb, "__wrapped__", None) is getattr(reset_cb, "__wrapped__", 0)
    argv = ["project", "{}", "-P", "alpha-x", "--dry-run"]
    fresh = runner.invoke(app, ["--json", *[w.format("fresh") for w in argv]])
    reset = runner.invoke(app, ["--json", *[w.format("reset") for w in argv]])
    assert fresh.exit_code == reset.exit_code == 0
    assert json.loads(fresh.stdout) == json.loads(reset.stdout)
    listed = runner.invoke(app, ["project", "--help"])
    assert "fresh" in listed.output


def test_it_is_refused_from_inside_one_of_the_projects_own_fleet_agents(
    runner: CliRunner,
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha, beta, _, _ = two
    with store_session() as store:
        mine = store.upsert_fleet_agent(_agent(alpha, "coder-1"))
        theirs = store.upsert_fleet_agent(_agent(beta, "coder-2"))
    before = _rows(alpha.id)
    monkeypatch.setenv(FLEET_AGENT_ENV_VAR, mine.id)

    refused = runner.invoke(app, ["project", "fresh", "-P", "alpha-x", "--yes"])

    assert refused.exit_code == 2, refused.output
    said = " ".join(refused.output.split())
    assert "can't start fresh from inside coder-1, one of alpha's own fleet agents" in said
    assert "Run it from the UI (asq) or from a terminal of your own" in said
    as_json = runner.invoke(app, ["--json", "project", "fresh", "-P", "alpha-x", "--yes"])
    refused_json = json.loads(as_json.stdout)
    assert {k: refused_json[k] for k in ("error", "ref")} == {
        "error": "inside_fleet_agent",
        "ref": alpha.id,
    }
    assert "from inside coder-1" in refused_json["detail"]
    assert _rows(alpha.id) == before and spawned == []
    # Another project's agent is not this project's pane: its plan is not refused for it.
    monkeypatch.setenv(FLEET_AGENT_ENV_VAR, theirs.id)
    monkeypatch.setattr(fleet_service, "shutdown_plan", lambda *_, **__: _NO_SESSIONS)
    assert fresh_service.plan(alpha).live_agents == ["coder-1"]


def test_a_board_another_projects_fleet_reads_is_refused_naming_them(
    runner: CliRunner,
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
) -> None:
    alpha, beta, _, _ = two
    with store_session() as store:
        for label in ("manager", "coder-1"):
            store.upsert_fleet_agent(_agent(beta, label, board=alpha.id))
    before = _rows(alpha.id)

    refused = runner.invoke(app, ["project", "fresh", "-P", "alpha-x", "--yes"])

    assert refused.exit_code == 2, refused.output
    said = " ".join(refused.output.split())
    assert "can't start fresh: alpha is the board beta's 2 live fleet agent(s) read:" in said
    assert "manager, coder-1 —" in said and "fleet stop --project beta-y <label>" in said
    as_json = runner.invoke(app, ["--json", "project", "fresh", "-P", "alpha-x", "--dry-run"])
    refused_json = json.loads(as_json.stdout)
    assert {k: refused_json[k] for k in ("error", "ref")} == {
        "error": "project_busy",
        "ref": alpha.id,
    }
    assert "beta's 2 live fleet agent(s) read" in refused_json["detail"]
    assert _rows(alpha.id) == before and spawned == []


def test_a_project_nobody_onboarded_is_refused(runner: CliRunner, tmp_path: Path) -> None:
    loose = tmp_path / "loose"
    loose.mkdir()

    refused = runner.invoke(app, ["project", "fresh", "-P", str(loose), "--yes"])

    assert refused.exit_code != 0, refused.output


# --- what outlives its rows: the tmux session (#3) -----------------------------------------


def test_a_tmux_session_with_no_live_row_is_stopped_too(
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
    stops: Stops,
) -> None:
    """The stop ran only for a live row, and ``fleet shutdown -P`` also kills the project's
    session when none is: a dead pane, or a process a row recorded as died, stayed beside
    the new manager (tester-start-fresh at fa1e62b6). The plan lists the session, and the
    stop runs for it."""
    alpha, _, _, _ = two
    stops.sessions = ["asq:asq-alpha-x"]

    planned = fresh_service.plan(alpha)
    report = fresh_service.start_fresh(alpha)

    assert planned.live_agents == [] and planned.sessions == ["asq:asq-alpha-x"]
    assert stops.asked == [alpha.id] and report.stopped == []


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("tmux") is None, reason="tmux is not installed"
)
def test_a_dead_pane_and_a_stray_process_go_and_only_the_new_managers_window_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a real tmux server (the test's own: ``no_real_fleet``'s ``TMUX_TMPDIR``): the
    manager exited and its dead pane stays (``remain-on-exit``), and a coder's row was
    recorded as died while its process still runs. No row is live. After the reset the old
    session is gone with both, the process too, and the one window is the new manager's."""
    alpha = _add(tmp_path / "alpha", "alpha-x")
    srv = fleet_service.server()
    session = fleet_service.session_name("alpha-x")
    srv.run("new-session", "-d", "-s", session, "-n", "coder-1", "sleep 600")
    srv.run("set-option", "-g", "remain-on-exit", "on")
    # The manager's window runs until the panes are listed, then is killed: a command that
    # ended first left a dead pane with no #{pane_pid}, and the listing below came short of a
    # field under load (tester-fresh-fix: 4 of 12 concurrent runs).
    srv.run("new-window", "-d", "-t", f"={session}:", "-n", "manager", "sleep 600")
    panes: dict[str, tuple[str, int]] = {}
    for line in srv.run(
        "list-panes", "-s", "-t", f"={session}", "-F", "#{window_name} #{pane_id} #{pane_pid}"
    ).splitlines():
        name, pane, pid = line.split()
        panes[name] = (pane, int(pid))
    os.kill(panes["manager"][1], signal.SIGTERM)
    deadline = time.monotonic() + 10
    while srv.run("display", "-p", "-t", panes["manager"][0], "#{pane_dead}").strip() != "1":
        assert time.monotonic() < deadline, "the manager's pane never died"
        time.sleep(0.05)
    with store_session() as store:
        for label, (pane, _) in panes.items():
            row = store.upsert_fleet_agent(
                _agent(alpha, label, pane_id=pane, tmux_socket=fleet_service.settings().tmux_socket)
            )
            store.end_fleet_agent(row.id, exit_status=None, ended_by="died")
    stray = panes["coder-1"][1]
    assert _alive(stray)

    def spawn(project: ProjectInfo, role: str, **_: Any) -> fleet_service.SpawnReceipt:
        srv.run("new-session", "-d", "-s", session, "-n", "manager", "sleep 600")
        pane = srv.run("list-panes", "-s", "-t", f"={session}", "-F", "#{pane_id}").split()[0]
        with store_session() as store:
            row = store.upsert_fleet_agent(_agent(project, "manager", pane_id=pane))
        return fleet_service.SpawnReceipt(agent=row, asked_label=None, tmux_session=session)

    monkeypatch.setattr(fleet_service, "spawn", spawn)
    planned = fresh_service.plan(alpha)

    report = fresh_service.start_fresh(alpha)

    assert planned.live_agents == [] and planned.sessions == [f"asq:{session}"], planned
    assert report.manager is not None
    deadline = time.monotonic() + 10
    while _alive(stray):
        assert time.monotonic() < deadline, "the stray process outlived the reset"
        time.sleep(0.05)
    assert srv.list_sessions() == [session]
    windows = srv.run("list-windows", "-t", f"={session}", "-F", "#{window_name}").split()
    assert windows == ["manager"]


def test_the_plans_counts_are_the_reports_and_the_stops_own_events_are_said_apart(
    runner: CliRunner,
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
    stops: Stops,
) -> None:
    """The plan counted 7 notes and events and the run cleared 11: the stop writes an
    ``agent_exited`` for each agent it stops, and those go with the board too. The report
    carries both: what the plan counted (``planned``) and what went (``cleared``), the
    difference said as the stop's own."""
    alpha, _, _, _ = two
    with store_session() as store:
        store.upsert_fleet_agent(_agent(alpha, "coder-1"))
    planned = fresh_service.plan(alpha)

    result = runner.invoke(app, ["--json", "project", "fresh", "-P", "alpha-x", "--yes"])

    report = FreshReport.model_validate_json(result.stdout)
    assert report.planned == planned.counts
    assert report.cleared.model_copy(update={"events": planned.counts.events}) == planned.counts
    assert (report.cleared.events, report.stop_events()) == (planned.counts.events + 1, 1)
    with store_session() as store:
        store.upsert_fleet_agent(_agent(alpha, "coder-2"))
        for row in store.fleet_agents(alpha.id, live_only=True):
            if row.label == "manager":
                store.end_fleet_agent(row.id, exit_status=0, ended_by="stopped")
    said = runner.invoke(app, ["project", "fresh", "-P", "alpha-x", "--yes"])
    assert said.exit_code == 0, said.output
    assert "1 note or event (1 written during the reset)" in " ".join(said.output.split())


def test_an_incomplete_reset_prints_its_partial_report_under_json(
    runner: CliRunner,
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha, _, _, _ = two
    with store_session() as store:
        live = store.upsert_fleet_agent(_agent(alpha, "coder-1"))
        stopped = store.upsert_fleet_agent(_agent(alpha, "coder-2"))
    monkeypatch.setattr(fleet_service, "shutdown_plan", lambda *_, **__: _NO_SESSIONS)
    monkeypatch.setattr(
        fleet_service,
        "shutdown",
        lambda *_, **__: fleet_service.ShutdownReport(
            stopped=[stopped], failed=[fleet_service.ShutdownRow(live, "its pane is still alive")]
        ),
    )

    result = runner.invoke(app, ["--json", "project", "fresh", "-P", "alpha-x", "--yes"])

    assert result.exit_code == 1, result.output
    said = json.loads(result.stdout)
    assert said["error"] == "fresh_incomplete" and said["ref"] == alpha.id
    assert "coder-1 (its pane is still alive)" in said["detail"]
    assert said["report"]["stopped"] == ["coder-2"]
    assert said["report"]["cleared"]["tasks"] == 0 and said["report"]["manager"] is None
    assert len(_rows(alpha.id)["team_task"]) == 3, "nothing on the board was cleared"


# --- review round 1 on #429 (#4) -----------------------------------------------------------


def _sessions_standing(monkeypatch: pytest.MonkeyPatch, *sessions: str) -> None:
    """The project's tmux sessions as the plan and its read-only listing see them: standing
    until a stop has run that left none standing (``_stop`` returned), as a real one takes
    them down, then gone, so the strict read after the stop confirms them down."""
    standing = list(sessions)
    monkeypatch.setattr(fleet_service, "project_sessions", lambda *_: list(standing))
    monkeypatch.setattr(
        fleet_service,
        "shutdown_plan",
        lambda *_, **__: fleet_service.ShutdownPlan(sessions=list(standing)),
    )
    real_stop = fresh_service._stop

    def stop(*args: Any, **kwargs: Any) -> None:
        real_stop(*args, **kwargs)  # raises for a stop that left anything standing
        standing.clear()

    monkeypatch.setattr(fresh_service, "_stop", stop)


def test_m1_a_confirmed_discard_covers_only_the_work_it_listed(
    tmp_path: Path,
    unpushed: tuple[ProjectInfo, Path, str],
    spawned: list[tuple[str, str, dict[str, Any]]],
) -> None:
    """M1: confirming a discard said yes to a list, and the run threw away whatever its own
    later plan found: work written after the dialog opened went unseen. A confirmed list
    covers what it named, and the run refuses the rest, with the plan that found it."""
    alpha, tree, _ = unpushed
    clean = tmp_path / "trees" / "coder-2"
    _git("worktree", "add", "-q", "-b", "fleet/alpha-x/coder-2", str(clean), cwd=alpha.root)
    with store_session() as store:
        row = store.upsert_fleet_agent(_agent(alpha, "coder-2", cwd=clean, worktree=True))
        store.end_fleet_agent(row.id, exit_status=0, ended_by="stopped")
    shown = fresh_service.plan(alpha)  # what ConfirmDiscard, or the CLI at a terminal, shows
    assert [item.path for item in shown.unpushed] == [tree]
    (clean / "new-work.txt").write_text("written after the dialog opened\n", encoding="utf-8")
    before = _rows(alpha.id)

    with pytest.raises(fresh_service.FreshRefused) as refused:
        fresh_service.start_fresh(alpha, discard=shown.unpushed)

    assert refused.value.code == "unpushed_work" and f"{clean} on fleet/alpha-x/coder-2" in str(
        refused.value
    )
    assert (clean / "new-work.txt").exists() and tree.exists()
    assert _rows(alpha.id) == before and spawned == []
    assert refused.value.plan is not None
    report = fresh_service.start_fresh(alpha, discard=refused.value.plan.unpushed)
    assert {item.path for item in report.discarded} == {tree, clean} and not clean.exists()


def test_m1_a_commit_made_before_the_stop_ended_its_agent_is_judged_after_it(
    unpushed: tuple[ProjectInfo, Path, str],
    spawned: list[tuple[str, str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M1: agents run until the stop, so the check that counts is after it. A commit an
    agent made while the dialog was open moves its tip past the one confirmed: the run stops
    the fleet, removes nothing, clears nothing, and hands back the plan of what it found."""
    alpha, tree, _ = unpushed
    shown = fresh_service.plan(alpha)
    _sessions_standing(monkeypatch, "asq:asq-alpha-x")

    def shutdown(project: ProjectInfo, **_: Any) -> fleet_service.ShutdownReport:
        (tree / "late.txt").write_text("the agent's last word\n", encoding="utf-8")
        _git("add", "late.txt", cwd=tree)
        _git("commit", "-q", "-m", "late", cwd=tree)
        return fleet_service.ShutdownReport(sessions_killed=["asq:asq-alpha-x"])

    monkeypatch.setattr(fleet_service, "shutdown", shutdown)
    before = _rows(alpha.id)

    with pytest.raises(fresh_service.FreshIncomplete) as stopped:
        fresh_service.start_fresh(alpha, discard=shown.unpushed)

    assert stopped.value.plan is not None
    assert [item.commits for item in stopped.value.plan.unpushed] == [2]
    said = str(stopped.value)
    assert "found once its fleet was stopped" in said
    # Refused there, the project is left with nothing running: the refusal says how back.
    assert "its agents are stopped. Start them again with aisquare fleet restart --project " in said
    assert (
        "alpha-x <label> (the manager first), or a new manager with aisquare fleet spawn " in said
    )
    assert (tree / "late.txt").exists() and _rows(alpha.id) == before and spawned == []


def test_m2_a_dangling_origin_head_does_not_read_unpushed_commits_as_pushed(
    unpushed: tuple[ProjectInfo, Path, str], spawned: list[tuple[str, str, dict[str, Any]]]
) -> None:
    """M2: origin renamed master to main and a prune took origin/master, but origin/HEAD
    still names it. Counted against it, every rev-list failed and read as 0: --branches
    deleted a commit no remote had, unasked. The count fails closed."""
    alpha, tree, branch = unpushed
    _git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master", cwd=alpha.root)

    planned = fresh_service.plan(alpha, branches=True)

    assert [(item.path, item.branch, item.commits) for item in planned.unpushed] == [
        (tree, branch, 1)
    ]
    with pytest.raises(fresh_service.FreshRefused, match="1 commit on no remote"):
        fresh_service.start_fresh(alpha, branches=True)
    assert branch in _branches(alpha.root) and tree.exists() and spawned == []


def test_m3_a_session_tmux_would_not_kill_clears_nothing_and_starts_no_manager(
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M3: read on ``failed`` alone, a session tmux would not kill was passed over: the trees
    went, the board was cleared and a manager started beside the old session. The reset
    stops there, says which session stands and what to run, and reports the sessions."""
    alpha, _, _, _ = two
    _sessions_standing(monkeypatch, "asq:asq-alpha-x")
    monkeypatch.setattr(
        fleet_service,
        "shutdown",
        lambda *_, **__: fleet_service.ShutdownReport(
            sessions_failed=["asq:asq-alpha-x"], incomplete_projects=[alpha.id]
        ),
    )
    before = _rows(alpha.id)

    with pytest.raises(fresh_service.FreshIncomplete) as stopped:
        fresh_service.start_fresh(alpha)

    said = str(stopped.value)
    assert "tmux session asq:asq-alpha-x" in said and "the board was not cleared" in said
    # The next step is fleet shutdown's, which takes --forget; project fresh does not.
    assert "aisquare fleet shutdown --project alpha-x (whose --forget" in said
    assert stopped.value.report.sessions_failed == ["asq:asq-alpha-x"]
    assert _rows(alpha.id) == before and spawned == []


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("tmux") is None, reason="tmux is not installed"
)
def test_m1_a_project_with_nothing_to_stop_is_not_refused_inside_the_fleet_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m1: the plan asked ``fleet shutdown``'s plan always, whose guard refuses any caller
    inside the fleet's tmux server, here beta's pane, though alpha had nothing to stop. It is
    asked only when there is something to stop, and then refused in this command's words."""
    alpha = _add(tmp_path / "alpha", "alpha-x")
    srv = fleet_service.server()
    srv.run("new-session", "-d", "-s", "asq-other-project", "sleep 600")  # beta's manager
    monkeypatch.setenv("TMUX", f"{srv.socket_path()},{os.getpid()},0")

    assert fresh_service.plan(alpha).sessions == []

    srv.run("new-session", "-d", "-s", fleet_service.session_name("alpha-x"), "sleep 600")
    with pytest.raises(fresh_service.FreshRefused) as refused:
        fresh_service.plan(alpha)
    assert refused.value.code == "inside_fleet"
    assert "can't start fresh from inside the fleet's own tmux server" in str(refused.value)
    assert "fleet shutdown is running" not in str(refused.value)


def test_m2_a_store_that_fails_after_the_stop_keeps_the_partial_report(
    runner: CliRunner,
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
    stops: Stops,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """m2: past the stop only FleetError and ValueError were mapped; a store locked past its
    busy timeout took the report with it (agents stopped, trees removed). Any failure after
    the stop is a FreshIncomplete with the report so far."""
    alpha, _, _, _ = two
    with store_session() as store:
        store.upsert_fleet_agent(_agent(alpha, "coder-1"))
    real = SqliteStore.reset_project

    def locked(self: SqliteStore, project_id: str, **kwargs: Any) -> dict[str, int]:
        if kwargs.get("apply"):
            raise sqlite3.OperationalError("database is locked")
        return real(self, project_id, **kwargs)

    monkeypatch.setattr(SqliteStore, "reset_project", locked)

    result = runner.invoke(app, ["--json", "project", "fresh", "-P", "alpha-x", "--yes"])

    assert result.exit_code == 1, result.output
    said = json.loads(result.stdout)
    assert said["error"] == "fresh_incomplete"
    assert "OperationalError: database is locked" in said["detail"]
    assert said["report"]["stopped"] == ["coder-1"]
    assert len(_rows(alpha.id)["team_task"]) == 3 and spawned == []


def test_m3_discarded_names_only_what_went(
    unpushed: tuple[ProjectInfo, Path, str], spawned: list[tuple[str, str, dict[str, Any]]]
) -> None:
    """m3: ``discarded`` listed everything the plan found, so a tree git then kept (a locked
    one, which ``remove --force`` refuses) read both "threw away" and "kept"."""
    alpha, tree, _ = unpushed
    _git("worktree", "lock", str(tree), cwd=alpha.root)
    shown = fresh_service.plan(alpha)

    report = fresh_service.start_fresh(alpha, discard=shown.unpushed)

    assert report.discarded == [] and [kept.path for kept in report.kept] == [tree]
    assert tree.exists()


def test_reset_project_without_apply_deletes_nothing(
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
) -> None:
    """Nit: a destructive method whose read-only mode sat behind a flag that defaulted to
    deleting; a dropped ``apply=False`` deleted. ``apply`` has no default."""
    alpha, _, _, _ = two
    before = _rows(alpha.id)
    with store_session() as store, pytest.raises(TypeError):
        store.reset_project(alpha.id)  # type: ignore[call-arg]
    assert _rows(alpha.id) == before


def test_the_success_report_says_the_tmux_session_was_killed(
    runner: CliRunner,
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tester-fresh-fix: the report never said the tmux session went."""
    _sessions_standing(monkeypatch, "asq:asq-alpha-x")
    monkeypatch.setattr(
        fleet_service,
        "shutdown",
        lambda *_, **__: fleet_service.ShutdownReport(sessions_killed=["asq:asq-alpha-x"]),
    )

    result = runner.invoke(app, ["project", "fresh", "-P", "alpha-x", "--yes"])

    assert result.exit_code == 0, result.output
    assert "tmux session asq:asq-alpha-x ended" in result.output


def test_m2_a_count_git_will_not_give_is_never_read_as_none(
    unpushed: tuple[ProjectInfo, Path, str],
) -> None:
    """M2, the other half: whatever makes git refuse the count (here a tip it does not
    have), a failed count was 0 commits, pushed. It raises, and the plan refuses."""
    alpha, _, _ = unpushed

    with pytest.raises(fleet_service.FleetError, match="could not count the commits"):
        fleet_service.unpushed_work(alpha, [], {"fleet/alpha-x/gone": "0" * 40})


# --- review round 2 on #429 (#5) -----------------------------------------------------------


def test_n1_a_tree_spawned_after_the_stop_is_not_removed_under_its_agent(
    tmp_path: Path,
    unpushed: tuple[ProjectInfo, Path, str],
    spawned: list[tuple[str, str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """n1: the re-read after the stop took trees from live rows too, so a tree a spawn made
    between the stop and the re-read was removed, forced, before the store refused the live
    row. A live row stops the run before any tree is touched."""
    alpha, tree, _ = unpushed
    shown = fresh_service.plan(alpha)
    late = tmp_path / "trees" / "coder-9"
    _sessions_standing(monkeypatch, "asq:asq-alpha-x")

    def shutdown(project: ProjectInfo, **_: Any) -> fleet_service.ShutdownReport:
        # A spawn lands between the stop and the re-read (a respawn, a hand `fleet spawn`).
        _git("worktree", "add", "-q", "-b", "fleet/alpha-x/coder-9", str(late), cwd=alpha.root)
        with store_session() as store:
            store.upsert_fleet_agent(_agent(alpha, "coder-9", cwd=late, worktree=True))
        return fleet_service.ShutdownReport(sessions_killed=["asq:asq-alpha-x"])

    monkeypatch.setattr(fleet_service, "shutdown", shutdown)
    before = len(_rows(alpha.id)["team_task"])

    with pytest.raises(fresh_service.FreshIncomplete) as stopped:
        fresh_service.start_fresh(alpha, discard=shown.unpushed)

    assert "started since its fleet was stopped: coder-9" in str(stopped.value)
    assert stopped.value.report.worktrees_removed == []
    assert late.exists() and tree.exists(), "no tree is touched while a row is live"
    assert len(_rows(alpha.id)["team_task"]) == before and spawned == []


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("tmux") is None, reason="tmux is not installed"
)
def test_n2_a_session_tmux_cannot_be_asked_refuses_the_reset_with_nothing_touched(
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """n2, as tester-fresh-fix ran it at c85beaa4, against ``fleet shutdown``'s real plan: no
    live row, the session standing, and only ``has-session`` failing (Permission denied)
    while the socket answers the rest. The read-only listing said it could not tell
    (``None``), and the plan it then asked, which asks a session no live row lived on only
    just in case, passed over the failure and listed none: the stop was skipped, a tree
    removed under its running process and the board cleared. It refuses, nothing touched."""
    alpha, _, _, _ = two  # its manager's row ended: no live row

    def unasked(self: TmuxServer, name: str) -> bool:
        raise TmuxError("error connecting to /private/tmp/tmux-501/asq (Permission denied)")

    monkeypatch.setattr(TmuxServer, "has_session_or_raise", unasked)
    monkeypatch.setattr(TmuxServer, "has_session", lambda self, name: False)  # the lenient read
    monkeypatch.setattr(TmuxServer, "reachable", lambda self: True)  # the rest answers
    stopped: list[str] = []
    monkeypatch.setattr(fleet_service, "shutdown", lambda project, **_: stopped.append(project.id))
    before = _rows(alpha.id)

    assert fleet_service.project_sessions(alpha) is None
    with pytest.raises(fresh_service.FreshRefused) as refused:
        fresh_service.plan(alpha)
    assert refused.value.code == "fleet_error"
    assert "tmux could not say whether asq-alpha-x stands" in str(refused.value)
    with pytest.raises(fresh_service.FreshRefused):
        fresh_service.start_fresh(alpha)
    assert stopped == [] and _rows(alpha.id) == before and spawned == []


def test_no_usable_tmux_and_no_live_row_is_no_session(
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no tmux to run here and no live row, there is no session to know of, as
    ``fleet shutdown``'s plan reads it: the plan is not refused for it (a machine without
    tmux, the Windows runner)."""
    alpha, _, _, _ = two

    def no_tmux(self: TmuxServer, name: str) -> bool:
        raise TmuxUnavailable("tmux is not runnable: [Errno 2] No such file or directory")

    monkeypatch.setattr(TmuxServer, "has_session_or_raise", no_tmux)

    assert fleet_service.project_sessions(alpha) == []
    assert fresh_service.plan(alpha).sessions == []


def test_an_interrupted_stop_keeps_the_sessions_tmux_would_not_kill(
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nit: the interrupted stop's report carried the sessions killed, not those that
    failed."""
    alpha, _, _, _ = two
    _sessions_standing(monkeypatch, "asq:asq-alpha-x", "asq:asq-alpha-old")

    def interrupted(*_: Any, **__: Any) -> fleet_service.ShutdownReport:
        raise fleet_service.FleetInterrupted(
            fleet_service.ShutdownReport(
                sessions_killed=["asq:asq-alpha-old"],
                sessions_failed=["asq:asq-alpha-x"],
                interrupted="interrupted during the kill phase",
            )
        )

    monkeypatch.setattr(fleet_service, "shutdown", interrupted)

    with pytest.raises(fresh_service.FreshIncomplete) as stopped:
        fresh_service.start_fresh(alpha)

    assert stopped.value.report.sessions_killed == ["asq:asq-alpha-old"]
    assert stopped.value.report.sessions_failed == ["asq:asq-alpha-x"]


# --- tester-fresh-fix's notes at a14eafc1 (#5) ---------------------------------------------


def test_a_session_that_ended_with_its_last_window_is_said_to_have_ended(
    runner: CliRunner,
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every pane a live agent, the stop's window kills end the session before the kill
    phase looks (``sessions_absent``): the success report said "stopped: manager" and
    nothing of the session. It is ended all the same, and said so."""
    alpha, _, _, _ = two
    with store_session() as store:
        live = store.upsert_fleet_agent(_agent(alpha, "coder-1"))
    _sessions_standing(monkeypatch, "asq:asq-alpha-x")

    def shutdown(project: ProjectInfo, **_: Any) -> fleet_service.ShutdownReport:
        with store_session() as store:  # its window killed, the session went with it
            store.end_fleet_agent(live.id, exit_status=0, ended_by="stopped")
        return fleet_service.ShutdownReport(stopped=[live], sessions_absent=["asq:asq-alpha-x"])

    monkeypatch.setattr(fleet_service, "shutdown", shutdown)

    result = runner.invoke(app, ["project", "fresh", "-P", "alpha-x", "--yes"])

    assert result.exit_code == 0, result.output
    assert "tmux session asq:asq-alpha-x ended" in result.output


def test_work_found_after_the_stop_tells_a_terminal_user_to_run_it_again(
    runner: CliRunner,
    unpushed: tuple[ProjectInfo, Path, str],
    spawned: list[tuple[str, str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal found once the fleet was stopped told someone who had passed
    --discard-unpushed and confirmed the list to "pass --discard-unpushed"."""
    _, tree, _ = unpushed
    _sessions_standing(monkeypatch, "asq:asq-alpha-x")

    def shutdown(project: ProjectInfo, **_: Any) -> fleet_service.ShutdownReport:
        (tree / "late.txt").write_text("late\n", encoding="utf-8")
        _git("add", "late.txt", cwd=tree)
        _git("commit", "-q", "-m", "late", cwd=tree)
        return fleet_service.ShutdownReport(sessions_killed=["asq:asq-alpha-x"])

    monkeypatch.setattr(fleet_service, "shutdown", shutdown)
    monkeypatch.setattr("aisquare.cli.project._stdin_is_a_terminal", lambda: True)

    result = runner.invoke(
        app, ["project", "fresh", "-P", "alpha-x", "--discard-unpushed"], input="y\ny\n"
    )

    assert result.exit_code == 1, result.output
    said = " ".join(result.output.split())
    assert "run it again to see and confirm the new list" in said
    assert "2 commits on no remote" in said and "pass --discard-unpushed" not in said


def test_a_partial_reset_in_text_lists_the_worktrees_it_already_removed(
    runner: CliRunner,
    unpushed: tuple[ProjectInfo, Path, str],
    spawned: list[tuple[str, str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--json carried ``worktrees_removed`` for a reset that stopped part-way; the text said
    nothing of the trees already gone."""
    _, tree, _ = unpushed
    real = SqliteStore.reset_project

    def locked(self: SqliteStore, project_id: str, **kwargs: Any) -> dict[str, int]:
        if kwargs.get("apply"):
            raise sqlite3.OperationalError("database is locked")
        return real(self, project_id, **kwargs)

    monkeypatch.setattr(SqliteStore, "reset_project", locked)

    result = runner.invoke(
        app, ["project", "fresh", "-P", "alpha-x", "--yes", "--discard-unpushed"]
    )

    assert result.exit_code == 1, result.output
    said = " ".join(result.output.split())
    assert f"🗑 removed worktree {tree}" in said and "database is locked" in said


def test_json_refusals_carry_their_detail(
    runner: CliRunner,
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--json refusals other than unpushed_work carried only {error, ref}: a script could
    not read why (a fleet the stop cannot ask, a call from inside the fleet's server)."""
    alpha, _, _, _ = two
    with store_session() as store:
        store.upsert_fleet_agent(_agent(alpha, "coder-1"))

    def unusable(*_: Any, **__: Any) -> fleet_service.ShutdownPlan:
        raise fleet_service.FleetUnavailable("tmux is not installed (or not on PATH)")

    monkeypatch.setattr(fleet_service, "shutdown_plan", unusable)

    result = runner.invoke(app, ["--json", "project", "fresh", "-P", "alpha-x", "--yes"])

    assert result.exit_code == 2, result.output
    said = json.loads(result.stdout)
    assert said["error"] == "fleet_error" and said["ref"] == alpha.id
    assert "tmux is not installed" in said["detail"]


def test_a_commit_made_while_the_dialog_was_open_is_refused_with_nothing_stopped(
    unpushed: tuple[ProjectInfo, Path, str],
    spawned: list[tuple[str, str, dict[str, Any]]],
    stops: Stops,
) -> None:
    """tester-fresh-ui at a14eafc1: tips were judged only after the stop, so the likeliest
    case, a live agent committing while the dialog is open, was refused only once the whole
    fleet was stopped, its session killed and its claims released. A tip that moved before
    the stop is refused before it, with the fleet still running."""
    alpha, tree, _ = unpushed
    with store_session() as store:
        store.upsert_fleet_agent(_agent(alpha, "coder-2"))  # still at work
    stops.sessions = ["asq:asq-alpha-x"]
    shown = fresh_service.plan(alpha)
    (tree / "more.txt").write_text("committed while the dialog was open\n", encoding="utf-8")
    _git("add", "more.txt", cwd=tree)
    _git("commit", "-q", "-m", "more", cwd=tree)

    with pytest.raises(fresh_service.FreshRefused) as refused:
        fresh_service.start_fresh(alpha, discard=shown.unpushed)

    assert refused.value.code == "unpushed_work" and "2 commits on no remote" in str(refused.value)
    assert stops.asked == [], "nothing was stopped"
    with store_session() as store:
        assert [row.label for row in store.fleet_agents(alpha.id, live_only=True)] == ["coder-2"]
    assert spawned == []


# --- review round 4 on #429 (#5) -----------------------------------------------------------


def _stray_session(tmp_path: Path) -> tuple[ProjectInfo, TmuxServer, str, int]:
    """Alpha with no live row, and its session standing with the process a row recorded as
    died left running in it, on the test's own tmux server."""
    alpha = _add(tmp_path / "alpha", "alpha-x")
    srv = fleet_service.server()
    session = fleet_service.session_name("alpha-x")
    srv.run("new-session", "-d", "-s", session, "-n", "coder-1", "sleep 600")
    pane, pid = srv.run(
        "list-panes", "-s", "-t", f"={session}", "-F", "#{pane_id} #{pane_pid}"
    ).split()
    with store_session() as store:
        row = store.upsert_fleet_agent(
            _agent(alpha, "coder-1", pane_id=pane, tmux_socket=fleet_service.settings().tmux_socket)
        )
        store.end_fleet_agent(row.id, exit_status=None, ended_by="died")
    return alpha, srv, session, int(pid)


def _denied(self: TmuxServer, name: str) -> bool:
    raise TmuxError("error connecting to /private/tmp/tmux-501/asq (Permission denied)")


_NEEDS_TMUX = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("tmux") is None, reason="tmux is not installed"
)


@_NEEDS_TMUX
def test_n2_a_session_tmux_stops_answering_for_during_the_stop_is_never_said_to_have_ended(
    tmp_path: Path,
    spawned: list[tuple[str, str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N2 (round 4), path 1: has-session stops answering between the plan and the stop's
    kill phase, which passes over a session it asked just in case. "Neither killed nor
    failed" was read as gone: the report said the session ended, and the reset went on
    beside it and its running process. The strict read after the stop decides: it stands,
    or cannot be told of, so nothing is removed and the board stays."""
    alpha, srv, session, stray = _stray_session(tmp_path)
    real_shutdown = fleet_service.shutdown
    real_ask = TmuxServer.has_session_or_raise

    def shutdown(project: ProjectInfo, **kwargs: Any) -> fleet_service.ShutdownReport:
        monkeypatch.setattr(TmuxServer, "has_session_or_raise", _denied)
        try:
            return real_shutdown(project, **kwargs)
        finally:
            monkeypatch.setattr(TmuxServer, "has_session_or_raise", real_ask)

    monkeypatch.setattr(fleet_service, "shutdown", shutdown)
    tasks = len(_rows(alpha.id)["team_task"])

    with pytest.raises(fresh_service.FreshIncomplete) as stopped:
        fresh_service.start_fresh(alpha)

    assert f"tmux session asq:{session} still stands" in str(stopped.value)
    assert "aisquare fleet shutdown --project alpha-x" in str(stopped.value)
    assert stopped.value.report.sessions_gone == []
    assert srv.has_session(session) and _alive(stray)
    assert len(_rows(alpha.id)["team_task"]) == tasks and spawned == []


@_NEEDS_TMUX
def test_n2_a_session_the_plans_just_in_case_ask_misses_is_still_stopped(
    tmp_path: Path,
    spawned: list[tuple[str, str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N2 (round 4), path 2: has-session answers the strict listing, then fails for the
    plan's just-in-case ask, which passes over it: the plan listed none, no stop ran, and the
    reset went on over the session and its process. The stop runs for what the strict read
    saw, and the strict read after it decides; here tmux cannot answer it, so nothing is
    removed."""
    alpha, srv, session, stray = _stray_session(tmp_path)
    real_ask = TmuxServer.has_session_or_raise
    asks: list[str] = []

    def flaky(self: TmuxServer, name: str) -> bool:
        asks.append(name)
        if len(asks) == 1:  # the strict listing's ask answers…
            return real_ask(self, name)
        return _denied(self, name)  # …and every one after it fails

    monkeypatch.setattr(TmuxServer, "has_session_or_raise", flaky)
    stopped: list[str] = []

    def shutdown(project: ProjectInfo, **_: Any) -> fleet_service.ShutdownReport:
        stopped.append(project.id)
        return fleet_service.ShutdownReport()  # its kill phase passed over the session too

    monkeypatch.setattr(fleet_service, "shutdown", shutdown)
    tasks = len(_rows(alpha.id)["team_task"])

    with pytest.raises(fresh_service.FreshIncomplete) as incomplete:
        fresh_service.start_fresh(alpha)

    assert stopped == [alpha.id], "the stop ran for the session the strict read saw"
    assert f"tmux could not say whether {session} stands" in str(incomplete.value)
    monkeypatch.setattr(TmuxServer, "has_session_or_raise", real_ask)
    assert srv.has_session(session) and _alive(stray)
    assert len(_rows(alpha.id)["team_task"]) == tasks and spawned == []


def test_a_session_standing_with_nothing_stopped_refuses_with_nothing_touched(
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The strict read runs when no stop ran too: a session that came up after the plan read
    none is a refusal, before anything is touched."""
    alpha, _, _, _ = two
    reads: list[int] = []

    def sessions(*_: Any) -> list[str]:
        reads.append(1)
        return [] if len(reads) == 1 else ["asq:asq-alpha-x"]

    monkeypatch.setattr(fleet_service, "project_sessions", sessions)
    before = _rows(alpha.id)

    with pytest.raises(fresh_service.FreshRefused) as refused:
        fresh_service.start_fresh(alpha)

    assert refused.value.code == "fleet_error"
    assert "tmux session asq:asq-alpha-x still stands" in str(refused.value)
    assert _rows(alpha.id) == before and spawned == []


def test_the_brief_line_is_built_from_the_fields_not_parsed_from_the_path() -> None:
    """Nit: the brief form re-parsed the long one on ": ", and a path may hold ": "."""
    item = FreshUnpushed(path=Path("/work/a: b/coder-1"), commits=2, uncommitted=True)

    assert fresh_service.unpushed_brief(item) == (
        "coder-1: 2 commits on no remote, uncommitted changes"
    )


def test_work_found_before_the_stop_tells_a_terminal_user_to_run_it_again(
    runner: CliRunner,
    unpushed: tuple[ProjectInfo, Path, str],
    spawned: list[tuple[str, str, dict[str, Any]]],
    stops: Stops,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tips are judged before the stop now, so the refusal a terminal user who passed
    --discard-unpushed and confirmed the list most often meets is this one, and it still
    told them to "pass --discard-unpushed". Like the one after the stop, it says to run it
    again and confirm the new list, and nothing is stopped."""
    alpha, tree, _ = unpushed
    with store_session() as store:
        store.upsert_fleet_agent(_agent(alpha, "coder-2"))  # still at work
    real_plan = fresh_service.plan
    plans: list[int] = []

    def plan(project: ProjectInfo, **kwargs: Any) -> FreshPlan:
        plans.append(1)
        if len(plans) == 2:  # the run's own plan, after the terminal's yes to the first
            (tree / "more.txt").write_text("committed meanwhile\n", encoding="utf-8")
            _git("add", "more.txt", cwd=tree)
            _git("commit", "-q", "-m", "more", cwd=tree)
        return real_plan(project, **kwargs)

    monkeypatch.setattr(fresh_service, "plan", plan)
    monkeypatch.setattr("aisquare.cli.project._stdin_is_a_terminal", lambda: True)

    result = runner.invoke(
        app, ["project", "fresh", "-P", "alpha-x", "--discard-unpushed"], input="y\ny\n"
    )

    assert result.exit_code == 2, result.output
    said = " ".join(result.output.split())
    assert "run it again to see and confirm the new list" in said
    assert "2 commits on no remote" in said and "pass --discard-unpushed" not in said
    assert stops.asked == [], "nothing was stopped"
    without = runner.invoke(app, ["project", "fresh", "-P", "alpha-x", "--yes"])
    assert "pass --discard-unpushed" in " ".join(without.output.split()), (
        "without the flag, the flag is still what to pass"
    )


# --- review round 5 on #429 (#5) -----------------------------------------------------------


def test_a_store_error_in_the_confirming_read_keeps_the_partial_report(
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
    stops: Stops,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 5: the strict read after the stop ran outside the try that turns a failure past
    the stop into a FreshIncomplete with the report so far. Its read opens the store, and a
    store locked past its busy timeout escaped raw: a traceback, and no word of the agents
    already stopped."""
    alpha, _, _, _ = two
    with store_session() as store:
        store.upsert_fleet_agent(_agent(alpha, "coder-1"))
    real = fleet_service.shutdown
    after_stop = {"yes": False}

    def shutdown(project: ProjectInfo, **kwargs: Any) -> fleet_service.ShutdownReport:
        after_stop["yes"] = True
        return real(project, **kwargs)

    def project_sessions(project: ProjectInfo) -> list[str] | None:
        if after_stop["yes"]:  # the store locked past its busy timeout, after the stop
            raise sqlite3.OperationalError("database is locked")
        return []

    monkeypatch.setattr(fleet_service, "shutdown", shutdown)
    monkeypatch.setattr(fleet_service, "project_sessions", project_sessions)
    tasks = len(_rows(alpha.id)["team_task"])

    with pytest.raises(fresh_service.FreshIncomplete) as stopped:
        fresh_service.start_fresh(alpha)

    assert "OperationalError: database is locked" in str(stopped.value)
    assert stopped.value.report.stopped == ["coder-1"] and stops.asked == [alpha.id]
    assert len(_rows(alpha.id)["team_task"]) == tasks and spawned == []


# --- what 0.9.0 added to a project: briefs, the manager's state, the brain (#6) ------------


def _briefs_with_evidence(
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ProjectInfo, ProjectInfo]:
    """Two briefs on alpha and one on beta, each with a finding whose screenshot the brief
    keeps a copy of under ``~/.aisquare/briefs/<project>/``."""
    from aisquare.services import briefs, team

    alpha, beta, _, _ = two
    for project in (alpha, alpha, beta):
        monkeypatch.chdir(project.root)
        created = briefs.create(f"{project.root.name} brief", ["a requirement"])
        task, _ = team.add_task(f"{project.root.name} task")
        (project.root / "shot.png").write_bytes(b"a screenshot")
        briefs.finding(created.id, "R1", task_ref=task.id, summary="broken",
                       artifact=project.root / "shot.png")  # fmt: skip
    return alpha, beta


def _brief_files(project_id: str) -> int:
    root = paths.aisquare_home() / "briefs" / project_id
    return sum(1 for path in root.rglob("*") if path.is_file()) if root.exists() else 0


def test_a_reset_clears_the_projects_briefs_and_their_evidence_and_numbers_restart(
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0.9.0's work briefs are board content: their rows and the evidence copies they keep go
    with the board, counted in the plan and the report, and the next brief is B1 again.
    Beta's briefs and evidence are untouched."""
    from aisquare.services import briefs

    alpha, beta = _briefs_with_evidence(two, monkeypatch)
    evidence = _brief_files(alpha.id)
    assert evidence >= 2 and _brief_files(beta.id) >= 1
    beta_briefs = _raw("SELECT * FROM brief WHERE project_id = ? ORDER BY id", (beta.id,))
    beta_files = _brief_files(beta.id)

    planned = fresh_service.plan(alpha)
    report = fresh_service.start_fresh(alpha)

    assert (planned.counts.briefs, planned.counts.evidence) == (2, evidence)
    assert (report.cleared.briefs, report.cleared.evidence) == (2, evidence)
    assert _raw("SELECT COUNT(*) FROM brief WHERE project_id = ?", (alpha.id,)) == [(0,)]
    assert not (paths.aisquare_home() / "briefs" / alpha.id).exists()
    assert _raw("SELECT * FROM brief WHERE project_id = ? ORDER BY id", (beta.id,)) == beta_briefs
    assert _brief_files(beta.id) == beta_files
    monkeypatch.chdir(alpha.root)
    assert briefs.create("the next piece of work", ["a requirement"]).number == 1


def _manager_state(project: ProjectInfo, goal: str) -> Path:
    """The manager's state file, as a manager writes it, with a note beside it."""
    state = paths.manager_state_file(project.id)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(f"Goal: {goal}\nNext step: carry on\n", encoding="utf-8")
    (state.parent / "notes.md").write_text("an aside the manager kept\n", encoding="utf-8")
    return state


def test_a_reset_clears_the_managers_state_so_the_new_manager_starts_blank(
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The must: the manager's ``state.md`` (goal, decisions, open items, next step) was not
    cleared, so the "blank" manager read the old goal back from its start pack. The whole
    ``manager/`` folder goes (``state.md`` and anything kept beside it); beta's stays."""
    alpha, beta, _, _ = two
    state = _manager_state(alpha, "ship the old feature")
    theirs = _manager_state(beta, "beta's own goal")

    planned = fresh_service.plan(alpha)
    report = fresh_service.start_fresh(alpha)

    assert planned.counts.manager_state == report.cleared.manager_state == 2
    assert not state.parent.exists() and theirs.read_text(encoding="utf-8").startswith("Goal")
    monkeypatch.setenv("AISQUARE_ROLE", "manager")
    pack = team_service.hook_session_start(
        "cccc3333-0000-0000-0000-000000000000", alpha.root, "startup"
    )
    assert "ship the old feature" not in pack
    assert "Resume: no state file yet" in pack


def test_a_manager_state_that_cannot_be_removed_starts_no_manager(
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A state file that stays would be read back by the new manager: none is started, and
    the report says why and what to run."""
    alpha, _, _, _ = two
    state = _manager_state(alpha, "ship the old feature")
    real = shutil.rmtree

    def refuse(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path) == state.parent:
            raise PermissionError(13, "Permission denied")
        real(path, *args, **kwargs)

    monkeypatch.setattr("aisquare.services.fresh.shutil.rmtree", refuse)

    report = fresh_service.start_fresh(alpha)

    assert report.manager is None and spawned == []
    assert report.manager_error is not None and "state could not be removed" in report.manager_error
    assert "aisquare fleet spawn --project alpha-x manager" in report.manager_error
    assert state.exists() and report.cleared.tasks == 3


def _brain(project: ProjectInfo) -> Path:
    """A brain on disk, as gbrain leaves one (its PGLite marker)."""
    home = brain_core.brain_home(project.id)
    marker = home / ".gbrain" / "brain.pglite" / "PG_VERSION"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("16\n", encoding="utf-8")
    return home


def test_memory_takes_the_project_brain_too_and_only_the_projects(
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
    stops: Stops,
) -> None:
    """The brain is the project's remembered context beside its entries: kept by default,
    gone with --memory, counted; another project's brain is untouched."""
    alpha, beta, _, _ = two
    mine, theirs = _brain(alpha), _brain(beta)

    kept = fresh_service.start_fresh(alpha)
    assert kept.cleared.brain is None and mine.is_dir()

    planned = fresh_service.plan(alpha, memory=True)
    report = fresh_service.start_fresh(alpha, memory=True)

    assert planned.counts.brain == report.cleared.brain == 1
    assert not mine.exists() and theirs.is_dir() and brain_core.brain_ready(beta.id)


def test_a_brain_a_gbrain_run_holds_is_kept_not_torn(
    two: tuple[ProjectInfo, ProjectInfo, dict[str, str], dict[str, str]],
    spawned: list[tuple[str, str, dict[str, Any]]],
) -> None:
    """PGLite is single-process, and gbrain runs under aisquare's own lock on the brain: a
    distill or a recall in it keeps it, named as kept, rather than deleted under it. The
    board is cleared all the same."""
    alpha, _, _, _ = two
    home = _brain(alpha)

    with locking.holding(home / ".aisquare.lock", wait_s=0):  # a distill is running
        report = fresh_service.start_fresh(alpha, memory=True)

    assert home.is_dir() and brain_core.brain_ready(alpha.id)
    assert report.cleared.brain == 0 and report.cleared.tasks == 3
    assert [(kept.path, kept.reason) for kept in report.kept] == [
        (home, "a distill or a recall is running in it; kept as it is")
    ]
