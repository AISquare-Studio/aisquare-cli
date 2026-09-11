"""Bounded, read-only capture of a pane the *server* chose.

:class:`TerminalReader` implements P01's ``TerminalSource``. It observes fleet
panes through the existing :class:`aisquare.core.tmux.TmuxServer` and returns
immutable :class:`~aisquare.office.models.TerminalFrame` values. It sends no
keys, pastes nothing, resizes nothing, spawns nothing and interprets no prompt:
:class:`CaptureServer` — the only tmux surface this module is typed against —
has exactly two methods, so there is no mutating call for this code to make.

**The browser never names a pane.** What crosses the wire is a bounded integer
and a viewport hint; what reaches tmux is a socket and a pane id resolved *here*
from a :class:`~aisquare.office.models.TerminalTarget` that P03/P07 minted. An
``agent_id`` shaped like a tmux target (``%14``, ``=asq-…``, a path) is refused
by :meth:`TerminalReader._reject_raw_target` before any resolver is consulted,
so a resolver that was too trusting still could not be handed one.

**Scrollback for a live Claude pane is structurally empty, and says so.**
Measured by P03 against all five live Claude panes on this machine: every one
reports ``alternate_on=1`` and ``history_size=0``. The Claude Code TUI owns
tmux's alternate screen, and the alternate screen has no scrollback buffer at
all — so there is nothing above the live screen to fetch, at any offset, with
any parameter. A request for history against such a pane returns the live screen
plus a ``ServiceError`` saying *that* (:data:`ALTERNATE_SCREEN_DETAIL`), because
the alternative — an empty result with no explanation — invites a user to spend
the afternoon tuning an offset that was never going to matter. The distinction
is read from ``alternate_on``, never guessed: a pane not on the alternate screen
with an empty history is simply a pane nothing has scrolled off yet, that may
fill later, and it gets no error at all.

**Dead, gone, unknown and unavailable are four different answers.**

* ``dead`` — tmux reports ``pane_dead``. A real frame, with the final lines and
  the exit status preserved. Not an error: the process ended, which is news
  rather than a failure.
* ``gone`` — the server answered and does not have this pane. :class:`TargetGone`,
  because there is no frame to return and never will be. Never satisfied from a
  similarly named pane.
* ``unknown`` — nothing answered, or this caller's deadline passed. A frame
  carrying ``stale=True`` over the last real capture when there is one, and an
  empty ``source="none"`` frame when there is not. Crucially *not* ``gone``:
  ``TmuxServer.answers()`` is the one call that separates a server which said
  "no such pane" from a server that said nothing, and stopping a wait is not
  evidence about a pane.
* validation — an oversized request or a raw target raises before tmux is
  called at all.

**Budgets are enforced here.** ``core.tmux``'s ``_COMMAND_TIMEOUT`` is 30
seconds, so the wait is bounded by :class:`SingleFlight` at
:attr:`CaptureLimits.deadline_s` instead. As in :mod:`aisquare.office.observe`,
that stops the waiting and not the tmux process; the single flight bounds the
cost to one abandoned process per pane rather than one per request. Killing the
process would mean owning a ``subprocess.run`` here, which is a new spawn site
and needs a ruling in ``core.spawn.SEAMS`` — a registry this packet does not
own. The handoff carries the proposed entry and the measurement behind it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol

from aisquare.core.tmux import Capture, TmuxError, TmuxServer
from aisquare.office.config import OfficeConfig
from aisquare.office.models import (
    ServiceError,
    ServiceErrorCode,
    TerminalFrame,
    TerminalTarget,
)
from aisquare.office.observe import terminal_facts
from aisquare.office.ports import Clock
from aisquare.office.terminal_cache import FlightTimeout, FrameCache, FrameKey, SingleFlight

ALTERNATE_SCREEN_DETAIL: Final = (
    "this pane runs a full-screen program on the terminal's alternate screen, which has no "
    "scrollback buffer: there is no history above the live screen to fetch, at any offset"
)
"""Why a scrollback request against a Claude pane comes back with the live screen.

