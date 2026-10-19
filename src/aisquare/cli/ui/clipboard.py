"""Every copy in the TUI: OSC 52, the native clipboard beside it, and a toast that is true.

:class:`ClipboardApp` is the one ``copy_to_clipboard`` both apps run (the fleet's
``SelectionHost`` and ``aisquare watch``'s board), so Textual's own ``Input`` and
``TextArea`` copies take the same route as the app's. A copy that raises a toast goes
through :func:`copy_and_tell`, and the toast waits for the native write. It says
"copied" when that write took the text, or when OSC 52 was the only route and the
terminal is one known to take it. On Terminal.app or iTerm2, which drop OSC 52, it says
the copy did not land and how to copy with the terminal's own selection instead
(:func:`missed_copy`); on a terminal nobody can name — over SSH, behind a multiplexer —
it says the text was sent through the terminal, and what an empty paste would mean
(:func:`hedged_copy`). The native half is ``aisquare.core.clipboard``.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any, TypeVar

from textual.app import App
from textual.message import Message

from aisquare.core import clipboard

ReturnType = TypeVar("ReturnType")

MISSED_TIMEOUT = 12.0
"""Seconds a copy that did not land stays on screen: long enough to read the way round."""

HEDGED_TIMEOUT = 8.0
"""Seconds a copy only OSC 52 carried to an unnamed terminal stays on screen."""

_MISSED = {
    "Terminal.app": (
        "the copy did not reach the clipboard: Terminal.app ignores an app's clipboard"
        " writes. Hold Fn while you drag to select, then press Cmd+C."
    ),
    "iTerm2": (
        "the copy did not reach the clipboard: iTerm2 drops an app's clipboard writes"
        " unless Settings > General > Selection > 'Applications in terminal may access"
        " clipboard' is on. Or hold Option while you drag to select, then press Cmd+C."
    ),
}


def missed_copy(env: Mapping[str, str]) -> str | None:
    """What a copy only OSC 52 carried says on a terminal known to drop it; else ``None``."""
    terminal = clipboard.osc52_terminal(env)
    return _MISSED.get(terminal)


def hedged_copy(sent: str) -> str:
    """What a copy only OSC 52 carried says when nobody can name the terminal: it went,
    and whether it landed is the terminal's to say. ``sent`` names what went."""
    return (
        f"sent {sent} to your terminal's clipboard (OSC 52) — if paste is empty, your"
        " terminal ignores it (Terminal.app does)"
    )


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
    """

    @dataclass
    class Copied(Message):
        """A native write is over: raise the copy's toast with what became of it."""

        toast: str | None
        sent: str
        timeout: float | None
        landed: bool

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
        at once with none to wait for (:meth:`_tell_copy`). Without one nothing is said
        either way, as Textual's own copies say nothing. ``sent`` names the text where
        "copied" cannot be said (:func:`hedged_copy`): its length, unless given.
        """
        if not text:
            return
        super().copy_to_clipboard(text)
        told = (toast, characters(text) if sent is None else sent, timeout)
        if clipboard.cached_writer() is None:  # known: no native route here
            self._tell_copy(*told, landed=False)
            return
        clipboard.submit(text, partial(self._native_done, *told))

    def _native_done(
        self, toast: str | None, sent: str, timeout: float | None, landed: bool
    ) -> None:
        """On the clipboard thread: hand the outcome to the event loop, if the app is still
        there to hear it. ``post_message`` is thread-safe, and says ``False`` once the app
        has closed; a loop that closed in between raises, and the toast goes with it."""
        with contextlib.suppress(RuntimeError):
            self.post_message(self.Copied(toast, sent, timeout, landed))

    def on_clipboard_app_copied(self, message: ClipboardApp.Copied) -> None:
        self._tell_copy(message.toast, message.sent, message.timeout, landed=message.landed)

    def _tell_copy(
        self, toast: str | None, sent: str, timeout: float | None, *, landed: bool
    ) -> None:
        """Raise ``toast``, or what became of the copy instead, when one was asked for.

        A copy the native tool did not take had OSC 52 alone, and only a terminal known
        to take it (``core.clipboard.osc52_terminal``) makes that "copied". One known to
        drop it gets the warning that nothing landed (:func:`missed_copy`); one nobody
        can name, the hedge (:func:`hedged_copy`).
        """
        if toast is None:
            return
        terminal = "accepts" if landed else clipboard.osc52_terminal(os.environ)
        if terminal == "accepts":
            self.notify(toast, timeout=timeout, markup=False)
        elif terminal == "unknown":
            self.notify(hedged_copy(sent), timeout=HEDGED_TIMEOUT, markup=False)
        else:
            self.notify(_MISSED[terminal], severity="warning", timeout=MISSED_TIMEOUT, markup=False)


def copy_and_tell(
    app: App[Any], text: str, toast: str, *, sent: str | None = None, timeout: float | None = None
) -> None:
    """Copy ``text`` and raise ``toast`` once, or what became of the copy instead.

    ``sent`` names the text where "copied" cannot be said (:meth:`ClipboardApp.copy_to_clipboard`).
    An app that is not a :class:`ClipboardApp` copies Textual's way and says ``toast``.
    """
    if isinstance(app, ClipboardApp):
        app.copy_to_clipboard(text, toast=toast, sent=sent, timeout=timeout)
        return
    app.copy_to_clipboard(text)
    app.notify(toast, timeout=timeout, markup=False)
