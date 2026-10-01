"""A fleet seat lands on its OWN fleet's board, whatever hub the tmux server carries.

Card tsk_01m3k89b2f96tt7xc6crzvxzjk, fix 2, from the manager's measurement on
2026-09-28: the tmux server ``asqui`` holds a GLOBAL ``AISQUARE_TEAM_HUB``,
inherited from the shell that first started it. Every window inherits the
server's environment, and ``orchestrator.team_project`` let that hub override
everything. So the seats of two fleets on different projects, the captain's
and a release train's, all registered on the third project the server's hub
named. Their team deltas and stop hooks then mixed both trains, and a note
posted ``--as`` a seat landed on the wrong board.

The rule these pins hold: inside a fleet window (``AISQUARE_FLEET_AGENT`` set),
the board is the fleet row's project, ahead of any inherited hub. A differing
hub is said once on stderr. Outside a fleet window the hub behaves exactly as
before (``tests/test_a_relative_team_hub_is_ignored.py`` and the team suite).
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aisquare.core import orchestrator
from aisquare.core.ids import new_agent_id, new_event_id
from aisquare.core.store import store_session
from aisquare.core.workspace import project_id_for
from aisquare.models import FleetAgent, ProjectInfo, TeamEvent, TeamSession
from aisquare.services import team as team_service


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with the one-shot warnings unfired and no inherited pins."""
    monkeypatch.setattr(orchestrator, "_WARNED_HUBS", set(), raising=False)
    for name in ("AISQUARE_TEAM_HUB", "AISQUARE_FLEET_AGENT", "AISQUARE_ROLE"):
        monkeypatch.delenv(name, raising=False)


def _project(tmp_path: Path, name: str) -> ProjectInfo:
    root = tmp_path / name
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    info = ProjectInfo(id=project_id_for(root.resolve()), root=root.resolve(), linked_repos=[])
    with store_session() as store:
        return store.onboard_project(info)


def _seat(project: ProjectInfo, label: str) -> FleetAgent:
    """A live fleet row of ``project``, as ``fleet spawn`` records it before launch."""
    with store_session() as store:
        return store.upsert_fleet_agent(
            FleetAgent(
                id=new_agent_id(),
                project_id=project.id,
                label=label,
                role="coder",
                pane_id=f"%{label[-1]}",
                cwd=project.root,
                created_at=datetime.now(tz=UTC),
            )
        )


@pytest.fixture
def boards(tmp_path: Path) -> tuple[ProjectInfo, ProjectInfo, Path]:
    """Two fleets' projects, and a third directory the tmux server's hub names."""
    third = tmp_path / "workspace-rc"
    third.mkdir()
    return _project(tmp_path, "captain"), _project(tmp_path, "release"), third


