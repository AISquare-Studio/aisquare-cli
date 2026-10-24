"""P2-W6 (#199): a parent manager over a project group.

A manager could not hear past its own board: a member's question reached the manager
coordinating the group only when it next read that board. A group can name a parent, an
ordinary project whose manager is the group's, and its members' chosen events route up to it
as one ``routed`` line, in the member's own write, with the parent manager nudged.

The store is real (an isolated home); the nudges are recorded instead of typed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import harness
from aisquare.core.store import store_session
from aisquare.models import ProjectInfo, TeamEvent, TeamSession
from aisquare.services import fleet as fleet_service
from aisquare.services import parent as parent_service
from aisquare.services import project_groups as groups
from aisquare.services import team as team_service
from tests import test_fleet_service as fleet_suite
from tests.test_fleet_service import FakeTmux

pytestmark = pytest.mark.usefixtures("project_capture")

HQ_MANAGER = "aaaa1111-0000-0000-0000-000000000000"
API_CODER = "bbbb2222-0000-0000-0000-000000000000"


@pytest.fixture(autouse=True)
def nudges(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def record(project_id: str, *, reason: str, board: str | None = None) -> bool:
        calls.append((project_id, reason))
        return True

    monkeypatch.setattr(fleet_service, "nudge_manager", record)
    return calls


def _project(root: Path, codename: str) -> ProjectInfo:
    root.mkdir(parents=True, exist_ok=True)
    with store_session() as store:
        return store.onboard_project(
            ProjectInfo(id=f"prj_{codename}", root=root), codename=codename
        )


def _session(sid: str, project: ProjectInfo, role: str) -> None:
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=sid,
                project_id=project.id,
                role=role,
                started_at=now,
                last_seen_at=now,
                cursor=store.latest_seq(project.id),
            )
        )


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, ProjectInfo]:
    """``hq`` parents the group ``app`` of ``api`` and ``web``."""
    found = {name: _project(tmp_path / name, name) for name in ("hq", "api", "web")}
    with store_session() as store:
        group, _ = groups.create_group(store, "app", [found["api"].id, found["web"].id])
        store.set_group_settings(group.id, parent_project_id=found["hq"].id)
    _session(HQ_MANAGER, found["hq"], "manager")
    _session(API_CODER, found["api"], "coder")
    return found


def _events(project: ProjectInfo, kind: str) -> list[TeamEvent]:
    with store_session() as store:
        return [e for e in store.recent_events(project.id, limit=200) if e.kind == kind]


def _note(text: str, *, to: str | None = None, kind: str = "note") -> TeamEvent:
    return team_service.add_note(text, kind=kind, to_role=to, session_ref=API_CODER)


def _settings(**route: object) -> None:
    with store_session() as store:
        group = store.find_project_group("app")
        assert group is not None
        store.set_group_settings(group.id, route=route)


# --- route up --------------------------------------------------------------------------------


def test_a_members_question_to_the_parent_lands_on_its_board_and_wakes_its_manager(
    tree: dict[str, ProjectInfo], nudges: list[tuple[str, str]]
) -> None:
    """In the member's own write: one ``routed`` line on the parent's board, addressed to its
    manager (a wake under P1 WS-2), one line and a pointer, and the parent's manager nudged.
    Before this, the question stayed on the member's board until the parent read it."""
    event = _note("which base, rc/p1 or rc/p2?", to="parent", kind="question")

    [routed] = _events(tree["hq"], "routed")
    assert routed.to_role == "manager"
    assert routed.text == (
        f"routed from api#{event.seq} (question by coder): which base, rc/p1 or rc/p2?"
        " — aisquare board --project api"
    )
    assert ("prj_hq", "routed") in nudges
    with store_session() as store:
        manager = store.get_session(HQ_MANAGER)
    assert manager is not None and team_service.wake_class(routed, manager) == "wake"


def test_a_pattern_routes_a_matching_note_and_not_another(
    tree: dict[str, ProjectInfo],
) -> None:
    _settings(pattern="READY|BLOCKED")
    _note("READY: #12 merged")
    _note("lunch at noon")
    texts = [e.text for e in _events(tree["hq"], "routed")]
    assert len(texts) == 1 and "READY: #12 merged" in texts[0]


