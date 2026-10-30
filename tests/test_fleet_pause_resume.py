"""Pause and resume the agents of a project, or of every project: ``fleet pause`` / ``resume``.

A pause exits each running agent's Claude process as ``fleet stop`` does and keeps the rest of
it — its row (``ended_by = "stopped"`` with ``paused_at``), label, role, task claim, worktree,
account and transcript — and sets the project's ``fleet-paused`` signal. A resume clears the
signal and starts exactly the paused agents again through ``fleet restart``: same label, its
session resumed from its transcript, one carry-on line typed. Driven over the fleet's fake
tmux (tests/test_fleet_service.py), with two projects, each with a manager and a coder.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import TypeAdapter
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core import store as store_core
from aisquare.core import tmux as tmux_core
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo, TeamTask
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from aisquare.services.fleet import NOT_PAUSED, PAUSED_LEFT, BulkReceipt, FleetError
from tests import fakebin
from tests.test_fleet_service import (
    FakeClock,
    FakeTmux,
    _add_task,
    _board_session,
    _command,
    _events,
    _first_lines,
    _flag,
    _git,
    _with_transcript,
)


@dataclass(frozen=True)
class Fleet:
    """Two projects, each with a running manager and a running coder holding a task."""

    alpha: ProjectInfo
    beta: ProjectInfo
    agents: dict[str, FleetAgent]
    """``alpha/manager``, ``alpha/coder-1``, ``beta/manager``, ``beta/coder-1``."""
    tasks: dict[str, TeamTask]
    """``alpha`` and ``beta``: the task each project's coder claimed."""


def _project(tmp_path: Path, name: str) -> ProjectInfo:
    root = tmp_path / name
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=root)
    info = team_project(root)
    with store_session() as store:
        store.onboard_project(info)
    return info


def _claim(agent: FleetAgent, task: TeamTask) -> None:
    assert agent.session_id is not None
    with store_session() as store:
        assert store.claim_task(
            task.id, agent.session_id, datetime.now(tz=UTC) + timedelta(hours=1)
        )


def _task(task: TeamTask) -> TeamTask:
    with store_session() as store:
        found = store.get_task(task.id)
    assert found is not None
    return found


def _row(agent_id: str) -> FleetAgent:
    with store_session() as store:
        row = store.get_fleet_agent(agent_id)
    assert row is not None
    return row


@pytest.fixture
def tmux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeTmux:
    """``fleet.spawn`` and the listing as they are, on a tmux kept in memory, with a
    ``claude`` on PATH that is a script, and time that passes only when slept. A typed
    ``/exit`` runs the agent's own ``SessionEnd`` hook before its pane dies, as Claude Code
    does: what a pause must park the claims through."""
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
    send_literal = fake.send_literal

    def exits_with_its_hook(pane_id: str, text: str) -> None:
        if text == "/exit" and fake.honours_exit:
            with store_session() as store:
                rows = [a for a in store.live_fleet_agents() if a.pane_id == pane_id]
            for row in rows:
                if row.session_id is not None:
                    team_service.hook_session_end(
                        row.session_id, row.cwd, reason="prompt_input_exit"
                    )
        send_literal(pane_id, text)

    monkeypatch.setattr(fake, "send_literal", exits_with_its_hook)
    return fake


@pytest.fixture
def fleet(tmux: FakeTmux, tmp_path: Path) -> Fleet:
    alpha, beta = _project(tmp_path, "alpha"), _project(tmp_path, "beta")
    agents: dict[str, FleetAgent] = {}
    tasks: dict[str, TeamTask] = {}
    for project, name in ((alpha, "alpha"), (beta, "beta")):
        manager = fleet_service.spawn(project, "manager").agent
        task = _add_task(project, f"{name}'s work")
        coder = fleet_service.spawn(project, "coder", worktree=True, task_id=task.id).agent
        for agent in (manager, coder):
            _board_session(agent, "working")
        _claim(coder, task)
        agents[f"{name}/manager"] = manager
        agents[f"{name}/{coder.label}"] = coder
        tasks[name] = task
    return Fleet(alpha, beta, agents, tasks)


def _by_key(fleet: Fleet, receipts: tuple[BulkReceipt, ...]) -> dict[str, BulkReceipt]:
    names = {fleet.alpha.id: "alpha", fleet.beta.id: "beta"}
    return {f"{names[r.project.id]}/{r.agent.label}": r for r in receipts}


def _outcomes(fleet: Fleet, receipts: tuple[BulkReceipt, ...]) -> dict[str, tuple[str, str | None]]:
    return {key: (r.outcome, r.why) for key, r in _by_key(fleet, receipts).items()}


def _coder_label(fleet: Fleet, name: str) -> str:
    return next(key for key in fleet.agents if key.startswith(f"{name}/") and "manager" not in key)


def _states(project: ProjectInfo) -> dict[str, str]:
    return {s.agent.label: s.state for s in fleet_service.list_agents(project)}


# --- A2: the pause --------------------------------------------------------------------


def test_pause_of_a_project_exits_its_agents_keeps_their_claims_and_sets_its_signal(
    fleet: Fleet, tmux: FakeTmux
) -> None:
    coder_key = _coder_label(fleet, "alpha")
    coder = fleet.agents[coder_key]

    report = fleet_service.bulk("pause", projects=[fleet.alpha])

    assert _outcomes(fleet, report.receipts) == {
        "alpha/manager": ("done", None),
        coder_key: ("done", None),
    }
    assert [p.id for p in report.signals] == [fleet.alpha.id]
    assert report.ok and report.summary() == "paused 2 · skipped 0 · failed 0"
    # Each exited as `fleet stop` exits it: /exit, then its window killed.
    for key in ("alpha/manager", coder_key):
        pane = fleet.agents[key].pane_id
        assert (pane, "literal", "/exit") in tmux.typed and pane in tmux.killed
        row = _row(fleet.agents[key].id)
        assert row.ended_at is not None and row.paused_at is not None
        assert row.ended_by == "stopped", "a value 0.9.1 reads (review of #457, M1)"
    # The claim stays with the paused coder's session: released by nothing.
    task = _task(fleet.tasks["alpha"])
    assert task.status == "doing" and task.claimed_by == coder.session_id
    assert _events(fleet.alpha, "task_released") == []
    assert _events(fleet.alpha, "agent_exited") == [], "no exit announced as a finished agent"
    assert len(_events(fleet.alpha, "agent_paused")) == 2
    assert fleet_service.is_paused(fleet.alpha) and not fleet_service.is_paused(fleet.beta)
    # The listing shows them paused, not exited; beta runs on.
    assert set(_states(fleet.alpha).values()) == {"paused"}
    assert set(_states(fleet.beta).values()) <= set(fleet_service.PROCESS_STATES)


