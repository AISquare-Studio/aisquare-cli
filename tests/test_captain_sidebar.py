"""The captain's row in the fleet UI: under a home-level heading, never inside a project (T2).

Acceptance (contract 13121, item 6): the sidebar shows a "Captain" section above
the projects with the captain's agent row; the home never gets a project card;
selecting the row opens the same agent view any agent opens. Driven with the
UI suite's harness: the real store in the isolated home, ``list_agents``
scripted, and every tmux call held to a private socket.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Static
from textual.widgets._toast import Toast

from aisquare.cli.ui.sidebar import Activatable, AgentRow, DoctorTitle, ProjectCard
from aisquare.cli.ui.terminal import TerminalPane
from aisquare.cli.ui.views.agent import AgentView
from aisquare.cli.ui.views.project import ProjectView
from aisquare.core.store import store_session
from aisquare.models import FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services.captain import state as captain_state
from tests import test_ui_shell as ui_suite
from tests.test_ui_shell import Script, drive, fleet_app, row_for, seed, shown, status

# The UI suite's fixtures, bound here so pytest finds them for this module's tests
# (``no_real_tmux`` is autouse there: every tmux call held to a private socket).
no_real_tmux = ui_suite.no_real_tmux
script = ui_suite.script


async def until(pilot: Pilot[None], done: Callable[[], bool], *, what: str) -> None:
    """Pause until ``done()`` holds.

    Selecting an agent is a handler that AWAITS the view's mount, then selects the
    row, scopes the Doctor and focuses the pane in a later turn of the loop. On
    Windows CI ``pilot.pause()`` returned between the two halves (its idle wait
    rides a coarse timer): the test body ended, and the rest of the handler ran
    during teardown — ``NoMatches`` for the Doctor section (#219, 77ed31cb, job
    108064714234). So a test waits for the handler's LAST effect, not for a pause.
    """
    for _ in range(200):
        if done():
            return
        await pilot.pause(0.02)
    raise AssertionError(f"the UI never settled: {what}")


async def quiet(pilot: Pilot[None]) -> None:
    """Let the app's workers answer before the test ends: a Doctor scope change runs the
    doctor in a thread, and its answer painted ``#doctor`` during teardown otherwise."""
    await ui_suite.settle(fleet_app(pilot))
    await pilot.pause()


def agent_opened(pilot: Pilot[None], agent_id: str) -> Callable[[], bool]:
    """The last effect of selecting an agent: its row selected, its pane focused (#147)."""
    app = fleet_app(pilot)

    def done() -> bool:
        return app.sidebar.selected_key == f"agent:{agent_id}" and isinstance(
            app.focused, TerminalPane
        )

    return done


