"""The sidebar's header says how many agents are live: ``Fleet · 19 live``.

The owner wanted the count at a glance (tsk_01m67vf4mpsgdcvt6kq6znsrwc). It is every live agent
of every project, the captain excluded — ``live_total`` of ``aisquare fleet capacity``, read
from the same rows — so a folded group's members, a hidden directory's and a forgotten
project's agents count although no card shows them. It follows the next frame after a spawn or
a stop, and with none live the header is ``Fleet`` alone. Driven with the UI suite's harness:
the real store in the isolated home, every tmux call held to a private socket.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Static

from aisquare.cli.ui.app import _FleetRead
from aisquare.cli.ui.sidebar import fleet_title, fleet_title_tooltip
from aisquare.core.store import store_session
from aisquare.models import FleetAgent
from aisquare.services import fleet as fleet_service
from tests import test_ui_shell as ui_suite
from tests.test_ui_shell import (
    Script,
    composited,
    drive,
    fleet_app,
    refreshed,
    seed,
    settle,
    shown,
    status,
)

no_real_tmux = ui_suite.no_real_tmux
script = ui_suite.script

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
        counted = fleet_service.live_total(store)

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
