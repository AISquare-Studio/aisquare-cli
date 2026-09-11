"""Shared capture results: one bounded cache and one single-flight gate.

Two mechanisms, one file, because they answer the same question from opposite
sides. Several browsers watch the same pane at once, and the office must not
pay for the same frame N times:

* :class:`FrameCache` answers "has this exact frame already been captured
  recently?" — the rate limit and the backpressure store in one. Entries are
  immutable frames keyed by pane identity, lifecycle generation, requested
  offset and viewport hint, so a frame captured for one client is the same
  object handed to the next.
* :class:`SingleFlight` answers "is that capture happening right now?" — the
  second client waits on the first client's work instead of starting its own.

**Generation is part of the key, not a field beside it.** A recycled pane is a
different target with the same name, so a frame captured before the recycle can
never be found by a lookup made after it: there is no code path that compares
generations and decides, because a mismatched generation simply does not hash to
a stored entry. :meth:`FrameCache.invalidate` exists for the other direction —
dropping what is known to be obsolete when P03 or P05 says the lifecycle moved.

**Last-good is narrower than cached.** A frame carrying a
:class:`~aisquare.office.models.ServiceError` is still cached, because serving it
back inside the rate-limit window is exactly the backpressure the plan asks for;
it is *not* eligible as last-good, because a stale envelope built from a stale
envelope would launder a failure into evidence. :meth:`FrameCache.latest_good`
returns only frames that came from a real capture.

**The single flight owns the thread, and the waiter owns the deadline.** tmux's
own ``_COMMAND_TIMEOUT`` is 30 seconds — sixty times Office's 500 ms observation
budget — so a caller that simply waits for ``capture`` has no budget at all. The
work runs on a daemon thread and every waiter stops waiting at *its* deadline,
which is the same bound :mod:`aisquare.office.observe` enforces and the same
honest description: the wait stops, the tmux process does not.

What this file adds over that is a bound on the damage. The in-flight entry is
removed by the worker itself, in a ``finally``, so it lives exactly as long as
the tmux command does. A pane that has stopped answering therefore holds **one**
abandoned thread and **one** abandoned tmux process, not one per request: the
next caller joins the stuck flight and gives up on it rather than starting a
second. At the 10 fps visible-capture ceiling the difference is one stuck
process versus three hundred in the thirty seconds before tmux's own timeout
ends the first.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import TypeVar, cast

from aisquare.office.models import TerminalFrame, TerminalTarget

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class FrameKey:
    """What makes two capture requests the same piece of work.

    The requested offset rather than the effective one, deliberately. tmux
    reports the effective offset only *after* the capture, and a lookup has to
    happen before it, so keying on the effective value would be a key that
    cannot be computed when it is needed. The consequence is visible and
    acceptable: two requests that clamp to the same place (5 000 and 9 000 lines
    above a 900-line history) occupy two entries holding equal frames.
    """

    agent_id: str
    session_id: str | None
    generation: int
    scrollback: int
    viewport_height: int | None

    @classmethod
    def of(
        cls, target: TerminalTarget, *, scrollback: int, viewport_height: int | None
    ) -> FrameKey:
        """The key for one request against one resolved target."""
        return cls(
            agent_id=target.agent_id,
            session_id=target.session_id,
            generation=target.generation,
            scrollback=scrollback,
            viewport_height=viewport_height,
        )


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One stored frame and when it was stored, on the monotonic clock.

    Monotonic because every use of this timestamp is an age compared against a
    budget, and a wall clock corrected backwards mid-session would make a frame
    captured a second ago look newer than the rate limit allows.
    """

    frame: TerminalFrame
    stored_at: float
    good: bool
    """Whether this came from a real capture, and may therefore be shown as
    last-good under a later failure."""


