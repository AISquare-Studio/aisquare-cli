"""The click guard (``aisquare.cli.ui.click_guard``), on a host of its own.

User feedback (2026-10-19): a view that came up under a resting pointer took the
click aimed at what was there before, on its Stop or Restart. A
:class:`GuardedButton` ignores a mouse press that reaches the app before its changed
place on screen is painted, says nothing, and presses as a Button does once the paint
is out: no time to wait out (user, 2026-10-30). Keys are not guarded. The agent view's
own buttons are tested through the shell in ``tests/test_ui_shell.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import pytest
from textual import events, on
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.pilot import Pilot
from textual.screen import ModalScreen
from textual.widgets import Button, ContentSwitcher, Static

from aisquare.cli.ui.click_guard import GuardedButton
from tests import guard_paint, toast_log
from tests.guard_paint import PaintHold
from tests.ui_workers import released, settle_page, watch_until

T = TypeVar("T")


class Dialog(ModalScreen[None]):
    def compose(self) -> ComposeResult:
        yield Static("a dialog over the button")


class Host(App[None]):
    """A view to switch from, and one with a neighbour beside the guarded button."""

    def __init__(self) -> None:
        super().__init__()
        self.pressed: list[str] = []
        self.told = toast_log.record(self)

    def compose(self) -> ComposeResult:
        yield Button("Typing here", id="typing")
        with ContentSwitcher(initial="one", id="switcher"):
            yield Static("the view the user was on", id="one")
            with Horizontal(id="two"):
                yield Button("A wide neighbour", id="neighbour")
                yield GuardedButton("Restart", id="restart", compact=True)

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.pressed.append(event.button.id or "")

    def notices(self) -> list[str]:
        return list(self.told)

    def focused_id(self) -> str | None:
        return self.focused.id if self.focused else None


@pytest.fixture
def hold(monkeypatch: pytest.MonkeyPatch) -> PaintHold:
    return guard_paint.install(monkeypatch)


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


def test_a_click_once_the_button_is_painted_presses_it_with_no_time_to_wait() -> None:
    """User, 2026-10-30: the second the guard held a button after it was drawn was in the way
    of someone who knows where it is. The clock is not stepped: there is none."""

    async def go(pilot: Pilot[None], host: Host) -> tuple[list[str], list[str], str | None]:
        button = await _shown(pilot, host)  # switched in, laid out and painted
        await pilot.click(button)
        await settle_page(host)
        await host.push_screen(Dialog())
        await settle_page(host)
        host.pop_screen()  # uncovered: guarded until the screen beneath is painted again
        await settle_page(host)
        await released(pilot, button)  # Button's own 0.2 s after the first press, not the guard
        await pilot.click(button)
        await settle_page(host)
        return host.pressed, host.notices(), host.focused_id()

    pressed, told, focused = drive(go)
    assert pressed == ["restart", "restart"]
    assert told == []
    assert focused == "restart"


def test_a_press_queued_before_the_paint_is_ignored_and_takes_no_focus() -> None:
    """The 2026-10-19 case as it happens. The app is busy putting the new view up (the shell
    awaits its mount), and the press the user made on the view before it waits in the app's
    queue behind that. Meanwhile the screen lays the view out and paints it, so when the app
    reads the press, the button is what is under it. It must press nothing, take no focus
    and raise no toast; the guard comes down behind it, and the next press goes through."""

    async def go(
        pilot: Pilot[None], host: Host
    ) -> tuple[bool, list[str], str | None, list[str], list[str]]:
        button = await _shown(pilot, host)
        where = button.region.offset  # where it comes up again
        host.query_one(ContentSwitcher).current = "one"
        host.query_one("#typing", Button).focus()
        await settle_page(host)
        host.query_one(ContentSwitcher).current = "two"
        free = guard_paint.busy(host)
        guard_paint.queue_press(host, where)
        # The app is held, so its queue cannot move; the screen lays out and paints apart.
        await watch_until(lambda: host.get_widget_at(*where)[0] is button)
        laid_out = host.get_widget_at(*where)[0] is button
        free.set()
        await settle_page(host)
        ignored, focused = list(host.pressed), host.focused_id()
        await pilot.click(button)
        await settle_page(host)
        return laid_out, ignored, focused, host.pressed, host.notices()

    laid_out, ignored, focused, pressed, told = drive(go)
    assert laid_out, "the press must reach the app with the button under it"
    assert ignored == [], "the press made before the button was painted pressed it"
    assert focused == "typing"
    assert pressed == ["restart"]
    assert told == []


def test_a_button_its_bar_moves_is_guarded_again_and_one_left_in_place_is_not(
    hold: PaintHold,
) -> None:
    """A layout pass that leaves the button where it was is no change under the pointer;
    one that moves it is, as when the neighbour beside it goes."""

    async def go(pilot: Pilot[None], host: Host) -> tuple[list[str], list[str]]:
        button = await _shown(pilot, host)
        hold.release()
        await settle_page(host)
        hold.hold()
        host.screen.refresh(layout=True)  # a pass that moves nothing
        await settle_page(host)
        await pilot.click(button)
        await settle_page(host)
        in_place = list(host.pressed)
        host.query_one("#neighbour").display = False
        await settle_page(host)
        await pilot.click(button)
        await settle_page(host)
        return in_place, host.pressed[len(in_place) :]

    in_place, moved = drive(go)
    assert in_place == ["restart"]
    assert moved == [], "the click on the button that had just moved pressed it"


def test_a_press_that_reaches_the_app_with_the_pop_is_ignored() -> None:
    """Re-review of #478 (seq 5737): a dialog closing off the button puts the guard up until
    the frame that shows the button again. A press queued at the app in the same turn as
    the pop was aimed at the dialog: ignored, and the focus stays where it was."""

    async def go(pilot: Pilot[None], host: Host) -> tuple[list[str], str | None, list[str]]:
        button = await _shown(pilot, host)
        where = button.region.offset
        host.query_one("#neighbour", Button).focus()
        await host.push_screen(Dialog())
        await settle_page(host)
        host.pop_screen()
        guard_paint.queue_press(host, where)
        await settle_page(host)
        return host.pressed, host.focused_id(), host.notices()

    assert drive(go) == ([], "neighbour", [])


def test_a_press_right_after_the_frame_that_uncovers_the_button_presses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UI tester on #8 (seq 5724, PR #478 at 26643fa7): Stop, clicked once the dialog over it
    had visibly gone, was dropped as late as 0.36 s after. The screen beneath repaints on its
    resume with no layout pass of its own, and the guard came down on the layout pass it
    asked for, a frame or more later. A press that reaches the app right after the frame
    that shows the button again presses it. No settling between the pop and the press."""
    frame = guard_paint.next_frame(monkeypatch)

    async def go(pilot: Pilot[None], host: Host) -> tuple[list[str], list[str]]:
        button = await _shown(pilot, host)
        where = button.region.offset
        await host.push_screen(Dialog())
        await settle_page(host)
        host.pop_screen()
        frame.then(lambda: guard_paint.queue_press(host, where))
        await settle_page(host)
        return host.pressed, host.notices()

    assert drive(go) == (["restart"], [])


