"""#38: Restart all brings back the agents that died, not only the running ones.

At about 16:50 on 2026-10-23 an outside SIGTERM killed every agent of a fleet at once
(exit 143). The owner's Restart all listed each one as exited and restarted none: the plan
acted on running agents alone. The owner's rule: an agent that ended for any reason it was
not told to (killed, crashed, its tmux server gone, a reboot) is restarted by Restart all;
one aisquare stopped on purpose (``fleet stop``, Stop all, ``shutdown``) is not. A
restarted manager is told which of its team died, with the command that brings each back.

Over the real service on the in-memory tmux of ``test_fleet_bulk`` (``fleet``: alpha and
beta, each with a running manager and coder-1).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.store import store_session
from aisquare.core.tmux import WindowInfo
from aisquare.models import FleetAgent, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import project as project_service
from tests.rendered import ANSI
from tests.test_fleet_bulk import (  # noqa: F401 (fixtures)
    Fleet,
    _by_key,
    _live_ids,
    _outcomes,
    fleet,
    tmux,
)
from tests.test_fleet_service import FakeTmux, _add_task

KILLED = 143
"""An outside SIGTERM's exit status, as the 16:50 crash recorded it."""


def _kill(server: FakeTmux, agent: FleetAgent) -> None:
    """The agent's process dies of a SIGTERM nobody in aisquare sent; its window lingers."""
    server.die(agent.pane_id, KILLED)


