"""The sidebar's active-only filter: the ○ after the header's ``+``, or ``l`` from the sidebar.

On (●), the sidebar lists only the projects with a live agent — an agent in ``ALIVE_STATES``,
the card's "alive" chip; an exited or lost row is no live agent. A group header none of whose
members passes is hidden with them, and so is the Pinned label with no pinned entry shown; the
Captain section is not a project and is never filtered. The choice is remembered across a
relaunch under ``fleet.active_only``.

Driven through the whole app: what is asserted is what the sidebar SHOWS (``display``), the
cursor's walk and the store's ``ui_state``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Static

from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.sidebar import (
    ACTIVE_ONLY_EMPTY,
    ALIVE_STATES,
    NO_PROJECTS,
    Activatable,
    ActiveOnlyButton,
    GroupHeader,
    ProjectCard,
    ProjectTitle,
    SectionLabel,
    has_live_agent,
)
from aisquare.core.store import store_session
from aisquare.models import FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import project_groups as groups_service
from aisquare.services.captain import state as captain_state
from tests import toast_log
from tests.fleet_fakes import list_agents_as
from tests.test_captain_view import _captain
from tests.test_ui_shell import (
    Script,
    card_for,
    composited,
    drive,
    fleet_app,
    refreshed,
    seed,
    settle,
    shown,
    status,
)
from tests.test_ui_shell import no_real_tmux as _no_real_tmux
from tests.test_ui_shell import script as _script

#: Re-exported so pytest collects them here (the guard is autouse there, and here).
no_real_tmux = _no_real_tmux
script = _script

KEY = "fleet.active_only"

FOUR = (
    ("prj_a", "alpha", None),
    ("prj_b", "bravo", None),
    ("prj_c", "charlie", None),
    ("prj_d", "delta", None),
)
"""Four projects whose names sort as their ids do: the loose ones are listed by name."""


def visible(app: FleetApp) -> list[str]:
    """What the projects list shows, top to bottom: cards by id, group headers by name, the
    Pinned label, and the empty line by what it says."""
    out: list[str] = []
    for child in app.sidebar.query_one("#projects").children:
        if not child.display:
            continue
        if isinstance(child, ProjectCard):
            out.append(child.project.id)
        elif isinstance(child, GroupHeader):
            out.append(f"group:{child.group.name}")
        elif isinstance(child, SectionLabel):
            out.append("pinned")
        elif isinstance(child, Static) and child.id == "projects-empty":
            out.append(shown(child))
    return out


def toggle(app: FleetApp) -> ActiveOnlyButton:
    return app.query_one("#active-only", ActiveOnlyButton)


def stored() -> str | None:
    with store_session() as store:
        return store.ui_state(KEY)


def exited(project_id: str, label: str) -> FleetAgentStatus:
    return status(project_id, label, "coder", "exited", exit_status=0)


def cursor(app: FleetApp) -> str:
    """The selection key of the one row under the keyboard cursor."""
    (row,) = app.query(".cursor").results(Activatable)
    return row.selection_key


# --- what counts as live --------------------------------------------------------------------


@pytest.mark.parametrize("state", sorted(ALIVE_STATES))
def test_every_alive_state_is_a_live_agent(state: str) -> None:
    assert has_live_agent("prj_a", {"prj_a": [status("prj_a", "x", "coder", state)]})


@pytest.mark.parametrize("state", ["exited", "lost"])
def test_an_exited_or_lost_row_is_no_live_agent(state: str) -> None:
    assert not has_live_agent("prj_a", {"prj_a": [status("prj_a", "x", "coder", state)]})
    assert not has_live_agent("prj_a", {"prj_a": []})


def test_a_project_folded_away_counts_its_live_rows_from_the_store() -> None:
    """A collapsed group's member is not read on the tick (#83): no key in ``agents``."""
    assert has_live_agent("prj_a", {}, {"prj_a": 1})
    assert not has_live_agent("prj_a", {}, {"prj_b": 1})
    assert not has_live_agent("prj_a", {"prj_a": []}, {"prj_a": 1}), "read beats counted"


# --- the glyph --------------------------------------------------------------------------


