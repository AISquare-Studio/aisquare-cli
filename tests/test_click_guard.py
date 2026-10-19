"""The click guard (``aisquare.cli.ui.click_guard``), on a host of its own.

User feedback (2026-10-19): a view that came up under a resting pointer took the
click aimed at what was there before, on its Stop or Restart. A
:class:`GuardedButton` ignores a mouse press within ``CLICK_GUARD`` of its place on
screen changing, says so once, and presses as a Button does after that. Keys
are not guarded. The agent view's own buttons are tested through the shell in
``tests/test_ui_shell.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import pytest
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.pilot import Pilot
from textual.screen import ModalScreen
from textual.widgets import Button, ContentSwitcher, Static

from aisquare.cli.ui.click_guard import GuardedButton
from tests import guard_clock
from tests.guard_clock import GuardClock
from tests.ui_workers import settle_page

T = TypeVar("T")
NOTICE = "ignored a click on Restart — the view had just changed under the pointer"


class Dialog(ModalScreen[None]):
    def compose(self) -> ComposeResult:
        yield Static("a dialog over the button")


class Host(App[None]):
    """A view to switch from, and one with a neighbour beside the guarded button."""

    def __init__(self) -> None:
        super().__init__()
        self.pressed: list[str] = []

    def compose(self) -> ComposeResult:
        with ContentSwitcher(initial="one", id="switcher"):
            yield Static("the view the user was on", id="one")
            with Horizontal(id="two"):
                yield Button("A wide neighbour", id="neighbour")
                yield GuardedButton("Restart", id="restart", compact=True)

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.pressed.append(event.button.id or "")

    def notices(self) -> list[str]:
        return [str(note.message) for note in self._notifications]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> GuardClock:
    return guard_clock.install(monkeypatch)


def drive(fn: Callable[[Pilot[None], Host], Awaitable[T]]) -> T:
    async def run() -> T:
        host = Host()
        async with host.run_test(size=(60, 8), notifications=True) as pilot:
            await settle_page(host)
            return await fn(pilot, host)

    return asyncio.run(run())


async def _shown(pilot: Pilot[None], host: Host) -> GuardedButton:
    host.query_one(ContentSwitcher).current = "two"
    await settle_page(host)
    return host.query_one("#restart", GuardedButton)


def test_a_click_as_the_view_switches_in_is_ignored_and_said_once_and_one_after_presses(
    clock: GuardClock,
) -> None:
    async def go(pilot: Pilot[None], host: Host) -> tuple[list[str], list[str], list[str]]:
        button = await _shown(pilot, host)
        await pilot.click(button)
        await pilot.click(button)  # still inside the window: no second notice
        await settle_page(host)
        ignored, told = list(host.pressed), host.notices()
        clock.step()
        await pilot.click(button)
        await settle_page(host)
        return ignored, told, host.pressed

    ignored, told, pressed = drive(go)
    assert ignored == []
    assert told == [NOTICE]
    assert pressed == ["restart"]


def test_a_button_its_bar_moves_is_guarded_again_and_one_left_in_place_is_not(
    clock: GuardClock,
) -> None:
    """A layout pass that leaves the button where it was is no change under the pointer;
    one that moves it is, as when the neighbour beside it goes."""

    async def go(pilot: Pilot[None], host: Host) -> tuple[list[str], list[str]]:
        button = await _shown(pilot, host)
        clock.step()
        host.screen.refresh(layout=True)  # a pass that moves nothing
        await settle_page(host)
        await pilot.click(button)
        await settle_page(host)
        in_place = list(host.pressed)
        clock.step()
        host.query_one("#neighbour").display = False
        await settle_page(host)
        await pilot.click(button)
        await settle_page(host)
        return in_place, host.pressed[len(in_place) :]

    in_place, moved = drive(go)
    assert in_place == ["restart"]
    assert moved == [], "the click on the button that had just moved pressed it"


def test_a_dialog_closing_off_the_button_guards_it_again(clock: GuardClock) -> None:
    async def go(pilot: Pilot[None], host: Host) -> tuple[list[str], list[str]]:
        button = await _shown(pilot, host)
        clock.step()
        await host.push_screen(Dialog())
        await settle_page(host)
        host.pop_screen()
        await settle_page(host)
        await pilot.click(button)
        await settle_page(host)
        return host.pressed, host.notices()

    pressed, told = drive(go)
    assert pressed == []
    assert told == [NOTICE]


def test_a_key_or_a_press_from_code_is_not_guarded(clock: GuardClock) -> None:
    """Enter on the focused button is a deliberate gesture, whenever it comes."""

    async def go(pilot: Pilot[None], host: Host) -> tuple[bool, list[str], list[str]]:
        button = await _shown(pilot, host)
        guarding = button.guarding
        button.focus()
        await pilot.press("enter")
        await settle_page(host)
        button.press()
        await settle_page(host)
        return guarding, host.pressed, host.notices()

    guarding, pressed, told = drive(go)
    assert guarding is True
    assert pressed == ["restart", "restart"]
    assert told == []
