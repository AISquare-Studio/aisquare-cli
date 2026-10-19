"""Restart or stop every running agent of every project at once: ``fleet_service.bulk``.

``fleet restart --all`` / ``fleet stop --all`` and asq's Settings ▸ Fleet both call it, so
what it acts on is decided here once: every agent with a process, in every project, the
managers' alone when asked, never the caller's own row, each pinned to its row id.
Driven over the fleet's fake tmux (tests/test_fleet_service.py) with two projects, each
with a manager and a coder.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import tmux as tmux_core
from aisquare.core.orchestrator import FLEET_AGENT_ENV_VAR, team_project
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services.fleet import ALREADY_EXITED, THIS_SESSION, BulkReceipt, FleetError
from tests import fakebin
from tests.test_fleet_service import FakeClock, FakeTmux, _add_task, _board_session, _git


@dataclass(frozen=True)
class Fleet:
    """Two projects, each with a running manager and a running coder."""

    alpha: ProjectInfo
    beta: ProjectInfo
    agents: dict[str, FleetAgent]
    """``alpha/manager``, ``alpha/coder-1``, ``beta/manager``, ``beta/coder-1``."""


def _project(tmp_path: Path, name: str) -> ProjectInfo:
    root = tmp_path / name
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=root)
    info = team_project(root)
    with store_session() as store:
        store.onboard_project(info)
    return info


@pytest.fixture
def tmux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeTmux:
    """``fleet.spawn`` and the listing as they are, on a tmux kept in memory, with a
    ``claude`` on PATH that is a script, and time that passes only when slept."""
    fake = FakeTmux()
    monkeypatch.setattr(fleet_service, "server", lambda config=None: fake)
    monkeypatch.setattr(tmux_core, "desktop_environment", lambda environ=None: {})
    clock = FakeClock()
    monkeypatch.setattr(fleet_service, "_sleep", clock.sleep)
    monkeypatch.setattr(fleet_service, "_monotonic", clock.monotonic)
    fakebin.executable_fake(
        tmp_path / "bin",
        "claude",
        windows='echo fake claude: "%*"\nset /p line=',
        posix='echo "fake claude: $*"\nread line',
    )
    fakebin.prepend_to_path(tmp_path / "bin", monkeypatch)
    return fake


@pytest.fixture
def fleet(tmux: FakeTmux, tmp_path: Path) -> Fleet:
    alpha, beta = _project(tmp_path, "alpha"), _project(tmp_path, "beta")
    agents: dict[str, FleetAgent] = {}
    for project, name in ((alpha, "alpha"), (beta, "beta")):
        manager = fleet_service.spawn(project, "manager").agent
        coder = fleet_service.spawn(project, "coder", worktree=False).agent
        agents[f"{name}/manager"] = manager
        agents[f"{name}/{coder.label}"] = coder
    return Fleet(alpha, beta, agents)


def _by_key(fleet: Fleet, receipts: tuple[BulkReceipt, ...]) -> dict[str, BulkReceipt]:
    names = {fleet.alpha.id: "alpha", fleet.beta.id: "beta"}
    return {f"{names[r.project.id]}/{r.agent.label}": r for r in receipts}


def _outcomes(fleet: Fleet, receipts: tuple[BulkReceipt, ...]) -> dict[str, tuple[str, str | None]]:
    return {key: (r.outcome, r.why) for key, r in _by_key(fleet, receipts).items()}


def _live_ids(project: ProjectInfo) -> set[str]:
    with store_session() as store:
        return {agent.id for agent in store.fleet_agents(project.id, live_only=True)}


ALL = {"alpha/manager", "alpha/coder-1", "beta/manager", "beta/coder-1"}


def test_restart_all_restarts_every_running_agent_in_every_project(fleet: Fleet) -> None:
    report = fleet_service.bulk("restart")

    assert _outcomes(fleet, report.receipts) == {key: ("done", None) for key in ALL}
    for key, receipt in _by_key(fleet, report.receipts).items():
        assert isinstance(receipt.result, fleet_service.RestartReceipt)
        assert receipt.result.replaced.id == fleet.agents[key].id
        assert receipt.result.started.label == fleet.agents[key].label
        assert receipt.result.was_running is True
    started = {r.result.started.id for r in report.receipts if r.result is not None}  # type: ignore[union-attr]
    assert _live_ids(fleet.alpha) | _live_ids(fleet.beta) == started
    assert report.ok and report.summary() == "restarted 4 · skipped 0 · failed 0"


def test_stop_all_stops_every_running_agent_in_every_project(fleet: Fleet) -> None:
    report = fleet_service.bulk("stop")

    assert _outcomes(fleet, report.receipts) == {key: ("done", None) for key in ALL}
    assert _live_ids(fleet.alpha) == set() and _live_ids(fleet.beta) == set()
    assert report.ok and report.summary() == "stopped 4 · skipped 0 · failed 0"


def test_the_plan_groups_by_project_with_each_manager_first(fleet: Fleet) -> None:
    plan = fleet_service.bulk_plan("stop")

    assert [(row.project.id, row.agent.label) for row in plan.rows] == [
        (fleet.alpha.id, "manager"),
        (fleet.alpha.id, "coder-1"),
        (fleet.beta.id, "manager"),
        (fleet.beta.id, "coder-1"),
    ]
    assert [row.agent.id for row in plan.targets] == [row.agent.id for row in plan.rows]


@pytest.mark.parametrize("action", ["restart", "stop"])
def test_managers_only_acts_on_the_managers_alone(
    fleet: Fleet, action: fleet_service.BulkAction
) -> None:
    report = fleet_service.bulk(action, managers_only=True)

    assert _outcomes(fleet, report.receipts) == {
        "alpha/manager": ("done", None),
        "beta/manager": ("done", None),
    }
    for name, project in (("alpha", fleet.alpha), ("beta", fleet.beta)):
        assert fleet.agents[f"{name}/coder-1"].id in _live_ids(project)  # untouched
        assert fleet.agents[f"{name}/manager"].id not in _live_ids(project)


@pytest.mark.parametrize("action", ["restart", "stop"])
def test_an_exited_agent_is_left_as_it_is(
    fleet: Fleet, tmux: FakeTmux, action: fleet_service.BulkAction
) -> None:
    exited = fleet.agents["beta/coder-1"]
    tmux.die(exited.pane_id, 0)

    report = fleet_service.bulk(action)

    outcomes = _outcomes(fleet, report.receipts)
    assert outcomes.pop("beta/coder-1") == ("skipped", "exited")
    assert outcomes == {key: ("done", None) for key in ALL - {"beta/coder-1"}}
    assert exited.pane_id not in tmux.killed  # its last screen stays, as a 💤 row's does
    assert report.ok


@pytest.mark.parametrize("action", ["restart", "stop"])
def test_the_row_the_command_runs_in_is_never_acted_on(
    fleet: Fleet, monkeypatch: pytest.MonkeyPatch, action: fleet_service.BulkAction
) -> None:
    """A manager running ``fleet restart --all`` would stop itself, and the command with
    it, half-way: its own row is left and named."""
    manager = fleet.agents["alpha/manager"]
    monkeypatch.setenv(FLEET_AGENT_ENV_VAR, manager.id)

    report = fleet_service.bulk(action)

    outcomes = _outcomes(fleet, report.receipts)
    assert outcomes.pop("alpha/manager") == ("skipped", THIS_SESSION)
    assert outcomes == {key: ("done", None) for key in ALL - {"alpha/manager"}}
    assert manager.id in _live_ids(fleet.alpha)


@pytest.mark.parametrize("on_the_board", [False, True])
def test_the_row_of_the_acting_session_is_never_acted_on(fleet: Fleet, on_the_board: bool) -> None:
    """``--as``: its full id names the row it was spawned on; a prefix of it, once its hooks
    put it on the board, the row bound to it."""
    manager = fleet.agents["beta/manager"]
    assert manager.session_id is not None
    acting = manager.session_id
    if on_the_board:
        _board_session(manager, "working")
        acting = acting[:12]

    report = fleet_service.bulk("stop", acting_session=acting)

    assert _outcomes(fleet, report.receipts)["beta/manager"] == ("skipped", THIS_SESSION)
    assert manager.id in _live_ids(fleet.beta)
    assert len(report.of("done")) == 3


@pytest.mark.parametrize("action", ["restart", "stop"])
def test_one_failing_agent_does_not_stop_the_rest_and_reads_as_failed(
    fleet: Fleet, monkeypatch: pytest.MonkeyPatch, action: fleet_service.BulkAction
) -> None:
    failing = fleet.agents["alpha/coder-1"]
    real = getattr(fleet_service, action)

    def refusing(project: ProjectInfo, label: str, **kwargs: Any) -> object:
        if kwargs.get("agent_id") == failing.id:
            raise FleetError("tmux refused the kill")
        return real(project, label, **kwargs)

    monkeypatch.setattr(fleet_service, action, refusing)

    report = fleet_service.bulk(action)

    outcomes = _outcomes(fleet, report.receipts)
    assert outcomes.pop("alpha/coder-1") == ("failed", "tmux refused the kill")
    assert outcomes == {key: ("done", None) for key in ALL - {"alpha/coder-1"}}
    assert not report.ok
    assert report.summary().endswith("· skipped 0 · failed 1")


def test_an_unexpected_error_is_that_agent_s_failure_too(
    fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = fleet.agents["beta/manager"]
    real = fleet_service.stop

    def crashing(project: ProjectInfo, label: str, **kwargs: Any) -> object:
        if kwargs.get("agent_id") == failing.id:
            raise OSError("disk full")
        return real(project, label, **kwargs)

    monkeypatch.setattr(fleet_service, "stop", crashing)

    report = fleet_service.bulk("stop")

    assert _outcomes(fleet, report.receipts)["beta/manager"] == ("failed", "OSError: disk full")
    assert len(report.of("done")) == 3


@pytest.mark.parametrize("action", ["restart", "stop"])
def test_each_action_is_pinned_to_the_row_never_the_label(
    fleet: Fleet, monkeypatch: pytest.MonkeyPatch, action: fleet_service.BulkAction
) -> None:
    asked: list[tuple[str, str | None]] = []
    real: Callable[..., object] = getattr(fleet_service, action)

    def recording(project: ProjectInfo, label: str, **kwargs: Any) -> object:
        asked.append((label, kwargs.get("agent_id")))
        return real(project, label, **kwargs)

    monkeypatch.setattr(fleet_service, action, recording)

    report = fleet_service.bulk(action)

    assert sorted(asked) == sorted((a.label, a.id) for a in fleet.agents.values())
    assert {r.agent.id for r in report.receipts} == {a.id for a in fleet.agents.values()}


@pytest.mark.parametrize("action", ["restart", "stop"])
def test_a_shown_row_whose_label_went_to_another_agent_is_skipped_and_the_new_one_left(
    fleet: Fleet, action: fleet_service.BulkAction
) -> None:
    """The dialog acts on the agents it SHOWED: one that ended since is skipped, and the
    agent that took its label since was never shown, so nothing is done to it."""
    shown = fleet_service.bulk_plan(action)
    old = fleet.agents["alpha/coder-1"]
    fleet_service.stop(fleet.alpha, old.label, agent_id=old.id)
    new = fleet_service.spawn(fleet.alpha, "coder", worktree=False, label=old.label).agent
    assert new.label == old.label and new.id != old.id

    report = fleet_service.bulk(action, shown=shown)

    outcomes = _outcomes(fleet, report.receipts)
    assert outcomes.pop("alpha/coder-1") == ("skipped", ALREADY_EXITED)
    assert outcomes == {key: ("done", None) for key in ALL - {"alpha/coder-1"}}
    assert new.id in _live_ids(fleet.alpha)


def test_an_agent_started_after_the_dialog_was_drawn_is_not_acted_on(fleet: Fleet) -> None:
    shown = fleet_service.bulk_plan("stop")
    late = fleet_service.spawn(fleet.beta, "coder", worktree=False).agent

    report = fleet_service.bulk("stop", shown=shown)

    assert late.id not in {r.agent.id for r in report.receipts}
    assert _live_ids(fleet.beta) == {late.id}


def test_a_shown_plan_must_be_the_action_and_scope_asked(fleet: Fleet) -> None:
    shown = fleet_service.bulk_plan("stop")
    with pytest.raises(ValueError, match="another action or scope"):
        fleet_service.bulk("restart", shown=shown)
    with pytest.raises(ValueError, match="another action or scope"):
        fleet_service.bulk("stop", managers_only=True, shown=shown)
    assert len(_live_ids(fleet.alpha)) == 2  # refused before anything was done


def test_the_managers_switch_redraws_from_the_same_read(fleet: Fleet) -> None:
    plan = fleet_service.bulk_plan("restart")
    managers = plan.managers()

    assert managers.managers_only and managers.action == "restart"
    assert [row.agent.id for row in managers.rows] == [
        fleet.agents["alpha/manager"].id,
        fleet.agents["beta/manager"].id,
    ]
    report = fleet_service.bulk("restart", managers_only=True, shown=managers)
    assert len(report.of("done")) == 2


def test_a_restart_skips_a_coder_whose_task_is_done_and_a_stop_stops_it(fleet: Fleet) -> None:
    task = _add_task(fleet.beta, "Ship the router")
    coder = fleet_service.spawn(fleet.beta, "coder", worktree=False, task_id=task.id).agent
    with store_session() as store:
        store.set_task_status(task.id, "done")

    restarted = fleet_service.bulk("restart")
    assert _by_key(fleet, restarted.receipts)[f"beta/{coder.label}"].why == "task is done"
    assert coder.id in _live_ids(fleet.beta)

    stopped = fleet_service.bulk("stop")
    assert _by_key(fleet, stopped.receipts)[f"beta/{coder.label}"].outcome == "done"


def test_a_project_whose_agents_cannot_be_read_is_named_and_the_rest_go_on(
    fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = fleet_service.list_agents

    def listing(project: ProjectInfo, **kwargs: Any) -> object:
        if project.id == fleet.alpha.id:
            raise FleetError("its store row is locked")
        return real(project, **kwargs)

    monkeypatch.setattr(fleet_service, "list_agents", listing)

    report = fleet_service.bulk("stop")

    assert [(p.id, why) for p, why in report.unread] == [
        (fleet.alpha.id, "its store row is locked")
    ]
    assert set(_outcomes(fleet, report.receipts)) == {"beta/manager", "beta/coder-1"}
    assert not report.ok and report.summary().endswith("· 1 project(s) not read")


def test_each_receipt_is_handed_over_as_it_is_made(fleet: Fleet) -> None:
    seen: list[str] = []
    report = fleet_service.bulk("stop", on_receipt=lambda r: seen.append(r.agent.id))
    assert seen == [r.agent.id for r in report.receipts] and len(seen) == 4


def test_nothing_running_is_an_empty_plan_and_a_clean_report(
    tmux: FakeTmux, tmp_path: Path
) -> None:
    _project(tmp_path, "quiet")
    report = fleet_service.bulk("restart")
    assert report.receipts == () and report.ok
    assert report.summary() == "restarted 0 · skipped 0 · failed 0"
