"""asq's notifications: small, top-right, at most three, each quick to clear (R13, #146).

The owner, verbatim: "make notifications a lot better, currently they are too large and too
covering, make them appear from top right, no more than 3 active at a given moment, all with
automatic time to auto dismiss ... the right most block is 'x' to dismiss it instantly ...
on clicking and dragging on the text portion allows user to select it which automatically
copies it to clipboard ... the goal of right most region being dismiss is user can spam
click at the first X and the next one will keep moving up in its place".

What triggers a notification is untouched: every ``notify`` is Textual's, and
``app._notifications`` keeps every one. Only the showing changes, in the app's
``_refresh_notifications`` (``cli.ui.clipboard.ClipboardApp``), which hands the screen's
stock ``ToastRack`` to :func:`show_calm`:

- **Top-right**, from row 1 (row 0 keeps the agent bar's Stop and Restart clear), a fixed
  :data:`WIDTH` so every toast's dismiss column is the same screen column, no padding rows,
  and one blank row between toasts.
- **Newest on top, at most** :data:`MAX_SHOWN`. A newer one makes the oldest shown one
  WAIT: hidden, its time stopped, back at the bottom when a slot frees. ``+N more`` says so
  (:class:`MoreToasts`), and a click on it dismisses those that wait.
- **Two regions** (:class:`CalmToast`): the text, where a click does nothing and a drag
  selects and copies (``ClipboardApp.copy_text``), its outcome cued in the dismiss column
  for :data:`CUE_SECONDS` (``✓`` copied, ``↗`` only sent, ``!`` failed); and the right-most
  :data:`DISMISS` cells of every row, ``✕`` on the first, which dismiss at once.
- **The spam-click**: dismissing the top toast moves the next one's first row, and its
  ``✕``, to the row just clicked, so one spot clicked again and again closes them all. A
  click there just after the last one closed is swallowed (:class:`ClickGuard`) rather
  than reaching the pane beneath.
- **Time stops** while the pointer is over a toast, and resumes with at least
  :data:`RESUME_AT_LEAST` seconds.

Textual (8.x) finds the rack by its exact type, so the rack is the stock one and only what
it holds is asq's; :data:`CALM` is false, and the stock toasts stand, should the private
hooks this reads ever move.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Any

from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.app import App
from textual.geometry import Region
from textual.notifications import Notification, Notifications
from textual.strip import Strip
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static
from textual.widgets._toast import Toast, ToastHolder, ToastRack

if TYPE_CHECKING:
    from aisquare.core.clipboard import CopyResult

MAX_SHOWN = 3
"""Toasts shown at once; the rest wait, their time stopped."""

WIDTH = 46
"""Every toast's width in cells, at most the screen's: one width, so every ``✕`` is in one
column and a click held there closes toast after toast."""

DISMISS = 3
"""The right-most cells of every row of a toast that dismiss it; ``✕`` is drawn in the
middle one, on the first row."""

CUE_SECONDS = 1.5
"""How long a copy's outcome shows in the dismiss column before ``✕`` comes back."""

RESUME_AT_LEAST = 2.0
"""The least time a toast has left when the pointer leaves it."""

GUARD_SECONDS = 0.6
"""How long a click where the last ``✕`` was is swallowed once the last toast has closed."""

CUES = {"copied": "✓", "sent": "↗", "failed": "!"}

GUARD_ID = "calm-toast-guard"
MORE_ID = "calm-toast-more"

_NEVER = 365 * 24 * 3600.0
"""Textual's own toast timer, set to never: the notification keeps the time
(:meth:`CalmToast._arm`)."""
"""What the dismiss column shows of a copy made from a toast's text, by its verdict."""

CSS = f"""
Screen > ToastRack {{
    dock: top;
    align: right top;
    margin-bottom: 0;
    overflow-y: hidden;
}}
ToastHolder > CalmToast {{
    width: {WIDTH};
    max-width: 100%;
    margin-top: 1;
    padding: 0 {DISMISS} 0 1;
}}
MoreToasts {{
    width: {WIDTH};
    max-width: 100%;
    height: 1;
    visibility: visible;
    text-align: right;
    padding: 0 {DISMISS} 0 1;
    color: $text-muted;
}}
ClickGuard {{
    width: {DISMISS};
    height: 1;
    margin-top: 1;
    visibility: visible;
}}
"""
"""Asq's notification styles: the app's own (``ClipboardApp.DEFAULT_CSS``), so they hold on
every screen, modal ones included. Each rule names the parent too, to outrank Textual's own
``ToastRack`` and ``Toast`` defaults, which are as specific as a bare type and load after.
Theme variables only, so dark, light and rlx follow."""


