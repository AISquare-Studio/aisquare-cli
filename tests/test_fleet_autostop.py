"""P2-W1b (#283): a finished worker is stopped by the fleet, and only a finished one.

A tester that posted its verdict and a reviewer whose review was in kept their seats, their
memory and a slot of ``max_agents_total`` until the manager, woken for it, stopped them.
``services.autostop.finished`` is the one predicate; the Stop hook, ``fleet reap
--finished`` and a refused spawn act on it, and the exit says ``actor: auto-stop`` and wakes
nobody.

Driven as ``tests/test_fleet_service.py`` drives the fleet (its fixtures, its in-memory
``FakeTmux``), with ``tests/test_first_prompt_delivery.py``'s Claude Code input box. The idle
window is crossed by moving ``autostop``'s clock, never by sleeping.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import harness, selfcli
from aisquare.core.config import load_config, save_config
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo, TeamSession
from aisquare.services import autostop
from aisquare.services import fleet as fleet_service
from aisquare.services import hooks as hooks_service
from aisquare.services import team as team_service
from tests import test_fleet_service as fleet_suite
from tests.test_first_prompt_delivery import _box
from tests.test_fleet_service import FakeTmux

tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project

LATER = timedelta(minutes=10)


@pytest.fixture(autouse=True)
def nudges(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every manager nudge, recorded: an auto-stop must cause none."""
    calls: list[str] = []

    def record(project_id: str, *, reason: str, board: str | None = None) -> bool:
        calls.append(reason)
        return False

    monkeypatch.setattr(fleet_service, "nudge_manager", record)
    return calls


@pytest.fixture
def later(monkeypatch: pytest.MonkeyPatch) -> None:
    """``autostop``'s clock past the idle window (5 min by default)."""
    monkeypatch.setattr(autostop, "_now", lambda: datetime.now(tz=UTC) + LATER)


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waits: list[float] = []
    monkeypatch.setattr(autostop, "_sleep", waits.append)
    return waits


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    started: list[list[str]] = []
    monkeypatch.setattr(hooks_service, "_detach", started.append)
    return started


def _worker(
    project: ProjectInfo, tmux: FakeTmux, role: str = "reviewer", task_id: str | None = None
) -> FleetAgent:
    """A live worker idle at an empty prompt: its pane runs claude, last printed long ago,
    and its board row reads ``waiting``."""
    agent = fleet_service.spawn(project, role, task_id=task_id, worktree=False).agent
    assert agent.session_id is not None
    tmux.set_command(agent.pane_id, "claude")
    tmux.screens[agent.pane_id] = _box()
    tmux.output_at[agent.pane_id] = datetime.now(tz=UTC) - LATER
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=agent.session_id,
                project_id=agent.project_id,
                role=role,
                started_at=now,
                last_seen_at=now,
                cursor=store.latest_seq(agent.project_id),
            )
        )
        store.touch_session(agent.session_id, state="waiting")
    return agent


def _result(agent: FleetAgent, text: str = "REVIEW #361: PASS") -> None:
    assert agent.session_id is not None
    team_service.add_note(text, kind="result", session_ref=agent.session_id)
    with store_session() as store:  # it read the board up to its own result
        store.touch_session(agent.session_id, cursor=store.latest_seq(agent.project_id))


def _verdict(agent: FleetAgent) -> autostop.Verdict:
    return autostop._decide(fleet_service.status_of(agent), load_config().fleet)


def _live(agent: FleetAgent) -> bool:
    with store_session() as store:
        row = store.get_fleet_agent(agent.id)
    return row is not None and row.ended_at is None


# --- the predicate -------------------------------------------------------------------------


def test_a_reviewer_that_posted_its_verdict_and_idles_has_finished(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, later: None
) -> None:
    reviewer = _worker(project, tmux)
    assert _verdict(reviewer).blocked == "it has written no result since it was spawned"
    _result(reviewer)
    verdict = _verdict(reviewer)
    assert verdict.finished and verdict.reason == "it posted its result"


def test_a_tester_spawned_for_a_task_has_finished_once_that_task_is_closed(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, later: None
) -> None:
    task, _ = team_service.add_task("wire auth", cwd=project.root)
    tester = _worker(project, tmux, role="tester", task_id=task.id)
    assert _verdict(tester).blocked == "the task it was spawned for is still open"
    with store_session() as store:
        store.set_task_status(task.id, "done")
    verdict = _verdict(tester)
    assert verdict.finished and verdict.reason == f"its task #{task.number} is done"


def _draft(agent: FleetAgent, tmux: FakeTmux) -> None:
    tmux.screens[agent.pane_id] = _box("also check the CI on #3")


def _unread(agent: FleetAgent, tmux: FakeTmux) -> None:
    team_service.add_note("one more: look at #362", to_role=agent.label, cwd=agent.cwd)


def _attention(agent: FleetAgent, tmux: FakeTmux) -> None:
    assert agent.session_id is not None
    with store_session() as store:
        store.touch_session(agent.session_id, state="attention")