@pytest.mark.parametrize("width", [30, 24])
def test_the_toggle_sits_after_the_plus_and_reads_off_and_on(
    tmp_path: Path, script: Script, width: int
) -> None:
    """Dim ○ off, the accent's bold ● on, a tooltip naming what it does and its state; the
    header still fits at the sidebar's default and minimum widths."""
    seed(tmp_path, *FOUR)

    async def go(pilot: Pilot[None]) -> list[tuple[str, bool, str, list[str]]]:
        app = fleet_app(pilot)
        app.sidebar.styles.width = width
        await settle(app)
        header = app.query_one("#fleet-header")
        seen = []
        for _ in range(2):
            button = toggle(app)
            assert button.region.right <= header.content_region.right, "inside the header"
            parts = [composited(child) for child in header.children if isinstance(child, Static)]
            seen.append((composited(button), button.has_class("-on"), str(button.tooltip), parts))
            await pilot.click("#active-only")
            await settle(app)
        return seen

    (off_glyph, off_on, off_tip, off_parts), (on_glyph, on_on, on_tip, on_parts) = drive(go)
    assert [part.strip() for part in off_parts] == ["Fleet", "+", "○"], "after the +, whole"
    assert (off_glyph, off_on) == (" ○ ", False)
    assert (on_glyph, on_on) == (" ● ", True)
    assert [part.strip() for part in on_parts] == ["Fleet", "+", "●"]
    assert "Active only: off" in off_tip and "live agent" in off_tip
    assert "Active only: on" in on_tip and "live agent" in on_tip


# --- filtering ----------------------------------------------------------------------------


def test_on_lists_only_projects_with_a_live_agent_and_off_lists_them_all(
    tmp_path: Path, script: Script
) -> None:
    """A working and a waiting agent keep their cards; an exited agent's project and one
    with no agent are hidden; a click off shows every card again, as before."""
    seed(tmp_path, *FOUR)
    script["prj_a"] = [status("prj_a", "coder", "coder", "working")]
    script["prj_b"] = [exited("prj_b", "coder")]
    script["prj_d"] = [exited("prj_d", "old"), status("prj_d", "new", "coder", "waiting")]

    async def go(pilot: Pilot[None]) -> tuple[list[str], list[str], list[str]]:
        app = fleet_app(pilot)
        before = visible(app)
        await pilot.click("#active-only")
        await settle(app)
        on = visible(app)
        await pilot.click("#active-only")
        await settle(app)
        return before, on, visible(app)

    before, on, off = drive(go)
    assert before == ["prj_a", "prj_b", "prj_c", "prj_d"]
    assert on == ["prj_a", "prj_d"]
    assert off == before


def test_a_project_comes_and_goes_on_the_tick_as_its_agents_start_and_exit(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, *FOUR)
    script["prj_a"] = [status("prj_a", "coder", "coder", "working")]

    async def go(pilot: Pilot[None]) -> tuple[list[str], list[str], list[str]]:
        app = fleet_app(pilot)
        await pilot.click("#active-only")
        await settle(app)
        first = visible(app)
        script["prj_c"] = [status("prj_c", "coder", "coder", "attention")]
        await refreshed(app)
        started = visible(app)
        script["prj_a"] = [exited("prj_a", "coder")]
        await refreshed(app)
        return first, started, visible(app)

    first, started, ended = drive(go)
    assert first == ["prj_a"]
    assert started == ["prj_a", "prj_c"], "an agent started: its project appears"
    assert ended == ["prj_c"], "its last agent exited: the project goes"


