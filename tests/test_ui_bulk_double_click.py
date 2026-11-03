"""A double-click on Settings ▸ Fleet's *Stop all…* or *Pause all…* never confirms the dialog it
opens (REVIEW-RC092-MI round 3, MAJOR 1).

ConfirmBulk's box is centred on the screen, so with a small fleet its Confirm is drawn over
the hub button that opened it: at 120x40 with one agent, *Stop all…* and Confirm share 12
cells, *Pause all…* and Confirm 2 (*Restart all…* and *Resume all…* none, from 80x24 to
200x60). The first click of a double-click there opened the dialog, and the second, which
reached asq once Confirm was drawn and the plan read, stopped every agent, the manager
included. A click on Confirm as soon as it is drawn still acts when it is a click of its own
(#478, user 2026-10-30): there is no time to wait out.

The presses are queued at the app as the terminal's driver queues them, so Textual counts the
run of clicks itself (``Click.chain``), and stamped (``guard_paint.queue_press``), so a loaded
runner cannot send the second of a double-click late enough to make it a click of its own,
nor a lone click early enough to make it a double-click's second.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import TypeVar

import pytest
from textual.geometry import Offset
from textual.pilot import Pilot

from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.click_guard import GuardedButton
from aisquare.cli.ui.sidebar import SettingsRow
from aisquare.cli.ui.views.fleet_bulk import ConfirmBulk
from tests import guard_paint
from tests.test_ui_fleet_bulk import _ROWS, Script
from tests.test_ui_fleet_bulk import no_tmux as _no_tmux
from tests.test_ui_fleet_bulk import script as _script
from tests.test_ui_pause_resume import Paused
from tests.test_ui_shell import seed, settle, status
from tests.ui_workers import released, settle_until

#: Re-exported so pytest collects them here (the guard is autouse there, and here).
no_tmux = _no_tmux
script = _script

T = TypeVar("T")
SIZE = (120, 40)
"""Where Confirm covers 12 cells of *Stop all…* and 2 of *Pause all…* with one agent."""
DOUBLE = FleetApp.CLICK_CHAIN_TIME_THRESHOLD / 2
"""The second press of a double-click, this long after the first."""
APART = FleetApp.CLICK_CHAIN_TIME_THRESHOLD * 2
"""A second press this long after the first is a click of its own."""


@pytest.fixture
def one_agent(tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch) -> Iterator[Paused]:
    """alpha, with its manager at work and nobody else: the fleet whose dialog is smallest.
    The stop, restart and pause of one agent are stand-ins that record whom they were for."""
    seed(tmp_path, ("prj_a", "alpha", "amber-otter"))
    script["prj_a"] = [status("prj_a", "manager", "manager", "working")]
    _ROWS[:] = list(script["prj_a"])
    yield Paused(monkeypatch)
    _ROWS.clear()


def drive(fn: Callable[[Pilot[None], FleetApp], Awaitable[T]]) -> T:
    """``fn`` against a mounted ``FleetApp`` at :data:`SIZE`, on the Settings hub."""

    async def run() -> T:
        app = FleetApp(refresh_seconds=3600, doctor=lambda: [])
        async with app.run_test(size=SIZE, notifications=True) as pilot:
            await pilot.pause()
            await settle(app)
            await pilot.click(app.query_one(SettingsRow))
            await settle(app)
            return await fn(pilot, app)

    return asyncio.run(run())


def _confirm_at(app: FleetApp, where: Offset) -> GuardedButton | None:
    """ConfirmBulk's Confirm, once it is drawn at ``where``, its plan read and its guard
    down: a click there now presses it unless it is the second of a double-click."""
    dialog = app.screen
    if not isinstance(dialog, ConfirmBulk):
        return None
    confirm = dialog.query_one("#bulk-confirm", GuardedButton)
    if confirm.disabled or confirm.guarding or app.get_widget_at(*where)[0] is not confirm:
        return None
    return confirm


async def _under_confirm(pilot: Pilot[None], app: FleetApp, action: str) -> Offset:
    """A cell of the hub's button for ``action`` that its dialog's Confirm is drawn over, read
    off the dialog opened once with a key's press and closed with Esc."""
    opener = app.query_one(f"#fleet-{action}-all", GuardedButton)
    at = opener.region
    opener.press()
    await settle_until(app, lambda: isinstance(app.screen, ConfirmBulk))
    dialog = app.screen
    assert isinstance(dialog, ConfirmBulk)
    shared = at.intersection(dialog.query_one("#bulk-confirm", GuardedButton).region)
    await pilot.press("escape")
    await settle_until(app, lambda: not isinstance(app.screen, ConfirmBulk) and not opener.guarding)
    await released(pilot, opener)  # the key's press effect, during which Button drops a click
    assert shared, f"Confirm is drawn over no cell of {action} all… at {SIZE}: pick a new size"
    return shared.offset


async def _second_click_on_confirm(
    pilot: Pilot[None], app: FleetApp, action: str, gap: float
) -> bool:
    """Click the hub's button for ``action`` on a cell Confirm will cover, wait for Confirm
    to be drawn there and ready, and click that cell again, stamped ``gap`` seconds after the
    first. Whether Confirm was under the second click when it reached the app."""
    where = await _under_confirm(pilot, app, action)
    first = guard_paint.queue_press(app, where)
    await settle_until(app, lambda: _confirm_at(app, where))
    drawn = _confirm_at(app, where) is not None
    guard_paint.queue_press(app, where, at=first + gap)
    await settle(app)
    return drawn


@pytest.mark.parametrize("action", ["stop", "pause"])
def test_a_double_click_on_the_hub_button_leaves_its_dialog_up_and_acts_on_nobody(
    action: str, one_agent: Paused
) -> None:
    """The reviewer's probe at 8142584b: two clicks on *Stop all…* at 120x40 with one manager,
    and the second stopped it."""

    async def go(
        pilot: Pilot[None], app: FleetApp
    ) -> tuple[bool, list[tuple[str, str, str | None]], bool, str | None, bool]:
        drawn = await _second_click_on_confirm(pilot, app, action, DOUBLE)
        acted = list(one_agent.calls)
        up = isinstance(app.screen, ConfirmBulk)
        focused = app.focused.id if app.focused else None
        await pilot.press("enter")  # meant for Cancel, which has the focus
        await settle(app)
        return drawn, acted, up, focused, isinstance(app.screen, ConfirmBulk)

    drawn, acted, up, focused, still_up = drive(go)
    assert drawn, "the second click must reach the app with Confirm drawn under it"
    assert acted == [], "the double-click's second click confirmed the dialog"
    assert up
    assert focused == "bulk-cancel", "the ignored click took the focus off Cancel"
    assert not still_up, "the Enter went to Cancel"
    assert one_agent.calls == [], "the Enter after the double-click confirmed the dialog"


@pytest.mark.parametrize("action", ["stop", "pause"])
def test_a_click_of_its_own_on_confirm_as_soon_as_it_is_drawn_acts(
    action: str, one_agent: Paused
) -> None:
    """#478: once the frame showing Confirm is out, the next click acts at once. The same
    click on the same cell as the opener's, only too long after it for a double-click."""

    async def go(pilot: Pilot[None], app: FleetApp) -> tuple[bool, bool]:
        drawn = await _second_click_on_confirm(pilot, app, action, APART)
        await settle_until(app, lambda: one_agent.calls and not isinstance(app.screen, ConfirmBulk))
        return drawn, isinstance(app.screen, ConfirmBulk)

    drawn, up = drive(go)
    assert drawn
    assert one_agent.calls == [(action, "manager", "agt_a_manager")]
    assert not up
