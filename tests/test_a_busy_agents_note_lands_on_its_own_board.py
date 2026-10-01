"""``tell``'s board-note fallback lands on the TARGET's board, whatever the caller's env says.

Review of #240, finding 8. A busy agent cannot be typed into, so ``fleet tell`` files a
board note addressed to it, and the agent reads that note in its next prompt's delta.
Since #230 every fleet seat reads its own fleet's board. The note was filed with
``team.add_note(..., cwd=project.root)``, and a ``cwd`` is only the LAST thing
``orchestrator.team_project`` asks: an exported absolute ``AISQUARE_TEAM_HUB`` wins over
it, and so does the caller's own ``AISQUARE_FLEET_AGENT`` row. So a tell from a shell with
a hub exported, or from inside another fleet's window, put the note on a board the target
never reads, and still printed "filed as board note #N to coder-1". A sender session
registered on another board pulled the note onto that board, and ``persona attach`` wrote
its ``persona_attached`` line through the same resolution.

The rule these pins hold: the fleet names the target's project by id
(``team.add_note(project_id=…)``), and nothing else picks that board. A note that names
no project resolves its board exactly as before.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import orchestrator
from aisquare.core.ids import new_task_id
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.core.workspace import project_id_for
from aisquare.models import FleetAgent, ProjectInfo, TeamEvent, TeamTask
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from tests import test_fleet_service as fleet_suite
from tests.test_fleet_service import FakeTmux, _board_session, _git

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path

TOLD = "rebase onto main before you push"


class Boards(NamedTuple):
    """Two projects with a fleet each, and a directory an exported hub names."""

    a: ProjectInfo
    """The caller's own fleet."""
    b: ProjectInfo
    """The target's fleet: the board its agents read."""
    hub: Path


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with the one-shot hub warnings unfired."""
    monkeypatch.setattr(orchestrator, "_WARNED_HUBS", set(), raising=False)


def _repo(tmp_path: Path, name: str) -> ProjectInfo:
    root = tmp_path / name
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=root)
    info = team_project(root)
    with store_session() as store:
        store.ensure_project(info)
    return info


@pytest.fixture
def boards(tmp_path: Path, tmux: FakeTmux, claude_on_path: Path) -> Boards:
    hub = tmp_path / "hub"
    hub.mkdir()
    return Boards(_repo(tmp_path, "repo-a"), _repo(tmp_path, "repo-b"), hub)


def _busy(project: ProjectInfo) -> FleetAgent:
    """A live coder of ``project`` in the middle of a turn: ``tell`` may not type into it."""
    agent = fleet_service.spawn(project, "coder", worktree=False).agent
    _board_session(agent, "working")
    return agent


def _filed(project_id: str, kind: str = "note") -> list[TeamEvent]:
    with store_session() as store:
        return store.filtered_events(project_id, kind=kind, since_seq=0, limit=100)


def _delta(agent: FleetAgent, monkeypatch: pytest.MonkeyPatch) -> str:
    """The agent's next prompt delta, as its own hook reads it: inside its fleet window."""
    assert agent.session_id is not None
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", agent.id)
    return team_service.hook_prompt_heartbeat(agent.session_id, agent.cwd)


# --- the finding's repros: a hub, another fleet's window, a sender of another board -------


def test_under_an_exported_hub_a_tell_to_a_busy_agent_is_filed_on_its_own_board(
    boards: Boards, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """``AISQUARE_TEAM_HUB=/work/hub aisquare fleet tell -P B coder-1 '…'``: the hub is where
    this SHELL's own notes go, not where B's coder reads. The receipt named a note #N that
    sat on the hub's board, so the CLI said "filed" about a message nobody would deliver."""
    coder = _busy(boards.b)
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(boards.hub))

    result = runner.invoke(app, ["fleet", "tell", "-P", boards.b.id, "coder-1", TOLD])

    assert result.exit_code == 0, result.output
    filed = _filed(boards.b.id)
    assert [(note.text, note.to_role) for note in filed] == [(TOLD, "coder-1")]
    assert f"filed as board note #{filed[0].seq} to coder-1" in " ".join(result.stdout.split())
    assert _filed(project_id_for(boards.hub.resolve())) == [], "nothing went to the hub's board"
    assert TOLD in _delta(coder, monkeypatch), "the agent's next delta delivers it"