def _ready(server: FakeTmux, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every new window runs Claude Code at once, so a fresh restart's hand-off prompt, which
    has several lines, is typed rather than (correctly) held for an agent not up."""
    original = server.spawn_window

    def ready(*args: Any, **kwargs: Any) -> WindowInfo:
        window = original(*args, **kwargs)
        server.set_command(window.pane_id, "claude")
        return window

    monkeypatch.setattr(server, "spawn_window", ready)


def _row(agent_id: str) -> FleetAgent:
    with store_session() as store:
        row = store.get_fleet_agent(agent_id)
    assert row is not None
    return row


def _spawned(project: ProjectInfo, label: str, task: str | None = None) -> FleetAgent:
    task_id = _add_task(project, task).id if task is not None else None
    return fleet_service.spawn(
        project, "coder", worktree=False, label=label, task_id=task_id, prompt=""
    ).agent


def test_the_plan_targets_a_killed_and_a_lost_agent_and_leaves_one_stopped_on_purpose(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
) -> None:
    """Acceptance 1: a killed row (exited 143, recorded as died by the listing), a lost one
    (its pane gone) and one ``reap`` recorded after its server went are targeted as dead;
    one ``fleet stop`` ended is not, and Stop all's plan is as it was."""
    killed = fleet.agents["alpha/coder-1"]
    _kill(tmux, killed)
    lost = _spawned(fleet.alpha, "coder-lost")
    tmux.vanish(lost.pane_id)
    reaped = _spawned(fleet.beta, "coder-reaped")
    tmux.vanish(reaped.pane_id)
    fleet_service.reap(fleet.beta)
    stopped = fleet.agents["beta/coder-1"]
    # Killed (no exit status): only `ended_by` tells it was on purpose.
    fleet_service.stop(fleet.beta, stopped.label, agent_id=stopped.id, force=True)

    plan = fleet_service.bulk_plan("restart")

    targets = {row.agent.id: row for row in plan.targets}
    assert {killed.id, lost.id, reaped.id} <= set(targets)
    assert all(targets[agent.id].dead for agent in (killed, lost, reaped))
    assert _row(killed.id).ended_by == "died" and _row(reaped.id).ended_by == "died"
    assert stopped.id not in targets and _row(stopped.id).ended_by == "stopped"
    assert targets[fleet.agents["alpha/manager"].id].dead is False
    # The managers come back after their teams (their prompt names who is still dead).
    alpha = [row.agent.label for row in plan.rows if row.project.id == fleet.alpha.id]
    assert alpha[-1] == "manager"

    stop_plan = fleet_service.bulk_plan("stop")
    assert {row.agent.id for row in stop_plan.targets} == {
        fleet.agents["alpha/manager"].id,
        fleet.agents["beta/manager"].id,
    }


def test_restart_all_starts_the_dead_again_and_counts_them_apart(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
) -> None:
    killed = fleet.agents["alpha/coder-1"]
    _kill(tmux, killed)
    stopped = fleet.agents["beta/coder-1"]
    # Killed (no exit status): only `ended_by` tells it was on purpose.
    fleet_service.stop(fleet.beta, stopped.label, agent_id=stopped.id, force=True)

    report = fleet_service.bulk("restart")

    receipts = _by_key(fleet, report.receipts)
    assert set(receipts) == {"alpha/manager", "alpha/coder-1", "beta/manager"}
    revived = receipts["alpha/coder-1"]
    assert revived.outcome == "done" and revived.dead
    assert isinstance(revived.result, fleet_service.RestartReceipt)
    assert revived.result.started.label == killed.label
    assert revived.result.started.id in _live_ids(fleet.alpha)
    assert (
        report.ok and report.summary() == "restarted 3 (2 running, 1 dead) · skipped 0 · failed 0"
    )
    assert not [a for a in _live_rows(fleet.beta) if a.label == stopped.label], "left stopped"


def _live_rows(project: ProjectInfo) -> list[FleetAgent]:
    with store_session() as store:
        return store.fleet_agents(project.id, live_only=True)


def test_an_agent_that_dies_after_the_dialog_was_drawn_is_restarted_not_skipped(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
) -> None:
    """Acted on as dead: the contract's "a row that died after the plan was read must now be
    acted on, not skipped"."""
    shown = fleet_service.bulk_plan("restart")
    coder = fleet.agents["beta/coder-1"]
    _kill(tmux, coder)

    report = fleet_service.bulk("restart", shown=shown)

    assert _outcomes(fleet, report.receipts)["beta/coder-1"] == ("done", None)
    assert [a.label for a in _live_rows(fleet.beta)].count(coder.label) == 1


def test_restart_all_managers_from_the_command_line_restarts_the_dead_manager_alone(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
    runner: CliRunner,
) -> None:
    """Acceptance 3: ``fleet restart --all --managers`` after a crash that took alpha's
    manager and coder: the manager comes back, the coder is its to bring back, and the
    receipt's JSON says which row was dead."""
    manager, coder = fleet.agents["alpha/manager"], fleet.agents["alpha/coder-1"]
    _kill(tmux, manager)
    _kill(tmux, coder)

    result = runner.invoke(app, ["fleet", "restart", "--all", "--managers"])

    assert result.exit_code == 0, result.output
    lines = [line.rstrip() for line in ANSI.sub("", result.stdout).splitlines()]
    assert lines[0] == "alpha"
    assert lines[1].startswith(f"  ✓ {'manager':<24} {'manager':<10} was dead (exit {KILLED}), ")
    assert lines[-1] == "restarted 2 (1 running, 1 dead) · skipped 0 · failed 0"
    labels = [a.label for a in _live_rows(fleet.alpha)]
    assert labels == ["manager"], "the dead coder is the manager's to restart, not this run's"

    _kill(tmux, next(a for a in _live_rows(fleet.beta) if a.label == "manager"))
    result = runner.invoke(app, ["--json", "fleet", "restart", "--all", "--managers"])
    payload = json.loads(result.stdout)
    was = {(r["project"]["name"], r["agent"]["label"]): r["was"] for r in payload["receipts"]}
    assert was == {("alpha", "manager"): "running", ("beta", "manager"): "dead"}


@pytest.mark.parametrize("how", ["fleet restart manager", "restart --all --managers"])
def test_a_restarted_manager_is_told_its_dead_team_and_the_commands_that_restart_them(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
    how: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 4: the manager's first message names each teammate that died, with its
    state, its task and the exact command; one stopped on purpose is left out."""
    manager = fleet.agents["alpha/manager"]
    dead = _spawned(fleet.alpha, "coder-auth", task="Ship auth")
    _kill(tmux, dead)
    _kill(tmux, fleet.agents["alpha/coder-1"])
    stopped = _spawned(fleet.alpha, "coder-stopped")
    # Killed (no exit status): only `ended_by` tells it was on purpose.
    fleet_service.stop(fleet.alpha, stopped.label, agent_id=stopped.id, force=True)
    _ready(tmux, monkeypatch)
    tmux.typed.clear()

    if how == "fleet restart manager":
        started = fleet_service.restart(fleet.alpha, "manager").started
    else:
        report = fleet_service.bulk("restart", managers_only=True)
        [receipt] = [r for r in report.receipts if r.agent.id == manager.id]
        assert isinstance(receipt.result, fleet_service.RestartReceipt)
        started = receipt.result.started

    [prompt] = [
        text for pane, kind, text in tmux.typed if pane == started.pane_id and kind == "paste"
    ]
    with store_session() as store:
        alpha = store.get_project(fleet.alpha.id)
    assert alpha is not None and alpha.codename  # named by its codename, as a spawn gave it
    for label in ("coder-auth", "coder-1"):
        command = project_service.fleet_command("restart", alpha, label)
        assert f"`{command}`" in prompt, (label, prompt)
    assert f"coder-auth (exited ({KILLED}), task #" in prompt
    assert "Ship auth" in prompt
    assert "coder-stopped" not in prompt
    assert "2 of your team died" in prompt


def test_a_manager_whose_team_is_alive_is_told_nothing_more(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready(tmux, monkeypatch)
    tmux.typed.clear()
    started = fleet_service.restart(fleet.alpha, "manager").started
    [prompt] = [
        text for pane, kind, text in tmux.typed if pane == started.pane_id and kind == "paste"
    ]
    assert "of your team died" not in prompt


def test_stop_all_is_unchanged_it_leaves_the_dead_and_stops_the_running(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
) -> None:
    """Acceptance 5: a dead agent has nothing to stop: Stop all leaves it, as before."""
    killed = fleet.agents["alpha/coder-1"]
    _kill(tmux, killed)

    report = fleet_service.bulk("stop")

    outcomes = _outcomes(fleet, report.receipts)
    assert outcomes.pop("alpha/coder-1") == ("skipped", "exited")
    assert outcomes == {
        key: ("done", None) for key in ("alpha/manager", "beta/manager", "beta/coder-1")
    }
    assert report.summary() == "stopped 3 · skipped 1 · failed 0"
    assert killed.pane_id not in tmux.killed, "its last screen stays, as a 💤 row's does"


def test_a_window_stopped_after_its_agent_died_is_stopped_on_purpose(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
) -> None:
    """``fleet stop`` of a 💤 row removes its window: the owner said it is done with it, so
    Restart all leaves it."""
    killed = fleet.agents["alpha/coder-1"]
    _kill(tmux, killed)
    fleet_service.list_agents(fleet.alpha)  # the listing records the death
    assert _row(killed.id).ended_by == "died"

    fleet_service.stop(fleet.alpha, killed.label, agent_id=killed.id)

    assert _row(killed.id).ended_by == "stopped"
    assert killed.id not in {row.agent.id for row in fleet_service.bulk_plan("restart").targets}


def test_an_agent_that_died_before_the_column_is_read_by_its_exit_status(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
    tmp_path: Path,
) -> None:
    """A row ended before v34 (``ended_by`` NULL): killed (143) is restarted; no status
    (a stop's kill, or a reap) cannot be told apart and is left."""
    killed, quiet = fleet.agents["alpha/coder-1"], fleet.agents["beta/coder-1"]
    for agent, status in ((killed, KILLED), (quiet, None)):
        tmux.vanish(agent.pane_id)
        with store_session() as store:
            store._conn.execute(  # type: ignore[attr-defined]
                "UPDATE fleet_agent SET ended_at = ?, exit_status = ?, ended_by = NULL "
                "WHERE id = ?",
                ("2026-10-23T16:50:00+00:00", status, agent.id),
            )
            store._conn.commit()  # type: ignore[attr-defined]

    targets = {row.agent.id for row in fleet_service.bulk_plan("restart").targets}

    assert killed.id in targets and quiet.id not in targets