class FrameCache:
    """A bounded, thread-safe store of immutable frames, newest-eviction-last.

    Thread-safe because the reader runs on a worker pool: two request threads
    can be capturing two panes and storing the results at the same instant. The
    lock is held only around dictionary operations — never across a tmux call.
    """

    def __init__(self, *, max_entries: int = 64) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: dict[FrameKey, CacheEntry] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def get(self, key: FrameKey, *, max_age_s: float, now: float) -> TerminalFrame | None:
        """The stored frame for ``key`` when it is younger than ``max_age_s``.

        This is the rate limit: within the window a caller is handed the frame
        that already exists rather than starting a capture, which is what keeps
        N browsers watching one pane to one capture per window.
        """
        with self._lock:
            entry = self._entries.get(key)
        if entry is None or now - entry.stored_at > max_age_s:
            return None
        return entry.frame

    def latest_good(self, agent_id: str, generation: int) -> TerminalFrame | None:
        """The newest real capture for this agent *at this generation*, if any.

        Generation-scoped because a frame from the pane that used to have this
        name is not this pane's last-good state; showing it under a failure
        would be the recycled-pane confusion arriving by a side door.
        """
        with self._lock:
            entries = list(self._entries.items())
        for key, entry in reversed(entries):
            if key.agent_id == agent_id and key.generation == generation and entry.good:
                return entry.frame
        return None

    def put(self, key: FrameKey, frame: TerminalFrame, *, now: float) -> None:
        """Store ``frame``, evicting the least recently stored entry if full."""
        entry = CacheEntry(frame=frame, stored_at=now, good=_is_real_capture(frame))
        with self._lock:
            self._entries.pop(key, None)
            self._entries[key] = entry
            while len(self._entries) > self._max_entries:
                oldest = next(iter(self._entries))
                del self._entries[oldest]

    def invalidate(self, agent_id: str, *, generation: int | None = None) -> int:
        """Drop this agent's frames; with ``generation``, only older ones.

        Returns how many entries went, so a caller that wants to publish "the
        view you had is gone" can tell whether anything was actually dropped.
        """
        with self._lock:
            doomed = [
                key
                for key in self._entries
                if key.agent_id == agent_id and (generation is None or key.generation < generation)
            ]
            for key in doomed:
                del self._entries[key]
        return len(doomed)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def _is_real_capture(frame: TerminalFrame) -> bool:
    """Whether this frame is evidence rather than an envelope explaining its absence."""
    return frame.error is None and frame.source == "pane" and not frame.stale


class FlightTimeout(TimeoutError):
    """A waiter's deadline passed before the shared work finished.

    Deliberately not an answer about the work: the capture may still be running,
    may still succeed, and its result may still reach the cache. All this says is
    that *this* caller stopped waiting.
    """


class _Flight:
    """One piece of shared work and its result."""

    __slots__ = ("done", "error", "value")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: object = None
        self.error: BaseException | None = None


class SingleFlight:
    """One execution per key, however many callers ask for it.

    The first caller starts a daemon thread; every other caller for the same key
    waits on the same event and receives the same result object. Frames are
    immutable, so sharing one is sharing a value rather than a race.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flights: dict[Hashable, _Flight] = {}

    @property
    def in_flight(self) -> int:
        """How many pieces of work are running right now."""
        with self._lock:
            return len(self._flights)

    def run(self, key: Hashable, work: Callable[[], _T], *, deadline_s: float) -> _T:
        """``work()``'s result, shared with everyone else asking for ``key``.

        Raises :class:`FlightTimeout` when this caller's deadline passes first,
        and re-raises the worker's exception — the same object to every waiter —
        when the work failed.
        """
        with self._lock:
            flight = self._flights.get(key)
            leader = flight is None
            if flight is None:
                flight = _Flight()
                self._flights[key] = flight

        if leader:
            thread = threading.Thread(
                target=self._execute,
                args=(key, flight, work),
                name="office-terminal-capture",
                daemon=True,
            )
            thread.start()

        if not flight.done.wait(max(deadline_s, 0.0)):
            raise FlightTimeout(f"capture did not answer within {deadline_s:.3f}s")
        if flight.error is not None:
            raise flight.error
        return cast(_T, flight.value)

    def _execute(self, key: Hashable, flight: _Flight, work: Callable[[], _T]) -> None:
        """Run the work, then retire the flight — in that order, always.

        The entry is removed *before* the event is set so that a waiter which
        wakes and immediately asks again starts a new flight rather than joining
        the finished one. It is removed in a ``finally`` so a raising worker
        cannot leave a key permanently occupied, which would wedge that pane for
        the life of the process.
        """
        try:
            flight.value = work()
        except BaseException as exc:
            flight.error = exc
        finally:
            with self._lock:
                if self._flights.get(key) is flight:
                    del self._flights[key]
            flight.done.set()


__all__ = [
    "CacheEntry",
    "FlightTimeout",
    "FrameCache",
    "FrameKey",
    "SingleFlight",
]
