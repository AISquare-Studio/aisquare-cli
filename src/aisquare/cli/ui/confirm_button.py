"""The inline confirm: a first press that asks "sure?" where it was made, and a second that acts.

User (2026-10-30): confirming should be a double click in place, not a move of the mouse to a
dialog's button. :class:`ConfirmButton` is that button. Its first press arms it, relabelled in
place (``Reset all? click again``); a second press on it, a click or Enter, acts; a double-click
is two presses. Armed, it disarms after :data:`ARMED_FOR` seconds, on Esc, when it loses the
focus and when its view changes (hidden, or another screen over it), and disarming gives it its
label back. What the second press will do is in sight before it: in the armed label, or in a
note line the button fills while armed (``note``, ``note_in``).

Only the simple yes/no questions ask this way: Remove from fleet, Clear a role binding, Reset
all settings, Take over a teammate, and attaching a persona to a running agent (an agent row of
the target picker, ``attach.py``, which arms in place with :class:`Arming`). A question with
more to it than yes keeps its dialog: Stop, Start fresh, Discard, the bulk restart and pause,
Update, usage Consent (user, 2026-10-30).
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from typing import Any, ClassVar, Generic, Self, TypeVar

from rich.cells import cell_len
from rich.text import Text
from textual.binding import Binding, BindingType
from textual.content import Content, ContentText
from textual.geometry import Region, Size
from textual.screen import Screen
from textual.timer import Timer
from textual.visual import VisualType
from textual.widget import Widget
from textual.widgets import Static

from aisquare.cli.ui.click_guard import GuardedButton

ARMED_FOR = 3.0
"""Seconds an armed confirm waits for its second press before it disarms."""

K = TypeVar("K", bound=Hashable)


class Arming(Generic[K]):
    """What a first press armed, and the clock that disarms it.

    A press on a key arms it (:meth:`press`); a second press on the same key within
    :data:`ARMED_FOR` seconds confirms it, and a press on another key arms that one instead.
    ``changed(was, now)`` is told each time what is armed changes, ``None`` for nothing, so
    its owner can relabel what it armed and give the label back.
    """

    def __init__(self, owner: Widget, changed: Callable[[K | None, K | None], None]) -> None:
        self._owner = owner
        self._changed = changed
        self._timer: Timer | None = None
        self.armed: K | None = None

    def press(self, key: K) -> bool:
        """A press on ``key``: ``True`` if it confirms (``key`` was armed); else it arms ``key``."""
        if key == self.armed:
            self.disarm()
            return True
        self.disarm()
        self.armed = key
        self._timer = self._owner.set_timer(ARMED_FOR, self.disarm, name="confirm-disarm")
        self._changed(None, key)
        return False

    def disarm(self) -> None:
        """Nothing armed any more; a no-op when nothing is."""
        was = self.armed
        if was is None:
            return
        self.armed = None
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._changed(was, None)


def _label_width(label: Content) -> int:
    """The cells ``Button.get_content_width`` gives ``label``."""
    return max((cell_len(line) for line in label.plain.splitlines()), default=0) + 2


class ConfirmButton(GuardedButton):
    """A :class:`GuardedButton` whose first press arms it and whose second press presses it.

    Armed, it shows ``armed_label`` and, when ``note`` and ``note_in`` are given, ``note`` in
    the nearest :class:`Static` that ``note_in`` selects (shown if it was hidden; its content
    and tooltip given back on disarming). It is never narrower armed than at rest, so the
    place the first press was made on is still the button.

    A drop-in for a Button whose press needed a yes: the confirming press posts the same
    ``Button.Pressed``, so the press handler acts on it as it used to act on the dialog's yes.
    The arming press posts nothing. ``asks`` off, it is a plain guarded button whose every
    press acts (the Spawn dialog's submit, which asks only to take over).
    """

    DEFAULT_CSS = """
    ConfirmButton.-armed { background: $error !important; color: $text !important; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "disarm", "cancel", show=False)]

    _rest: Content | None = None
    """The label at rest, while the armed one shows."""
    _relabeled: bool = False
    """Its label changed since the last layout pass (:meth:`_laid_out`)."""
    _note_saved: tuple[VisualType, Any, bool] | None = None
    """What ``note_in`` showed before the note: its content, its tooltip, whether it was hidden."""

    def __init__(
        self,
        label: ContentText,
        *,
        armed_label: ContentText,
        note: Text | str | None = None,
        note_in: str | None = None,
        asks: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(label, **kwargs)
        self.armed_label = armed_label
        self.note = note
        self.note_in = note_in
        self._asks = asks
        self._arming: Arming[bool] = Arming(self, self._armed_changed)

    @property
    def armed(self) -> bool:
        """Whether the next press acts."""
        return self._arming.armed is not None

    @property
    def asks(self) -> bool:
        """Whether a press arms it first; off, every press acts."""
        return self._asks

    @asks.setter
    def asks(self, asks: bool) -> None:
        self._asks = asks
        if not asks:
            self.disarm()

    def disarm(self) -> None:
        """Back to rest, its label given back; a no-op when it is not armed."""
        self._arming.disarm()

    def press(self) -> Self:
        if self.disabled or not self.display:
            return self
        if not self._asks or self._arming.press(True):
            return super().press()
        # The arming press is not Button's press: no Pressed, and none of its 0.2 s "active"
        # effect, during which Button drops a click, so a double-click is two presses.
        return self

    def _armed_changed(self, was: bool | None, now: bool | None) -> None:
        if now:
            self._rest = Content.from_text(self.label)
            self._show_label(self.armed_label)
            self.add_class("-armed")
            self._show_note()
        else:
            rest, self._rest = self._rest, None
            if rest is not None:
                self._show_label(rest)
            self.remove_class("-armed")
            self._give_note_back()

    def _show_label(self, label: ContentText) -> None:
        if Content.from_text(label) != self.label:
            self._relabeled = True
            self.label = label
            # Button's label is no layout reactive: sized for the old one, the new one is cut.
            self.clear_cached_dimensions()
            self.refresh(layout=True)

    def get_content_width(self, container: Size, viewport: Size) -> int:
        width = super().get_content_width(container, viewport)
        return width if self._rest is None else max(width, _label_width(self._rest))

    def _note_line(self) -> Static | None:
        """The nearest :class:`Static` ``note_in`` selects: in the closest container that has
        one, as two views on one screen may each have a line of that id."""
        if self.note is None or self.note_in is None or not self.is_attached:
            return None
        for container in self.ancestors:
            for line in container.query(self.note_in).results(Static):
                return line
        return None

    def _show_note(self) -> None:
        line = self._note_line()
        if line is None or self.note is None:
            return
        self._note_saved = (line.content, line.tooltip, not line.display)
        line.update(self.note)
        line.tooltip = self.note  # whole, where the line cuts it short
        line.display = True

    def _give_note_back(self) -> None:
        saved, self._note_saved = self._note_saved, None
        line = self._note_line()
        if saved is None or line is None:
            return
        content, tooltip, hidden = saved
        line.update(content)
        line.tooltip = tooltip
        if hidden:  # shown for the note; one its view hid meanwhile stays as the view set it
            line.display = False

    # --- what disarms it: Esc, the focus leaving, its view changing ------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "disarm":
            return self.armed  # at rest, Esc goes on to what it does on this screen
        return super().check_action(action, parameters)

    def action_disarm(self) -> None:
        self.disarm()

    def on_blur(self) -> None:
        self.disarm()

    def _screen_changed(self, screen: Screen[Any]) -> None:
        if screen is not self.screen:
            self.disarm()  # another screen over it
        super()._screen_changed(screen)

    def _laid_out(self, screen: Screen[Any]) -> None:
        region = self.region
        if self._relabeled:
            self._relabeled = False
            # Its own relabel only widened or narrowed it where it was, under the pointer that
            # armed it: not a change under the pointer, and the guard stays down for the second
            # press. Anything else that moved it in the same pass still puts the guard up.
            if _in_place(self._placed_at, region):
                self._placed_at = region
        if not region:
            self.disarm()  # its view switched away, or it was hidden
        super()._laid_out(screen)


def _in_place(was: Region, now: Region) -> bool:
    """Whether ``now`` only widened or narrowed ``was`` where it stands."""
    return bool(was) and bool(now) and (now.contains_region(was) or was.contains_region(now))
