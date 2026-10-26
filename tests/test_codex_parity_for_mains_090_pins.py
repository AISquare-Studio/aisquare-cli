"""Codex pins for three of main's 0.9.0 rows that said PARITY by reading (review r23 of #113, 8).

Each is a row of ``docs/coding-agents.md``'s parity table whose cited tests ran no Codex row
through the code the row names:

- **W1b, auto-stop**: ``test_codex_parity_for_mains_090.py`` stubs ``autostop.arm``, so a Codex
  row never went through ``store.fleet_agent_for_session`` and the predicate behind the arming.
  Here a Codex tester's real Stop does, with only the detached worker not started.
- **W2, ``aisquare watch``**: the watcher's crash and stall are ``remote_needs``' ``crashed``
  card and the captain queue's ``stale`` row. ``test_remote_needs_codex.py`` pins Codex's
  dialogs and limits, and ``test_watcher.py`` / ``test_watch.py`` had no Codex row at all.
- **W6, a parent manager**: ``test_parent_manager.py`` records the nudge instead of typing it,
  and the real Codex nudge is opt-in live (``tests/test_codex_native.py``). Here a member's
  question is routed up to a parent whose manager is a Codex at its prompt, and the real nudge
  types into that pane.

The fixtures are the neighbouring suites': the fleet's fake tmux, Codex's own hook payloads,
the phone's fake sources and the captain queue's.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aisquare.core.config import WatchSettings
from aisquare.core.store import store_session
from aisquare.models import FleetAgentStatus, ProjectInfo, TeamSession
from aisquare.services import autostop, hooks
from aisquare.services import fleet as fleet_service
from aisquare.services import project_groups as groups
from aisquare.services import team as team_service
from aisquare.services import watch as watch_service
from aisquare.services.captain import queue as captain_queue
from tests.test_captain_queue import FakeClock as QueueClock
from tests.test_captain_queue import FakeSources
from tests.test_captain_queue import _agent as queue_agent
from tests.test_captain_queue import _project as queue_project
from tests.test_captain_queue import _session as queue_session
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_codex_fleet_parity import (
    THREAD_ONE,
    Codex,
    _waiting_manager_on,
)
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeClock,
    FakeTmux,
    _timed_input,
    clock,
    project,
    repo,
    tmux,
)
from tests.test_remote_needs import NOW, PROJECT, Fleet, _row, _sources

pytestmark = pytest.mark.usefixtures("project_capture")

CODEX = "codex"

# --- W1b: a real Codex row arms the auto-stop ---------------------------------------------


def test_a_codex_testers_stop_arms_the_auto_stop_through_its_fleet_row(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``autostop.arm`` is not stubbed: the Stop's session id finds the fleet row the Codex
    start bound (``store.fleet_agent_for_session``), the row is an auto-stoppable role's
    that reads waiting with its task closed, and the detached worker is started for it with
    the token of that Stop. While its task is open the same Stop starts none."""
    task, _ = team_service.add_task("verify the change", role="tester", cwd=project.root)
    agent = fleet_service.spawn(
        project, "tester", agent=CODEX, task_id=task.id, worktree=False
    ).agent
    assert agent.agent == CODEX and agent.session_id is None, "Codex's id binds at its start"
    home = tmp_path / "codex-home"
    home.mkdir()
    codex = Codex(agent, project, home, tmux, monkeypatch)
    board_id = codex.board_id(THREAD_ONE)
    detached: list[list[str]] = []
    monkeypatch.setattr(hooks, "_detach", detached.append)
    tmux.set_command(agent.pane_id, CODEX)
    codex.start(THREAD_ONE)
    with store_session() as store:
        row = store.fleet_agent_for_session(board_id)
    assert row is not None and row.id == agent.id, "the Codex session is the fleet row's"

    codex.hook("UserPromptSubmit", THREAD_ONE, turn_id="turn-1", prompt="verify it")
    codex.hook("Stop", THREAD_ONE, turn_id="turn-1")
    assert detached == [], "its task is open: not finished, no worker"

    with store_session() as store:
        store.set_task_status(task.id, "done")
    codex.hook("UserPromptSubmit", THREAD_ONE, turn_id="turn-2", prompt="and again")
    codex.hook("Stop", THREAD_ONE, turn_id="turn-2")

    [argv] = detached
    assert argv[argv.index("auto-stop") + 1] == board_id
    with store_session() as store:
        assert argv[argv.index("--token") + 1] == store.get_meta(autostop.arm_key(board_id))


# --- W2: the watcher reads a Codex row's crash and stall ----------------------------------


def _watcher(fleet: Fleet, *, stale: bool = False) -> watch_service.Watcher:
    return watch_service.Watcher(
        WatchSettings(auto_stop=False),
        needs_sources=lambda: replace(_sources(fleet), list_projects=lambda: [PROJECT]),
        stale=stale,
    )


