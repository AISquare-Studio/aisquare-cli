"""Every copy in the TUI: OSC 52, the native clipboard beside it, and a toast that is true.

:class:`ClipboardApp` is the one ``copy_to_clipboard`` both apps run (the fleet's
``SelectionHost`` and ``aisquare watch``'s board), so Textual's own ``Input`` and
``TextArea`` copies take the same route as the app's. A copy that raises a toast goes
through :func:`copy_and_tell`, and the toast waits for the native write. It says
"copied" when that write took the text, or when OSC 52 was the only route and the
terminal is one known to take it. On Terminal.app, which never implemented OSC 52, it
says the copy did not land and how to copy with the terminal's own selection instead.
Elsewhere it says the text was sent through the terminal and what an empty paste would
mean: on iTerm2, whose own setting decides and cannot be seen from here, which setting
to turn on; on a terminal nobody can name — over SSH, behind a multiplexer — that the
terminal ignores it (:func:`copy_toast`). The native half is ``aisquare.core.clipboard``.

ONE copy (R13, #146): :meth:`ClipboardApp.copy_text` is what every copy in the app runs —
Textual's own (``Input``, ``TextArea``, a screen's ctrl+c, through
:meth:`ClipboardApp.copy_to_clipboard`), the toasts', the board's, the panes', Remote's —
and it tells its caller a ``core.clipboard.CopyResult``: copied, only sent, or failed and
why, so no surface says "copied" for a copy that did not land.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any, TypeVar

from textual import events
from textual.app import App
from textual.css.query import NoMatches
from textual.message import Message
from textual.notifications import SeverityLevel
from textual.widgets._toast import ToastRack

from aisquare.cli.ui import toasts
from aisquare.core import clipboard

ReturnType = TypeVar("ReturnType")

MISSED_TIMEOUT = 12.0
"""Seconds a copy that did not land stays on screen: long enough to read the way round."""

HEDGED_TIMEOUT = 8.0
"""Seconds a copy only OSC 52 carried to an unnamed terminal stays on screen."""

# By core.terminal.Osc52: Terminal.app is the one terminal that drops OSC 52, iTerm2 the
# one that asks for a setting (tests/test_clipboard.py holds the table to it).
_MISSED = (
    "the copy did not reach the clipboard: Terminal.app ignores an app's clipboard"
    " writes. Hold Fn while you drag to select, then press Cmd+C."
)
_ASKS = (
    "if paste is empty, turn on iTerm2 > Settings > General > Selection > 'Applications"
    " in terminal may access clipboard', or hold ⌥ while you drag"
)


@dataclass(frozen=True)
class Toast:
    """What a copy says, once what became of it is known."""

    message: str
    severity: SeverityLevel
    timeout: float | None


def copy_toast(fate: clipboard.Osc52Fate, toast: str, sent: str, timeout: float | None) -> Toast:
    """What a copy says: ``toast`` (with ``timeout``) when it landed — ``fate`` is then
    ``"accepts"``, whether the native tool took it or a terminal known to take OSC 52
    did — else what the terminal makes of an OSC 52 write alone. ``sent`` names the text
    where "copied" cannot be said.

    The one place a copy's outcome becomes words: the app raises what it returns, and
    the tests assert on it, so the two cannot drift (review of #266, round 1).
    """
    if fate == "accepts":
        return Toast(toast, "information", timeout)
    if fate == "drops":
        return Toast(_MISSED, "warning", MISSED_TIMEOUT)
    sent_through = f"sent {sent} to your terminal's clipboard (OSC 52)"
    if fate == "asks":
        # iTerm2's setting cannot be seen from here, so it is never a flat "did not
        # reach": for the users who turned it on, that warned at every copy that landed
        # (review of #266, round 1).
        return Toast(f"{sent_through} — {_ASKS}", "information", MISSED_TIMEOUT)
    return Toast(
        f"{sent_through} — if paste is empty, your terminal ignores it (Terminal.app does)",
        "information",
        HEDGED_TIMEOUT,
    )


def result_toast(
    result: clipboard.CopyResult, toast: str, sent: str, timeout: float | None
) -> Toast:
    """What a copy that asked for ``toast`` says, once ``result`` is known: ``toast`` when it
    was copied; what the terminal makes of OSC 52 when it rests on that (:func:`copy_toast`);
    else that it could not copy, and why."""
    if result.ok:
        return Toast(toast, "information", timeout)
    if result.fate is not None:
        return copy_toast(result.fate, toast, sent, timeout)
    return Toast(f"could not copy {sent}: {result.reason}", "warning", MISSED_TIMEOUT)


Cue = Callable[[clipboard.CopyResult], None]
"""Told, on the event loop, what became of a copy."""


def characters(text: str) -> str:
    """``"N characters"``, or ``"1 character"``."""
    return f"{len(text)} character{'s' if len(text) != 1 else ''}"


class ClipboardApp(App[ReturnType]):
    """An app whose copies reach the system clipboard, and say so only when they can have.

    Textual's write is kept — the OSC 52, which is the only route over SSH, and
    ``App.clipboard``, which ``Input``'s paste reads — and on a local session the
    native tool runs too, off the event loop (``core.clipboard.submit``). The loop
    neither scans PATH nor starts a process: the clipboard thread finds the tool, and
    the loop reads only what it found (``core.clipboard.cached_writer``).

    Its notifications are asq's (``cli.ui.toasts``): top-right, three at a time, each
    with a dismiss column and text that copies on a drag (R13, #146).
    """

    DEFAULT_CSS = toasts.CSS

    async def on_event(self, event: events.Event) -> None:
        """Every event the driver hands in, on every screen, modal ones included: a press on
        what a fast click has just closed — a toast, asq's or Textual's own — is dropped
        before Textual builds a selection from its parent, which is gone
        (``toasts.pressed_on_a_pruned_widget``, #517)."""
        if isinstance(event, events.MouseDown) and not event.is_forwarded:
            try:
                screen = self.screen
            except Exception:  # no screen yet
                screen = None
            if screen is not None and toasts.pressed_on_a_pruned_widget(screen, event):
                return
        await super().on_event(event)

    def _refresh_notifications(self) -> None:
        """Show the notifications on the current screen as asq does (``toasts.show_calm``);
        Textual's own way, should the hooks that reads have moved (``toasts.CALM``)."""
        if not toasts.CALM:
            super()._refresh_notifications()
            return
        try:
            screen = self.screen
        except Exception:  # no screen yet, or none left: one will turn up, or never
            return
        try:
            rack = screen.get_child_by_type(ToastRack)
        except NoMatches:
            return
        self.call_later(toasts.show_calm, rack, self._notifications)

    @dataclass
    class Copied(Message):
        """A native write is over: tell the copy's caller what became of it."""

        written: clipboard.Written
        osc52: bool
        too_large: bool
        cue: Cue | None

    def copy_to_clipboard(
        self,
        text: str,
        *,
        toast: str | None = None,
        sent: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """Copy ``text`` to the clipboard by every route there is; ``toast`` once told.

        Nothing at all is written for ``""``: an OSC 52 with an empty payload CLEARS the
        terminal's clipboard, and no gesture in this app means that — Textual's
        ``screen.copy_text`` produced one whenever the screen's selections extracted as
        nothing (review of #135, finding 10). Every copy in the app goes through here,
        so the rule holds for the pane's own paths and Textual's alike.

        ``toast`` (with ``timeout``) is raised once, when the native write is over, or
        at once with none to wait for (:meth:`copy_text`). Without one nothing is said
        either way, as Textual's own copies say nothing. ``sent`` names the text where
        "copied" cannot be said (:func:`copy_toast`): its length, unless given.
        """
        if not text:
            return
        said = characters(text) if sent is None else sent
        cue = None if toast is None else partial(self._tell_copy, toast, said, timeout)
        self.copy_text(text, cue=cue)

    def copy_text(self, text: str, *, cue: Cue | None = None) -> None:
        """THE copy: ``text`` to the terminal's clipboard by OSC 52 at once, and to the
        native tools on the clipboard thread; ``cue`` told, on the loop, what became of it
        (``core.clipboard.verdict``). Nothing is written, or told, for ``""``.

        The OSC 52 goes at the gesture, as Textual's own did: ``App.clipboard`` (which
        ``Input``'s paste reads) holds the text, and the driver writes the sequence —
        wrapped for tmux's or screen's passthrough when one sits between
        (``core.clipboard.osc52_writes``), and none past what a terminal takes.
        """
        if not text:
            return
        osc52 = self._send_osc52(text)
        too_large = not clipboard.osc52_fits(text)
        if clipboard.cached_writer() is None:  # known: no native route here
            self._settle(clipboard.Written(False), osc52=osc52, too_large=too_large, cue=cue)
            return
        clipboard.copy_later(text, partial(self._native_done, osc52, too_large, cue))

    def _send_osc52(self, text: str) -> bool:
        """Hold ``text`` as ``App.clipboard`` and write its OSC 52; whether one went out."""
        self._clipboard = text
        writes = clipboard.osc52_writes(text, os.environ)
        driver = self._driver
        if driver is None or not writes:
            return False
        for sequence in writes:
            driver.write(sequence)
        return True

    def _native_done(
        self, osc52: bool, too_large: bool, cue: Cue | None, written: clipboard.Written
    ) -> None:
        """On the clipboard thread: hand the outcome to the event loop, if the app is still
        there to hear it. ``post_message`` is thread-safe, and says ``False`` once the app
        has closed; a loop that closed in between raises, and the cue goes with it."""
        with contextlib.suppress(RuntimeError):
            self.post_message(self.Copied(written, osc52, too_large, cue))

    def on_clipboard_app_copied(self, message: ClipboardApp.Copied) -> None:
        self._settle(
            message.written, osc52=message.osc52, too_large=message.too_large, cue=message.cue
        )

    def _settle(
        self, written: clipboard.Written, *, osc52: bool, too_large: bool, cue: Cue | None
    ) -> None:
        """Tell ``cue`` what became of the copy: a tool that took it is a copy; else it rests
        on OSC 52, and only a terminal known to take it makes that a copy
        (``core.clipboard.osc52_terminal``)."""
        if cue is None:
            return
        fate = clipboard.osc52_terminal(os.environ)
        cue(clipboard.verdict(written, osc52=osc52, fate=fate, too_large=too_large))

    def _tell_copy(
        self, toast: str, sent: str, timeout: float | None, result: clipboard.CopyResult
    ) -> None:
        """Raise ``toast``, or what became of the copy instead (:func:`result_toast`)."""
        told = result_toast(result, toast, sent, timeout)
        self.notify(told.message, severity=told.severity, timeout=told.timeout, markup=False)


def copy_and_tell(
    app: App[Any], text: str, toast: str, *, sent: str | None = None, timeout: float | None = None
) -> None:
    """Copy ``text`` and raise ``toast`` once, or what became of the copy instead.

    ``sent`` names the text where "copied" cannot be said (:meth:`ClipboardApp.copy_to_clipboard`).
    An app that is not a :class:`ClipboardApp` copies Textual's way and says ``toast``;
    for ``""`` nothing is written or said on either path, since an OSC 52 with an empty
    payload clears the terminal's clipboard.
    """
    if not text:
        return
    if isinstance(app, ClipboardApp):
        app.copy_to_clipboard(text, toast=toast, sent=sent, timeout=timeout)
        return
    app.copy_to_clipboard(text)
    app.notify(toast, timeout=timeout, markup=False)