def test_the_captain_row_sits_under_a_home_heading_not_inside_a_project(
    tmp_path: Path,
    script: Script,
) -> None:
    seed(tmp_path, ("prj_aaa", "alpha", "amber-otter"))
    home = captain_state.home_project()
    captain = status(home.id, "captain", "captain", "waiting")
    script[home.id] = [captain]
    script["prj_aaa"] = [status("prj_aaa", "coder-1", "coder", "working")]

    async def body(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        app.refresh_data()
        await pilot.pause()
        section = app.query_one("#captain-section")
        row = row_for(app, captain.agent.id)
        assert section in row.ancestors, "the captain's row is in the home-level section"
        assert "captain" in shown(row)
        assert not app.query(f"#card-{home.id}"), "the home is never a project card"
        assert app.query("#card-prj_aaa"), "the projects are still listed"
        row.activate()
        await until(pilot, agent_opened(pilot, captain.agent.id), what="the captain selected")
        view = app.query_one(AgentView)
        assert view.status.agent.id == captain.agent.id, "selecting it opens its agent view"
        await quiet(pilot)

    drive(body)


def test_with_no_captain_the_section_says_how_to_start_one(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, ("prj_aaa", "alpha", "amber-otter"))

    async def body(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        app.refresh_data()
        await pilot.pause()
        section = app.query_one("#captain-section")
        assert not section.query(AgentRow)
        assert "aisquare captain" in shown(app.query_one("#captain-empty"))  # type: ignore[arg-type]

    drive(body)


# --- review of T2: the home is a board, never a project ------------------------------------


def _remember(value: str | None) -> None:
    with store_session() as store:
        store.set_ui_state("fleet.selected", value)


def _remembered() -> str | None:
    with store_session() as store:
        return store.ui_state("fleet.selected")


@pytest.mark.parametrize("shown_by", ["the a key", "the saved setting"])
def test_with_the_captured_directories_shown_the_home_is_still_no_card(
    tmp_path: Path, script: Script, shown_by: str
) -> None:
    """``a`` lists the captured directories, and the home is one — it stays off the list.

    ``list_projects(all=True)`` answered the home's captured row, so it got a
    card: the captain's row twice (the section's and the card's, under one
    selection key), a spawn row on the home board, and ↓ from the second copy
    jumped back to the first — ``_move_cursor`` anchors on the first match — so
    the cursor cycled between the home's card and the captain and never
    reached a project below them. ``a`` is saved, so an owner who pressed it
    once met this at every launch: both routes are driven.
    """
    seed(tmp_path, ("prj_aaa", "alpha", None), ("prj_zzz", "zulu", None))
    with store_session() as store:
        store.ensure_project(ProjectInfo(id="prj_scratch", root=tmp_path / "scratch"))  # a hook
    home = captain_state.home_project()
    captain = status(home.id, "captain", "captain", "waiting")
    script[home.id] = [captain]
    script["prj_aaa"] = [status("prj_aaa", "coder-1", "coder", "working")]
    if shown_by == "the saved setting":
        with store_session() as store:
            store.set_ui_state("fleet.show_captured", "1")

    async def body(pilot: Pilot[None]) -> tuple[list[str], list[str], list[str]]:
        app = fleet_app(pilot)
        app.sidebar.focus()
        await pilot.pause()
        if shown_by == "the a key":
            await pilot.press("a")
            await pilot.pause()
        cards = [card.project.id for card in app.query(ProjectCard)]
        copies = [
            type(row.parent).__name__
            for row in app.query(AgentRow)
            if row.status.agent.id == captain.agent.id
        ]
        walk: list[str] = []
        for _ in range(12):  # more presses than there are rows: the walk ends at the last one
            await pilot.press("down")
            await pilot.pause()
            walk.extend(row.selection_key for row in app.query(".cursor").results(Activatable))
        return cards, copies, walk

    cards, copies, walk = drive(body)
    assert "prj_scratch" in cards, "the premise: the captured directories are listed"
    assert home.id not in cards, "the home is never a project card, `a` or not"
    assert copies == ["CaptainSection"], "one captain row, the section's"
    distinct = [key for index, key in enumerate(walk) if index == 0 or key != walk[index - 1]]
    assert len(distinct) == len(set(distinct)), f"the cursor went round in a loop: {walk}"
    assert {"project:prj_aaa", "project:prj_scratch", "project:prj_zzz"} <= set(walk), walk


def test_stopping_the_captain_goes_back_to_the_welcome_page_not_a_project_of_the_home(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop still reaches the service with the home's board; after it, no project view.

    A stopped agent's view goes back to its project (``stop_finished``). The
    captain's project is the home, and ``FleetSnapshot.project`` answered it, so
    a ProjectView of ``$AISQUARE_HOME`` opened — "has no manager yet", with a
    Start-manager button that spawns one on the home board — and the sidebar
    selected a ``project:`` row that does not exist. The home has no page of
    its own, so the shell goes back to where it starts: the welcome page,
    nothing selected, nothing remembered.
    """
    seed(tmp_path, ("prj_aaa", "alpha", None))
    home = captain_state.home_project()
    captain = status(home.id, "captain", "captain", "working")
    script[home.id] = [captain]
    recorder = ui_suite.stopper(monkeypatch, captain.agent)

    async def body(pilot: Pilot[None]) -> tuple[str | None, str | None, list[str], list[str]]:
        app = fleet_app(pilot)
        await ui_suite.open_stop_dialog(pilot, app, captain.agent.id)
        script[home.id] = [FleetAgentStatus(agent=ui_suite.ended(captain.agent), state="exited")]
        await pilot.click("#stop-confirm")
        await ui_suite.settle(app)
        await pilot.pause()
        await pilot.pause()
        toasts = [toast.render().plain for toast in app.screen.query(Toast)]
        pages = [view.project.id for view in app.query(ProjectView)]
        return app.content.current, app.sidebar.selected_key, pages, toasts

    current, selected, pages, toasts = drive(body, notifications=True)
    assert recorder.calls == [(home.id, "captain", False, captain.agent.id)], "Stop still works"
    assert f"✓ stopped captain ({captain.agent.id})" in toasts
    assert home.id not in pages, "the home board never opens as a project"
    assert (current, selected) == ("welcome", None)
    assert not [toast for toast in toasts if "no longer listed" in toast], toasts
    assert _remembered() is None, "nothing to reopen at the next launch either"


def test_selecting_the_captain_leaves_the_doctor_global_not_scoped_to_the_home(
    tmp_path: Path, script: Script
) -> None:
    """Selecting an agent scopes the Doctor to its project; the captain's board is none.

    Scoped to the home, the Doctor ran the per-project checks — snapshot,
    brain, harness — with ``cwd=$AISQUARE_HOME``, and armed their one-click
    fixes there. The control is an ordinary agent, whose project IS the scope.
    """
    seed(tmp_path, ("prj_aaa", "alpha", None))
    home = captain_state.home_project()
    captain = status(home.id, "captain", "captain", "waiting")
    coder = status("prj_aaa", "coder-1", "coder", "working")
    script[home.id] = [captain]
    script["prj_aaa"] = [coder]

    async def body(pilot: Pilot[None]) -> tuple[tuple[str | None, ...], tuple[str | None, ...]]:
        app = fleet_app(pilot)
        title = app.sidebar.query_one(DoctorTitle)
        row_for(app, coder.agent.id).activate()
        await until(pilot, agent_opened(pilot, coder.agent.id), what="the coder selected")
        control = (app.doctor_scope, title.project_id)
        row_for(app, captain.agent.id).activate()
        await until(pilot, agent_opened(pilot, captain.agent.id), what="the captain selected")
        view = app.current_view()
        opened = view.id if view is not None else None
        await quiet(pilot)
        return control, (app.doctor_scope, title.project_id, opened)

    control, selected = drive(body)
    assert control == ("prj_aaa", "prj_aaa"), "an ordinary agent scopes the Doctor to its project"
    assert selected == (None, None, f"agent-{captain.agent.id}"), "the captain's: global"


def test_restarting_the_captain_selects_its_new_row_and_keeps_the_doctor_global(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restart on the captain's row: the service is asked with the home's board, the
    shell follows the new row as it does any agent's (#138), and the Doctor's
    scope stays the global one — the restart path scopes it too."""
    seed(tmp_path, ("prj_aaa", "alpha", None))
    home = captain_state.home_project()
    exited = status(home.id, "captain", "captain", "exited", exit_status=130)
    script[home.id] = [exited]
    started = status(home.id, "captain", "captain", "waiting", minute=5)
    started = started.model_copy(
        update={"agent": started.agent.model_copy(update={"id": "agt_captain_new"})}
    )
    calls: list[tuple[str, str]] = []

    def fake_restart(
        project: ProjectInfo, label: str, *, size: tuple[int, int] | None = None, **kw: object
    ) -> fleet_service.RestartReceipt:
        calls.append((project.id, label))
        script[home.id] = [started]  # what the next listing answers
        return fleet_service.RestartReceipt(
            replaced=exited.agent, started=started.agent, resumed=True, was_running=False,
            tmux_session="asq-home",
        )  # fmt: skip

    monkeypatch.setattr(fleet_service, "restart", fake_restart)

    async def body(pilot: Pilot[None]) -> tuple[str | None, str | None, str | None]:
        app = fleet_app(pilot)
        await pilot.click(row_for(app, exited.agent.id))
        await until(pilot, agent_opened(pilot, exited.agent.id), what="the exited captain selected")
        view = app.current_view()
        assert isinstance(view, AgentView)
        await pilot.click(view.query_one("#agent-restart", Button))
        await ui_suite.settle(app)
        await until(pilot, agent_opened(pilot, "agt_captain_new"), what="the new row selected")
        await quiet(pilot)
        current = app.current_view()
        return (current.id if current else None), app.sidebar.selected_key, app.doctor_scope

    current, selected, scope = drive(body)
    assert calls == [(home.id, "captain")], "Restart still works on the home's board"
    assert (current, selected) == ("agent-agt_captain_new", "agent:agt_captain_new")
    assert scope is None


def test_a_remembered_selection_never_reopens_the_home_as_a_project(
    tmp_path: Path, script: Script
) -> None:
    """#144's fallback is an agent's project; the captain's board is no project.

    A remembered captain whose row has left the frame fell back to
    ``project:<home>``, which ``FleetSnapshot.project`` resolved — the next
    launch opened a ProjectView of ``$AISQUARE_HOME``. It falls through to the
    welcome page and the memory is dropped, as for a project that is gone. The
    control: a captain still in the frame is reopened like any agent.
    """
    seed(tmp_path, ("prj_aaa", "alpha", None))
    home = captain_state.home_project()
    captain = status(home.id, "captain", "captain", "waiting")
    script[home.id] = [captain]

    reopens: list[str] = []
    """The agent the next launch reopens, when it reopens one — its handler is waited out."""

    async def relaunch(pilot: Pilot[None]) -> tuple[str | None, str | None, list[str]]:
        app = fleet_app(pilot)
        await pilot.pause()
        await pilot.pause()
        if reopens:
            await until(pilot, agent_opened(pilot, reopens.pop()), what="the captain reopened")
        await quiet(pilot)
        view = app.current_view()
        pages = [page.project.id for page in app.query(ProjectView)]
        return (view.id if view else None), app.sidebar.selected_key, pages

    _remember(f"agent:{home.id}/{captain.agent.id}")
    reopens.append(captain.agent.id)
    assert drive(relaunch) == (f"agent-{captain.agent.id}", f"agent:{captain.agent.id}", [])

    script[home.id] = []  # the captain's row is gone: its board is no fallback
    assert drive(relaunch) == ("welcome", None, [])
    assert _remembered() is None, "a memory with nothing to reopen is dropped, not retried"

    _remember(f"project:{home.id}")
    assert drive(relaunch) == ("welcome", None, [])
    assert _remembered() is None


# --- review of T2: a read that failed is not "no captain" ----------------------------------


def test_a_captain_read_that_failed_keeps_the_captain_row_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The captain's twin of ``test_a_fleet_read_that_failed_open_keeps_the_live_manager_pane``.

    A read that could not ask must not reach the screen as "there is none":
    ``_captain`` answered a failed read the way it answers an empty board, so
    one momentarily locked sqlite db (``list_agents`` opens its own session)
    removed the LIVE captain's row and told the owner to start one. A failed
    read keeps the last frame's row and says why where the empty line would
    be — also on the very first frame, when there is no row to keep. The
    control is the fleet ANSWERING none, which must still show the empty line.
    """
    seed(tmp_path, ("prj_aaa", "alpha", None))
    home = captain_state.home_project()
    live = [status(home.id, "captain", "captain", "working")]
    captain_id = live[0].agent.id
    failure: list[Exception] = [sqlite3.OperationalError("database is locked")]  # from mount

    def fake(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        if project.id != home.id:
            return []
        if failure:
            raise failure[0]
        return list(live)

    monkeypatch.setattr(fleet_service, "list_agents", fake)
    State = tuple[list[str], bool, str]

    async def body(pilot: Pilot[None]) -> tuple[State, State, State, bool, State]:
        app = fleet_app(pilot)
        section = app.query_one("#captain-section")
        empty = section.query_one("#captain-empty", Static)

        def state() -> State:
            rows = [row.status.agent.id for row in section.query(AgentRow)]
            # query, not query_one: on the unfixed tree there is no notice line at
            # all, and the claim should read as a diff, not as a NoMatches.
            notice = next(iter(section.query("#captain-notice").results(Static)), None)
            said = shown(notice) if notice is not None and notice.display else ""
            return rows, empty.display, said

        first = state()  # the mount's read failed: nothing to keep, and it is not "none"
        failure.clear()
        app.refresh_data()
        await pilot.pause()
        answered = state()
        failure.append(sqlite3.OperationalError("database is locked"))
        app.refresh_data()
        await pilot.pause()
        failed = state()
        snapshot = app.snapshot
        kept = (
            snapshot is not None
            and snapshot.home is not None
            and snapshot.home.id == home.id
            and snapshot.agent(home.id, captain_id) is not None
        )
        # The control: the fleet answers, and its answer is that there is none.
        failure.clear()
        live.clear()
        app.refresh_data()
        await pilot.pause()
        return first, answered, failed, kept, state()

    first, answered, failed, kept, none = drive(body)
    locked = "captain unavailable — OperationalError: database is locked"
    assert first == ([], False, locked), "a first read that failed is not 'no captain'"
    assert answered == ([captain_id], False, "")
    assert failed == ([captain_id], False, locked), "the live captain's row survived the failure"
    assert kept, "…and so did the snapshot's home and row: Stop and Restart still resolve"
    assert none == ([], True, ""), "the fleet ANSWERED none: the empty line, and no notice"