def test_pause_of_every_project_pauses_them_all(fleet: Fleet) -> None:
    report = fleet_service.bulk("pause")

    assert {key: outcome for key, (outcome, _) in _outcomes(fleet, report.receipts).items()} == {
        key: "done" for key in fleet.agents
    }
    assert {p.id for p in report.signals} == {fleet.alpha.id, fleet.beta.id}
    assert fleet_service.is_paused(fleet.alpha) and fleet_service.is_paused(fleet.beta)


def test_an_agent_that_had_already_exited_is_left_and_not_marked_paused(
    fleet: Fleet, tmux: FakeTmux
) -> None:
    coder_key = _coder_label(fleet, "alpha")
    exited = fleet.agents[coder_key]
    tmux.die(exited.pane_id, 1)

    report = fleet_service.bulk("pause", projects=[fleet.alpha])

    outcomes = _outcomes(fleet, report.receipts)
    assert outcomes[coder_key] == ("skipped", "exited")
    assert outcomes["alpha/manager"] == ("done", None)
    assert _row(exited.id).ended_by == "died"


def test_the_manager_is_paused_first(fleet: Fleet) -> None:
    plan = fleet_service.bulk_plan("pause", projects=[fleet.alpha])

    assert next(row.agent.label for row in plan.rows) == "manager"


def test_managers_only_pauses_the_managers_alone(fleet: Fleet) -> None:
    report = fleet_service.bulk("pause", managers_only=True)

    assert {key for key, (outcome, _) in _outcomes(fleet, report.receipts).items()} == {
        "alpha/manager",
        "beta/manager",
    }
    assert _row(fleet.agents[_coder_label(fleet, "alpha")].id).ended_at is None


# --- A3: the resume -------------------------------------------------------------------


def test_resume_brings_each_paused_agent_back_under_its_label_with_its_session(
    fleet: Fleet, tmux: FakeTmux, tmp_path: Path
) -> None:
    coder_key = _coder_label(fleet, "alpha")
    coder = fleet.agents[coder_key]
    transcript = tmp_path / f"{coder.session_id}.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    _with_transcript(coder, transcript)
    fleet_service.bulk("pause", projects=[fleet.alpha])
    spawned = len(tmux.spawned)

    report = fleet_service.bulk("resume", projects=[fleet.alpha])

    outcomes = _outcomes(fleet, report.receipts)
    assert outcomes == {coder_key: ("done", None), "alpha/manager": ("done", None)}
    assert [r.agent.label for r in report.receipts][-1] == "manager", "its team first"
    assert report.ok and report.summary() == "resumed 2 · skipped 0 · failed 0"
    assert [p.id for p in report.signals] == [fleet.alpha.id]
    assert not fleet_service.is_paused(fleet.alpha)
    restarted = _by_key(fleet, report.receipts)[coder_key].result
    assert isinstance(restarted, fleet_service.RestartReceipt)
    started = restarted.started
    # Same label, role, task, worktree, account; the session resumed from its transcript.
    assert (started.label, started.role, started.task_id) == (coder.label, "coder", coder.task_id)
    assert started.cwd == coder.cwd and started.worktree
    assert started.account_slot == coder.account_slot
    assert restarted.resumed and started.session_id == coder.session_id
    command = next(
        _command(tmux, i)
        for i in range(spawned, len(tmux.spawned))
        if tmux.spawned[i].get("pane_id") == started.pane_id
    )
    assert _flag(command, "--resume") == str(transcript)
    [line] = _first_lines(tmux, started.pane_id)
    assert line.startswith(f"You are {coder.label}, resumed after `fleet pause`")
    assert "\n" not in line, "one carry-on line"
    # The claim is still the coder's: it never left its session.
    assert _task(fleet.tasks["alpha"]).claimed_by == coder.session_id
    assert set(_states(fleet.alpha).values()) <= set(fleet_service.PROCESS_STATES)
    assert any(
        text.startswith(f"{coder.label} resumed after a pause")
        for text in _events(fleet.alpha, "restarted")
    )
    # Resumed, a paused row is history, recorded as a stop: no `paused` row is left behind
    # for a reader that does not know the word (0.9.1, moved back to).
    with store_session() as store:
        assert all(row.paused_at is None for row in store.fleet_agents(fleet.alpha.id))


def test_a_paused_agent_with_no_transcript_starts_fresh_and_takes_its_claims_over(
    fleet: Fleet, tmux: FakeTmux
) -> None:
    coder_key = _coder_label(fleet, "alpha")
    coder = fleet.agents[coder_key]
    fleet_service.bulk("pause", projects=[fleet.alpha])

    report = fleet_service.bulk("resume", projects=[fleet.alpha])

    restarted = _by_key(fleet, report.receipts)[coder_key].result
    assert isinstance(restarted, fleet_service.RestartReceipt)
    assert restarted.resumed is False
    started = restarted.started
    assert started.session_id != coder.session_id
    [prompt] = _first_lines(tmux, started.pane_id)
    assert prompt.startswith(f"You are {coder.label}, taking over from a previous session")
    assert fleet.tasks["alpha"].title in prompt
    task = _task(fleet.tasks["alpha"])
    assert task.status == "doing" and task.claimed_by == started.session_id