def _limited(agent: FleetAgent, tmux: FakeTmux) -> None:
    assert agent.session_id is not None
    with store_session() as store:
        store.touch_session(agent.session_id, state="limited")


def _claim(agent: FleetAgent, tmux: FakeTmux) -> None:
    task, _ = team_service.add_task("verify #12", cwd=agent.cwd)
    team_service.claim_task(task.id, session_ref=agent.session_id)


def _queued(agent: FleetAgent, tmux: FakeTmux) -> None:
    task, _ = team_service.add_task("verify #14", cwd=agent.cwd)
    with store_session() as store:
        store.set_task_status(task.id, "review")


def _printing(agent: FleetAgent, tmux: FakeTmux) -> None:
    tmux.output_at[agent.pane_id] = datetime.now(tz=UTC) + LATER


@pytest.mark.parametrize(
    ("setup", "blocked"),
    [
        (_draft, "a draft is in its input box"),
        (_unread, "a note addressed to it is unread"),
        (_attention, "it reads attention"),
        (_limited, "it reads limited"),
        (_claim, "it holds a claim"),
        (_queued, "a task is queued that a reviewer would take"),
        (_printing, "its pane printed within the idle window"),
    ],
    ids=["draft", "unread-note", "attention", "limited", "claim", "queued", "printing"],
)
def test_each_condition_keeps_a_finished_reviewer(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    later: None,
    setup: object,
    blocked: str,
) -> None:
    """The refusal table. The control is the reviewer above: the same agent, finished."""
    reviewer = _worker(project, tmux)
    _result(reviewer)
    assert _verdict(reviewer).finished, "the control"
    setup(reviewer, tmux)  # type: ignore[operator]
    verdict = _verdict(reviewer)
    assert not verdict.finished
    assert verdict.blocked is not None and verdict.blocked.startswith(blocked), verdict.blocked


def test_a_manager_or_a_coder_is_never_a_candidate(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, later: None
) -> None:
    """Coders opt in through ``auto_stop_roles``; a manager or the captain never can."""
    coder = _worker(project, tmux, role="coder")
    _result(coder)
    assert _verdict(coder).blocked == "coder is not in auto_stop_roles"
    config = load_config()
    config.fleet.auto_stop_roles = [*config.fleet.auto_stop_roles, "coder", "manager"]
    save_config(config)
    assert _verdict(coder).finished, "opted in"
    manager = _worker(project, tmux, role="manager")
    _result(manager)
    assert _verdict(manager).blocked == "manager is not in auto_stop_roles"


# --- (a) the Stop hook arms a worker; a later turn supersedes it -----------------------------


def _stop_hook(agent: FleetAgent, monkeypatch: pytest.MonkeyPatch) -> None:
    assert agent.session_id is not None
    with monkeypatch.context() as patch:
        patch.setenv("AISQUARE_FLEET_AGENT", agent.id)
        assert hooks_service.turn_stopped(agent.cwd, session_id=agent.session_id) is None


def _token(agent: FleetAgent) -> str:
    assert agent.session_id is not None
    with store_session() as store:
        token = store.get_meta(autostop.arm_key(agent.session_id))
    assert token is not None
    return token


