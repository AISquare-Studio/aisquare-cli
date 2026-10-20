"""Restart or stop every running agent of every project at once: ``fleet_service.bulk``.

``fleet restart --all`` / ``fleet stop --all`` and asq's Settings ▸ Fleet both call it, so
what it acts on is decided here once: every agent with a process, in every project, the
managers' alone when asked, never the caller's own row, each pinned to its row id.
Driven over the fleet's fake tmux (tests/test_fleet_service.py) with two projects, each
with a manager and a coder.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import tmux as tmux_core
from aisquare.core.orchestrator import FLEET_AGENT_ENV_VAR, team_project
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services.fleet import ALREADY_EXITED, THIS_SESSION, BulkReceipt, FleetError
from tests import fakebin
from tests.rendered import ANSI
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


def _ends_when_alpha_is_done(
    fleet: Fleet, end: Callable[[FleetAgent], None]
) -> Callable[[BulkReceipt], None]:
    """An ``on_receipt`` that ends ``beta/coder-1`` once alpha's coder is done with: after
    bulk's own read, before beta's turn, as a crash in a long ``stop --all`` would."""
    coder = fleet.agents["beta/coder-1"]

    def hook(receipt: BulkReceipt) -> None:
        if receipt.agent.id == fleet.agents["alpha/coder-1"].id:
            end(coder)

    return hook


@pytest.mark.parametrize("shown", [False, True])
@pytest.mark.parametrize("how", ["exits", "stopped elsewhere"])
@pytest.mark.parametrize("action", ["restart", "stop"])
def test_an_agent_that_ends_after_the_read_is_skipped_on_its_turn_and_left_as_it_is(
    fleet: Fleet, tmux: FakeTmux, action: fleet_service.BulkAction, how: str, shown: bool
) -> None:
    """Review of #266, round 2: the turn acted on whatever the row was by then. A coder
    that exited during the run was started again by a restart, and a stop removed its 💤
    window and said "stopped"; one stopped from its own view meanwhile came back."""
    coder = fleet.agents["beta/coder-1"]

    def end(agent: FleetAgent) -> None:
        if how == "exits":
            tmux.die(agent.pane_id, 0)
        else:
            fleet_service.stop(fleet.beta, agent.label, agent_id=agent.id)

    plan = fleet_service.bulk_plan(action) if shown else None
    killed = list(tmux.killed)

    report = fleet_service.bulk(action, shown=plan, on_receipt=_ends_when_alpha_is_done(fleet, end))

    outcomes = _outcomes(fleet, report.receipts)
    assert outcomes.pop("beta/coder-1") == ("skipped", ALREADY_EXITED)
    assert outcomes == {key: ("done", None) for key in ALL - {"beta/coder-1"}}
    with store_session() as store:
        assert not [
            agent
            for agent in store.fleet_agents(fleet.beta.id, live_only=True)
            if agent.label == coder.label
        ], "the agent that ended was started again"
    if how == "exits":
        assert coder.pane_id not in tmux.killed[len(killed) :], "its 💤 window was removed"


def test_an_agent_this_run_sent_exit_to_and_another_command_ended_failed_not_skipped(
    fleet: Fleet, tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #266, round 3: every ``NoSuchAgent`` was "skipped — already exited",
    the one the hand-over's stop raises after its ``/exit`` too, when another writer (an
    operator's ``fleet stop``) ended the row in the grace. The run had taken the agent
    down and started no replacement, and said it had left it alone."""
    coder = fleet.agents["alpha/coder-1"]
    _board_session(coder, "working")  # on the board, so the hand-over marks it
    real_keys = tmux.send_keys
    stopped: list[fleet_service.StopReceipt] = []

    def exit_then_the_operator_stops_it(pane_id: str, *keys: str) -> None:
        real_keys(pane_id, *keys)
        if pane_id == coder.pane_id and not stopped:
            stopped.append(fleet_service.stop(fleet.alpha, coder.label))  # another terminal

    monkeypatch.setattr(tmux, "send_keys", exit_then_the_operator_stops_it)

    report = fleet_service.bulk("restart")

    outcomes = _outcomes(fleet, report.receipts)
    assert outcomes.pop("alpha/coder-1") == (
        "failed",
        "its /exit was sent, and another command ended it meanwhile; no replacement was started",
    )
    assert outcomes == {key: ("done", None) for key in ALL - {"alpha/coder-1"}}
    assert [s.agent.id for s in stopped] == [coder.id]
    assert coder.label not in {
        s.agent.label
        for s in fleet_service.list_agents(fleet.alpha)
        if s.state in fleet_service.PROCESS_STATES
    }, "nothing was started in its place"
    assert not report.ok