def test_resume_leaves_an_agent_that_exited_without_a_pause(fleet: Fleet, tmux: FakeTmux) -> None:
    coder_key = _coder_label(fleet, "alpha")
    tmux.die(fleet.agents[coder_key].pane_id, 1)
    fleet_service.bulk("pause", projects=[fleet.alpha])  # the manager alone is paused

    report = fleet_service.bulk("resume", projects=[fleet.alpha])

    outcomes = _outcomes(fleet, report.receipts)
    assert outcomes[coder_key] == ("skipped", NOT_PAUSED)
    assert outcomes["alpha/manager"] == ("done", None)
    assert _row(fleet.agents[coder_key].id).ended_by == "died"


def test_resume_twice_does_nothing_the_second_time(fleet: Fleet, tmux: FakeTmux) -> None:
    fleet_service.bulk("pause", projects=[fleet.alpha])
    fleet_service.bulk("resume", projects=[fleet.alpha])
    spawned = len(tmux.spawned)

    again = fleet_service.bulk("resume", projects=[fleet.alpha])

    assert again.of("done") == [] and again.of("failed") == []
    assert len(tmux.spawned) == spawned
    assert again.signals == (), "no signal was on to clear, and none is said cleared"


def test_resume_of_every_project_resumes_them_all(fleet: Fleet) -> None:
    fleet_service.bulk("pause")

    report = fleet_service.bulk("resume")

    assert {key: outcome for key, (outcome, _) in _outcomes(fleet, report.receipts).items()} == {
        key: "done" for key in fleet.agents
    }
    assert not fleet_service.is_paused(fleet.alpha) and not fleet_service.is_paused(fleet.beta)


# --- A1 and A4: what reap, Restart all, Stop all and spawn make of a paused agent -------


def test_a_paused_agent_outlives_its_tmux_server_and_reap_server_down(
    fleet: Fleet, tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    coder = fleet.agents[_coder_label(fleet, "alpha")]
    fleet_service.bulk("pause", projects=[fleet.alpha])
    # The reboot: the server is gone with every pane, and the operator says so.
    tmux.running = False
    tmux.sessions.clear()
    tmux.facts.clear()
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "another-boot")

    report = fleet_service.reap(fleet.alpha, server_down=True)

    assert report.lost == [] and report.ended == []
    assert coder.cwd not in report.worktrees_removed and coder.cwd.is_dir()
    row = _row(coder.id)
    assert row.paused_at is not None
    task = _task(fleet.tasks["alpha"])
    assert task.status == "doing" and task.claimed_by == coder.session_id
    assert {s.agent.label: s.state for s in fleet_service.list_agents(fleet.alpha)} == {
        "manager": "paused",
        coder.label: "paused",
    }
    # A reboot outlasts the 120-minute lease: the claim is still the paused coder's then.
    later = (datetime.now(tz=UTC) + timedelta(hours=8)).isoformat()
    monkeypatch.setattr(store_core, "_now_iso", lambda: later)
    with store_session() as store:
        assert not store.claim_task(task.id, "someone-else", datetime.now(tz=UTC))


def test_reap_never_removes_a_paused_agents_clean_worktree(fleet: Fleet) -> None:
    """A tree with nothing only in it is what reap removes once its agent ended; a paused
    agent's is where its resume starts it again."""
    coder = fleet.agents[_coder_label(fleet, "alpha")]
    fleet_service.bulk("pause", projects=[fleet.alpha])

    report = fleet_service.reap(fleet.alpha)

    assert coder.cwd not in report.worktrees_removed and coder.cwd.is_dir()


@pytest.mark.parametrize("action", ["restart", "stop"])
def test_restart_all_and_stop_all_leave_a_paused_agent(
    fleet: Fleet, action: fleet_service.BulkAction
) -> None:
    fleet_service.bulk("pause", projects=[fleet.alpha])

    report = fleet_service.bulk(action)

    outcomes = _outcomes(fleet, report.receipts)
    left = PAUSED_LEFT if action == "restart" else "paused"
    assert outcomes["alpha/manager"] == ("skipped", left)
    assert outcomes[_coder_label(fleet, "alpha")] == ("skipped", left)
    assert outcomes["beta/manager"] == ("done", None)
    assert _row(fleet.agents["alpha/manager"].id).paused_at is not None


def test_a_restart_lost_after_an_update_leaves_a_paused_agent(fleet: Fleet) -> None:
    fleet_service.bulk("pause", projects=[fleet.alpha])
    [status] = [s for s in fleet_service.list_agents(fleet.alpha) if s.agent.label == "manager"]

    assert fleet_service.restart_leaves(fleet.alpha, status) == PAUSED_LEFT


def test_a_spawn_does_not_take_a_paused_agents_label(fleet: Fleet) -> None:
    coder = fleet.agents[_coder_label(fleet, "alpha")]
    fleet_service.bulk("pause", projects=[fleet.alpha])

    spawned = fleet_service.spawn(fleet.alpha, "coder", label=coder.label, worktree=False)

    assert spawned.agent.label != coder.label
    with pytest.raises(FleetError, match="manager is paused"):
        fleet_service.spawn(fleet.alpha, "manager")


def test_the_owners_stop_of_a_paused_agent_ends_its_pause_for_good(fleet: Fleet) -> None:
    """The human's own ``fleet stop <label>`` (no ``--as``, from a shell that is no fleet
    window: ``user``) ends a pause: the row is recorded stopped, the claims it kept go back to
    the board, the exit is announced, and a resume leaves it."""
    coder = fleet.agents[_coder_label(fleet, "alpha")]
    fleet_service.bulk("pause", projects=[fleet.alpha])

    stopped = CliRunner().invoke(
        app, ["--json", "fleet", "stop", coder.label, "--project", fleet.alpha.id]
    )

    assert stopped.exit_code == 0, stopped.output
    assert json.loads(stopped.stdout)["claims_released"] == [fleet.tasks["alpha"].id]
    row = _row(coder.id)
    assert row.ended_by == "stopped" and row.paused_at is None
    assert _task(fleet.tasks["alpha"]).status == "todo"
    assert [e for e in _events(fleet.alpha, "agent_exited") if "stopped by user" in e]
    again = fleet_service.bulk("resume", projects=[fleet.alpha])
    assert coder.label not in {r.agent.label for r in again.of("done")}