def test_a_finished_reviewer_is_stopped_once_by_auto_stop_and_wakes_nobody(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    armed: list[list[str]],
    slept: list[float],
    nudges: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its Stop arms the worker; the worker sleeps the idle window, asks again and stops it.
    The exit is ``actor=auto-stop`` with its reason, info to the manager, and no nudge."""
    reviewer = _worker(project, tmux)
    _result(reviewer)
    _stop_hook(reviewer, monkeypatch)
    token = _token(reviewer)
    assert reviewer.session_id is not None
    assert armed == [
        selfcli.argv_for(
            [
                "--quiet",
                "hook",
                "auto-stop",
                reviewer.session_id,
                "--after",
                "305",
                "--token",
                token,
            ]
        )
    ]
    monkeypatch.setattr(autostop, "_now", lambda: datetime.now(tz=UTC) + LATER)
    nudges.clear()  # the result note itself wakes the manager, as it should

    assert autostop.check(reviewer.session_id, after=305, token=token) == "stopped"

    assert slept == [305] and not _live(reviewer) and nudges == []
    with store_session() as store:
        [exited] = [
            e for e in store.recent_events(project.id, limit=10) if e.kind == "agent_exited"
        ]
        manager = TeamSession(
            id="aaaa1111-0000-0000-0000-000000000000",
            project_id=project.id,
            role="manager",
            started_at=datetime.now(tz=UTC),
            last_seen_at=datetime.now(tz=UTC),
        )
    assert exited.actor == autostop.ACTOR
    assert exited.meta is not None and exited.meta["reason"] == "it posted its result"
    assert "stopped by auto-stop (it posted its result)" in exited.text
    assert team_service.wake_class(exited, manager) == "info"
    # Once: the row is gone, so a second worker finds nothing to stop.
    assert autostop.check(reviewer.session_id, after=305, token=token) == "it has ended"


def test_a_new_turn_supersedes_the_worker_an_earlier_stop_armed(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    armed: list[list[str]],
    slept: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer = _worker(project, tmux)
    _result(reviewer)
    _stop_hook(reviewer, monkeypatch)
    first = _token(reviewer)
    _stop_hook(reviewer, monkeypatch)  # the human asked it something; that turn ended too
    assert reviewer.session_id is not None and _token(reviewer) != first
    monkeypatch.setattr(autostop, "_now", lambda: datetime.now(tz=UTC) + LATER)

    assert autostop.check(reviewer.session_id, after=305, token=first) == (
        "a later turn ended since it was armed"
    )
    assert _live(reviewer)


def test_a_worker_that_turned_busy_is_kept_when_its_worker_wakes(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    armed: list[list[str]],
    slept: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer = _worker(project, tmux)
    _result(reviewer)
    _stop_hook(reviewer, monkeypatch)
    token = _token(reviewer)
    monkeypatch.setattr(autostop, "_now", lambda: datetime.now(tz=UTC) + LATER)
    _draft(reviewer, tmux)  # someone started typing to it
    assert reviewer.session_id is not None
    assert autostop.check(reviewer.session_id, after=305, token=token) == (
        "kept: a draft is in its input box"
    )
    assert _live(reviewer)


def test_a_manager_or_an_unfinished_worker_arms_nothing(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    armed: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _worker(project, tmux, role="manager")
    reviewer = _worker(project, tmux)  # no result yet
    _stop_hook(manager, monkeypatch)
    _stop_hook(reviewer, monkeypatch)
    assert armed == []


def test_off_arms_nothing(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    armed: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config()
    config.fleet.auto_stop_finished = False
    save_config(config)
    reviewer = _worker(project, tmux)
    _result(reviewer)
    _stop_hook(reviewer, monkeypatch)
    assert armed == []


# --- (b) fleet reap --finished -------------------------------------------------------------


def test_reap_finished_dry_run_lists_candidates_with_reasons_and_stops_none(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, later: None
) -> None:
    done = _worker(project, tmux)
    _result(done)
    busy = _worker(project, tmux)

    shown = CliRunner().invoke(
        app, ["--json", "fleet", "reap", "--finished", "--dry-run", "--project", project.id]
    )
    assert shown.exit_code == 0, shown.output
    rows = {row["label"]: row for row in json.loads(shown.stdout)}
    assert rows[done.label]["finished"] is True and rows[done.label]["stopped"] is False
    assert rows[done.label]["reason"] == "it posted its result"
    assert rows[busy.label]["kept_because"] == "it has written no result since it was spawned"
    assert _live(done) and _live(busy)

    stopped = CliRunner().invoke(
        app, ["--json", "fleet", "reap", "--finished", "--project", project.id]
    )
    rows = {row["label"]: row for row in json.loads(stopped.stdout)}
    assert rows[done.label]["stopped"] is True and not _live(done) and _live(busy)


def test_dry_run_alone_is_refused(tmux: FakeTmux, project: ProjectInfo) -> None:
    result = CliRunner().invoke(app, ["fleet", "reap", "--dry-run", "--project", project.id])
    # Read as a person reads it: Rich colours the usage box and wraps it to the terminal.
    plain = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", result.output).replace("│", " ").split())
    assert result.exit_code == 2 and "--dry-run goes with --finished" in plain, plain


# --- (c) a refused spawn makes room ---------------------------------------------------------


def test_a_spawn_the_budget_refuses_stops_a_finished_worker_and_goes_ahead(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, later: None
) -> None:
    done = _worker(project, tmux)
    _result(done)
    config = load_config()
    config.fleet.max_agents_total = 1
    config.fleet.min_free_mb = 0
    save_config(config)

    receipt = fleet_service.spawn(project, "coder", worktree=False)

    assert not _live(done) and _live(receipt.agent)
    assert any(
        note.startswith(f"stopped finished workers to make room: {done.label}")
        for note in receipt.notes
    ), receipt.notes


def test_with_auto_stop_off_the_refusal_stands(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, later: None
) -> None:
    done = _worker(project, tmux)
    _result(done)
    config = load_config()
    config.fleet.max_agents_total = 1
    config.fleet.min_free_mb = 0
    config.fleet.auto_stop_finished = False
    save_config(config)

    with pytest.raises(fleet_service.CapacityRefused):
        fleet_service.spawn(project, "coder", worktree=False)
    assert _live(done)


def test_the_manager_cycle_says_which_workers_stop_by_themselves() -> None:
    def cycle() -> str:
        return " ".join(harness.role_cycle("manager", "abcd1234", fleet=lambda verb: verb))

    assert "stop by themselves (tester, reviewer, validator, ui-tester;" in cycle()
    config = load_config()
    config.fleet.auto_stop_finished = False
    save_config(config)
    assert "stop by themselves" not in cycle(), "off: the manager stops them, as before"