def _hooks_present() -> bool:
    """Whether the Textual hooks this module reads are where it reads them (Textual 8.x)."""
    return all(
        (
            hasattr(App, "_refresh_notifications"),
            hasattr(App, "_unnotify"),
            hasattr(ToastRack, "_toast_id"),
            hasattr(Toast, "_expire"),
            hasattr(Toast, "_on_mount"),
            {"raised_at", "timeout", "identity"} <= set(Notification.__dataclass_fields__),
        )
    )


CALM = _hooks_present()
"""Whether asq shows its own toasts; else Textual's stock ones stand, untouched."""


def held(app: App[Any]) -> dict[str, float]:
    """The time each stopped notification had left, by its identity: those waiting and those
    under the pointer. Kept on the app, as a notification is shown on whichever screen."""
    store: dict[str, float] | None = getattr(app, "_calm_held", None)
    if store is None:
        store = {}
        app._calm_held = store  # type: ignore[attr-defined]
    return store


def hold(app: App[Any], notification: Notification) -> None:
    """Stop ``notification``'s time: it never expires while it waits or is pointed at."""
    stopped = held(app)
    if notification.identity not in stopped:
        stopped[notification.identity] = max(notification.time_left, 0.5)
        notification.timeout = math.inf


def release(app: App[Any], notification: Notification, *, at_least: float = 0.0) -> None:
    """Start ``notification``'s time again where it stopped, with at least ``at_least``."""
    left = held(app).pop(notification.identity, None)
    if left is not None:
        notification.raised_at = time.time()
        notification.timeout = max(left, at_least)


def show_calm(rack: ToastRack, notifications: Notifications) -> None:
    """Show ``notifications`` in ``rack`` as asq does: the newest :data:`MAX_SHOWN` on top,
    newest first, and the rest waiting, their time stopped, under ``+N more``."""
    if not rack.is_attached or getattr(rack, "_closing", False) or getattr(rack, "_pruning", False):
        return  # a screen not up yet, or going: its resume refreshes the notifications again
    app = rack.app
    live = list(notifications)  # oldest first; the expired are gone
    shown = live[::-1][:MAX_SHOWN]
    waiting = live[: max(len(live) - MAX_SHOWN, 0)]
    stopped = held(app)
    for identity in set(stopped) - {notification.identity for notification in live}:
        del stopped[identity]  # dismissed or cleared meanwhile
    for notification in waiting:
        hold(app, notification)
    ids = {ToastRack._toast_id(notification): notification for notification in shown}
    present: dict[str, ToastHolder] = {}
    for holder in rack.query_children(ToastHolder):
        if getattr(holder, "_calm_removed", False) or holder.id in (GUARD_ID, MORE_ID):
            continue
        # By the holder's id, which it has from the start: its toast may not be mounted yet
        # when several arrive in one tick, and a second mount of it was a duplicate id.
        found = ids.get(holder.id or "")
        stock = any(type(toast) is Toast for toast in holder.query_children(Toast))
        if found is None or stock:
            _remove(holder)
        else:
            present[found.identity] = holder
    for notification in shown:
        waited = notification.identity not in present and notification.identity in stopped
        if waited and not _pointed_at(app, notification):
            release(app, notification)  # it waited: its time runs again
    more = _more_holder(rack)
    for index, notification in enumerate(shown):
        if notification.identity in present:
            continue
        holder = ToastHolder(CalmToast(notification), id=ToastRack._toast_id(notification))
        after = [present[n.identity] for n in shown[index + 1 :] if n.identity in present]
        if after:
            rack.mount(holder, before=after[0])
        elif more is not None:
            rack.mount(holder, before=more)
        else:
            rack.mount(holder)
        present[notification.identity] = holder
    _show_more(rack, more, len(waiting))
    rack.display = bool(live) or _guarded(rack)


def _guarded(rack: ToastRack) -> bool:
    return any(
        holder.id == GUARD_ID and not getattr(holder, "_calm_removed", False)
        for holder in rack.query_children(ToastHolder)
    )