def test_a_project_whose_agents_could_not_be_read_is_not_hidden_as_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its card says why its rows are missing; not read is not "no live agent"."""
    seed(tmp_path, *FOUR)

    def fake(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        if project.id == "prj_b":
            raise fleet_service.FleetError("tmux did not answer")
        return [status("prj_a", "coder", "coder", "working")] if project.id == "prj_a" else []

    list_agents_as(monkeypatch, fake)

    async def go(pilot: Pilot[None]) -> list[str]:
        app = fleet_app(pilot)
        await pilot.click("#active-only")
        await settle(app)
        return visible(app)

    assert drive(go) == ["prj_a", "prj_b"]


def test_on_with_nothing_live_says_so_in_place_of_the_onboard_line(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, *FOUR)
    script["prj_b"] = [exited("prj_b", "coder")]

    async def go(pilot: Pilot[None]) -> tuple[list[str], list[str]]:
        app = fleet_app(pilot)
        await pilot.click("#active-only")
        await settle(app)
        on = visible(app)
        await pilot.click("#active-only")
        await settle(app)
        return on, visible(app)

    on, off = drive(go)
    assert on == [ACTIVE_ONLY_EMPTY]
    assert "show all" in ACTIVE_ONLY_EMPTY and "l" in ACTIVE_ONLY_EMPTY.split()
    assert off == ["prj_a", "prj_b", "prj_c", "prj_d"], "off, no empty line: there are cards"


def test_off_with_no_projects_the_onboard_line_is_unchanged(script: Script) -> None:
    async def go(pilot: Pilot[None]) -> tuple[list[str], list[str]]:
        app = fleet_app(pilot)
        off = visible(app)
        await pilot.click("#active-only")
        await settle(app)
        on = visible(app)
        await pilot.click("#active-only")
        await settle(app)
        assert visible(app) == off
        return off, on

    off, on = drive(go)
    assert off == [NO_PROJECTS] == ["No projects yet — press + to onboard one."]
    assert on == [ACTIVE_ONLY_EMPTY]


def test_a_group_header_and_the_pinned_label_hide_with_their_members(
    tmp_path: Path, script: Script
) -> None:
    """A header shows while one of its members passes; the Pinned label while one pinned
    entry does. Off, both show as they always have."""
    seed(tmp_path, *FOUR)
    script["prj_a"] = [status("prj_a", "coder", "coder", "working")]
    with store_session() as store:
        groups_service.create_group(store, "tools", ["prj_b", "prj_c"])
        groups_service.pin(store, "prj_d")

    async def go(pilot: Pilot[None]) -> list[list[str]]:
        app = fleet_app(pilot)
        frames = [visible(app)]
        await pilot.click("#active-only")
        await settle(app)
        frames.append(visible(app))
        script["prj_c"] = [status("prj_c", "coder", "coder", "working")]
        await refreshed(app)
        frames.append(visible(app))
        script["prj_d"] = [status("prj_d", "coder", "coder", "limited")]
        await refreshed(app)
        frames.append(visible(app))
        await pilot.click("#active-only")
        await settle(app)
        frames.append(visible(app))
        return frames

    every, idle, grouped, pinned, off = drive(go)
    assert every == ["pinned", "prj_d", "group:tools", "prj_b", "prj_c", "prj_a"]
    assert idle == ["prj_a"], "no member passes: no header, no Pinned label"
    assert grouped == ["group:tools", "prj_c", "prj_a"], "the header comes back with prj_c"
    assert pinned == ["pinned", "prj_d", "group:tools", "prj_c", "prj_a"]
    assert off == every


def test_a_folded_group_with_a_live_member_keeps_its_header(tmp_path: Path, script: Script) -> None:
    """A collapsed group's members are not read on the tick (#83): their live rows are
    counted from the store, as the header's roll-up counts them."""
    seed(tmp_path, *FOUR)
    with store_session() as store:
        tools, _ = groups_service.create_group(store, "tools", ["prj_b", "prj_c"])
        groups_service.create_group(store, "docs", ["prj_d"])
        groups_service.set_collapsed(store, tools.id, True)

    async def go(pilot: Pilot[None]) -> list[str]:
        app = fleet_app(pilot)
        await pilot.click("#active-only")
        await settle(app)
        # The fleet fake counts no live rows (``list_agents_as``): this frame is the app's
        # with the store's count for the folded member, as ``read_listings`` gives it.
        with store_session() as store:
            projects, groups = store.list_projects(), store.project_groups()
        agents: dict[str, list[FleetAgentStatus]] = {"prj_a": [], "prj_d": []}
        app.sidebar.show_projects(projects, agents, groups=groups, live={"prj_c": 1})
        await settle(app)
        return visible(app)

    assert drive(go) == ["group:tools"], "folded, its cards hidden; docs has nobody live"


