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
from aisquare.core.tmux import TmuxError, WindowInfo
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
from tests.test_fleet_service import FakeTmux, _add_task, _first_lines

KILLED = 143
"""An outside SIGTERM's exit status, as the 16:50 crash recorded it."""


def _kill(server: FakeTmux, agent: FleetAgent) -> None:
    """The agent's process dies of a SIGTERM nobody in aisquare sent; its window lingers."""
    server.die(agent.pane_id, KILLED)


def _ready(server: FakeTmux, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every new window runs Claude Code at once, so the replacement is Claude Code from its
    first poll. Its first message, the hand-off or resume line with the dead team in it, rides
    its command line (P0-W1a); :func:`_first_lines` reads it, typed or not."""
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

    [prompt] = _first_lines(tmux, started.pane_id)
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
    [prompt] = _first_lines(tmux, started.pane_id)
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


# --- #274 delta review 2: m1-m3 ------------------------------------------------------


def _nudges(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Each manager wake-up's reason, from here on."""
    reasons: list[str] = []

    def nudge(project_id: str, *, reason: str, board: str | None = None) -> bool:
        reasons.append(reason)
        return True

    monkeypatch.setattr(fleet_service, "nudge_manager", nudge)
    return reasons


def _exited(project: ProjectInfo, agent: FleetAgent) -> list[str]:
    """The ``agent_exited`` lines the board holds for ``agent``."""
    with store_session() as store:
        events = store.recent_events(project.id)
    return [
        e.text for e in events if e.kind == "agent_exited" and e.text.startswith(f"{agent.label} ")
    ]


def _respawn_refused(agent_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The replacement for ``agent_id`` fails to start; every other starts."""
    real = fleet_service._respawn

    def refused(project: ProjectInfo, agent: FleetAgent, *args: Any, **kwargs: Any) -> Any:
        if agent.id == agent_id:
            raise fleet_service.FleetError("tmux refused the window")
        return real(project, agent, *args, **kwargs)

    monkeypatch.setattr(fleet_service, "_respawn", refused)


def _dies_before_its_turn(
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
    agent: FleetAgent,
    status: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``agent`` dies with ``status`` after Restart all's plan, just before its turn's look."""
    look = fleet_service._still_to_restart

    def dies_first(row: fleet_service.BulkRow) -> None:
        if row.agent.id == agent.id:
            tmux.die(agent.pane_id, status)
        look(row)

    monkeypatch.setattr(fleet_service, "_still_to_restart", dies_first)


@pytest.mark.parametrize("respawn", ["fails", "starts"])
def test_a_row_that_dies_between_the_plan_and_its_turn_keeps_its_window_and_comes_back(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
    monkeypatch: pytest.MonkeyPatch,
    respawn: str,
) -> None:
    """m1: the turn's look records the death with the wake-up held back, as the restart
    holds its own. It woke the manager for an agent coming back, and a respawn that then
    failed woke it a second time. Either way the row's last screen stays until a
    replacement supersedes it, and one whose respawn failed is still one that died."""
    coder = fleet.agents["alpha/coder-1"]
    _dies_before_its_turn(tmux, coder, KILLED, monkeypatch)
    if respawn == "fails":
        _respawn_refused(coder.id, monkeypatch)
    nudges = _nudges(monkeypatch)

    report = fleet_service.bulk("restart")

    outcome = _outcomes(fleet, report.receipts)["alpha/coder-1"]
    if respawn == "starts":
        assert outcome == ("done", None)
        assert f"{coder.label} exited" not in nudges, "no manager woken for one coming back"
        return
    assert outcome[0] == "failed"
    assert coder.pane_id not in tmux.killed, "its last screen stays"
    assert _row(coder.id).ended_by == "died" and _row(coder.id).exit_status == KILLED
    assert nudges.count(f"{coder.label} exited") == 1, "woken once, for the failed start"
    targets = {row.agent.id: row for row in fleet_service.bulk_plan("restart").targets}
    assert coder.id in targets and targets[coder.id].dead, "the next Restart all brings it back"


def test_a_row_that_exits_0_before_its_turn_is_left_and_its_manager_woken(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """m1's held-back announcement is owed when the look then leaves the row: exit 0 is
    ended by itself, so nothing comes back, and the board hears of it as from any listing,
    once. A clean exit is news, not a wake (P1 WS-2, #279), so no manager is woken."""
    coder = fleet.agents["alpha/coder-1"]
    _dies_before_its_turn(tmux, coder, 0, monkeypatch)
    nudges = _nudges(monkeypatch)

    report = fleet_service.bulk("restart")

    assert _outcomes(fleet, report.receipts)["alpha/coder-1"][0] == "skipped"
    assert _exited(fleet.alpha, coder) == [f"{coder.label} exited (0)"]
    assert not [n for n in nudges if f"{coder.label} exited" in n], "a clean exit is news"


def test_a_restart_whose_vanished_row_another_reader_ends_first_still_died(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """m1 with m3: a pane that VANISHED (its server gone) is ended by the restart through
    ``stop`` on the copy it read. When a ``reap`` records it in between, ``stop`` finds an
    ended row with no window; m3 marks such a row stopped for ``fleet stop``, and for the
    restart that left a respawn that failed as an agent no Restart all brings back. The
    restart's ``stop`` hands the row back as it is: still one that died."""
    coder = fleet.agents["alpha/coder-1"]
    observe = fleet_service._observe_sockets
    reaped: list[bool] = []

    def reap_first(*args: Any, **kwargs: Any) -> Any:
        if not reaped:
            reaped.append(True)
            fleet_service.reap(fleet.alpha)
            assert _row(coder.id).ended_by == "died"
        return observe(*args, **kwargs)

    tmux.vanish(coder.pane_id)
    monkeypatch.setattr(fleet_service, "_observe_sockets", reap_first)
    _respawn_refused(coder.id, monkeypatch)

    with pytest.raises(fleet_service.FleetError, match="tmux refused"):
        fleet_service.restart(fleet.alpha, coder.label, agent_id=coder.id)

    assert reaped and _row(coder.id).ended_by == "died"
    assert coder.id in {row.agent.id for row in fleet_service.bulk_plan("restart").targets}


def _the_tick_records_the_exit_first(
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent exits 1 on this stop's ``/exit`` (a hook error, another binary), and the
    UI's tick lists the project before the stop looks again: the listing records it
    first, as ``died``."""
    send_keys = tmux.send_keys

    def enter(pane_id: str, *keys: str) -> None:
        send_keys(pane_id, *keys)
        if keys == ("Enter",) and pane_id in tmux.facts:
            tmux.die(pane_id, 1)
            fleet_service.list_agents(project)

    monkeypatch.setattr(tmux, "send_keys", enter)


def test_a_stop_whose_exit_the_tick_recorded_first_is_left_down_by_restart_all(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """m2, ``fleet stop``: the death this stop's ``/exit`` caused is the owner's stop."""
    coder = fleet.agents["alpha/coder-1"]
    _the_tick_records_the_exit_first(tmux, fleet.alpha, monkeypatch)

    receipt = fleet_service.stop(fleet.alpha, coder.label, agent_id=coder.id)

    assert receipt.agent.exit_status == 1
    assert _row(coder.id).ended_by == "stopped"
    assert coder.id not in {row.agent.id for row in fleet_service.bulk_plan("restart").targets}


@pytest.mark.parametrize("record", ["_record_lost", "_record_self_exit"])
def test_a_shutdown_row_a_listing_recorded_first_is_left_down_by_restart_all(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
    record: str,
) -> None:
    """m2, ``shutdown``: its two recorders keep a row's first end, and a listing that got
    there first wrote ``died``; the shutdown still took it down on purpose."""
    coder = fleet.agents["alpha/coder-1"]
    tmux.die(coder.pane_id, 1)
    fleet_service.list_agents(fleet.alpha)  # the tick, between shutdown's snapshot and this
    assert _row(coder.id).ended_by == "died"
    report = fleet_service.ShutdownReport()

    if record == "_record_lost":
        fleet_service._record_lost(coder, "its session was killed under it", report)
    else:
        fleet_service._record_self_exit(coder, 1, report)

    assert _row(coder.id).ended_by == "stopped"
    assert coder.id not in {row.agent.id for row in fleet_service.bulk_plan("restart").targets}


def test_stopping_a_dead_agent_whose_server_is_gone_keeps_it_down(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
    runner: CliRunner,
) -> None:
    """m3: the motivating crash took the tmux server with it, so the dead row has no window
    left to remove. ``fleet stop`` records the owner's word anyway, and says that is all it
    did; refused, nothing could keep the agent down and Restart all brought it back."""
    coder = fleet.agents["alpha/coder-1"]
    _kill(tmux, coder)
    fleet_service.list_agents(fleet.alpha)  # recorded as died
    tmux.kill_server()
    assert coder.id in {row.agent.id for row in fleet_service.bulk_plan("restart").targets}

    result = runner.invoke(app, ["fleet", "stop", "--project", "alpha", coder.label])

    assert result.exit_code == 0, result.output
    said = " ".join(ANSI.sub("", result.stdout).split())
    assert f"marked {coder.label} ({coder.id}) stopped" in said, said
    assert "Restart all leaves it" in said
    assert _row(coder.id).ended_by == "stopped"
    assert coder.id not in {row.agent.id for row in fleet_service.bulk_plan("restart").targets}
    again = runner.invoke(app, ["fleet", "stop", "--project", "alpha", coder.label])
    assert again.exit_code != 0, "stopped already, and nothing left to remove"


def test_fleet_stop_json_says_it_only_marked_a_dead_agent_whose_server_is_gone(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
    runner: CliRunner,
) -> None:
    """Final delta review, n4: the human line said "marked … stopped" and ``--json`` read as
    a plain stop. It carries ``marked_only``."""
    coder = fleet.agents["alpha/coder-1"]
    _kill(tmux, coder)
    fleet_service.list_agents(fleet.alpha)
    tmux.kill_server()

    result = runner.invoke(app, ["--json", "fleet", "stop", "--project", "alpha", coder.label])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["marked_only"] is True


def test_a_dead_row_no_listing_could_record_still_shows_its_exit_code(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """n6: a dead pane whose death no listing could write (a locked store) holds its exit
    code only in the pane. Restart all's list and ``restart --all``'s line show it, as for
    a row that was recorded."""
    from aisquare.cli.ui.views.fleet_bulk import plan_text

    coder = fleet.agents["alpha/coder-1"]
    _kill(tmux, coder)
    monkeypatch.setattr(fleet_service, "_end_dead_rows", lambda *args, **kwargs: [])

    plan = fleet_service.bulk_plan("restart")
    [row] = [row for row in plan.targets if row.agent.id == coder.id]
    assert row.dead and row.agent.exit_status is None, "the premise: not recorded"

    assert fleet_service.exit_code_of(row.status) == KILLED
    assert "coder-1 · coder · " in plan_text(plan).plain
    assert f"exited ({KILLED}) — " in plan_text(plan).plain
    result = runner.invoke(app, ["fleet", "restart", "--all"])
    assert result.exit_code == 0, result.output
    assert f"was dead (exit {KILLED}), restarted" in ANSI.sub("", result.stdout)


# --- final delta review of #274 (seq 1719), in #48 ------------------------------------


@pytest.mark.parametrize("pane", ["vanished", "dead"])
def test_a_fleet_stop_that_lands_mid_restart_is_never_undone(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
    monkeypatch: pytest.MonkeyPatch,
    pane: str,
) -> None:
    """m1: the owner's ``fleet stop`` ends the row between the restart's read and its own
    look. The ended row came back from ``stop`` (a vanished pane) or ``_end_dead_rows`` (a
    dead one) as it was, and the restart started the agent again over the stop. It is
    refused, and nothing is started."""
    coder = fleet.agents["alpha/coder-1"]
    observe = fleet_service._observe_sockets
    stopped: list[bool] = []

    def the_owner_stops_it(*args: Any, **kwargs: Any) -> Any:
        if not stopped:
            stopped.append(True)
            with store_session() as store:
                store.end_fleet_agent(coder.id, exit_status=None, ended_by="stopped")
        return observe(*args, **kwargs)

    if pane == "vanished":
        tmux.vanish(coder.pane_id)
    else:
        _kill(tmux, coder)
    monkeypatch.setattr(fleet_service, "_observe_sockets", the_owner_stops_it)
    monkeypatch.setattr(fleet_service, "_pane_alive", lambda agent: False)
    spawned = len(tmux.spawned)

    with pytest.raises(fleet_service.NoSuchAgent, match="stopped meanwhile"):
        fleet_service.restart(fleet.alpha, coder.label, agent_id=coder.id)

    assert stopped and len(tmux.spawned) == spawned, "nothing started over the stop"
    assert _row(coder.id).ended_by == "stopped"
    assert not [a for a in _live_rows(fleet.alpha) if a.label == coder.label]


def test_a_row_another_reader_recorded_first_wakes_the_manager_once(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """n2: the row exits 0 before its turn, and the UI's tick records it just before the
    turn's own look: the tick announces it. The look's ``_end_dead_rows`` hands that row
    back too, and the look then owed an announcement for it as if it had held one back: two
    "exited" for one exit. It owes only its own. A clean exit wakes no manager (P1 WS-2,
    #279), so the board is where the one announcement is counted."""
    coder = fleet.agents["alpha/coder-1"]
    _dies_before_its_turn(tmux, coder, 0, monkeypatch)
    observe = fleet_service._observe_sockets
    ticked: list[bool] = []

    def the_tick_first(*args: Any, **kwargs: Any) -> Any:
        if not ticked and tmux.facts[coder.pane_id].dead:
            ticked.append(True)
            fleet_service.list_agents(fleet.alpha)  # records it, and wakes the manager
        return observe(*args, **kwargs)

    monkeypatch.setattr(fleet_service, "_observe_sockets", the_tick_first)
    nudges = _nudges(monkeypatch)

    report = fleet_service.bulk("restart")

    assert ticked
    assert _outcomes(fleet, report.receipts)["alpha/coder-1"][0] == "skipped"
    assert _exited(fleet.alpha, coder) == [f"{coder.label} exited (0)"]
    assert not [n for n in nudges if f"{coder.label} exited" in n], "a clean exit is news"


def test_a_stop_of_a_dead_agent_on_a_silent_tmux_marks_nothing(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
) -> None:
    """n3: a server that will not answer (here, a socket this user may not open) reads as
    "no window" to the lingering-window look, and m3 marked the row stopped saying no window
    was left, while its dead window may stand. tmux has to vouch for it: no server, or one
    that answers. Otherwise nothing is changed and the stop says why."""
    coder = fleet.agents["alpha/coder-1"]
    _kill(tmux, coder)
    fleet_service.list_agents(fleet.alpha)  # recorded as died; its dead window stands
    tmux.socket_denied = True

    with pytest.raises(fleet_service.FleetUnavailable, match="tmux did not answer"):
        fleet_service.stop(fleet.alpha, coder.label, agent_id=coder.id)

    assert _row(coder.id).ended_by == "died"
    tmux.socket_denied = False
    assert fleet_service.stop(fleet.alpha, coder.label, agent_id=coder.id).marked_only is False
    assert coder.pane_id in tmux.killed and _row(coder.id).ended_by == "stopped"


def test_a_projects_label_heads_are_read_for_that_project_alone(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """n5: ``_label_heads`` read the label heads of every project and filtered them in
    Python, once per plan and again per row. The store is asked for the one project."""
    from aisquare.core.store import SqliteStore

    real = SqliteStore.fleet_label_heads
    asked: list[str | None] = []

    def spied(self: SqliteStore, project_id: str | None = None) -> list[FleetAgent]:
        asked.append(project_id)
        return real(self, project_id)

    monkeypatch.setattr(SqliteStore, "fleet_label_heads", spied)
    heads = fleet_service._label_heads(fleet.alpha)

    assert asked == [fleet.alpha.id]
    assert {row.project_id for row in heads.values()} == {fleet.alpha.id}
    assert set(heads) == {"manager", "coder-1"}


# --- last delta review of #274 (#49), in #50 ------------------------------------------


@pytest.mark.parametrize("pane", ["vanished", "dead"])
def test_a_fleet_stop_that_lands_while_the_replacement_starts_is_never_undone(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
    monkeypatch: pytest.MonkeyPatch,
    pane: str,
) -> None:
    """m3: #48's m1 guard read the row once, before ``_respawn``. The owner's ``fleet stop``
    of the 💤 row while the replacement's window was starting was still undone by the
    spawn's insert. The insert is refused, the new window killed; a later ``fleet restart``
    of the stopped row, asked for, brings it back."""
    coder = fleet.agents["alpha/coder-1"]
    original = tmux.spawn_window
    opened: list[str] = []

    def the_owner_stops_it(*args: Any, **kwargs: Any) -> WindowInfo:
        window = original(*args, **kwargs)
        if not opened:
            opened.append(window.pane_id)
            with store_session() as store:
                assert store.get_fleet_agent(coder.id).ended_by == "died"  # type: ignore[union-attr]
                store.mark_fleet_agent_stopped(coder.id)
        return window

    if pane == "vanished":
        tmux.vanish(coder.pane_id)
    else:
        _kill(tmux, coder)
    monkeypatch.setattr(fleet_service, "_pane_alive", lambda agent: False)
    monkeypatch.setattr(tmux, "spawn_window", the_owner_stops_it)

    with pytest.raises(fleet_service.NoSuchAgent, match="stopped meanwhile"):
        fleet_service.restart(fleet.alpha, coder.label, agent_id=coder.id)

    assert opened and opened[0] in tmux.killed, "the replacement's window does not stay up"
    assert _row(coder.id).ended_by == "stopped"
    assert not [a for a in _live_rows(fleet.alpha) if a.label == coder.label]

    fleet_service.restart(fleet.alpha, coder.label, agent_id=coder.id)
    assert [a.label for a in _live_rows(fleet.alpha) if a.label == coder.label] == [coder.label]


def test_a_stop_tmux_did_not_answer_is_one_to_try_again_not_a_gone_agent(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
    runner: CliRunner,
) -> None:
    """n1: "tmux did not answer … run the stop again" was a ``NoSuchAgent``: ``--json`` said
    ``no_such_agent``, and the Remote stop of a pinned row mapped it to 409 ``stale``. It
    is ``fleet_unavailable``, a refusal to retry."""
    from aisquare.services import remote_actions
    from aisquare.services.remote_server import RequestError

    coder = fleet.agents["alpha/coder-1"]
    _kill(tmux, coder)
    fleet_service.list_agents(fleet.alpha)
    tmux.socket_denied = True

    result = runner.invoke(app, ["--json", "fleet", "stop", "--project", "alpha", coder.label])
    assert result.exit_code != 0
    assert '"fleet_unavailable"' in result.output, result.output

    with pytest.raises(RequestError) as refused:
        remote_actions.action_pinned_call(
            fleet.alpha,
            coder.label,
            coder.id,
            lambda: fleet_service.stop(fleet.alpha, coder.label, agent_id=coder.id),
        )
    assert (refused.value.status, refused.value.error) == (503, "fleet_unavailable")
    assert _row(coder.id).ended_by == "died"


@pytest.mark.parametrize("how", ["kill refused", "view unread"])
def test_a_stop_that_could_not_remove_a_standing_dead_window_marks_nothing(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
    monkeypatch: pytest.MonkeyPatch,
    how: str,
) -> None:
    """n2: the server answered, so the stop vouched that no window was left. But the
    lingering-window kill also came back empty-handed when tmux refused the kill, or when
    the project's view could not be read: the dead window stood, and the row was marked
    stopped "with no window left". Only tmux's own word that none stands marks it."""
    coder = fleet.agents["alpha/coder-1"]
    _kill(tmux, coder)
    fleet_service.list_agents(fleet.alpha)  # recorded as died; its dead window stands
    if how == "kill refused":

        def refused(pane_id: str) -> None:
            raise TmuxError("server busy")

        monkeypatch.setattr(tmux, "kill_window", refused)
    else:
        observe = fleet_service._observe_sockets
        monkeypatch.setattr(
            fleet_service,
            "_observe_sockets",
            lambda *args, **kwargs: dict.fromkeys(observe(*args, **kwargs)),
        )

    with pytest.raises(fleet_service.FleetUnavailable, match="nothing was changed"):
        fleet_service.stop(fleet.alpha, coder.label, agent_id=coder.id)

    assert _row(coder.id).ended_by == "died"
    assert tmux.facts[coder.pane_id].dead, "the premise: its dead window still stands"


def test_a_restart_plan_reads_the_label_heads_once_for_every_project(
    fleet: Fleet,  # noqa: F811 — pytest resolves fixtures by NAME
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """n6: the plan read every project's label heads once, then each project's again: 1 + N
    reads of the store. The one read serves every project, and the plan is the same."""
    from aisquare.core.store import SqliteStore

    _kill(tmux, fleet.agents["alpha/coder-1"])
    _kill(tmux, fleet.agents["beta/coder-1"])
    real = SqliteStore.fleet_label_heads
    asked: list[str | None] = []

    def spied(self: SqliteStore, project_id: str | None = None) -> list[FleetAgent]:
        asked.append(project_id)
        return real(self, project_id)

    monkeypatch.setattr(SqliteStore, "fleet_label_heads", spied)
    plan = fleet_service.bulk_plan("restart")

    assert asked == [None]
    targets = {row.agent.id for row in plan.targets}
    assert {fleet.agents["alpha/coder-1"].id, fleet.agents["beta/coder-1"].id} <= targets