Named rather than inlined so a test can assert the exact sentence, and so the
one place that explains a structural emptiness cannot drift into sounding like a
parameter the caller got wrong.
"""

TMUX_UNAVAILABLE_DETAIL: Final = (
    "the tmux server did not answer, so this pane's state is unknown — an unreachable "
    "server is not evidence that the agent exited"
)

TMUX_TIMEOUT_DETAIL: Final = (
    "the capture did not finish within the read budget, so this pane's state is unknown — "
    "the wait stopped, which says nothing about the pane"
)

_TARGET_SIGILS: Final = frozenset("%$@=:./\\ \t\n\r")
"""Characters that could make an identifier addressable as something else.

``%``, ``$``, ``@`` and ``=`` are tmux's own target sigils; ``:`` and ``.`` are
its window and pane separators (``core.tmux._UNTARGETABLE``); ``/`` and ``\\``
are the beginning of a path. Whitespace is here because an identifier with a
space in it is an identifier that could become two arguments.
"""

_MAX_IDENTIFIER_CHARS: Final = 128

_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1a\x1c-\x1f\x7f]")
"""Every C0 control and DEL except TAB (``\\x09``) and ESC (``\\x1b``).

ESC survives because the frame keeps its SGR colours; TAB survives because it is
ordinary pane content. Everything else — NUL, a bare carriage return, the shift
codes — is removed rather than bounded: none of it means anything in a line that
tmux has already split, and a NUL travelling toward a browser inside a JSON
string is a decoding problem waiting to be somebody's bug.
"""

_COMPLETE_CSI = re.compile(r"\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]")


@dataclass(frozen=True, slots=True)
class CaptureLimits:
    """Every ceiling one capture observes. Starting points, not measured capacity.

    Owned here rather than added to ``OfficeConfig`` because P08 may not edit
    that module; :meth:`from_config` binds the one value the configuration
    already carries (``capture_fps``) and the handoff proposes the rest.
    """

    max_lines: int = 500
    """Lines returned in one frame — the bottom ones, which is where a terminal's
    meaning is."""

    max_line_chars: int = 1000
    """One line, SGR escapes included. A pane is at most a few hundred columns
    wide; the rest of this budget is colour."""

    max_total_chars: int = 200_000
    """The whole frame. A viewport hint is advisory and cannot lift this."""

    max_scrollback: int = 10_000
    """The deepest offset a caller may ask for. Over this is a validation error,
    before tmux is called."""

    max_viewport_height: int = 500

    deadline_s: float = 0.5
    """How long one capture is waited on, matching the observation budget. The
    wait ends; the tmux process is not killed — see the module docstring."""

    min_interval_s: float = 0.1
    """At most one capture per key per window: the 10 fps ceiling, as a period."""

    cache_entries: int = 64

    @classmethod
    def from_config(cls, config: OfficeConfig) -> CaptureLimits:
        """Take the capture rate from the resolved configuration; keep the rest."""
        return cls(min_interval_s=1.0 / config.capture_fps)


@dataclass(frozen=True, slots=True)
class PaneLocation:
    """Where a resolved target actually lives. Internal; never serialized.

    ``generation`` is the resolver's own view of the pane's lifecycle, compared
    against the target's so a capture prepared for the pane that used to hold
    this name is refused rather than answered from the one that holds it now.
    """

    socket: str
    pane_id: str
    generation: int = 0


class TargetResolver(Protocol):
    """Server-side resolution of an opaque target into a pane.

    The whole reason a browser cannot name a pane: this is the only thing that
    produces a tmux target, it is supplied by the application, and it answers
    from P03's observations rather than from the request.
    """

    def locate(self, target: TerminalTarget) -> PaneLocation | None:
        """Where this target is now, or ``None`` when it is not there."""


class CaptureServer(Protocol):
    """The tmux surface this module is allowed to have — and all of it.

    Two methods, both read-only. ``send_keys``, ``send_literal``, ``paste`` and
    ``resize`` exist on :class:`aisquare.core.tmux.TmuxServer` and are absent
    here, so the read adapter has no name to call them by: the proof that this
    packet never mutates a pane is structural rather than a promise.
    """

    def capture(
        self, pane_id: str, *, scrollback: int = 0, height: int | None = None
    ) -> Capture: ...

    def answers(self) -> bool: ...


class TerminalReadError(Exception):
    """A capture could not be attempted, or must not be answered."""


class TargetRejected(TerminalReadError):
    """The identifier is shaped like something other than an identifier."""


class RequestTooLarge(TerminalReadError):
    """A bound was exceeded. Raised before tmux is called."""


class TargetGone(TerminalReadError):
    """The server answered and does not have this pane — now, or ever."""


class StaleGeneration(TerminalReadError):
    """The pane was recycled; this request belongs to the one that is gone."""


class ReaderClosed(TerminalReadError):
    """The reader is shutting down and starts no new captures."""


class TerminalReader:
    """P01's ``TerminalSource``: shared, bounded, read-only pane capture.

    Every seam is injected — the clock, the resolver, the server factory, the
    limits — so a test states the machine it means and no test needs a tmux
    server. The cache and the single flight are owned rather than injected
    because they are this object's identity: two readers over one pane would be
    two rate limits and two captures, which is the thing the packet exists to
    prevent.
    """

    def __init__(
        self,
        clock: Clock,
        resolver: TargetResolver,
        *,
        limits: CaptureLimits | None = None,
        server_factory: Callable[[str], CaptureServer] | None = None,
    ) -> None:
        self._clock = clock
        self._resolver = resolver
        self._limits = limits or CaptureLimits()
        self._server_factory: Callable[[str], CaptureServer] = server_factory or TmuxServer
        self._cache = FrameCache(max_entries=self._limits.cache_entries)
        self._flight = SingleFlight()
        self._closed = False

    # -- the port ----------------------------------------------------------

    def capture(
        self,
        target: TerminalTarget,
        *,
        scrollback: int = 0,
        viewport_height: int | None = None,
    ) -> TerminalFrame:
        """One frame; ``scrollback`` 0 is the live screen.

        Blocking, and meant for a worker thread — the ASGI loop must never call
        this directly. The returned frame reports the *effective* offset tmux
        honoured, never the requested one.
        """
        requested, height = self._validate(scrollback, viewport_height)
        location = self._locate(target)
        key = FrameKey.of(target, scrollback=requested, viewport_height=height)

        cached = self._cache.get(
            key, max_age_s=self._limits.min_interval_s, now=self._clock.monotonic()
        )
        if cached is not None:
            return cached

        try:
            frame = self._flight.run(
                key,
                lambda: self._capture_once(target, location, requested, height),
                deadline_s=self._limits.deadline_s,
            )
        except FlightTimeout:
            # Not cached: this caller's deadline is not an observation, and the
            # capture behind it may still land in the cache a moment later.
            return self._unavailable(target, requested, "timeout", TMUX_TIMEOUT_DETAIL)
        self._cache.put(key, frame, now=self._clock.monotonic())
        return frame

    def scrollback(
        self,
        target: TerminalTarget,
        *,
        before: int,
        viewport_height: int | None = None,
    ) -> TerminalFrame:
        """The screen as it was ``before`` lines above the live view.

        The same read as :meth:`capture` with an offset — there is no separate
        tmux scrollback command, and inventing a second path would be a second
        set of bounds to keep in step.
        """
        return self.capture(target, scrollback=before, viewport_height=viewport_height)

    # -- what the neighbouring packets need --------------------------------

    def fresh_capture(
        self, target: TerminalTarget, *, viewport_height: int | None = None
    ) -> TerminalFrame:
        """P09's read-under-lock primitive: a capture that is never a cached frame.

        P09 holds the per-target action lock, re-reads the pane and decides
        whether the prompt it is about to answer is still the prompt on screen.
        A frame from up to 100 ms ago cannot settle that, so this path skips the
        rate limit and the in-flight sharing entirely and captures. The result
        *is* stored, because a real observation is worth having whoever paid for
        it.
        """
        requested, height = self._validate(0, viewport_height)
        location = self._locate(target)
        frame = self._capture_once(target, location, requested, height)
        key = FrameKey.of(target, scrollback=requested, viewport_height=height)
        self._cache.put(key, frame, now=self._clock.monotonic())
        return frame

    def latest(self, target: TerminalTarget) -> TerminalFrame | None:
        """The newest real capture for this target, without capturing.

        P05's publication hook: a poll that wants to know whether anything
        changed must not itself become a capture at poll frequency.
        """
        return self._cache.latest_good(target.agent_id, target.generation)

    def invalidate(self, agent_id: str, *, generation: int | None = None) -> int:
        """P03/P05's lifecycle hook: forget this agent's frames. Returns how many."""
        return self._cache.invalidate(agent_id, generation=generation)

    def close(self) -> None:
        """Start no further captures. Work already in flight is left to finish.

        Cancelling it would neither stop the tmux process nor make the shutdown
        quicker — the threads are daemons — and would discard a frame somebody is
        waiting on.
        """
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def in_flight(self) -> int:
        """Captures running right now. Exposed so a shutdown can be observed."""
        return self._flight.in_flight

    # -- validation --------------------------------------------------------

    def _validate(self, scrollback: int, viewport_height: int | None) -> tuple[int, int | None]:
        """Bounds first, so an oversized request never becomes a tmux command."""
        if self._closed:
            raise ReaderClosed("the terminal reader is closed and starts no new captures")
        if scrollback > self._limits.max_scrollback:
            raise RequestTooLarge(
                f"scrollback {scrollback} exceeds the limit of {self._limits.max_scrollback} lines"
            )
        if viewport_height is not None and viewport_height > self._limits.max_viewport_height:
            raise RequestTooLarge(
                f"viewport height {viewport_height} exceeds the limit of "
                f"{self._limits.max_viewport_height} rows"
            )
        # A negative offset is the live screen, exactly as `TmuxServer.capture`
        # reads it; a non-positive height is no hint rather than a tiny one.
        height = viewport_height if viewport_height is not None and viewport_height > 0 else None
        return max(0, scrollback), height

    def _reject_raw_target(self, target: TerminalTarget) -> None:
        """Refuse an identifier that could be a tmux target or a path.

        Before the resolver rather than after: the guarantee wanted is that no
        browser-supplied string can become a tmux argument, and a guarantee that
        depends on every future resolver being careful is not one.
        """
        for field, value in (("agent_id", target.agent_id), ("session_id", target.session_id)):
            if value is None:
                continue
            if not value or len(value) > _MAX_IDENTIFIER_CHARS:
                raise TargetRejected(f"{field} must be 1 to {_MAX_IDENTIFIER_CHARS} characters")
            if set(value) & _TARGET_SIGILS:
                raise TargetRejected(
                    f"{field} contains a character that could address a pane, a session or a "
                    "path; the server resolves targets, a caller does not name them"
                )
        if target.generation < 0:
            raise TargetRejected("generation must not be negative")

    def _locate(self, target: TerminalTarget) -> PaneLocation:
        """Resolve, then fence the lifecycle."""
        self._reject_raw_target(target)
        location = self._resolver.locate(target)
        if location is None:
            raise TargetGone(f"no pane is resolved for agent {target.agent_id}")
        if location.generation != target.generation:
            raise StaleGeneration(
                f"agent {target.agent_id} is now at generation {location.generation}; "
                f"this request belongs to generation {target.generation}"
            )
        return location

    # -- the capture -------------------------------------------------------

    def _capture_once(
        self,
        target: TerminalTarget,
        location: PaneLocation,
        requested: int,
        height: int | None,
    ) -> TerminalFrame:
        """One tmux frame, or the honest account of why there is none.

        Runs on the single flight's worker thread, so every waiter for this key
        receives whatever this returns — or whatever it raises.
        """
        server = self._server_factory(location.socket)
        try:
            captured = server.capture(location.pane_id, scrollback=requested, height=height)
        except TmuxError:
            # Only now, and only because the capture failed: this probe is the
            # one call that separates a pane the server says is absent from a
            # server that said nothing at all.
            if self._answers(location.socket):
                raise TargetGone(
                    f"the tmux server has no pane for agent {target.agent_id}"
                ) from None
            return self._unavailable(
                target, requested, "service_unavailable", TMUX_UNAVAILABLE_DETAIL
            )
        return self._frame(target, captured, requested)

    def _answers(self, socket: str) -> bool:
        """Whether a server is listening on this socket at all."""
        try:
            return self._server_factory(socket).answers()
        except TmuxError:
            return False

    def _frame(self, target: TerminalTarget, captured: Capture, requested: int) -> TerminalFrame:
        """One :class:`Capture` as the frame the office serves.

        A dead pane arrives here like any other: tmux keeps its final screen, so
        the lines and the exit status are preserved and the lifecycle is carried
        by ``facts.dead``/``facts.dead_status`` rather than by an error. The
        process ending is news, not a failure of this read.
        """
        return TerminalFrame(
            target=target,
            lines=self._bound(captured.lines),
            requested_scrollback=requested,
            scrollback=captured.scrollback,
            captured_at=self._clock.now(),
            source="pane",
            facts=terminal_facts(captured.facts),
            history_size=captured.facts.history_size,
            stale=False,
            error=self._history_error(requested, captured),
        )

    def _history_error(self, requested: int, captured: Capture) -> ServiceError | None:
        """Say so when the history a caller asked for does not exist to be fetched.

        Three cases, and only one of them is a statement about capability:

        * history was reached — nothing to report;
        * history is empty and the pane is on the ORDINARY screen — a clamp, not
          a fault. Lines may scroll off later and then the same request works;
        * history is empty and the pane owns the ALTERNATE screen — there is no
          scrollback buffer for this pane to have, so no offset and no viewport
          will ever produce one. That is a capability, and it is reported as
          one so nobody tunes a parameter against it.
        """
        if requested <= 0 or captured.scrollback > 0 or captured.facts.history_size > 0:
            return None
        if not captured.facts.alternate_on:
            return None
        return ServiceError(
            code="unsupported_capability", detail=ALTERNATE_SCREEN_DETAIL, retryable=False
        )

    def _unavailable(
        self,
        target: TerminalTarget,
        requested: int,
        code: ServiceErrorCode,
        detail: str,
    ) -> TerminalFrame:
        """The pane's state is unknown: last-good marked stale, or nothing at all.

        ``captured_at`` keeps the ORIGINAL capture's timestamp when there is a
        last-good frame. A failure that refreshed the timestamp would turn a
        five-minute-old screen into a current one, which is the whole of what
        ``stale`` exists to prevent.
        """
        error = ServiceError(code=code, detail=detail, retryable=True)
        previous = self._cache.latest_good(target.agent_id, target.generation)
        if previous is not None:
            return TerminalFrame(
                target=target,
                lines=previous.lines,
                requested_scrollback=requested,
                scrollback=previous.scrollback,
                captured_at=previous.captured_at,
                source="pane",
                facts=previous.facts,
                history_size=previous.history_size,
                stale=True,
                error=error,
            )
        return TerminalFrame(
            target=target,
            lines=(),
            requested_scrollback=requested,
            scrollback=0,
            captured_at=self._clock.now(),
            source="none",
            facts=None,
            history_size=None,
            stale=False,
            error=error,
        )

    # -- bounds ------------------------------------------------------------

    def _bound(self, lines: list[str]) -> tuple[str, ...]:
        """The last ``max_lines`` rows, each sanitised, within the total budget.

        From the bottom in both directions: a terminal's newest rows are the
        ones that carry its meaning, so a frame that has to lose something loses
        the top. Trailing blank rows are part of the pane's height and are kept
        — a screen is 40 rows whether or not the program filled them.
        """
        kept: list[str] = []
        total = 0
        for line in reversed(lines[-self._limits.max_lines :]):
            cleaned = self._sanitize(line)
            if total + len(cleaned) > self._limits.max_total_chars:
                break
            kept.append(cleaned)
            total += len(cleaned)
        kept.reverse()
        return tuple(kept)

    def _sanitize(self, line: str) -> str:
        """One line: controls removed, length bounded, no half-written escape left."""
        cleaned = _CONTROL.sub("", line)
        if len(cleaned) <= self._limits.max_line_chars:
            return cleaned
        return _drop_dangling_escape(cleaned[: self._limits.max_line_chars])


def _drop_dangling_escape(text: str) -> str:
    """Remove a trailing escape sequence the truncation cut in half.

    A frame is not HTML and is never interpreted as any, but half of an SGR
    sequence is still a line whose tail renders as literal ``[38;5`` in whatever
    shows it. Conservative by design: anything after the last ESC that is not a
    complete CSI sequence is dropped, which also removes the non-CSI escapes a
    pane has no business emitting into a captured row.
    """
    index = text.rfind("\x1b")
    if index == -1 or _COMPLETE_CSI.match(text, index):
        return text
    return text[:index]


__all__ = [
    "ALTERNATE_SCREEN_DETAIL",
    "TMUX_TIMEOUT_DETAIL",
    "TMUX_UNAVAILABLE_DETAIL",
    "CaptureLimits",
    "CaptureServer",
    "PaneLocation",
    "ReaderClosed",
    "RequestTooLarge",
    "StaleGeneration",
    "TargetGone",
    "TargetRejected",
    "TargetResolver",
    "TerminalReadError",
    "TerminalReader",
]