@pytest.mark.needs_captain
def test_the_captain_section_is_not_filtered(tmp_path: Path, script: Script) -> None:
    """The captain is no project: its row stays, and so does *Spawn agent*, with no
    project card shown."""
    seed(tmp_path, *FOUR)
    captain = _captain("working")
    script[captain_state.home_project().id] = [captain]

    async def go(pilot: Pilot[None]) -> tuple[list[str], list[str]]:
        app = fleet_app(pilot)
        await refreshed(app)
        await pilot.click("#active-only")
        await settle(app)
        actions = [
            row.selection_key
            for row in app.query_one("#actions").query(Activatable)
            if app.sidebar._on_screen(row)
        ]
        return visible(app), actions

    projects, actions = drive(go)
    assert projects == [ACTIVE_ONLY_EMPTY]
    assert actions == [f"agent:{captain.agent.id}", "spawn-agent"]


# --- the key, the help, the memory -----------------------------------------------------------


def test_l_from_the_sidebar_toggles_it_and_the_help_lists_the_key(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, *FOUR)
    script["prj_c"] = [status("prj_c", "coder", "coder", "working")]

    async def go(pilot: Pilot[None]) -> tuple[list[str], list[str], list[str], str]:
        app = fleet_app(pilot)
        app.sidebar.focus()
        await pilot.press("l")
        await settle(app)
        on = visible(app)
        toasts = toast_log.record(app)
        await pilot.press("l")
        await settle(app)
        off = visible(app)
        await pilot.press("question_mark")
        await settle(app)
        return on, off, toasts, shown(app.screen.query_one(Static))

    on, off, toasts, keys = drive(go)
    assert on == ["prj_c"]
    assert off == ["prj_a", "prj_b", "prj_c", "prj_d"]
    assert any("every project" in toast for toast in toasts)
    assert "only the projects with a live agent" in keys, "the key is on the ? screen"
    assert "  l ○" in keys


