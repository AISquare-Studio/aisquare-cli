"""The sidebar's header says how many agents are live: ``Fleet · 19 live``.

The owner wanted the count at a glance (tsk_01m67vf4mpsgdcvt6kq6znsrwc). It is every live agent
of every project, the captain excluded — ``live_total`` of ``aisquare fleet capacity``, read
from the same rows — so a folded group's members, a hidden directory's and a forgotten
project's agents count although no card shows them. It follows the next frame after a spawn or
a stop, and with none live the header is ``Fleet`` alone. Driven with the UI suite's harness:
the real store in the isolated home, every tmux call held to a private socket; and, for an
agent whose pane dies, the tick budget's fake tmux server, read through the real listing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Static

from aisquare.cli.ui import app as app_mod
from aisquare.cli.ui.app import _FleetRead
from aisquare.cli.ui.sidebar import fleet_title, fleet_title_tooltip
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from tests import test_ui_shell as ui_suite
from tests import test_ui_tick_budget as tick_budget
from tests.test_ui_shell import (
    Script,
    composited,
    drive,
    fleet_app,
    refreshed,
    row_for,
    seed,
    settle,
    shown,
    status,
)
from tests.test_ui_tick_budget import AGENTS_EACH, TMUX_ONLY, WITH_AGENTS, Panes

no_real_tmux = ui_suite.no_real_tmux
script = ui_suite.script
fleet = tick_budget.fleet
"""50 projects, the first 10 with three live coders each, on a fake tmux server whose panes a
test can kill (``Panes``): what ``list_agents_many`` really reads, dead rows ended and all."""

THREE = (
    ("prj_a", "alpha", "amber-otter"),
    ("prj_b", "beta", "ruby-fox"),
    ("prj_c", "gamma", "jade-lynx"),
)


def _live(project_id: str, label: str, role: str = "coder", minute: int = 0) -> FleetAgent:
    """A live fleet row in the store, as a spawn records it."""
    agent = status(project_id, label, role, "working", minute=minute).agent
    with store_session() as store:
        return store.upsert_fleet_agent(agent)


def _stop(*agents: FleetAgent) -> None:
    with store_session() as store:
        for agent in agents:
            store.end_fleet_agent(agent.id, ended_by="stopped")


def _capacity_live_total() -> int:
    """What ``aisquare fleet capacity`` says (its ``live_total``), without the memory read."""
    return sum(fleet_service._live_per_project().values())


# --- the header's text ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("live", "text", "tip"),
    [
        (0, "Fleet", None),
        (1, "Fleet · 1 live", "1 agent live across every project"),
        (19, "Fleet · 19 live", "19 agents live across every project"),
    ],
)
def test_the_header_says_fleet_then_how_many_are_live_and_nothing_with_none(
    live: int, text: str, tip: str | None
) -> None:
    assert fleet_title(live).plain == text
    said = fleet_title_tooltip(live)
    assert (said is None) if tip is None else (said is not None and said.startswith(tip))


def test_the_count_is_fleet_capacitys_every_projects_and_never_the_captain(
    tmp_path: Path,
) -> None:
    """The figure the frame reads: forgotten projects included, ended rows and the captain not."""
    seed(tmp_path, *THREE)
    _live("prj_a", "coder-1")
    _live("prj_a", "manager", role="manager", minute=1)
    _live("prj_b", "coder-1")
    gone = _live("prj_c", "coder-1")
    _live("prj_c", "coder-2", minute=1)
    _live("prj_a", "captain", role="captain", minute=2)
    _stop(gone)
    with store_session() as store:
        store.forget_project("prj_c")  # its live coder-2 still counts, as capacity counts it
        counted = len(fleet_service.live_ids(store))

    assert counted == 4 == _capacity_live_total()


# --- the header in the app --------------------------------------------------------------------


def test_a_frame_paints_its_count_and_one_that_failed_keeps_the_last(
    tmp_path: Path, script: Script
) -> None:
    """From fake frames: 0, 1 and many, then a frame the store could not answer."""
    seed(tmp_path, *THREE)

    async def body(pilot: Pilot[None]) -> list[tuple[str, str | None]]:
        app = fleet_app(pilot)
        title = app.query_one("#fleet-title", Static)
        seen: list[tuple[str, str | None]] = []
        for read in (
            _FleetRead(True, live_total=0),
            _FleetRead(True, live_total=1),
            _FleetRead(True, live_total=19),
            _FleetRead(True, error="OperationalError: database is locked"),
            _FleetRead(True, live_total=0),
        ):
            app._paint_fleet(read)
            await settle(app)
            seen.append((shown(title), str(title.tooltip) if title.tooltip else None))
        return seen

    seen = drive(body)
    assert [text for text, _ in seen] == [
        "Fleet",
        "Fleet · 1 live",
        "Fleet · 19 live",
        "Fleet · 19 live",  # the store unreadable: the frame on screen, and its count, are kept
        "Fleet",
    ]
    assert seen[0][1] is None and seen[-1][1] is None
    assert seen[2][1] is not None and "aisquare fleet capacity" in seen[2][1]


def test_the_header_follows_a_spawn_and_a_stop_on_the_next_frame(
    tmp_path: Path, script: Script
) -> None:
    """Live rows in the store, read by the frame as the two-second tick reads it."""
    seed(tmp_path, *THREE)

    async def body(pilot: Pilot[None]) -> list[tuple[str, int]]:
        app = fleet_app(pilot)
        title = app.query_one("#fleet-title", Static)
        seen: list[tuple[str, int]] = []

        async def frame() -> None:
            await refreshed(app)
            seen.append((shown(title), _capacity_live_total()))

        await frame()
        first = _live("prj_a", "coder-1")
        await frame()
        rest = [
            _live("prj_a", "coder-2", minute=1),
            _live("prj_b", "manager", role="manager"),
            _live("prj_c", "coder-1"),
        ]
        _live("prj_a", "captain", role="captain", minute=2)  # the captain is never counted
        await frame()
        _stop(first, *rest)
        await frame()
        return seen

    assert drive(body) == [
        ("Fleet", 0),
        ("Fleet · 1 live", 1),
        ("Fleet · 4 live", 4),
        ("Fleet", 0),
    ]


@pytest.mark.parametrize("width", [30, 24])
def test_the_count_fits_the_header_beside_the_plus_at_the_sidebars_widths(
    tmp_path: Path, script: Script, width: int
) -> None:
    seed(tmp_path, *THREE)

    async def body(pilot: Pilot[None]) -> tuple[str, bool, bool]:
        app = fleet_app(pilot)
        app.query_one("#sidebar").styles.width = width
        app._paint_fleet(_FleetRead(True, live_total=128))
        await settle(app)
        title = app.query_one("#fleet-title", Static)
        plus = app.query_one("#add-project")
        return (
            composited(title),
            plus.display and plus.region.width > 0,
            title.region.right <= plus.region.x,
        )

    title, plus_shown, side_by_side = drive(body)
    assert title.rstrip() == "Fleet · 128 live"
    assert plus_shown and side_by_side


# --- an agent whose pane died (review of #10) -------------------------------------------------


def test_the_count_leaves_out_the_rows_the_listing_ended_and_only_those() -> None:
    """From a fake listing: of the rows counted before it, the ones it ended are not live; an
    ended row it shows that was not counted (one ended earlier) changes nothing."""
    alive = status("prj_a", "coder-1", "coder", "working").agent
    died = status("prj_a", "coder-2", "coder", "exited", exit_status=1).agent
    earlier = status("prj_b", "old", "coder", "exited", minute=5, exit_status=0).agent
    folded = status("prj_c", "coder-1", "coder", "working").agent  # counted, not listed
    many = fleet_service.ManyAgents(
        {
            "prj_a": [
                FleetAgentStatus(agent=alive, state="working"),
                FleetAgentStatus(agent=died, state="exited"),
            ],
            "prj_b": [FleetAgentStatus(agent=earlier, state="exited")],
        },
        {},
    )

    assert fleet_service.live_after({alive.id, died.id, folded.id}, many) == 2
    assert fleet_service.live_after(set(), many) == 0


def _kill(panes: Panes, pane_id: str) -> None:
    """The pane's process exits: tmux keeps it, dead (``remain-on-exit``)."""
    pane = next(p for p in panes.panes if p["pane_id"] == pane_id)
    pane["pane_dead"], pane["pane_dead_status"] = "1", "0"