def _remove(widget: Widget) -> None:
    """Remove ``widget`` once: a holder dismissed by a click is removed then, and again by the
    refresh that follows, before the first removal is over."""
    if getattr(widget, "_calm_removed", False):
        return
    widget._calm_removed = True  # type: ignore[attr-defined]
    widget.remove()


def _pointed_at(app: App[Any], notification: Notification) -> bool:
    return any(
        toast._notification.identity == notification.identity and toast.mouse_hover
        for toast in app.screen.query(CalmToast)
    )


def _more_holder(rack: ToastRack) -> ToastHolder | None:
    for holder in rack.query_children(ToastHolder):
        if holder.id == MORE_ID and not getattr(holder, "_calm_removed", False):
            return holder
    return None


def _show_more(rack: ToastRack, holder: ToastHolder | None, waiting: int) -> None:
    if not waiting:
        if holder is not None:
            _remove(holder)
        return
    if holder is None:
        more = MoreToasts(waiting)
        holder = ToastHolder(more, id=MORE_ID)
        holder._calm_more = more  # type: ignore[attr-defined]  # its own, mounted or not yet
        rack.mount(holder)
    else:
        holder._calm_more.count = waiting  # type: ignore[attr-defined]


class CalmToast(Toast):
    """One of asq's toasts: Textual's, with its text a place to select and copy from, and
    the right-most :data:`DISMISS` cells of every row the one place that dismisses it.

    It is still a ``Toast`` — what it says is Textual's ``render``, and every test that
    reads a toast's text reads it unchanged — with the ``✕`` drawn into its right padding
    (:meth:`render_lines`) and its time kept by the notification it shows, so stopping and
    starting it is the notification's own (:func:`hold`, :func:`release`).
    """

    COMPONENT_CLASSES = Toast.COMPONENT_CLASSES | {
        "calm-toast--dismiss",
        "calm-toast--copied",
        "calm-toast--failed",
    }

    # The glyph's colours: its own, so they resolve where component classes are looked up.
    DEFAULT_CSS = """
    CalmToast > .calm-toast--dismiss {
        color: $foreground-muted;
    }
    CalmToast:hover > .calm-toast--dismiss {
        color: $text-error;
    }
    CalmToast > .calm-toast--copied {
        color: $text-success;
    }
    CalmToast > .calm-toast--failed {
        color: $text-warning;
    }
    """

    def __init__(self, notification: Notification) -> None:
        super().__init__(notification)
        self._timeout = _NEVER  # Textual's own timer; the notification keeps the time
        self._timer: Timer | None = None
        self._cue: str | None = None
        """The verdict of a copy just made from the text, shown in the dismiss column."""

    # --- time -------------------------------------------------------------------------

    def on_mount(self) -> None:
        self._arm()

    def _arm(self) -> None:
        """One timer, for the time the notification has left; none while it is stopped."""
        if self._timer is not None:
            self._timer.stop()
        left = self._notification.time_left
        self._timer = self.set_timer(max(left, 0.0), self._expire) if math.isfinite(left) else None

    def _expire(self) -> None:
        """The timer's end: dismissed when the notification's time is up, else armed again
        for what is left. A stopped one waits for :meth:`on_leave` to start it again."""
        if self._notification.time_left > 0.05:
            self._arm()
            return
        self.dismiss()

    def on_enter(self, event: events.Enter) -> None:
        hold(self.app, self._notification)
        self._arm()

    def on_leave(self, event: events.Leave) -> None:
        release(self.app, self._notification, at_least=RESUME_AT_LEAST)
        self._arm()

    # --- the two regions ----------------------------------------------------------------

    def in_dismiss_column(self, x: int) -> bool:
        """Whether ``x`` (an offset into the toast) is in the right-most :data:`DISMISS` cells."""
        return x >= self.outer_size.width - DISMISS

    def dismiss(self) -> None:
        """Gone at once: the notification is forgotten, and the toast leaves the rack before
        the next click, so the one below is under the pointer for it."""
        app = self.app
        holder = self.parent if isinstance(self.parent, ToastHolder) else self
        rack = holder.parent
        last = sum(1 for _ in app._notifications) <= 1
        if self._timer is not None:
            self._timer.stop()
        held(app).pop(self._notification.identity, None)
        app._unnotify(self._notification)
        _remove(holder)
        if last and isinstance(rack, ToastRack) and not _guarded(rack):
            guard = ToastHolder(ClickGuard(), id=GUARD_ID)
            rack.mount(guard)
            rack.display = True
            rack.set_timer(GUARD_SECONDS, lambda: _drop_guard(rack, guard))

    async def _on_click(self, event: events.Click) -> None:
        """A click in the dismiss column dismisses; anywhere else it selects as text does
        (twice a word's, the whole on a third), and never dismisses: a drag that ends on
        the toast is a click to Textual, and it would have taken the text with it."""
        event.prevent_default()  # Textual's own Toast is dismissed by any click at all
        event.stop()
        if self.in_dismiss_column(event.x):
            self.dismiss()
            return
        if event.chain >= 2 and self.allow_select:
            self.text_select_all()
            self.call_after_refresh(self.copy_selection)

    def on_mouse_up(self, event: events.MouseUp) -> None:
        """A drag over the text ends here: what it selected is copied."""
        if not self.in_dismiss_column(event.x):
            self.call_after_refresh(self.copy_selection)

    def copy_selection(self) -> None:
        """Copy what is selected of this toast's text by the app's one copy, and cue the
        outcome. Nothing for no selection."""
        selection = self.text_selection
        extracted = self.get_selection(selection) if selection is not None else None
        text = extracted[0] if extracted else ""
        if not text.strip():
            return
        from aisquare.cli.ui.clipboard import ClipboardApp

        app = self.app
        if isinstance(app, ClipboardApp):
            app.copy_text(text, cue=self.cue)
        else:
            app.copy_to_clipboard(text)

    def cue(self, result: CopyResult) -> None:
        """Show ``result``'s verdict in the dismiss column for :data:`CUE_SECONDS`; its reason,
        when it is not a copy, as the toast's tooltip."""
        self._cue = result.verdict
        if result.reason:
            self.tooltip = f"could not copy: {result.reason}"
        elif result.verdict == "sent":
            self.tooltip = "sent to the terminal's clipboard; if paste is empty, it ignored it"
        self.refresh()
        self.set_timer(CUE_SECONDS, self._end_cue)

    def _end_cue(self) -> None:
        self._cue = None
        self.tooltip = None
        self.refresh()

    def render_lines(self, crop: Region) -> list[Strip]:
        """Textual's toast, with the dismiss glyph drawn in its right padding on its first
        row: ``✕``, or a copy's cue (:data:`CUES`)."""
        lines = super().render_lines(crop)
        column = self.outer_size.width - 1 - DISMISS // 2
        if crop.y != 0 or not lines or not crop.x <= column < crop.right:
            return lines
        glyph = CUES[self._cue] if self._cue else "✕"
        part = {"copied": "calm-toast--copied", "sent": "calm-toast--copied"}.get(
            self._cue or "", "calm-toast--failed" if self._cue else "calm-toast--dismiss"
        )
        at = column - crop.x
        first = lines[0]
        under = next(iter(first.crop(at, at + 1)), Segment(" "))
        style = (under.style or Style()) + self.get_component_rich_style(part, partial=True)
        lines[0] = Strip.join(
            [first.crop(0, at), Strip([Segment(glyph, style)], 1), first.crop(at + 1, crop.width)]
        )
        return lines


def _drop_guard(rack: ToastRack, guard: ToastHolder) -> None:
    _remove(guard)
    rack.display = any(True for _ in rack.app._notifications)


class MoreToasts(Static):
    """``+N more``: toasts waiting for a slot. A click dismisses those that wait."""

    DEFAULT_CLASSES = "-textual-system"

    def __init__(self, count: int) -> None:
        super().__init__()
        self.count = count

    @property
    def count(self) -> int:
        return self._count

    @count.setter
    def count(self, value: int) -> None:
        self._count = value
        self.update(f"+{value} more")

    def on_click(self, event: events.Click) -> None:
        event.stop()
        app = self.app
        live = list(app._notifications)
        for notification in live[: max(len(live) - MAX_SHOWN, 0)]:
            held(app).pop(notification.identity, None)
            app._unnotify(notification, refresh=False)
        app._refresh_notifications()


class ClickGuard(Static):
    """Where the last ``✕`` was, for :data:`GUARD_SECONDS` after it closed the last toast: a
    click of the same spam-clicking hand stops here instead of reaching the pane beneath."""

    DEFAULT_CLASSES = "-textual-system"

    def on_click(self, event: events.Click) -> None:
        event.stop()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        event.stop()