def test_the_guard_comes_down_after_an_installed_dialog_closes() -> None:
    """Re-review of #478 (seq 5737): Textual prunes a dialog it does not keep, and the prune
    lays out the screen beneath, which takes a guard down on its own. An installed dialog is
    kept, and nothing lays the screen out on its pop. The guard asks for that pass itself."""

    async def go(pilot: Pilot[None], host: Host) -> tuple[list[str], bool]:
        button = await _shown(pilot, host)
        host.install_screen(Dialog(), name="dialog")
        await host.push_screen("dialog")
        await settle_page(host)
        host.pop_screen()
        await settle_page(host)
        await pilot.click(button)
        await settle_page(host)
        return host.pressed, button.guarding

    assert drive(go) == (["restart"], False)


def test_a_dialog_closing_off_the_button_guards_it_again(hold: PaintHold) -> None:
    async def go(pilot: Pilot[None], host: Host) -> tuple[list[str], list[str]]:
        button = await _shown(pilot, host)
        hold.release()
        await settle_page(host)
        hold.hold()
        await host.push_screen(Dialog())
        await settle_page(host)
        host.pop_screen()
        await settle_page(host)
        await pilot.click(button)
        await settle_page(host)
        return host.pressed, host.notices()

    pressed, told = drive(go)
    assert pressed == []
    assert told == []