def test_the_managers_stop_cycle_leaves_a_paused_agent(fleet: Fleet) -> None:
    """Tester's reopen of #9 (seq 4902), as it was run: the coder claims #1, ``fleet pause -P
    alpha``, ``fleet resume -P alpha --managers``, then the manager's own cycle line
    (``harness``: an agent whose task is done → ``fleet stop <label> --as <its session>``).
    It released #1, announced ``coder exited (0) — stopped by manager`` and ended the row,
    so the next reap took its tree. Refused now: the pause is the owner's (A4, decision 1)."""
    coder = fleet.agents[_coder_label(fleet, "alpha")]
    runner = CliRunner()
    ref = fleet.alpha.id
    assert runner.invoke(app, ["fleet", "pause", "--project", ref]).exit_code == 0
    resumed = runner.invoke(app, ["fleet", "resume", "--project", ref, "--managers"])
    assert resumed.exit_code == 0, resumed.output
    with store_session() as store:
        manager = store.fleet_agent_by_label(ref, "manager")
    assert manager is not None and manager.ended_at is None and manager.session_id is not None
    exits_before = _events(fleet.alpha, "agent_exited")

    stopped = runner.invoke(
        app,
        ["--json", "fleet", "stop", coder.label, "-P", ref, "--as", manager.session_id],
    )

    assert stopped.exit_code == 1, stopped.output
    assert "paused by the owner" in json.loads(stopped.stdout)["detail"]
    task = _task(fleet.tasks["alpha"])
    assert task.status == "doing" and task.claimed_by == coder.session_id, "#1 not released"
    assert _events(fleet.alpha, "task_released") == []
    assert _events(fleet.alpha, "agent_exited") == exits_before, "no exit announced"
    assert _row(coder.id).paused_at is not None and _states(fleet.alpha)[coder.label] == "paused"
    assert fleet_service.is_paused(fleet.alpha), "its team is paused: the manager spawns nothing"
    assert coder.cwd not in fleet_service.reap(fleet.alpha).worktrees_removed


def test_restart_of_a_paused_label_resumes_it(fleet: Fleet) -> None:
    coder = fleet.agents[_coder_label(fleet, "alpha")]
    fleet_service.bulk("pause", projects=[fleet.alpha])

    receipt = fleet_service.restart(fleet.alpha, coder.label)

    assert receipt.replaced.id == coder.id and receipt.started.label == coder.label
    assert _task(fleet.tasks["alpha"]).claimed_by == receipt.started.session_id


# --- A7: shutdown still clears the signal -----------------------------------------------


def test_shutdown_still_clears_the_pause_signal(fleet: Fleet) -> None:
    fleet_service.bulk("pause", projects=[fleet.alpha])

    fleet_service.shutdown(fleet.alpha)

    assert not fleet_service.is_paused(fleet.alpha)
    assert _row(fleet.agents["alpha/manager"].id).paused_at is not None


# --- the commands ---------------------------------------------------------------------


def test_fleet_pause_and_resume_report_each_agent(fleet: Fleet) -> None:
    runner = CliRunner()
    ref = fleet.alpha.id

    paused = runner.invoke(app, ["fleet", "pause", "--project", ref])
    assert paused.exit_code == 0, paused.output
    assert "paused" in paused.output and "manager" in paused.output
    assert "fleet-paused" in paused.output

    resumed = runner.invoke(app, ["--json", "fleet", "resume", "--project", ref])
    assert resumed.exit_code == 0, resumed.output
    payload = json.loads(resumed.output)
    assert payload["action"] == "resume"
    assert {r["agent"]["label"] for r in payload["receipts"] if r["outcome"] == "done"} == {
        "manager",
        _coder_label(fleet, "alpha").split("/")[1],
    }
    [signal] = payload["signals"]
    assert signal["project"]["id"] == ref and signal["paused"] is False


def test_fleet_pause_all_and_resume_all(fleet: Fleet) -> None:
    runner = CliRunner()

    paused = runner.invoke(app, ["--json", "fleet", "pause", "--all"])
    assert paused.exit_code == 0, paused.output
    assert json.loads(paused.output)["summary"]["done"] == 4

    resumed = runner.invoke(app, ["--json", "fleet", "resume", "--all"])
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.output)["summary"]["done"] == 4


def test_fleet_ls_and_the_sidebar_show_a_paused_agent_as_paused(fleet: Fleet) -> None:
    from aisquare.cli.ui.sidebar import STATE_CHIP, agent_row_text

    fleet_service.bulk("pause", projects=[fleet.alpha])

    listed = CliRunner().invoke(app, ["fleet", "ls", "--project", fleet.alpha.id])
    assert listed.exit_code == 0, listed.output
    assert "⏯ paused" in listed.output and "exited" not in listed.output
    [manager] = [s for s in fleet_service.list_agents(fleet.alpha) if s.agent.label == "manager"]
    row = agent_row_text(manager).plain
    assert STATE_CHIP["paused"][0] in row and "paused" in row and "exited" not in row
    assert STATE_CHIP["paused"] != STATE_CHIP["exited"]


# --- review of #457 ---------------------------------------------------------------------

#: ``EndedBy`` as 0.9.1 validates it: one row with any other value ended every fleet command
#: there (M1).
ENDED_BY_091: TypeAdapter[Any] = TypeAdapter(Literal["stopped", "died"] | None)


def _raw_ended_by() -> list[object]:
    connection = sqlite3.connect(str(paths.db_path()))
    try:
        return [row[0] for row in connection.execute("SELECT ended_by FROM fleet_agent")]
    finally:
        connection.close()


