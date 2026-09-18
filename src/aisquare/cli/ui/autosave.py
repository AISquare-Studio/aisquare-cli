"""A preference's save for a TUI: debounced, off the event loop, said once when refused.

Two preferences autosave from the fleet UI and the board — the theme on every
picker highlight (at key autorepeat), the navigator's width on every settled
gesture — and each save is a lock, a read, a write, an fsync and a rename of
``state.json``, with a bounded wait for the lock in front. On the Textual
event loop that is a freeze: the agent's pane stops painting and keys queue
for as long as the lock is contended (``core.state_file.LOCK_WAIT_S``), and a
``flock`` stuck in an NFS RPC is not bounded by that wait at all. The two TUI
callers need nothing back from a save but its refusal, which they toast, so
the save belongs on a worker thread.

One writer per key at a time, in order: a burst of changes collapses into the
value at the end of it (:data:`Autosave.DEBOUNCE`), the worker drains whatever
is due when it finishes, and a value that arrives while it runs is written
after it, never raced past it. At quit the pending value is written on the
calling thread — no worker can outlive the app, and a ``>`` pressed inside the
debounce before ``q`` is a preference the user expressed.

The refusal is said once per session and names what refused
(``StateUnwritableError``'s message); after it the saver keeps trying, since a
held lock clears and a fixed file is fixed, and off the loop a retry costs
nothing the user can feel.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from textual.app import App
from textual.timer import Timer
from textual.widget import Widget

from aisquare.core.state_file import StateUnwritableError, update_state


class Autosave:
    """The saver for one ``state.json`` key, owned by the widget or app whose preference it is."""

    DEBOUNCE: float = 0.1
    """Seconds a value waits to be written: a held key is one write, not thirty a second."""

    def __init__(
        self,
        host: Widget | App[Any],
        key: str,
        *,
        what: str,
        on_saved: Callable[[object], None] | None = None,
    ) -> None:
        self._host = host
        """The widget or app that owns the preference: its timers, its workers, its toasts."""
        self._key = key
        self._what = what
        """How the toast names the preference: "the theme", "the navigator's width"."""
        self._on_saved = on_saved
        """Told, on the event loop, each value the file now holds."""
        self._pending: object = None
        self._due = False
        self._running = False
        self._guard = threading.Lock()
        """Around ``_pending`` / ``_due`` / ``_running``: the loop queues, the worker drains."""
        self._timer: Timer | None = None
        self._refused = False

    def remember(self, value: object) -> None:
        """Queue ``value``; after :data:`DEBOUNCE` the last value of the burst is written."""
        with self._guard:
            self._pending, self._due = value, True
        self._restart_timer()

    def cancel(self) -> None:
        """Nothing to write after all — the value is back to what the file already says."""
        with self._guard:
            self._due = False
        self._stop_timer()

    def flush(self) -> None:
        """Write what is due NOW, on this thread — for quit, when no worker can outlive the app."""
        self._stop_timer()
        with self._guard:
            if not self._due:
                return
            value, self._due = self._pending, False
        try:
            update_state(self._key, value)
        except StateUnwritableError:
            return  # quitting: nobody left to tell
        self._saved(value)

    # --- the loop side ---------------------------------------------------------------------

    def _restart_timer(self) -> None:
        self._stop_timer()
        self._timer = self._host.set_timer(self.DEBOUNCE, self._start, name=f"autosave:{self._key}")

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _start(self) -> None:
        """The timer fired: hand the drain to a worker, unless one is already draining."""
        self._timer = None
        with self._guard:
            if self._running or not self._due:
                return
            self._running = True
        self._host.run_worker(
            self._drain,
            name=f"autosave:{self._key}",
            group=f"autosave:{self._key}",
            thread=True,
            exit_on_error=False,
        )

    def _saved(self, value: object) -> None:
        if self._on_saved is not None:
            self._on_saved(value)

    def _refuse(self, why: str) -> None:
        if self._refused:
            return
        self._refused = True
        self._host.notify(
            f"{why} — {self._what} will not be remembered",
            severity="warning",
            timeout=8,
            markup=False,
        )

    # --- the worker side -------------------------------------------------------------------

    def _drain(self) -> None:
        """On the worker thread: write every value that is due, in order, until none is."""
        app = self._host.app
        while True:
            with self._guard:
                if not self._due:
                    self._running = False
                    return
                value, self._due = self._pending, False
            try:
                update_state(self._key, value)
            except StateUnwritableError as exc:
                self._tell(app.call_from_thread, self._refuse, str(exc))
            else:
                self._tell(app.call_from_thread, self._saved, value)

    @staticmethod
    def _tell(
        call_from_thread: Callable[..., object], callback: Callable[..., None], arg: object
    ) -> None:
        """Run ``callback`` on the event loop; an app that has gone away has nobody to tell."""
        try:
            call_from_thread(callback, arg)
        except RuntimeError:
            return