def test_a_fleet_window_answers_its_fleets_board_over_an_inherited_hub(
    boards: tuple[ProjectInfo, ProjectInfo, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    captain, release, third = boards
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(third))
    for project in (captain, release):
        monkeypatch.setenv("AISQUARE_FLEET_AGENT", _seat(project, "coder-1").id)
        answer = orchestrator.team_project()
        assert (answer.id, answer.root) == (project.id, project.root)


def test_the_overridden_hub_is_said_once_on_stderr(
    boards: tuple[ProjectInfo, ProjectInfo, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captain, _, third = boards
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(third))
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", _seat(captain, "coder-1").id)
    orchestrator.team_project()
    orchestrator.team_project()
    err = capsys.readouterr().err
    assert err.count(str(third)) == 1, err
    assert "fleet" in err and captain.root.name in err, err


def test_a_hub_that_already_names_the_fleets_board_is_silent(
    boards: tuple[ProjectInfo, ProjectInfo, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """What fix 1 sets on every window: the fleet's own root, so nothing to say."""
    captain, _, _ = boards
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(captain.root))
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", _seat(captain, "coder-1").id)
    assert orchestrator.team_project().id == captain.id
    assert capsys.readouterr().err == ""


def test_outside_a_fleet_window_the_hub_still_wins(
    boards: tuple[ProjectInfo, ProjectInfo, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: the hub's documented job, one board for a multi-repo run."""
    captain, _, third = boards
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(third))
    assert orchestrator.team_project(captain.root).root == third.resolve()


def test_a_fleet_id_with_no_row_falls_back_to_the_hub(
    boards: tuple[ProjectInfo, ProjectInfo, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale or foreign id never strands a command: the old resolution stands."""
    _, _, third = boards
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(third))
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", "agt_no_such_row")
    assert orchestrator.team_project().root == third.resolve()


def test_two_fleets_seats_register_on_their_own_boards_and_see_only_their_own_delta(
    boards: tuple[ProjectInfo, ProjectInfo, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The card's pin: two fleets of different projects under one server hub.

    Each seat's session-start runs with the server's hub inherited and its
    window's fleet id set, as the session-start hook does inside a fleet window.
    Each session must register on its own fleet's board, and a seat's delta must
    carry its own board's news and never the other fleet's."""
    captain, release, third = boards
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(third))
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    seats = {
        "cap-1": _seat(captain, "coder-1"),
        "rel-1": _seat(release, "coder-1"),
        "cap-2": _seat(captain, "coder-2"),
    }
    sessions = {
        "cap-1": "cccc1111-0000-0000-0000-000000000001",
        "rel-1": "rrrr1111-0000-0000-0000-000000000001",
        "cap-2": "cccc2222-0000-0000-0000-000000000002",
    }
    for key in ("cap-1", "rel-1"):
        monkeypatch.setenv("AISQUARE_FLEET_AGENT", seats[key].id)
        team_service.hook_session_start(sessions[key], seats[key].cwd, "startup")
    with store_session() as store:
        registered = {key: store.get_session(sessions[key]) for key in ("cap-1", "rel-1")}
    assert registered["cap-1"] is not None and registered["cap-1"].project_id == captain.id
    assert registered["rel-1"] is not None and registered["rel-1"].project_id == release.id

    # News on each board after both joined: a second captain seat, and a release note.
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", seats["cap-2"].id)
    team_service.hook_session_start(sessions["cap-2"], seats["cap-2"].cwd, "startup")
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", seats["rel-1"].id)
    team_service.add_note("release train: the cut is done", session_ref=sessions["rel-1"])

    monkeypatch.setenv("AISQUARE_FLEET_AGENT", seats["cap-1"].id)
    delta = team_service.hook_prompt_heartbeat(sessions["cap-1"], seats["cap-1"].cwd)
    assert "the cut is done" not in delta, delta
    with store_session() as store:
        cap2 = store.get_session(sessions["cap-2"])
    assert cap2 is not None and cap2.project_id == captain.id

    monkeypatch.setenv("AISQUARE_FLEET_AGENT", seats["rel-1"].id)
    with store_session() as store:
        release_events = store.filtered_events(release.id, since_seq=0, limit=100)
        third_board = store.get_project(project_id_for(third.resolve()))
    assert any("the cut is done" in event.text for event in release_events)
    assert third_board is None, "nothing registered on the server hub's board"


def test_the_same_row_id_in_another_store_answers_that_stores_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row id is unique within ONE store only. A process that reads a second store (a
    test, or a command pointed at another ``AISQUARE_HOME``) must get that store's answer:
    a per-process cache keyed by the id served the first store's project to the second
    (measured on this card's first build, in tests/test_persona_briefing.py's fixed id)."""
    seen = []
    for home in ("home-a", "home-b"):
        monkeypatch.setenv("AISQUARE_HOME", str(tmp_path / home))
        project = _project(tmp_path, f"proj-{home}")
        with store_session() as store:
            store.upsert_fleet_agent(
                FleetAgent(
                    id="agt_01samerowid",
                    project_id=project.id,
                    label="coder-1",
                    role="coder",
                    pane_id="%1",
                    cwd=project.root,
                    created_at=datetime.now(tz=UTC),
                )
            )
        monkeypatch.setenv("AISQUARE_FLEET_AGENT", "agt_01samerowid")
        seen.append((orchestrator.team_project().id, project.id))
    assert [answer for answer, _ in seen] == [expected for _, expected in seen], seen


# --- fix 2b (14435): a session registered on the wrong board moves on its next start -------


def _note_on(project: ProjectInfo, text: str) -> int:
    with store_session() as store:
        event = store.add_team_event(
            TeamEvent(
                id=new_event_id(),
                project_id=project.id,
                kind="note",
                text=text,
                created_at=datetime.now(tz=UTC),
            )
        )
    assert event.seq is not None
    return event.seq


def _registered(session_id: str, project: ProjectInfo, cursor: int) -> TeamSession:
    now = datetime.now(tz=UTC)
    with store_session() as store:
        return store.upsert_session(
            TeamSession(
                id=session_id,
                project_id=project.id,
                role="coder",
                started_at=now,
                last_seen_at=now,
                cursor=cursor,
            )
        )


def test_a_session_registered_under_a_then_under_b_reads_b_from_a_fresh_cursor(
    boards: tuple[ProjectInfo, ProjectInfo, Path],
) -> None:
    """``upsert_session``'s conflict branch kept the first board forever, and a fleet
    restart resumes the same session id, so a seat that once registered on the wrong
    board stayed there (the manager moved 4 rows by hand on 09-28). Moving it also moves
    its cursor to where a fresh registration on B starts, or its first delta on B would
    replay B's history since its old place on A."""
    captain, release, _ = boards
    sid = "aaaa0000-0000-0000-0000-00000000000a"
    _registered(sid, captain, cursor=_note_on(captain, "captain news"))
    backlog = _note_on(release, "release news the seat never needs replayed")
    moved = _registered(sid, release, cursor=backlog)
    assert moved.project_id == release.id
    assert moved.cursor == backlog, "a move starts from the new board's cursor"


def test_a_session_re_registered_on_its_own_board_keeps_its_cursor(
    boards: tuple[ProjectInfo, ProjectInfo, Path],
) -> None:
    """The control: a resume on the SAME board is a refresh, exactly as before."""
    captain, _, _ = boards
    sid = "aaaa0000-0000-0000-0000-00000000000b"
    first = _note_on(captain, "before")
    _registered(sid, captain, cursor=first)
    later = _note_on(captain, "after")
    again = _registered(sid, captain, cursor=later)
    assert (again.project_id, again.cursor) == (captain.id, first)


def test_a_seat_on_the_wrong_board_moves_to_its_fleets_board_on_restart(
    boards: tuple[ProjectInfo, ProjectInfo, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end, as the fleet restart runs it: the seat first registered under the
    server's hub (the defect), then resumes the SAME session id inside its fleet window.
    It must land on its fleet's board, and news posted there before the move must not
    come back in its first delta (the start already briefed it on the board)."""
    captain, _, third = boards
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(third))
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    sid = "cccc9999-0000-0000-0000-000000000009"
    team_service.hook_session_start(sid, captain.root, "startup")
    with store_session() as store:
        before = store.get_session(sid)
    assert before is not None and before.project_id == project_id_for(third.resolve())

    _note_on(captain, "posted on the captain board before the seat arrived")
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", _seat(captain, "coder-1").id)
    team_service.hook_session_start(sid, captain.root, "resume")
    with store_session() as store:
        after = store.get_session(sid)
    assert after is not None and after.project_id == captain.id

    delta = team_service.hook_prompt_heartbeat(sid, captain.root)
    assert "before the seat arrived" not in delta, delta


def test_the_overridden_hub_is_printed_as_typed_never_escaped(
    boards: tuple[ProjectInfo, ProjectInfo, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#230's Windows red (job 108830025123, 14482): the warning quoted the hub with repr, so a
    Windows path's backslashes came out doubled, and the owner could not find their own path
    in it. A backslash is a legal name character on POSIX, so this reproduces it on every OS."""
    captain, _, _ = boards
    odd = tmp_path / "work\\space-rc"
    odd.mkdir(parents=True)
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(odd))
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", _seat(captain, "coder-1").id)
    orchestrator.team_project()
    err = capsys.readouterr().err
    assert str(odd) in err, err
    assert "\\\\" not in err, "each backslash is printed once, as typed"
