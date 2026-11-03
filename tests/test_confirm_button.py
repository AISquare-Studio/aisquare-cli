"""The inline confirm (``aisquare.cli.ui.confirm_button``), on a host of its own.

User (2026-10-30): confirming is a double click in place, not a move to a dialog's button. A
:class:`ConfirmButton`'s first press arms it, relabelled where it is; a second press on it
acts; it disarms after a while, on Esc, on blur and when its view changes, and gives its
label back. The five flows that use it are tested where they live (``test_ui_project_cards``,
``test_ui_app_settings``, ``test_ui_spawn``, ``test_ui_personas``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import ClassVar, TypeVar

import pytest
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.geometry import Offset
from textual.pilot import Pilot
from textual.screen import ModalScreen
from textual.widgets import Button, ContentSwitcher, Input, Static

from aisquare.cli.ui import confirm_button
from aisquare.cli.ui.confirm_button import ConfirmButton
from tests import guard_paint
from tests.guard_paint import PaintHold
from tests.ui_workers import settle_page, watch_until

T = TypeVar("T")

REST = "Clear"
ARMED = "Clear the whole binding? click again"
NOTE = "Its binary, environment, args and account go."
SAVED = "Saved to config.toml"


class Dialog(ModalScreen[None]):
    def compose(self) -> ComposeResult:
        yield Static("a dialog over the button")


class Host(App[None]):
    """A row with the confirm button between two neighbours, a note line, a field to move the
    focus to and a second view to switch to."""

    BINDINGS: ClassVar = [Binding("escape", "escaped", "escape")]

    def __init__(self, *, note_shown: bool = False) -> None:
        super().__init__()
        self.pressed: list[str] = []
        self.escaped = 0
        self.note_shown = note_shown

    def compose(self) -> ComposeResult:
        yield Input(id="field")
        with ContentSwitcher(initial="one", id="switcher"):
            with Horizontal(id="one"):
                yield Button("Edit", id="edit", compact=True)
                yield ConfirmButton(
                    REST,
                    armed_label=ARMED,
                    note=NOTE,
                    note_in="#note",
                    id="clear",
                    compact=True,
                )
                yield Button("After", id="after", compact=True)
            yield Static("another view", id="two")
        note = Static(SAVED if self.note_shown else "", id="note")
        note.display = self.note_shown
        yield note

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.pressed.append(event.button.id or "")

    def action_escaped(self) -> None:
        self.escaped += 1

    def button(self) -> ConfirmButton:
        return self.query_one("#clear", ConfirmButton)

    def note(self) -> Static:
        return self.query_one("#note", Static)


@pytest.fixture
def hold(monkeypatch: pytest.MonkeyPatch) -> PaintHold:
    return guard_paint.install(monkeypatch)


async def _painted(host: Host, hold: PaintHold) -> None:
    """The first paint out, every guard down; the paints from here on held."""
    hold.release()
    await settle_page(host)
    hold.hold()


def drive(fn: Callable[[Pilot[None], Host], Awaitable[T]], **host: bool) -> T:
    async def run() -> T:
        app = Host(**host)
        async with app.run_test(size=(80, 8)) as pilot:
            await settle_page(app)
            return await fn(pilot, app)

    return asyncio.run(run())


def label(button: Button) -> str:
    return str(button.label)


def test_one_click_arms_in_place_and_the_second_on_the_same_spot_presses() -> None:
    async def go(pilot: Pilot[None], host: Host) -> list[object]:
        button = host.button()
        where = button.region.offset + Offset(1, 0)
        await pilot.click(offset=where)
        await settle_page(host)
        armed = [list(host.pressed), label(button), button.armed, str(host.note().content)]
        await pilot.click(offset=where)
        await settle_page(host)
        return [*armed, list(host.pressed), label(button), button.armed, host.note().display]

    assert drive(go) == [[], ARMED, True, NOTE, ["clear"], REST, False, False]


def test_a_double_click_is_two_presses_and_acts() -> None:
    """Button drops a click for 0.2 s after its press; the arming press is not one."""

    async def go(pilot: Pilot[None], host: Host) -> tuple[list[str], str]:
        await pilot.double_click("#clear")
        await settle_page(host)
        return host.pressed, label(host.button())

    assert drive(go) == (["clear"], REST)


def test_enter_arms_and_enter_again_confirms() -> None:
    async def go(pilot: Pilot[None], host: Host) -> list[object]:
        host.button().focus()
        await pilot.press("enter")
        await settle_page(host)
        first = [list(host.pressed), label(host.button())]
        await pilot.press("enter")
        await settle_page(host)
        return [*first, host.pressed]

    assert drive(go) == [[], ARMED, ["clear"]]


def test_its_own_wider_label_puts_no_guard_up_against_the_second_click(hold: PaintHold) -> None:
    """The armed label is wider, so arming changes the button's region, which is what puts a
    GuardedButton's guard up. Its own relabel, in place under the pointer that armed it, must
    not: with every paint held back, a guard that went up would stay up and drop the second
    click. The neighbour after it is pushed along."""

    async def go(pilot: Pilot[None], host: Host) -> list[object]:
        await _painted(host, hold)
        button = host.button()
        after = host.query_one("#after", Button)
        before = button.region
        where = before.offset + Offset(1, 0)
        await pilot.click(offset=where)
        await settle_page(host)
        widened = button.region.width > before.width and button.region.contains_region(before)
        pushed_on = after.region.x >= button.region.right
        guarding = button.guarding
        await pilot.click(offset=where)
        await settle_page(host)
        return [widened, pushed_on, guarding, host.pressed]

    assert drive(go) == [True, True, False, ["clear"]]


def test_a_button_moved_by_more_than_its_own_relabel_is_guarded(hold: PaintHold) -> None:
    """Armed as something else moves it in the same pass (a line put above it), the guard
    goes up as for any other move: only a relabel in place is the button's own."""

    async def go(pilot: Pilot[None], host: Host) -> tuple[bool, bool, list[str]]:
        await _painted(host, hold)
        button = host.button()
        pushed = Static("pushed")
        pushed.styles.width = 8
        host.query_one("#one", Horizontal).mount(pushed, before=0)  # laid out with the relabel
        button.press()
        await settle_page(host)
        return button.armed, button.guarding, host.pressed

    assert drive(go) == (True, True, [])


