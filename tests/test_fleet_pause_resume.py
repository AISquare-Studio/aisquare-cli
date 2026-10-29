"""Pause and resume the agents of a project, or of every project: ``fleet pause`` / ``resume``.

A pause exits each running agent's Claude process as ``fleet stop`` does and keeps the rest of
it — its row (``ended_by = "paused"``), label, role, task claim, worktree, account and
transcript — and sets the project's ``fleet-paused`` signal. A resume clears the signal and
starts exactly the paused agents again through ``fleet restart``: same label, its session
resumed from its transcript, one carry-on line typed. Driven over the fleet's fake tmux
(tests/test_fleet_service.py), with two projects, each with a manager and a coder.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
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
        assert row.ended_at is not None and row.ended_by == "paused"
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
        assert all(row.ended_by != "paused" for row in store.fleet_agents(fleet.alpha.id))


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
    assert row.ended_by == "paused"
    task = _task(fleet.tasks["alpha"])
    assert task.status == "doing" and task.claimed_by == coder.session_id
    assert {s.agent.label: s.state for s in fleet_service.list_agents(fleet.alpha)} == {
        "manager": "paused",
        coder.label: "paused",
    }


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
    assert _row(fleet.agents["alpha/manager"].id).ended_by == "paused"


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


def test_stopping_a_paused_agent_ends_its_pause_for_good(fleet: Fleet) -> None:
    coder = fleet.agents[_coder_label(fleet, "alpha")]
    fleet_service.bulk("pause", projects=[fleet.alpha])

    receipt = fleet_service.stop(fleet.alpha, coder.label)

    assert receipt.agent.ended_by == "stopped"
    assert [t.id for t in receipt.released] == [fleet.tasks["alpha"].id]
    assert _task(fleet.tasks["alpha"]).status == "todo"
    again = fleet_service.bulk("resume", projects=[fleet.alpha])
    assert coder.label not in {r.agent.label for r in again.of("done")}


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
    assert _row(fleet.agents["alpha/manager"].id).ended_by == "paused"


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