def test_from_inside_another_fleets_window_a_tell_is_filed_on_the_targets_board(
    boards: Boards, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manager in repo A's fleet window runs ``fleet tell coder-1 -P B`` without ``--as``:
    its ``AISQUARE_FLEET_AGENT`` row puts the manager's OWN traffic on A's board (#230), and
    it took the note for B's coder there with it."""
    manager = fleet_service.spawn(boards.a, "manager").agent
    coder = _busy(boards.b)
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", manager.id)

    result = fleet_service.tell(boards.b, "coder-1", TOLD)

    assert not result.delivered
    filed = _filed(boards.b.id)
    assert [(note.text, note.to_role) for note in filed] == [(TOLD, "coder-1")]
    assert result.how.endswith(f"filed as board note #{filed[0].seq} to coder-1")
    assert _filed(boards.a.id) == [], "the caller's own board got nothing"
    assert TOLD in _delta(coder, monkeypatch), "the agent's next delta delivers it"


def test_a_sender_registered_on_another_board_neither_moves_the_note_nor_fails_it(
    boards: Boards, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--as`` a session of board A: with a session, ``add_note`` delivers to the SESSION's
    board (#20), which is right for the session's own notes and wrong for a message to an
    agent of B. The note is still filed as that sender, on the board the target reads."""
    manager = fleet_service.spawn(boards.a, "manager").agent
    _board_session(manager, "waiting")
    coder = _busy(boards.b)

    result = fleet_service.tell(boards.b, "coder-1", TOLD, sender=manager.session_id)

    assert not result.delivered
    filed = _filed(boards.b.id)
    assert [(note.text, note.to_role) for note in filed] == [(TOLD, "coder-1")]
    assert filed[0].session_id == manager.session_id, "still filed as the caller"
    assert _filed(boards.a.id) == [], "the sender's own board got nothing"
    assert TOLD in _delta(coder, monkeypatch), "the agent's next delta delivers it"


@pytest.mark.parametrize("caller", ["under-a-hub", "in-another-fleets-window"])
def test_the_persona_attached_line_is_written_on_the_agents_own_board(
    boards: Boards, monkeypatch: pytest.MonkeyPatch, caller: str
) -> None:
    """``persona attach`` (the CLI, the Personas tab, the captain) wrote its one
    ``persona_attached`` line through the same ``cwd`` resolution as the note."""
    seat = fleet_service.spawn(boards.a, "manager").agent
    _busy(boards.b)
    if caller == "under-a-hub":
        monkeypatch.setenv("AISQUARE_TEAM_HUB", str(boards.hub))
        elsewhere = project_id_for(boards.hub.resolve())
    else:
        monkeypatch.setenv("AISQUARE_FLEET_AGENT", seat.id)
        elsewhere = boards.a.id

    fleet_service.attach_persona(boards.b, "coder-1", "skeptic")

    lines = _filed(boards.b.id, "persona_attached")
    assert [(line.text, line.to_role) for line in lines] == [
        ("persona skeptic attached to coder-1", "coder-1")
    ]
    assert _filed(elsewhere, "persona_attached") == []


# --- the keyword itself, and what stays as it was ----------------------------------------


def test_a_note_that_names_a_project_is_delivered_there_and_its_receipt_says_so(
    boards: Boards, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``project_id`` wins over everything ``add_note`` could resolve a board from: the hub,
    the caller's fleet row, the ``cwd`` and the session's own board. The receipt names the
    board the event was read back from, with no "cwd resolves to another board" warning:
    the caller asked for this board by name."""
    seat = fleet_service.spawn(boards.a, "manager").agent
    _board_session(seat, "waiting")
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(boards.hub))
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", seat.id)

    event = team_service.add_note(
        TOLD,
        session_ref=seat.session_id,
        to_role="coder-1",
        cwd=boards.a.root,
        project_id=boards.b.id,
    )

    assert (event.project_id, event.session_id) == (boards.b.id, seat.session_id)
    delivery = team_service.last_delivery()
    assert delivery is not None
    assert (delivery.seq, delivery.board_id, delivery.warning) == (event.seq, boards.b.id, None)


def test_a_task_ref_is_checked_against_the_board_the_note_names(boards: Boards) -> None:
    """``--task`` of another board stays refused (a foreign task renders as a broken ref for
    every reader): the board it is compared with is the one the note is delivered to."""
    now = datetime.now(tz=UTC)
    tasks: dict[str, TeamTask] = {}
    with store_session() as store:
        for project in (boards.a, boards.b):
            tasks[project.id], _ = store.upsert_task(
                TeamTask(
                    id=new_task_id(),
                    project_id=project.id,
                    key=f"task-of-{project.root.name}",
                    title=f"a task of {project.root.name}",
                    created_at=now,
                    updated_at=now,
                )
            )

    with pytest.raises(ValueError, match="belongs to another project's board"):
        team_service.add_note(TOLD, task_ref=tasks[boards.a.id].id, project_id=boards.b.id)
    own = team_service.add_note(TOLD, task_ref=tasks[boards.b.id].id, project_id=boards.b.id)

    assert (own.project_id, own.task_id) == (boards.b.id, tasks[boards.b.id].id)
    assert [note.id for note in _filed(boards.b.id)] == [own.id], "the refused one wrote nothing"


def test_a_note_that_names_no_project_resolves_its_board_as_before(
    boards: Boards, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: only the fleet's fallback names a project. ``aisquare note`` under a hub
    still files on the hub's board, and a note ``--as`` a session still follows the session
    (#20), whatever ``cwd`` it was typed in."""
    seat = fleet_service.spawn(boards.a, "manager").agent
    _board_session(seat, "waiting")
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(boards.hub))

    plain = team_service.add_note("for whoever shares this hub", cwd=boards.b.root)
    attributed = team_service.add_note(
        "from the manager of a", session_ref=seat.session_id, cwd=boards.b.root
    )

    assert plain.project_id == project_id_for(boards.hub.resolve())
    assert attributed.project_id == boards.a.id
    assert _filed(boards.b.id) == []