def test_every_row_a_pause_a_resume_or_a_stop_writes_reads_under_0_9_1(fleet: Fleet) -> None:
    """M1: a row ended ``paused`` made 0.9.1's ``fleet ls``, ``stop``, ``reap``, ``restart
    --all`` and ``spawn`` fail validation, with no way back there but a hand edit. The pause
    is ``paused_at``, a column 0.9.1 never reads; ``ended_by`` stays one it knows."""
    coder = fleet.agents[_coder_label(fleet, "beta")]
    fleet_service.bulk("pause")
    paused = _raw_ended_by()
    fleet_service.bulk("resume", projects=[fleet.alpha])
    fleet_service.stop(fleet.beta, coder.label)  # the owner's: ends that pause for good

    for value in [*paused, *_raw_ended_by()]:
        ENDED_BY_091.validate_python(value)
    assert "stopped" in paused
    assert _row(coder.id).paused_at is None and _row(coder.id).ended_by == "stopped"
    assert _states(fleet.beta)["manager"] == "paused", "beta's manager still paused"


def test_a_paused_agents_claim_outlives_its_lease_until_the_resume(
    fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m1: the owner decided claims are KEPT while paused, and a 120-minute lease lapsing let
    any session claim the task. A pause holds the lease; a resume leases it again as usual."""
    task = fleet.tasks["alpha"]
    fleet_service.bulk("pause", projects=[fleet.alpha])
    held = _task(task).claim_expires_at
    assert held is not None and held > datetime.now(tz=UTC) + timedelta(days=365)

    later = (datetime.now(tz=UTC) + timedelta(hours=3)).isoformat()  # past the lease
    with monkeypatch.context() as m:
        m.setattr(store_core, "_now_iso", lambda: later)
        with store_session() as store:
            taken = store.claim_task(task.id, "someone-else", datetime.now(tz=UTC))
    assert (
        taken is False
        and _task(task).claimed_by == fleet.agents[_coder_label(fleet, "alpha")].session_id
    )

    report = fleet_service.bulk("resume", projects=[fleet.alpha])
    started = _by_key(fleet, report.receipts)[_coder_label(fleet, "alpha")].result
    assert isinstance(started, fleet_service.RestartReceipt)
    resumed = _task(task)
    assert resumed.claimed_by == started.started.session_id
    assert resumed.claim_expires_at is not None
    assert resumed.claim_expires_at < datetime.now(tz=UTC) + timedelta(days=1), "leased as usual"


def test_a_managers_only_resume_leaves_the_project_paused_while_its_team_is(
    fleet: Fleet,
) -> None:
    """m2: a ``--managers`` resume cleared the signal, and the manager came back free to
    spawn beside a team still paused. The signal goes only when no agent stays paused."""
    fleet_service.bulk("pause", projects=[fleet.alpha])

    managers = fleet_service.bulk("resume", managers_only=True, projects=[fleet.alpha])

    assert [r.agent.label for r in managers.of("done")] == ["manager"]
    assert fleet_service.is_paused(fleet.alpha), "the coder is still paused"
    assert managers.signals == () and [(p.id, n) for p, n in managers.kept] == [(fleet.alpha.id, 1)]
    everyone = fleet_service.bulk("resume", projects=[fleet.alpha])
    assert not fleet_service.is_paused(fleet.alpha)
    assert [p.id for p in everyone.signals] == [fleet.alpha.id] and everyone.kept == ()


def test_a_resume_that_fails_leaves_the_project_paused(
    fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m2: cleared before anyone starts, a resume whose coder then failed left the manager
    spawning beside it. The signal is set again, and the report says why."""
    coder = _coder_label(fleet, "alpha").split("/")[1]
    real = fleet_service.restart

    def refuses_the_coder(project: ProjectInfo, label: str, **kwargs: Any) -> Any:
        if label == coder:
            raise FleetError("refused for the test")
        return real(project, label, **kwargs)

    fleet_service.bulk("pause", projects=[fleet.alpha])
    monkeypatch.setattr(fleet_service, "restart", refuses_the_coder)

    report = fleet_service.bulk("resume", projects=[fleet.alpha])

    assert _outcomes(fleet, report.receipts)[f"alpha/{coder}"][0] == "failed"
    assert fleet_service.is_paused(fleet.alpha)
    assert report.signals == () and [(p.id, n) for p, n in report.kept] == [(fleet.alpha.id, 1)]


def test_an_agent_may_not_stop_a_paused_agent_and_the_owner_may(fleet: Fleet) -> None:
    """m3: the manager's cycle (``fleet stop --as <its session>`` of an agent whose task is
    done) ended the owner's pause, released its claims and announced its exit. Refused for an
    agent's session; the owner's stop (``user``) ends it."""
    coder = fleet.agents[_coder_label(fleet, "alpha")]
    manager = fleet.agents["alpha/manager"]
    fleet_service.bulk("pause", projects=[fleet.alpha])
    assert manager.session_id is not None

    with pytest.raises(FleetError, match="paused by the owner"):
        fleet_service.stop(fleet.alpha, coder.label, actor=manager.session_id)
    assert _row(coder.id).paused_at is not None
    assert _task(fleet.tasks["alpha"]).claimed_by == coder.session_id
    refused = CliRunner().invoke(
        app,
        ["--json", "fleet", "stop", coder.label, "--project", fleet.alpha.id, "--as", "x"],
    )
    assert refused.exit_code == 1 and "paused by the owner" in json.loads(refused.stdout)["detail"]

    receipt = fleet_service.stop(fleet.alpha, coder.label, actor="user")
    assert receipt.agent.paused_at is None and receipt.released


def test_a_resume_ends_the_pause_of_an_agent_whose_task_closed(fleet: Fleet) -> None:
    """m4: its task done while it was paused, the coder stayed paused for good: its label,
    tree and branch held, listed however old. The resume ends its pause, releasing what it
    kept and saying so, and reap may take its tree."""
    key = _coder_label(fleet, "alpha")
    coder = fleet.agents[key]
    fleet_service.bulk("pause", projects=[fleet.alpha])
    with store_session() as store:
        store.set_task_status(fleet.tasks["alpha"].id, "done")
    plan = fleet_service.bulk_plan("resume", projects=[fleet.alpha])
    assert [row.closes for row in plan.rows if row.agent.id == coder.id] == ["task is done"]

    report = fleet_service.bulk("resume", projects=[fleet.alpha])

    receipt = _by_key(fleet, report.receipts)[key]
    assert receipt.outcome == "skipped" and receipt.why is not None
    assert "its pause was ended, not resumed" in receipt.why
    assert _outcomes(fleet, report.receipts)["alpha/manager"] == ("done", None)
    row = _row(coder.id)
    assert row.paused_at is None and row.ended_by == "stopped"
    assert coder.label not in _states(fleet.alpha), "no longer a paused agent"
    assert not fleet_service.is_paused(fleet.alpha), "no agent is left paused"
    assert coder.cwd in fleet_service.reap(fleet.alpha).worktrees_removed


def test_a_pause_older_than_a_day_is_still_listed_kept_and_resumed(fleet: Fleet) -> None:
    """n1: the overnight or reboot case, past the 24 h the listing keeps an ended row for."""
    coder = fleet.agents[_coder_label(fleet, "alpha")]
    fleet_service.bulk("pause", projects=[fleet.alpha])
    long_ago = (datetime.now(tz=UTC) - timedelta(days=3)).isoformat()
    connection = sqlite3.connect(str(paths.db_path()))
    try:
        connection.execute(
            "UPDATE fleet_agent SET ended_at = ?, paused_at = ? WHERE paused_at IS NOT NULL",
            (long_ago, long_ago),
        )
        connection.commit()
    finally:
        connection.close()

    assert set(_states(fleet.alpha).values()) == {"paused"}
    assert coder.cwd not in fleet_service.reap(fleet.alpha).worktrees_removed
    plan = fleet_service.bulk_plan("resume", projects=[fleet.alpha])
    assert {row.agent.label for row in plan.targets} == {"manager", coder.label}


def test_a_resume_clears_a_signal_with_no_agent_paused(fleet: Fleet) -> None:
    """n3: a signal-only pause (from an earlier version), or one a ``--managers`` resume left on:
    the resume clears it with no agent to bring back."""
    fleet_service.pause(fleet.alpha)  # the signal alone
    plan = fleet_service.bulk_plan("resume", projects=[fleet.alpha])
    assert plan.targets == [] and plan.acts and plan.fleet_paused == {fleet.alpha.id}

    report = fleet_service.bulk("resume", projects=[fleet.alpha])

    assert not fleet_service.is_paused(fleet.alpha)
    assert [p.id for p in report.signals] == [fleet.alpha.id]


def test_an_agent_may_not_restart_switch_or_resume_a_paused_teammate_and_the_owner_may(
    fleet: Fleet, tmux: FakeTmux
) -> None:
    """Review of #457 delta (seq 4918), 3: as for its stop (m3), an agent's ``fleet restart
    <label> --as <its session>`` resumed the owner's paused teammate, and ``fleet resume --as``
    each of them. Refused, nothing started; the owner's restart (no ``--as``) resumes it."""
    coder = fleet.agents[_coder_label(fleet, "alpha")]
    manager = fleet.agents["alpha/manager"]
    assert manager.session_id is not None
    runner = CliRunner()
    ref = fleet.alpha.id
    fleet_service.bulk("pause", projects=[fleet.alpha])
    spawned = len(tmux.spawned)

    restarted = runner.invoke(
        app, ["--json", "fleet", "restart", coder.label, "-P", ref, "--as", manager.session_id]
    )
    assert restarted.exit_code == 1, restarted.output
    assert "paused by the owner" in json.loads(restarted.stdout)["detail"]
    with pytest.raises(FleetError, match="paused by the owner"):
        fleet_service.switch(fleet.alpha, coder.label, spawned_by=manager.session_id)
    with pytest.raises(FleetError, match="no process to move"):
        fleet_service.switch(fleet.alpha, coder.label)  # the owner's: resume it first
    resumed = fleet_service.bulk(
        "resume", projects=[fleet.alpha], acting_session=manager.session_id
    )
    outcomes = _outcomes(fleet, resumed.receipts)
    assert outcomes["alpha/manager"] == ("skipped", fleet_service.THIS_SESSION)  # its own row
    assert outcomes[_coder_label(fleet, "alpha")][0] == "failed"
    assert "paused by the owner" in (outcomes[_coder_label(fleet, "alpha")][1] or "")
    assert len(tmux.spawned) == spawned, "nothing was started"
    assert _row(coder.id).paused_at is not None and fleet_service.is_paused(fleet.alpha)
    assert _task(fleet.tasks["alpha"]).claimed_by == coder.session_id

    owners = runner.invoke(app, ["--json", "fleet", "restart", coder.label, "-P", ref])

    assert owners.exit_code == 0, owners.output
    assert json.loads(owners.stdout)["started"]["label"] == coder.label
    assert _row(coder.id).paused_at is None and _states(fleet.alpha)[coder.label] != "paused"


# --- the fold into 0.9.2: a paused agent survives a relabel (#455) and a project rename (#456)


def _resumed(fleet: Fleet, report: fleet_service.BulkReport, key: str) -> FleetAgent:
    result = _by_key(fleet, report.receipts)[key].result
    assert isinstance(result, fleet_service.RestartReceipt), report.receipts
    return result.started


def test_a_paused_agent_is_not_relabelled_and_keeps_its_label_from_a_running_one(
    fleet: Fleet,
) -> None:
    """``fleet relabel`` of a paused agent is refused, saying it is paused and how it comes
    back; a running agent renamed to the paused one's label takes the next suffix, as for a
    live one's. Given it, the store retired the paused row to ``~<label>`` and its resume
    found nothing under its label."""
    coder_key = _coder_label(fleet, "alpha")
    paused = fleet.agents[coder_key]
    fleet_service.bulk("pause", projects=[fleet.alpha])
    with pytest.raises(FleetError, match=f"{paused.label} is paused — only a running agent"):
        fleet_service.relabel(fleet.alpha, paused.label, "login fix")
    assert "fleet resume" in (fleet_service.relabel_refusal(_row(paused.id)) or "")
    other = fleet_service.spawn(fleet.alpha, "coder", label="other", worktree=False).agent

    receipt = fleet_service.relabel(fleet.alpha, other.label, paused.label)

    assert receipt.agent.label == f"{paused.label}-2"
    assert receipt.notes[0] == (
        f"{paused.label} is a paused agent's label — named it {paused.label}-2"
    )
    row = _row(paused.id)
    assert (row.label, row.paused_at is not None) == (paused.label, True)
    assert _states(fleet.alpha)[paused.label] == "paused"
    resumed = fleet_service.bulk("resume", projects=[fleet.alpha])
    started = _resumed(fleet, resumed, coder_key)
    assert (started.label, started.cwd) == (paused.label, paused.cwd)


def test_the_store_refuses_a_relabel_onto_a_paused_agents_label(fleet: Fleet) -> None:
    """The store's own guard, under the service's pick: a relabel onto the label a paused row
    heads raises as a live holder's does (the service then takes the next suffix), and
    writes nothing — neither the running row nor the paused one moves."""
    paused = fleet.agents[_coder_label(fleet, "alpha")]
    fleet_service.bulk("pause", projects=[fleet.alpha])
    other = fleet_service.spawn(fleet.alpha, "coder", label="other", worktree=False).agent

    with store_session() as store, pytest.raises(sqlite3.IntegrityError, match="paused"):
        store.relabel_fleet_agent(other.id, other.label, paused.label)

    assert _row(other.id).label == other.label
    row = _row(paused.id)
    assert (row.label, row.paused_at is not None) == (paused.label, True)


def test_a_spawn_does_not_take_the_tree_a_relabelled_paused_agent_keeps(
    fleet: Fleet,
) -> None:
    """A relabel moves no tree, so a coder spawned as ``coder-x`` and renamed ``login-fix``
    still works in ``…/coder-x``; paused, it keeps that tree for its resume. A spawn asking
    for ``coder-x`` is given another label, and told whose tree it is: given the label, it
    started in the paused agent's tree, uncommitted work and all."""
    coder_key = _coder_label(fleet, "alpha")
    coder = fleet.agents[coder_key]
    fleet_service.relabel(fleet.alpha, coder.label, "login fix")
    fleet_service.bulk("pause", projects=[fleet.alpha])

    spawned = fleet_service.spawn(fleet.alpha, "coder", label=coder.label, worktree=True)

    assert spawned.agent.label == f"{coder.label}-2"
    assert spawned.agent.cwd != coder.cwd
    assert spawned.notes[0] == (
        f"label {coder.label!r} is the worktree login-fix paused in ({coder.cwd}) — "
        f"using {coder.label + '-2'!r}"
    )
    resumed = fleet_service.bulk("resume", projects=[fleet.alpha])
    started = _resumed(fleet, resumed, "alpha/login-fix")
    assert (started.label, started.cwd) == ("login-fix", coder.cwd)


def test_a_paused_agent_survives_a_project_rename_and_resumes_in_its_session(
    fleet: Fleet, tmux: FakeTmux
) -> None:
    """A project renamed while its agents are paused stays paused (its signal is the
    project's by id), and each paused agent resumes under its label, in its own worktree and
    branch, in the tmux session of the codename the rename gave."""
    coder_key = _coder_label(fleet, "alpha")
    coder = fleet.agents[coder_key]
    fleet_service.bulk("pause", projects=[fleet.alpha])
    branch = _row(coder.id).branch

    renamed = fleet_service.rename_project(fleet.alpha, "Alpha Two")

    assert renamed.codename == "alpha-two"
    assert fleet_service.is_paused(renamed)
    assert set(_states(renamed).values()) == {"paused"}
    spawned = len(tmux.spawned)
    resumed = fleet_service.bulk("resume", projects=[renamed])
    started = _resumed(fleet, resumed, coder_key)
    assert (started.label, started.cwd, started.branch) == (coder.label, coder.cwd, branch)
    assert started.branch is not None
    sessions = {str(entry["session"]) for entry in tmux.spawned[spawned:]}
    assert sessions == {"asq-alpha-two"}, sessions
    assert not fleet_service.is_paused(renamed)


# --- the fold's review of d537df59 (reviewer-457d, seq 5050; validator, seq 5043) -------------


def _the_running_manager(project: ProjectInfo) -> FleetAgent:
    with store_session() as store:
        row = store.fleet_agent_by_label(project.id, "manager", live_only=True)
    assert row is not None
    return row


def test_an_agents_own_window_may_not_end_the_owners_pause_with_or_without_as(
    fleet: Fleet, tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m1 (MAJOR): the gate trusted the name a caller passed. From the manager's own window
    (its ``AISQUARE_FLEET_AGENT``) with no ``--as``, ``fleet restart`` and ``fleet resume``
    said ``user``: the restart resumed the owner's paused coder, and the resume brought the
    team back and cleared the owner's signal. And ``--as captain`` from any agent ended a
    pause by a stop, releasing its claim. Each is refused, nothing started, the claim and the
    signal as they were; from the owner's shell, the resume brings the team back."""
    coder = fleet.agents[_coder_label(fleet, "alpha")]
    fleet_service.bulk("pause", projects=[fleet.alpha])
    fleet_service.bulk("resume", managers_only=True, projects=[fleet.alpha])
    manager = _the_running_manager(fleet.alpha)
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", manager.id)
    runner = CliRunner()
    ref = fleet.alpha.id
    spawned, signals = len(tmux.spawned), len(_events(fleet.alpha, "signal"))

    for args in (
        ["fleet", "restart", coder.label, "-P", ref],
        ["fleet", "resume", "-P", ref],
        ["fleet", "stop", coder.label, "-P", ref],
        ["fleet", "restart", coder.label, "-P", ref, "--as", "captain"],
        ["fleet", "resume", "-P", ref, "--as", "captain"],
        ["fleet", "stop", coder.label, "-P", ref, "--as", "captain"],
    ):
        result = runner.invoke(app, ["--json", *args])
        assert result.exit_code == 1, (args, result.output)
        assert "paused by the owner" in result.stdout, (args, result.stdout)
    with pytest.raises(FleetError, match="paused by the owner"):
        fleet_service.switch(fleet.alpha, coder.label)  # `user`, from the manager's window

    assert len(tmux.spawned) == spawned, "nothing was started"
    assert len(_events(fleet.alpha, "signal")) == signals, "the owner's signal was not touched"
    assert _row(coder.id).paused_at is not None and fleet_service.is_paused(fleet.alpha)
    assert _task(fleet.tasks["alpha"]).claimed_by == coder.session_id
    monkeypatch.delenv("AISQUARE_FLEET_AGENT")
    owners = runner.invoke(app, ["--json", "fleet", "resume", "-P", ref])
    assert owners.exit_code == 0, owners.output
    assert _row(coder.id).paused_at is None and not fleet_service.is_paused(fleet.alpha)


def test_an_agents_refused_resume_writes_no_signal(fleet: Fleet, tmux: FakeTmux) -> None:
    """m2: an agent's resume cleared the owner's ``fleet-paused`` before any row was tried,
    every row was then refused, and the signal was set again: two broadcasts, and a window
    in which the manager read it off. Refused in the plan, each row fails untried and the
    signal is never written."""
    fleet_service.bulk("pause", projects=[fleet.alpha])
    fleet_service.bulk("resume", managers_only=True, projects=[fleet.alpha])
    manager = _the_running_manager(fleet.alpha)
    assert manager.session_id is not None
    signals, spawned = len(_events(fleet.alpha, "signal")), len(tmux.spawned)

    report = fleet_service.bulk("resume", projects=[fleet.alpha], acting_session=manager.session_id)

    coder_key = _coder_label(fleet, "alpha")
    outcome, why = _outcomes(fleet, report.receipts)[coder_key]
    assert outcome == "failed" and "paused by the owner" in (why or ""), why
    assert not report.ok and report.signals == ()
    assert [(p.id, n) for p, n in report.kept] == [(fleet.alpha.id, 1)]
    assert len(_events(fleet.alpha, "signal")) == signals, "no signal written, on or off"
    assert len(tmux.spawned) == spawned and fleet_service.is_paused(fleet.alpha)


def test_an_agents_resume_of_a_paused_agent_whose_task_closed_names_what_the_owners_does(
    fleet: Fleet,
) -> None:
    """n1: refused an agent, the end of a closed task's pause said the owner's resume "brings
    it back"; for that row it ends the pause. The refusal says so."""
    coder = fleet.agents[_coder_label(fleet, "alpha")]
    fleet_service.bulk("pause", projects=[fleet.alpha])
    with store_session() as store:
        store.set_task_status(fleet.tasks["alpha"].id, "done")
    manager = fleet.agents["alpha/manager"]

    report = fleet_service.bulk("resume", projects=[fleet.alpha], acting_session=manager.session_id)

    outcome, why = _outcomes(fleet, report.receipts)[_coder_label(fleet, "alpha")]
    assert outcome == "failed"
    assert why == (
        f"{coder.label} was paused by the owner (`fleet pause`), and only the owner ends a pause "
        "(their resume ends the pause, as its task is done); nothing was changed"
    )
    assert _row(coder.id).paused_at is not None


def test_a_switch_pinned_to_a_row_the_paused_label_no_longer_names_is_a_stale_pin(
    fleet: Fleet,
) -> None:
    """n2: a switch pinned to row A (Remote's agent/switch), whose label has since gone to a row
    B now paused, was told about B. It is the stale pin a running B gets (``_replaced``)."""
    coder = fleet.agents[_coder_label(fleet, "alpha")]
    fleet_service.bulk("pause", projects=[fleet.alpha])

    with pytest.raises(fleet_service.NoSuchAgent, match="ended and was replaced since"):
        fleet_service.switch(fleet.alpha, coder.label, agent_id="agt_an_older_row")
    with pytest.raises(FleetError, match="no process to move"):
        fleet_service.switch(fleet.alpha, coder.label, agent_id=coder.id)  # pinned to B itself


def test_the_paused_manager_is_read_from_the_store_alone(fleet: Fleet) -> None:
    """n3: what the Manager tab shows with no frame yet: the project's paused manager, as a
    listing shows it, and nothing once it runs again."""
    manager = fleet.agents["alpha/manager"]
    assert fleet_service.paused_manager_of(fleet.alpha) is None
    fleet_service.bulk("pause", projects=[fleet.alpha])

    paused = fleet_service.paused_manager_of(fleet.alpha)

    assert paused is not None and paused.agent.id == manager.id
    assert (paused.state, paused.detail) == ("paused", fleet_service.PAUSED_DETAIL)
    fleet_service.bulk("resume", projects=[fleet.alpha])
    assert fleet_service.paused_manager_of(fleet.alpha) is None


def test_a_project_rename_tells_the_board_a_paused_agent_reads(fleet: Fleet) -> None:
    """A rename notes each board the project's agents read, so each learns the new codename
    on its next turn; a paused agent reads its board again when it is resumed. Its board (a
    hub's, here beta's) is told as a running agent's is."""
    coder = fleet.agents[_coder_label(fleet, "alpha")]
    fleet_service.bulk("pause", projects=[fleet.alpha])
    with store_session() as store:
        store._conn.execute(  # type: ignore[attr-defined]
            "UPDATE fleet_agent SET board = ? WHERE id = ?", (fleet.beta.id, coder.id)
        )
        store._conn.commit()  # type: ignore[attr-defined]

    with store_session() as store:
        stored = store.get_project(fleet.alpha.id)
    assert stored is not None and stored.codename is not None
    old = stored.codename

    fleet_service.rename_project(fleet.alpha, "Alpha Two")

    told = [text for text in _events(fleet.beta, "note") if text.startswith("project renamed:")]
    assert told == [f"project renamed: {old} → alpha-two; --project {old} still works"], told