def test_the_watcher_reports_a_codex_agent_that_crashed_as_one_that_crashed() -> None:
    """The phone's ``crashed`` card, which the watcher's needs are: a Codex row that ended with
    a failing exit status in the last hour, its task open and nobody on it."""
    codex = _row("coder-1", ended=NOW - timedelta(minutes=10), exit_status=137, task_id="tsk_1")
    codex = codex.model_copy(update={"agent": CODEX, "binary": CODEX})
    clean = _row("coder-2", ended=NOW - timedelta(minutes=10), exit_status=0)
    clean = clean.model_copy(update={"agent": CODEX, "binary": CODEX})
    fleet = Fleet(ended=[codex, clean], tasks={"tsk_1": "doing"})

    [item] = _watcher(fleet).needs(NOW)

    assert (item.kind, item.project_id) == ("crashed", PROJECT.id)
    assert item.summary == "coder-1: coder-1 exited unexpectedly (exit 137)"
    assert item.detail["actions"] == ["restart", "dismiss"]


def test_the_watcher_reports_a_codex_agent_that_shows_no_sign_of_life_or_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The captain queue's ``stale`` rule, the watcher's stall: a waiting Codex agent whose
    board session has been dark for over :data:`STALE_AFTER`, and one whose pane is lost.
    A Codex agent seen a minute ago is neither."""
    alpha = queue_project(tmp_path, "alpha")
    queue_clock = QueueClock()
    dark = queue_session(
        alpha, "s-dark", "coder",
        last_seen=queue_clock.now - captain_queue.STALE_AFTER - timedelta(minutes=15),
    )  # fmt: skip
    fresh = queue_session(
        alpha, "s-fresh", "coder", last_seen=queue_clock.now - timedelta(minutes=1)
    )
    sources = FakeSources(_projects=[alpha])
    sources._agents[alpha.id] = [
        _as_codex(queue_agent(alpha, "codex-1", "waiting", session=dark)),
        _as_codex(queue_agent(alpha, "codex-2", "lost", detail="pane gone")),
        _as_codex(queue_agent(alpha, "codex-3", "waiting", session=fresh)),
    ]
    monkeypatch.setattr(captain_queue, "StoreSources", lambda: sources)

    found = _watcher(Fleet(), stale=True).stale(queue_clock.now)

    assert sorted((item.kind, item.summary) for item in found) == [
        ("stale", "codex-1 shows no sign of life for 45 min"),
        ("stale", "codex-2 is lost: pane gone"),
    ]
    assert {item.detail["agent"] for item in found} == {"codex-1", "codex-2"}


def _as_codex(status: FleetAgentStatus) -> FleetAgentStatus:
    row = status.agent.model_copy(update={"agent": CODEX, "binary": CODEX})
    return status.model_copy(update={"agent": row})


# --- W6: a Codex parent manager is nudged -------------------------------------------------


def test_a_members_question_wakes_a_codex_parent_manager_at_its_prompt(
    tmux: FakeTmux,  # noqa: F811
    clock: FakeClock,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A group's parent project has a Codex manager waiting at its prompt; a member's coder
    asks the parent a question. The question is routed up in the member's own write and the
    real nudge (not recorded) is typed into the Codex pane: a paste, then its Enter apart
    (Codex keeps an Enter in the same burst as text). The control: a member's note that
    matches no route types nothing."""
    manager = _waiting_manager_on(CODEX, project, tmux, tmp_path / "codex-home", monkeypatch)
    api_root = tmp_path / "api"
    api_root.mkdir()
    coder_session = "bbbb2222-0000-0000-0000-000000000000"
    with store_session() as store:
        api = store.onboard_project(ProjectInfo(id="prj_api", root=api_root), codename="api")
        group, _ = groups.create_group(store, "app", [api.id])
        store.set_group_settings(group.id, parent_project_id=project.id)
        now = datetime.now(tz=UTC)
        store.upsert_session(
            TeamSession(
                id=coder_session, project_id=api.id, role="coder", started_at=now,
                last_seen_at=now, cursor=store.latest_seq(api.id),
            )
        )  # fmt: skip
    typed = _timed_input(tmux, clock, monkeypatch)

    team_service.add_note("lunch at noon", session_ref=coder_session)
    assert typed == [], "a note no route takes wakes nobody"
    team_service.add_note("which base, rc/p1 or rc/p2?", kind="question", to_role="parent",
                          session_ref=coder_session)  # fmt: skip

    [(kind, text, typed_at), (key, enter, entered_at)] = typed
    assert (kind, text, key, enter) == ("paste", fleet_service.NUDGE_TEXT, "key", "Enter")
    assert round(entered_at - typed_at, 9) >= fleet_service._KEY_GAP > 0
    with store_session() as store:
        routed = [e for e in store.recent_events(project.id, limit=50) if e.kind == "routed"]
        woken = store.get_session(manager.session_id) if manager.session_id else None
    [line] = routed
    assert line.to_role == "manager" and "which base, rc/p1 or rc/p2?" in line.text
    assert woken is not None and team_service.wake_class(line, woken) == "wake"