def test_a_stop_pinned_to_running_agents_leaves_an_ended_row_and_its_window(
    fleet: Fleet, tmux: FakeTmux
) -> None:
    """``live_only``: the turn's last word, for a row that ends after bulk's look at it and
    before the stop reads it. The 💤 row keeps its last screen, and nothing says stopped."""
    coder = fleet.agents["alpha/coder-1"]
    tmux.die(coder.pane_id, 3)
    fleet_service.list_agents(fleet.alpha)  # the listing records the death

    with pytest.raises(fleet_service.NoSuchAgent):
        fleet_service.stop(fleet.alpha, coder.label, agent_id=coder.id, live_only=True)

    assert coder.pane_id not in tmux.killed
    fleet_service.stop(fleet.alpha, coder.label, agent_id=coder.id)  # the 💤 row's own Stop
    assert coder.pane_id in tmux.killed


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


def test_managers_only_asks_the_board_about_no_coder_s_task(
    fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #266, round 2: each coder's task was looked up on its board for a restart
    of the managers alone, and then the coders were left out of the plan."""
    from aisquare.services import team as team_service

    task = _add_task(fleet.beta, "Ship the router")
    fleet_service.spawn(fleet.beta, "coder", worktree=False, task_id=task.id)
    asked: list[str] = []
    real = team_service.find_task

    def finding(store: Any, task_id: str, *args: Any, **kwargs: Any) -> object:
        asked.append(task_id)
        return real(store, task_id, *args, **kwargs)

    monkeypatch.setattr(team_service, "find_task", finding)

    fleet_service.bulk_plan("restart", managers_only=True)
    assert asked == []
    fleet_service.bulk_plan("restart")
    assert asked == [task.id]


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


def test_a_forgotten_project_s_running_agents_are_stopped_and_not_restarted_back(
    fleet: Fleet, tmux: FakeTmux, tmp_path: Path
) -> None:
    """Review of #266, round 2: a ``project forget`` that races a spawn leaves a tombstone
    over a running agent. ``shutdown`` reads past it, and ``stop --all`` left that agent
    running on a project nobody lists. A restart would add the project back (a spawn is a
    deliberate add), so it leaves the agent and says why."""
    with store_session() as store:
        store.forget_project(fleet.beta.id)
    gone = _project(tmp_path, "gone")
    with store_session() as store:
        store.forget_project(gone.id)  # a tombstone with no agent: nothing to list

    restarted = fleet_service.bulk("restart")
    outcomes = _outcomes(fleet, restarted.receipts)
    assert outcomes.pop("beta/manager") == ("skipped", fleet_service.PROJECT_FORGOTTEN)
    assert outcomes.pop("beta/coder-1") == ("skipped", fleet_service.PROJECT_FORGOTTEN)
    assert outcomes == {"alpha/manager": ("done", None), "alpha/coder-1": ("done", None)}
    with store_session() as store:
        assert fleet.beta.id not in {p.id for p in store.list_projects(all=True)}
    assert {r.project.id for r in restarted.receipts} == {fleet.alpha.id, fleet.beta.id}

    stopped = fleet_service.bulk("stop")
    assert _outcomes(fleet, stopped.receipts) == {key: ("done", None) for key in ALL}
    assert _live_ids(fleet.beta) == set()


def test_a_shown_project_the_second_read_cannot_list_is_named_not_called_exited(
    fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #266, round 2: the dialog showed alpha's agents, the read on confirm could
    not list alpha, and each was skipped as "already exited" while it ran on. Nothing is
    done there, and the report names the project and why, as it does with no dialog."""
    shown = fleet_service.bulk_plan("stop")
    real = fleet_service.list_agents

    def listing(project: ProjectInfo, **kwargs: Any) -> object:
        if project.id == fleet.alpha.id:
            raise FleetError("tmux did not answer in time")
        return real(project, **kwargs)

    monkeypatch.setattr(fleet_service, "list_agents", listing)

    report = fleet_service.bulk("stop", shown=shown)

    assert _outcomes(fleet, report.receipts) == {
        "beta/manager": ("done", None),
        "beta/coder-1": ("done", None),
    }
    assert [(p.id, why) for p, why in report.unread] == [
        (fleet.alpha.id, "tmux did not answer in time")
    ]
    assert _live_ids(fleet.alpha) == {
        fleet.agents["alpha/manager"].id,
        fleet.agents["alpha/coder-1"].id,
    }


def test_each_receipt_is_handed_over_as_it_is_made(fleet: Fleet) -> None:
    seen: list[str] = []
    report = fleet_service.bulk("stop", on_receipt=lambda r: seen.append(r.agent.id))
    assert seen == [r.agent.id for r in report.receipts] and len(seen) == 4


def test_an_interrupt_carries_the_receipts_made_so_far(
    fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #266, round 2: a Ctrl-C part-way through went up through ``bulk`` with the
    receipts of the agents already restarted, and ``--json`` printed nothing. As
    ``shutdown``'s does, the interrupt carries the report of how far it got."""
    real = fleet_service.stop
    coder = fleet.agents["alpha/coder-1"]

    def interrupted(project: ProjectInfo, label: str, **kwargs: Any) -> object:
        if kwargs.get("agent_id") == coder.id:
            raise KeyboardInterrupt
        return real(project, label, **kwargs)

    monkeypatch.setattr(fleet_service, "stop", interrupted)

    with pytest.raises(fleet_service.BulkInterrupted) as caught:
        fleet_service.bulk("stop")

    report = caught.value.report
    assert _outcomes(fleet, report.receipts) == {"alpha/manager": ("done", None)}
    assert report.interrupted == (
        "interrupted while stopping coder-1 (alpha); the rest were left as they were"
    )
    assert not report.ok
    assert fleet.agents["beta/manager"].id in _live_ids(fleet.beta)


def test_nothing_running_is_an_empty_plan_and_a_clean_report(
    tmux: FakeTmux, tmp_path: Path
) -> None:
    _project(tmp_path, "quiet")
    report = fleet_service.bulk("restart")
    assert report.receipts == () and report.ok
    assert report.summary() == "restarted 0 · skipped 0 · failed 0"


# --- the command line, over the real service -----------------------------------------


def test_stop_all_managers_from_the_command_line_stops_both_managers(
    fleet: Fleet, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["fleet", "stop", "--all", "--managers"])

    assert result.exit_code == 0, result.output
    lines = [line.rstrip() for line in ANSI.sub("", result.stdout).splitlines()]
    assert lines == [
        "alpha",
        f"  ✓ {'manager':<24} {'manager':<10} stopped",
        "beta",
        f"  ✓ {'manager':<24} {'manager':<10} stopped",
        "stopped 2 · skipped 0 · failed 0",
    ]
    assert fleet.agents["alpha/coder-1"].id in _live_ids(fleet.alpha)


def test_restart_all_from_inside_a_manager_leaves_it_and_says_so_in_json(
    fleet: Fleet, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = fleet.agents["beta/manager"]
    monkeypatch.setenv(FLEET_AGENT_ENV_VAR, manager.id)

    result = runner.invoke(app, ["--json", "fleet", "restart", "--all"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    outcomes = {
        (r["project"]["name"], r["agent"]["label"]): (r["outcome"], r["why"])
        for r in payload["receipts"]
    }
    assert outcomes == {
        ("alpha", "manager"): ("done", None),
        ("alpha", "coder-1"): ("done", None),
        ("beta", "manager"): ("skipped", THIS_SESSION),
        ("beta", "coder-1"): ("done", None),
    }
    assert payload["summary"] == {"done": 3, "skipped": 1, "failed": 0} and payload["ok"]
