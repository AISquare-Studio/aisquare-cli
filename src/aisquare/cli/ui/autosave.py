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

The model is one value and one flag. :attr:`Autosave.latest` is what this
process last asked the file to hold (or read from it at start); ``dirty``
says a :meth:`remember` has not yet been handed to ``update_state``. The
drain converges: while dirty, hand ``latest`` over, and let ``update_state``
decide UNDER ITS LOCK whether the file already says so — this process's
belief about a key another process also writes is not the truth, and a
loop-side "nothing to write" once dropped a pick that ``board -w`` had
changed underneath. A refusal leaves the value dirty, so the next start or
the quit flush retries it; nothing is "known" that the file has not confirmed.

One writer per key at a time, in order: a burst of changes collapses into the
value at the end of it (:data:`Autosave.DEBOUNCE`), a value that arrives while
a write runs is written after it, never raced past it, and ``_running`` is
cleared in the same critical section that finds nothing to do — cleared later
it let a value queued in between strand with no drain to write it. The drain
is a daemon thread rather than a Textual worker so quit can join it, and a
thread stuck in an NFS lock cannot hold the interpreter's exit hostage.

Quit: :meth:`Autosave.flush_all` starts EVERY saver's drain first and joins
them all against ONE deadline (two savers used to freeze the quit twice
over), tells a drain that missed the deadline not to start another write (an
exit mid-write left a temp file and no ``state.json``), and returns what did
not land so the launcher can say so after the app has closed — a toast at
quit has no screen left to land on. The refusal that a running app can show
is delivered to the loop with ``call_soon_threadsafe`` in the loop's own
context (Textual's active app is a context variable; without it a detached
widget's ``notify`` printed a traceback under the shell prompt), never a
blocking call from the thread, which would deadlock a quit that is joining it.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
import weakref
from collections.abc import Callable
from typing import Any

from textual.app import App
from textual.timer import Timer
from textual.widget import Widget

from aisquare.core.state_file import LOCK_WAIT_S, StateUnwritableError, update_state

_by_app: weakref.WeakKeyDictionary[App[Any], weakref.WeakSet[Autosave]] = (
    weakref.WeakKeyDictionary()
)
"""Every saver an app owns, so quit can flush them all against one deadline. Weak on both sides:
a saver holds its host, and a strong list of savers would have kept every app ever built alive
through the theme saver's host — the app itself."""

_POLL_S = 0.005


class Autosave:
    """The saver for one ``state.json`` key, owned by the widget or app whose preference it is."""

    DEBOUNCE: float = 0.1
    """Seconds a value waits to be written: a held key is one write, not thirty a second."""

    JOIN_S: float = LOCK_WAIT_S + 1.0
    """How long quit waits for the last writes — all of them, together: the lock's own bound
    and a little."""

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
        self.what = what
        """How a message names the preference: "the theme", "the navigator's width"."""
        self._latest: object = initial
        """What this process last asked the file to hold — ``initial`` is what it read at
        start. Its best knowledge, not the truth: ``update_state`` reads the truth under
        the lock."""
        self._dirty = False
        """A ``remember`` not yet handed to ``update_state`` (or handed and refused)."""
        self._refusal: str | None = None
        """Why the last hand-over did not land, while the value is still dirty."""
        self._running = False
        self._in_flight: object = None
        """The value the drain is writing right now — named in quit's report when it gives up
        waiting for it."""
        self._closing = False
        """Quit's deadline has passed: a drain must not START another write."""
        self._guard = threading.Lock()
        """Around every field above the timer: the loop asks, the drain takes."""
        self._timer: Timer | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._context: contextvars.Context | None = None
        self._said = False
        app = host if isinstance(host, App) else host.app
        _by_app.setdefault(app, weakref.WeakSet()).add(self)

    # --- what the owner may ask ------------------------------------------------------------

    @property
    def latest(self) -> object:
        """What this process last asked the file to hold, or read from it at start."""
        return self._latest

    @property
    def dirty(self) -> bool:
        """Whether a remembered value has not landed on file (not yet, or refused)."""
        with self._guard:
            return self._dirty

    @property
    def unsaved(self) -> str | None:
        """One line saying what did not land and why, or ``None`` when everything did.

        Read after :meth:`flush`: a value still dirty (refused, or never started
        because quit closed the saver) and a value still in flight when quit
        gave up waiting are both "not saved" as far as the user can know.
        """
        with self._guard:
            if self._dirty:
                reason = self._refusal or "the write did not finish before the app closed"
                return f"{self.what} was not saved: {reason}"
            if self._closing and self._running:
                return (
                    f"{self.what} was not saved: the write of {self._in_flight!r} did not finish "
                    "before the app closed"
                )
            return None

    def remember(self, value: object) -> None:
        """Ask for ``value`` on file; after :data:`DEBOUNCE` the drain hands the last one over."""
        with self._guard:
            self._latest = value
            self._dirty = True
            self._refusal = None
        self._restart_timer()

    def wake(self) -> None:
        """Start a drain now if anything is dirty and none is running. Never blocks.

        (Not ``start``: the repo's config-write reachability guard builds its call
        graph by bare name, and a project function called ``start`` reaches
        ``save_config`` — tests/test_config_writes_stay_in_the_cli.py.)
        """
        self._stop_timer()
        with self._guard:
            if self._running or not self._dirty or self._closing:
                return
            self._running = True
        try:
            self._loop = asyncio.get_running_loop()
            self._context = contextvars.copy_context()
        except RuntimeError:  # no loop here (a bare unit test): refusals have nowhere to go
            self._loop = None
            self._context = None
        # Bound to a local before `.start()`: the repo's config-write reachability
        # guard builds its call graph by bare name, and `start` is a project
        # function's name too (tests/test_config_writes_stay_in_the_cli.py).
        thread = threading.Thread(target=self._drain, name=f"autosave:{self._key}", daemon=True)
        self._thread = thread
        thread.start()

    def join(self, timeout: float) -> bool:
        """Wait for a running drain, at most ``timeout`` seconds; whether it finished."""
        thread = self._thread
        if thread is None or not thread.is_alive():
            return True
        thread.join(max(0.0, timeout))
        return not thread.is_alive()

    def settled(self, timeout: float) -> bool:
        """Wait until nothing is dirty and no drain runs — or a refusal stands; whether it did.

        For tests, and anyone who must read the file after a gesture: the clock
        is not the signal, the saver's state is.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._guard:
                idle = not self._running and (not self._dirty or self._refusal is not None)
            if idle:
                return True
            time.sleep(_POLL_S)
        return False

    def close(self) -> None:
        """No more writes may START — for a quit whose deadline has passed."""
        with self._guard:
            self._closing = True

    def flush(self, timeout: float | None = None) -> str | None:
        """Write what is dirty NOW — for quit — and say what did not land.

        Starts the drain (a running one picks the value up itself), joins it
        for at most ``timeout`` (default :data:`JOIN_S`), and closes the saver
        when the deadline passes so no further write starts. Returns
        :attr:`unsaved`.
        """
        self.wake()
        if not self.join(self.JOIN_S if timeout is None else timeout):
            self.close()
        return self.unsaved

    @classmethod
    def flush_all(cls, app: App[Any], timeout: float | None = None) -> list[str]:
        """Flush every saver ``app`` owns against ONE deadline; the lines for what did not land.

        Every drain is started before any is joined, so two savers cost one
        wait, not two; a saver that misses the deadline is closed.
        """
        savers = list(_by_app.get(app, ()))
        for saver in savers:
            saver.wake()
        deadline = time.monotonic() + (cls.JOIN_S if timeout is None else timeout)
        unsaved: list[str] = []
        for saver in savers:
            if not saver.join(deadline - time.monotonic()):
                saver.close()
            line = saver.unsaved
            if line is not None:
                unsaved.append(line)
        return unsaved

    # --- the loop side ---------------------------------------------------------------------

    def _restart_timer(self) -> None:
        self._stop_timer()
        self._timer = self._host.set_timer(self.DEBOUNCE, self.wake, name=f"autosave:{self._key}")

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _refuse(self, why: str) -> None:
        """On the loop: say it once. A host already detached at quit has no screen; the
        launcher reads :attr:`unsaved` instead."""
        if self._said:
            return
        self._said = True
        try:
            self._host.notify(
                f"{why} — {self.what} could not be saved; it will be retried at the next change "
                "and at quit",
                severity="warning",
                timeout=8,
                markup=False,
            )
        except Exception:
            return

    # --- the drain's thread ----------------------------------------------------------------

    def _drain(self) -> None:
        """Hand ``latest`` over while it is dirty; stop at the first refusal, or when clean.

        ``_running`` is cleared in the SAME critical section that finds nothing
        to do — cleared afterwards, a value queued in between saw a drain
        "running" that was about to exit, and stranded. A refusal leaves the
        value dirty for the next wake or the quit flush (a newer value queued
        meanwhile is tried at once), and is said once on the loop.
        """
        try:
            while True:
                with self._guard:
                    if not self._dirty or self._closing:
                        self._running = False
                        return
                    value = self._latest
                    self._dirty = False
                    self._in_flight = value
                try:
                    update_state(self._key, value)
                except StateUnwritableError as exc:
                    why = str(exc)
                except Exception as exc:
                    # ``update_state`` promises only StateUnwritableError; anything
                    # else is a bug, and a bug is better read in a toast than in a
                    # traceback garbling a TUI from its excepthook.
                    why = f"{self._key} could not be saved: {exc!r}"
                else:
                    with self._guard:
                        self._in_flight = None
                    continue
                with self._guard:
                    self._in_flight = None
                    if not self._dirty:  # nothing newer arrived: keep this value for a retry
                        self._dirty = True
                        self._refusal = why
                        self._running = False
                        self._tell(self._refuse, why)
                        return
                self._tell(self._refuse, why)
        except BaseException as exc:
            with self._guard:
                self._running = False
                if not self._dirty:  # keep the value it was writing for a retry
                    self._dirty = True
                    self._refusal = f"{self._key} could not be saved: {exc!r}"
                self._in_flight = None
            raise

    def _tell(self, callback: Callable[[str], None], arg: str) -> None:
        """Hand ``callback`` to the loop without waiting for it (a quit joining this thread must
        not be waited on in turn), in the loop's own context so Textual's active app is set. A
        loop that has gone away has nobody to tell."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(callback, arg, context=self._context)
        except RuntimeError:
            return