def test_the_toggle_survives_a_relaunch(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, *FOUR)
    script["prj_b"] = [status("prj_b", "coder", "coder", "working")]

    async def click(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        await pilot.click("#active-only")
        await settle(app)  # the handler has stored the toggle before the app quits

    async def relaunch(pilot: Pilot[None]) -> tuple[bool, bool, list[str]]:
        app = fleet_app(pilot)
        await pilot.pause()
        return app.sidebar.active_only, toggle(app).has_class("-on"), visible(app)

    drive(click)
    assert stored() == "1"
    assert drive(relaunch) == (True, True, ["prj_b"]), "on again after a relaunch"
    drive(click)
    assert stored() is None, "off is remembered too"
    assert drive(relaunch) == (False, False, ["prj_a", "prj_b", "prj_c", "prj_d"])


# --- the cursor, the marks, the open view, the moves ------------------------------------------


def test_the_cursor_walk_skips_the_hidden_cards_and_their_rows(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, *FOUR)
    script["prj_a"] = [status("prj_a", "coder", "coder", "working")]
    script["prj_b"] = [exited("prj_b", "coder")]
    script["prj_c"] = [status("prj_c", "coder", "coder", "working")]

    async def go(pilot: Pilot[None]) -> tuple[list[str], list[str]]:
        app = fleet_app(pilot)
        await pilot.click("#active-only")
        await settle(app)
        app.sidebar.focus()
        app.sidebar.select("project:prj_a")
        walked: list[str] = []
        for _ in range(6):
            await pilot.press("down")
            await settle(app)
            walked.append(cursor(app))
        rows = [row.selection_key for row in app.sidebar._rows()]
        return walked, rows

    walked, rows = drive(go)
    assert "project:prj_b" not in rows and "agent:agt_b_coder" not in rows
    assert "project:prj_d" not in rows
    assert walked[:3] == ["agent:agt_a_coder", "project:prj_c", "agent:agt_c_coder"]
    assert not any("prj_b" in key or "_b_" in key or "prj_d" in key for key in walked)


def test_a_cursor_on_a_card_that_is_hidden_moves_on_from_beside_it(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, *FOUR)
    script["prj_a"] = [status("prj_a", "coder", "coder", "working")]
    script["prj_c"] = [status("prj_c", "coder", "coder", "working")]

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        app.sidebar.focus()
        assert app.sidebar.put_cursor("project:prj_b")
        await pilot.press("l")
        await settle(app)
        await pilot.press("down")
        await settle(app)
        return cursor(app)

    assert drive(go) == "project:prj_c", "the nearest card on screen below, not the top"


def test_a_marked_project_that_is_hidden_is_unmarked(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, *FOUR)
    script["prj_a"] = [status("prj_a", "coder", "coder", "working")]

    async def go(pilot: Pilot[None]) -> tuple[list[str], list[str], list[str]]:
        app = fleet_app(pilot)
        app.sidebar.toggle_mark("prj_a")
        app.sidebar.toggle_mark("prj_b")
        marked = app.sidebar.marked_ids()
        await pilot.click("#active-only")
        await settle(app)
        on = app.sidebar.marked_ids()
        await pilot.click("#active-only")
        await settle(app)
        return marked, on, app.sidebar.marked_ids()

    marked, on, off = drive(go)
    assert marked == ["prj_a", "prj_b"]
    assert on == ["prj_a"], "the hidden card's mark is dropped; the shown one keeps its"
    assert off == ["prj_a"], "and showing it again does not bring the mark back"


def test_hiding_the_open_projects_card_leaves_its_view_open(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, *FOUR)
    script["prj_a"] = [status("prj_a", "coder", "coder", "working")]

    async def go(pilot: Pilot[None]) -> tuple[str | None, str | None, str | None, bool]:
        app = fleet_app(pilot)
        await pilot.click(card_for(app, "prj_b").query_one(ProjectTitle))
        await settle(app)
        opened = app.content.current
        await pilot.click("#active-only")
        await settle(app)
        await refreshed(app)
        shown_card = card_for(app, "prj_b").display
        return opened, app.content.current, app.sidebar.selected_key, shown_card

    opened, after, selected, shown_card = drive(go)
    assert opened == "project-prj_b"
    assert (after, selected, shown_card) == ("project-prj_b", "project:prj_b", False)


def test_shift_down_steps_over_the_projects_the_filter_hides(
    tmp_path: Path, script: Script
) -> None:
    """One step is one row on screen: swapped with a hidden card, shift+↓ moved nothing
    anyone could see (as for the captured rows, review of #171)."""
    seed(tmp_path, *FOUR)
    for pid in ("prj_a", "prj_c", "prj_d"):
        script[pid] = [status(pid, "coder", "coder", "working")]

    async def go(pilot: Pilot[None]) -> tuple[list[str], list[str]]:
        app = fleet_app(pilot)
        await pilot.click("#active-only")
        await settle(app)
        before = visible(app)
        app.sidebar.focus()
        app.sidebar.select("project:prj_a")
        await pilot.press("shift+down")
        await settle(app)
        return before, visible(app)

    before, after = drive(go)
    assert before == ["prj_a", "prj_c", "prj_d"]
    assert after == ["prj_c", "prj_a", "prj_d"], "past prj_c, the next card on screen"


def test_a_group_steps_over_the_headers_the_filter_hides(tmp_path: Path) -> None:
    with store_session() as store:
        for pid in ("prj_a", "prj_b", "prj_c"):
            store.onboard_project(ProjectInfo(id=pid, root=tmp_path / pid))
        for name, pid in (("one", "prj_a"), ("two", "prj_b"), ("three", "prj_c")):
            groups_service.create_group(store, name, [pid])
        ids = {g.name: g.id for g in store.project_groups()}
        groups_service.step_group(store, ids["one"], 1, hidden={ids["two"]})
        order = [g.name for g in sorted(store.project_groups(), key=lambda g: g.position)]
        unmoved = groups_service.step_group(store, ids["two"], 1, hidden={ids["two"]})
    assert order == ["two", "three", "one"], "past three, the next header on screen"
    assert unmoved.description == "nothing to move", "a hidden header is no row to move"


def test_the_header_glyph_is_no_stop_on_the_cursor_walk(tmp_path: Path, script: Script) -> None:
    """``l`` is the keyboard's way to it: ↑/↓ still walk from the + to *Spawn agent*."""
    seed(tmp_path, *FOUR)

    async def go(pilot: Pilot[None]) -> list[str]:
        app = fleet_app(pilot)
        return [row.selection_key for row in app.sidebar._rows()][:3]

    keys = drive(go)
    assert keys[0] == "add" and "active-only" not in keys