def test_an_ignored_click_leaves_the_focus_where_it_was_for_the_next_enter(
    hold: PaintHold,
) -> None:
    """Review of #266, round 2: Textual focuses a widget on the mouse-down, before the click
    the guard drops. The ignored click left the keyboard on Restart, and the user's next
    Enter, meant for what they were typing into, pressed it."""

    async def go(pilot: Pilot[None], host: Host) -> tuple[str | None, list[str], str | None]:
        button = await _shown(pilot, host)
        host.query_one("#neighbour", Button).focus()
        await settle_page(host)
        await pilot.click(button)
        await settle_page(host)
        ignored_on = host.focused_id()
        await pilot.press("enter")
        await settle_page(host)
        hold.release()
        await settle_page(host)
        await pilot.click(button)  # a click the guard lets through focuses it, as a Button's does
        await settle_page(host)
        return ignored_on, host.pressed, host.focused_id()

    ignored_on, pressed, after = drive(go)
    assert ignored_on == "neighbour"
    assert pressed == ["neighbour", "restart"]
    assert after == "restart"


async def _release(pilot: Pilot[None], button: GuardedButton) -> None:
    """The mouse-up of a press already down on ``button``, and the click Textual makes of it
    (``Pilot.click`` is the three events at one instant)."""
    await pilot._post_mouse_events([events.MouseUp, events.Click], button)


def test_a_press_held_as_the_guard_goes_up_gets_one_verdict_for_focus_and_press(
    hold: PaintHold,
) -> None:
    """Review of #266, round 3: the focus was judged at the mouse-down and the press again at
    the click, on the mouse-up. Down on a button at rest, the bar re-laid out under the
    held button before the release: the click was ignored, the focus taken all the same,
    and the next Enter pressed it. One verdict now, the mouse-down's, for both."""

    async def go(pilot: Pilot[None], host: Host) -> tuple[bool, list[str], str | None]:
        button = await _shown(pilot, host)
        hold.release()
        await settle_page(host)
        hold.hold()
        host.query_one("#neighbour", Button).focus()
        await settle_page(host)
        await pilot.mouse_down(button)
        await settle_page(host)
        button.changed_under_pointer()  # the bar moves it while the button is held
        up_at_release = button.guarding
        await _release(pilot, button)
        await settle_page(host)
        return up_at_release, host.pressed, host.focused_id()

    up_at_release, pressed, focused = drive(go)
    assert up_at_release is True
    assert (pressed, focused) == (["restart"], "restart"), "focus and press must agree"


def test_a_press_down_while_the_guard_is_up_is_ignored_though_released_after_it_came_down(
    hold: PaintHold,
) -> None:
    """The reverse straddle: the mouse-down lands before the paint, aimed at the old view, and
    the mouse-up comes after it. The focus stayed where it was, and the press went through
    on the button that had just appeared."""

    async def go(pilot: Pilot[None], host: Host) -> tuple[bool, list[str], str | None, list[str]]:
        button = await _shown(pilot, host)
        host.query_one("#neighbour", Button).focus()
        await settle_page(host)
        await pilot.mouse_down(button)
        await settle_page(host)
        hold.release()
        await settle_page(host)
        down_at_release = not button.guarding
        await _release(pilot, button)
        await settle_page(host)
        return down_at_release, host.pressed, host.focused_id(), host.notices()

    down_at_release, pressed, focused, told = drive(go)
    assert down_at_release is True
    assert (pressed, focused) == ([], "neighbour")
    assert told == []


def test_a_key_or_a_press_from_code_is_not_guarded(hold: PaintHold) -> None:
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