def test_unpressed_again_it_disarms_after_its_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(confirm_button, "ARMED_FOR", 0.05)
    seen: list[tuple[bool | None, bool | None]] = []
    changed = ConfirmButton._armed_changed

    def record(button: ConfirmButton, was: bool | None, now: bool | None) -> None:
        seen.append((was, now))
        changed(button, was, now)

    monkeypatch.setattr(ConfirmButton, "_armed_changed", record)

    async def go(pilot: Pilot[None], host: Host) -> list[object]:
        button = host.button()
        await pilot.click("#clear")
        await watch_until(lambda: not button.armed)
        await settle_page(host)
        return [label(button), host.note().display, host.pressed]

    assert drive(go) == [REST, False, []]
    assert seen == [(None, True), (True, None)]


@pytest.mark.parametrize("how", ["escape", "blur", "view", "screen"])
def test_it_disarms_and_gives_its_label_and_note_back(how: str) -> None:
    async def go(pilot: Pilot[None], host: Host) -> list[object]:
        button = host.button()
        await pilot.click("#clear")
        await settle_page(host)
        assert button.armed and host.note().display
        if how == "escape":
            await pilot.press("escape")
        elif how == "blur":
            host.query_one("#field", Input).focus()
        elif how == "view":
            host.query_one(ContentSwitcher).current = "two"
        else:
            await host.push_screen(Dialog())
        await settle_page(host)
        return [button.armed, label(button), host.note().display, host.escaped, host.pressed]

    assert drive(go) == [False, REST, False, 0, []]


def test_esc_at_rest_goes_on_to_the_screen() -> None:
    async def go(pilot: Pilot[None], host: Host) -> int:
        host.button().focus()
        await pilot.press("escape")
        await settle_page(host)
        return host.escaped

    assert drive(go) == 1


def test_a_note_line_that_was_showing_gets_its_own_text_back() -> None:
    async def go(pilot: Pilot[None], host: Host) -> list[object]:
        await pilot.click("#clear")
        await settle_page(host)
        armed = str(host.note().content)
        host.button().disarm()
        await settle_page(host)
        return [armed, str(host.note().content), host.note().display]

    assert drive(go, note_shown=True) == [NOTE, SAVED, True]


def test_a_button_that_does_not_ask_acts_on_one_press() -> None:
    async def go(pilot: Pilot[None], host: Host) -> list[str]:
        host.button().asks = False
        await pilot.click("#clear")
        await settle_page(host)
        return host.pressed

    assert drive(go) == ["clear"]


def test_turning_asking_off_disarms() -> None:
    async def go(pilot: Pilot[None], host: Host) -> tuple[bool, str]:
        await pilot.click("#clear")
        await settle_page(host)
        host.button().asks = False
        await settle_page(host)
        return host.button().armed, label(host.button())

    assert drive(go) == (False, REST)


class TwoViews(App[None]):
    """Two views, each with a note line of the same id, as the shell's Settings pages have."""

    def compose(self) -> ComposeResult:
        with Vertical(id="other"):
            yield Static(SAVED, id="note")
        with Vertical(id="mine"):
            yield ConfirmButton(REST, armed_label=ARMED, note=NOTE, note_in="#note", id="clear")
            yield Static(SAVED, id="note")


def test_the_note_goes_in_the_nearest_line_of_its_id() -> None:
    async def run() -> tuple[str, str]:
        app = TwoViews()
        async with app.run_test(size=(80, 12)) as pilot:
            await settle_page(app)
            await pilot.click("#clear")
            await settle_page(app)
            mine = app.query_one("#mine #note", Static)
            other = app.query_one("#other #note", Static)
            return str(mine.content), str(other.content)

    assert asyncio.run(run()) == (NOTE, SAVED)