def test_a_burst_from_one_child_is_one_routed_line_and_a_count(
    tree: dict[str, ProjectInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The storm guard: one routed event per child per 60 s; the rest are folded into a
    count the next one carries. 10 children with 5 results each in a minute is ≤ 10 routed lines."""
    with store_session() as store:
        group = store.find_project_group("app")
        assert group is not None
        children = [_project(tree["hq"].root.parent / f"c{n}", f"c{n}") for n in range(8)]
        groups.add_to_group(store, group.id, [c.id for c in children])
    members = [tree["api"], tree["web"], *children]
    for n, member in enumerate(members):
        _session(f"cccc{n:04d}-0000-0000-0000-000000000000", member, "coder")
        for i in range(5):
            team_service.add_note(
                f"result {i}", kind="result", session_ref=f"cccc{n:04d}-0000-0000-0000-000000000000"
            )
    routed = _events(tree["hq"], "routed")
    assert len(routed) == 10, [e.text for e in routed]

    later = datetime.now(tz=UTC) + timedelta(seconds=61)
    monkeypatch.setattr(parent_service, "_now", lambda: later)
    team_service.add_note(
        "result 5", kind="result", session_ref="cccc0000-0000-0000-0000-000000000000"
    )
    last = max(_events(tree["hq"], "routed"), key=lambda e: e.seq)
    assert "+4 folded" in last.text, last.text


def test_a_group_without_a_parent_routes_nothing(tmp_path: Path) -> None:
    """Byte for byte as before: no routed line anywhere, and no link marks."""
    a, b = _project(tmp_path / "a", "alpha"), _project(tmp_path / "b", "beta")
    with store_session() as store:
        groups.create_group(store, "plain", [a.id, b.id])
    _session(API_CODER, a, "coder")
    _note("anyone?", kind="question", to="parent")
    with store_session() as store:
        kinds = {e.kind for p in (a, b) for e in store.recent_events(p.id, limit=50)}
    assert "routed" not in kinds


def test_routing_off_routes_nothing(tree: dict[str, ProjectInfo]) -> None:
    _settings(off=True)
    _note("stuck", kind="question", to="parent")
    assert _events(tree["hq"], "routed") == []


# --- linked tasks ---------------------------------------------------------------------------


def test_a_delegated_task_shows_its_origin_routes_up_and_all_done_closes_the_loop(
    tree: dict[str, ProjectInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``task add --project <member> --origin <task>`` from the parent: both boards show the
    link; a child's done routes up; once every child is done, ``linked_all_done`` lands on
    the parent's board for its manager to close the origin."""
    origin, _ = team_service.add_task("ship auth", session_ref=HQ_MANAGER)
    api_task, _ = team_service.add_task(
        "auth API", session_ref=HQ_MANAGER, project="api", origin=str(origin.number)
    )
    web_task, _ = team_service.add_task(
        "auth UI", session_ref=HQ_MANAGER, project="web", origin=str(origin.number)
    )
    assert api_task.project_id == tree["api"].id and web_task.project_id == tree["web"].id

    with store_session() as store:
        hq_board = team_service.render_board(tree["hq"], [], store.team_tasks(tree["hq"].id), [])
        api_board = team_service.render_board(tree["api"], [], store.team_tasks(tree["api"].id), [])
    assert "ship auth → 2 linked, 0 done" in hq_board
    assert f"auth API ← hq#{origin.number}" in api_board

    team_service.claim_task(api_task.id, session_ref=API_CODER)
    team_service.finish_task(api_task.id, session_ref=API_CODER)
    assert any("task_done" in e.text for e in _events(tree["hq"], "routed"))
    assert _events(tree["hq"], "linked_all_done") == []

    later = datetime.now(tz=UTC) + timedelta(seconds=61)  # past the storm guard
    monkeypatch.setattr(parent_service, "_now", lambda: later)
    _session("dddd3333-0000-0000-0000-000000000000", tree["web"], "coder")
    team_service.claim_task(web_task.id, session_ref="dddd3333-0000-0000-0000-000000000000")
    team_service.finish_task(web_task.id, session_ref="dddd3333-0000-0000-0000-000000000000")
    [done] = _events(tree["hq"], "linked_all_done")
    assert done.to_role == "manager" and f"close #{origin.number}" in done.text


def test_delegating_to_a_project_this_board_does_not_parent_is_refused(
    tree: dict[str, ProjectInfo], tmp_path: Path
) -> None:
    stranger = _project(tmp_path / "other", "other")
    origin, _ = team_service.add_task("ship auth", session_ref=HQ_MANAGER)
    with pytest.raises(ValueError, match="not a member of a group this board"):
        team_service.add_task(
            "x", session_ref=HQ_MANAGER, project=stranger.codename, origin=str(origin.number)
        )
    with pytest.raises(ValueError, match="--origin goes with --project"):
        team_service.add_task("y", session_ref=HQ_MANAGER, origin=str(origin.number))


# --- the manager's cycle and the parent's briefing -------------------------------------------


def test_a_member_manager_asks_its_parent_and_a_parent_is_briefed_on_its_group(
    tree: dict[str, ProjectInfo],
) -> None:
    sid = "eeee4444-0000-0000-0000-000000000000"
    cycle = " ".join(
        harness.role_cycle("manager", sid[:8], fleet=lambda v: v, escalate_to_parent=True)
    )
    assert "--kind question --to parent" in cycle and "Ask the human" not in cycle
    plain = " ".join(harness.role_cycle("manager", sid[:8], fleet=lambda v: v))
    assert "Ask the human" in plain, "no parent: as before"

    with store_session() as store:
        lines = parent_service.briefing(store, tree["hq"].id)
    text = "\n".join(lines)
    assert "You are the parent manager of:" in text and "app: api, web" in text
    assert len(text) <= parent_service.BRIEFING_MAX


# --- the CLI --------------------------------------------------------------------------------


def test_the_group_verbs(tree: dict[str, ProjectInfo]) -> None:
    runner = CliRunner()
    shown = runner.invoke(
        app, ["--json", "project", "group", "route", "app", "--kinds", "result,linked"]
    )
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["route"] == {"kinds": ["result", "linked"], "off": False}
    bad = runner.invoke(app, ["project", "group", "route", "app", "--kinds", "gossip"])
    assert bad.exit_code != 0 and "unknown kind" in bad.output
    member = runner.invoke(app, ["project", "group", "parent", "app", "api"])
    assert member.exit_code != 0 and "a parent is a project outside it" in member.output
    cleared = runner.invoke(app, ["--json", "project", "group", "parent", "app", "--clear"])
    assert json.loads(cleared.stdout)["parent_project_id"] is None
    capped = runner.invoke(app, ["--json", "project", "group", "cap", "app", "3"])
    assert json.loads(capped.stdout)["max_agents"] == 3


# --- subtree pause and the cap (the fleet suite's fake tmux) ---------------------------------

tmux = fleet_suite.tmux
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project


def _member_of_a_parented_group(project: ProjectInfo, tmp_path: Path) -> ProjectInfo:
    hq = _project(tmp_path / "hq", "hq")
    with store_session() as store:
        group, _ = groups.create_group(store, "app", [project.id])
        store.set_group_settings(group.id, parent_project_id=hq.id)
    return hq


def test_a_group_pause_pauses_parent_and_members_and_an_agent_may_not_spawn_there(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    hq = _member_of_a_parented_group(project, tmp_path)
    result = CliRunner().invoke(app, ["--json", "fleet", "pause", "--group", "app"])
    assert result.exit_code == 0, result.output
    assert {row["project"]["id"] for row in json.loads(result.stdout)} == {project.id, hq.id}
    assert fleet_service.is_paused(project) and fleet_service.is_paused(hq)

    with pytest.raises(fleet_service.FleetError, match="is paused — an agent may not spawn"):
        fleet_service.spawn(project, "coder", worktree=False, spawned_by=HQ_MANAGER)
    fleet_service.spawn(project, "coder", worktree=False)  # a person's spawn goes ahead

    resumed = CliRunner().invoke(app, ["fleet", "resume", "--group", "app"])
    assert resumed.exit_code == 0 and not fleet_service.is_paused(project)
    fleet_service.spawn(project, "coder", worktree=False, spawned_by=HQ_MANAGER)


def test_a_paused_project_in_no_group_behaves_as_before(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The refusal is the subtree's: a project with no parent above or below it spawns for
    an agent while paused, as the cycle's own rule has always asked it not to."""
    fleet_service.pause(project)
    fleet_service.spawn(project, "coder", worktree=False, spawned_by=HQ_MANAGER)


def test_a_group_cap_refuses_the_next_spawn(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    _member_of_a_parented_group(project, tmp_path)
    with store_session() as store:
        group = store.find_project_group("app")
        assert group is not None
        store.set_group_settings(group.id, max_agents=1)
    fleet_service.spawn(project, "coder", worktree=False)
    with pytest.raises(fleet_service.CapacityRefused, match="group app already runs 1 agents"):
        fleet_service.spawn(project, "coder", worktree=False)
