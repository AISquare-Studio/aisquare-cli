"""A preference's save for a TUI: debounced, off the event loop, said once when refused.

Two preferences autosave from the fleet UI and the board — the theme on every
picker highlight (at key autorepeat), the navigator's width on every settled
gesture — and each save is a lock, a read, a write, an fsync and a rename of
``state.json``, with a bounded wait for the lock in front. On the Textual
event loop that is a freeze: the agent's pane stops painting and keys queue
for as long as the lock is contended (``core.state_file.LOCK_WAIT_S``), and a
``flock`` stuck in an NFS RPC is not bounded by that wait at all. The two TUI
callers need nothing back from a save but its refusal, which they toast, so
the save belongs on a thread of its own.

What the saver knows is :attr:`Autosave.latest`: the value the file holds OR
IS ABOUT TO — the last one handed over, queued or in flight. That is what
"already saved" has to mean to a caller. A dedupe against the last CONFIRMED
write let a step back during a slow write leave the in-flight width on file
(the screen showed 30, the file said 34), and let a reset with nothing on file
keep a width the user had just reset.

One writer per key at a time, in order: a burst of changes collapses into the
value at the end of it (:data:`Autosave.DEBOUNCE`), the drain writes whatever
is due when it finishes a write, and a value that arrives while it runs is
written after it, never raced past it — quit included: :meth:`flush` hands the
due value to the running drain (or starts one) and joins it with a bound,
instead of writing on the loop thread and racing the drain for the lock. The
drain is a daemon thread rather than a Textual worker so quit can join it
synchronously and a thread stuck in an NFS lock cannot hold the interpreter's
exit hostage.

The refusal is said once per session and names what refused
(``StateUnwritableError``'s message), delivered to the loop with
``call_soon_threadsafe`` — never a blocking call from the thread, which would
deadlock a quit that is joining it. After a refusal the saver keeps trying,
since a held lock clears and a fixed file is fixed, and off the loop a retry
costs nothing the user can feel.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

from textual.app import App
from textual.timer import Timer
from textual.widget import Widget

from aisquare.core.state_file import LOCK_WAIT_S, StateUnwritableError, update_state


class Autosave:
    """The saver for one ``state.json`` key, owned by the widget or app whose preference it is."""

    DEBOUNCE: float = 0.1
    """Seconds a value waits to be written: a held key is one write, not thirty a second."""

    JOIN_S: float = LOCK_WAIT_S + 1.0
    """How long quit waits for the last write: the lock's own bound, and a little."""

    def __init__(
        self,
        host: Widget | App[Any],
        key: str,
        *,
        what: str,
        initial: object = None,
    ) -> None:
        self._host = host
        """The widget or app that owns the preference: its timers and its toasts."""
        self._key = key
        self._what = what
        """How the toast names the preference: "the theme", "the navigator's width"."""
        self._latest: object = initial
        """The value the file holds or is about to: ``initial`` (what the file said at start),
        then the last value handed over — queued or in flight."""
        self._on_file: object = initial
        """The last value a write CONFIRMED; set by the drain under the guard."""
        self._pending: object = None
        self._due = False
        self._running = False
        self._guard = threading.Lock()
        """Around ``_pending`` / ``_due`` / ``_running`` / ``_on_file``: the loop queues, the
        drain takes."""
        self._timer: Timer | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._refused = False

    @property
    def latest(self) -> object:
        """What the file holds or is about to hold — the value to compare a new one against."""
        return self._latest

    def remember(self, value: object) -> None:
        """Queue ``value`` — unless it is what the file holds or is about to hold.

        Back to what the file already says, with nothing in flight, drops the
        queued value: there is nothing to write. With a write in flight the
        value is queued anyway, so it lands AFTER the one being written.
        """
        if value == self._latest:
            return
        self._latest = value
        with self._guard:
            if value == self._on_file and not self._running:
                self._due = False
                self._stop_timer()
                return
            self._pending, self._due = value, True
        self._restart_timer()

    def flush(self, timeout: float | None = None) -> None:
        """Write what is due NOW — for quit.

        A running drain picks the value up itself; otherwise one is started.
        Either way the drain is joined, for at most ``timeout`` (default
        :data:`JOIN_S`), so the app does not exit mid-write and never waits
        forever on a lock that will not come.
        """
        self._stop_timer()
        self._start()
        self.wait(self.JOIN_S if timeout is None else timeout)

    def wait(self, timeout: float) -> bool:
        """Wait for the drain to finish, at most ``timeout`` seconds; whether it did."""
        thread = self._thread
        if thread is None or not thread.is_alive():
            return True
        thread.join(timeout)
        return not thread.is_alive()

    # --- the loop side ---------------------------------------------------------------------

    def _restart_timer(self) -> None:
        self._stop_timer()
        self._timer = self._host.set_timer(self.DEBOUNCE, self._start, name=f"autosave:{self._key}")

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _start(self) -> None:
        """The timer fired (or quit is flushing): start a drain, unless one is already running."""
        self._timer = None
        with self._guard:
            if self._running or not self._due:
                return
            self._running = True
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop here (a bare unit test): refusals have nowhere to go
            self._loop = None
        # Bound to a local before `.start()`: the repo's config-write reachability
        # guard builds its call graph by bare name, and `start` is a project
        # function's name too (tests/test_config_writes_stay_in_the_cli.py).
        thread = threading.Thread(target=self._drain, name=f"autosave:{self._key}", daemon=True)
        self._thread = thread
        thread.start()

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

    # --- the drain's thread ----------------------------------------------------------------

    def _drain(self) -> None:
        """Write every value that is due, in order, until none is; then let a later drain start.

        ``_running`` is reset in a ``finally``: an exception out of here used to
        leave it set, and every later start returned early — the preference
        silently stopped saving for the rest of the session.
        """
        try:
            while True:
                with self._guard:
                    if not self._due:
                        return
                    value, self._due = self._pending, False
                try:
                    update_state(self._key, value)
                except StateUnwritableError as exc:
                    self._tell(self._refuse, str(exc))
                except Exception as exc:
                    # ``update_state`` promises only StateUnwritableError; anything
                    # else is a bug, and a bug is better read in a toast than in a
                    # traceback garbling a TUI from its excepthook.
                    self._tell(self._refuse, f"{self._key} could not be saved: {exc!r}")
                else:
                    with self._guard:
                        self._on_file = value
        finally:
            with self._guard:
                self._running = False

    def _tell(self, callback: Callable[[str], None], arg: str) -> None:
        """Hand ``callback`` to the loop without waiting for it: a quit joining this thread must
        not be waited on in turn. A loop that has gone away has nobody to tell."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(callback, arg)
        except RuntimeError:
            return