@TMUX_ONLY
def test_a_pane_that_died_between_two_reads_is_counted_out_by_the_read_that_ends_it(
    fleet: tuple[list[ProjectInfo], Panes],
) -> None:
    """The tester's repro: the count was read before the listing ended the dead row, so the read
    that showed coder-0 exited still said 30, and capacity said 29 right after it."""
    projects, panes = fleet
    total = WITH_AGENTS * AGENTS_EACH
    before = app_mod._read_fleet(False)
    _kill(panes, "%0")  # coder-0 of the first project
    after = app_mod._read_fleet(False)
    states = {s.agent.label: s.state for s in after.agents[projects[0].id]}

    assert before.live_total == total
    assert states["coder-0"] == "exited"
    assert after.live_total == total - 1 == _capacity_live_total()
    assert app_mod._read_fleet(False).live_total == total - 1


@TMUX_ONLY
def test_the_header_drops_on_the_frame_that_shows_the_agent_exited(
    fleet: tuple[list[ProjectInfo], Panes],
) -> None:
    """In the app, through its own read: one frame, card and header agree."""
    _projects, panes = fleet
    total = WITH_AGENTS * AGENTS_EACH

    async def body(pilot: Pilot[None]) -> list[tuple[str, str]]:
        app = fleet_app(pilot)
        title = app.query_one("#fleet-title", Static)
        seen: list[tuple[str, str]] = []
        await refreshed(app)
        seen.append((shown(title), row_for(app, "agt_00_0").status.state))
        _kill(panes, "%0")
        await refreshed(app)
        seen.append((shown(title), row_for(app, "agt_00_0").status.state))
        return seen

    (first, first_state), (second, second_state) = drive(body)
    assert (first, second) == (f"Fleet · {total} live", f"Fleet · {total - 1} live")
    assert first_state != "exited" and second_state == "exited"
