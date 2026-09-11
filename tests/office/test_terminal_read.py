"""Shared read-only terminal capture, asserted one property at a time.

Nothing here starts tmux, a server or an agent: the reader is typed against
:class:`~aisquare.office.terminal_read.CaptureServer`, a two-method read-only
protocol, and every test hands it a small fake. The fakes are deliberately
plain — a port that needed a framework to fake would be the wrong port.

Two of the doubles do work a comment could not. :class:`ReadOnlyServer` raises
on *any* attribute it does not define, so a read adapter that reached for
``send_keys``, ``paste`` or ``resize`` would fail the test that uses it rather
than the review that missed it. :class:`BlockingServer` holds a capture open on
an event, which is how the sharing, deadline and shutdown properties are
asserted without sleeping through them.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aisquare.core.tmux import Capture, PaneFacts, TmuxError
from aisquare.office.config import OfficeConfig
from aisquare.office.models import TerminalFrame, TerminalTarget
from aisquare.office.ports import TerminalSource
from aisquare.office.terminal_cache import (
    FlightTimeout,
    FrameCache,
    FrameKey,
    SingleFlight,
)
from aisquare.office.terminal_read import (
    ALTERNATE_SCREEN_DETAIL,
    CaptureLimits,
    CaptureServer,
    PaneLocation,
    ReaderClosed,
    RequestTooLarge,
    StaleGeneration,
    TargetGone,
    TargetRejected,
    TerminalReader,
    _drop_dangling_escape,
)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

TARGET = TerminalTarget(agent_id="agt_zephyr", session_id="11111111-2222-3333", generation=3)

LOCATION = PaneLocation(socket="asq", pane_id="%14", generation=3)

CURSOR = "\u276f"
"""The pane's prompt marker, escaped rather than pasted so it stays legible."""

LIVE_LINES = ["● Ran 3 shell commands", f"\x1b[32m{CURSOR}\x1b[39m ", "  ⏵⏵ auto mode on"]


def facts(
    *,
    height: int = 3,
    history_size: int = 0,
    alternate_on: bool = True,
    dead: bool = False,
    dead_status: int | None = None,
) -> PaneFacts:
    """Pane facts shaped like a live Claude pane unless a test says otherwise.

    ``alternate_on=True`` with ``history_size=0`` is the default because that is
    what P03 measured on every live Claude pane on this machine: the TUI owns
    tmux's alternate screen, which has no scrollback buffer at all.
    """
    return PaneFacts(
        pane_id="%14",
        width=120,
        height=height,
        cursor_x=2,
        cursor_y=height - 1,
        cursor_visible=True,
        alternate_on=alternate_on,
        history_size=history_size,
        dead=dead,
        dead_status=dead_status,
        in_mode=False,
        current_command="claude",
        title="a title the process chose",
    )


def capture_of(
    lines: list[str] | None = None,
    *,
    scrollback: int = 0,
    pane_facts: PaneFacts | None = None,
) -> Capture:
    return Capture(
        lines=list(LIVE_LINES if lines is None else lines),
        facts=pane_facts or facts(),
        scrollback=scrollback,
    )


class FakeClock:
    """Driven time: wall time for stamps, monotonic for budgets."""

    def __init__(self) -> None:
        self.wall = NOW
        self.mono = 1000.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall = self.wall + timedelta(seconds=seconds)
        self.mono += seconds


class FakeResolver:
    """The server's own view of where a target lives."""

    def __init__(self, locations: dict[str, PaneLocation] | None = None) -> None:
        self.locations = {TARGET.agent_id: LOCATION} if locations is None else locations
        self.asked: list[str] = []

    def locate(self, target: TerminalTarget) -> PaneLocation | None:
        self.asked.append(target.agent_id)
        return self.locations.get(target.agent_id)


class FakeServer:
    """A tmux server that answers with what the test prepared."""

    def __init__(
        self,
        result: Capture | None = None,
        *,
        error: TmuxError | None = None,
        answers: bool = True,
    ) -> None:
        self.result = result if result is not None else capture_of()
        self.error = error
        self._answers = answers
        self.calls: list[tuple[str, int, int | None]] = []
        self.probes = 0

    def capture(self, pane_id: str, *, scrollback: int = 0, height: int | None = None) -> Capture:
        self.calls.append((pane_id, scrollback, height))
        if self.error is not None:
            raise self.error
        return self.result

    def answers(self) -> bool:
        self.probes += 1
        return self._answers


class ReadOnlyServer(FakeServer):
    """Fails the test if the adapter reaches for anything but the two read calls."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the read adapter reached for tmux's {name!r}")


class BlockingServer(FakeServer):
    """Holds one capture open until the test releases it."""

    def __init__(self, result: Capture | None = None) -> None:
        super().__init__(result)
        self.entered = threading.Event()
        self.release = threading.Event()

    def capture(self, pane_id: str, *, scrollback: int = 0, height: int | None = None) -> Capture:
        self.calls.append((pane_id, scrollback, height))
        self.entered.set()
        assert self.release.wait(5.0), "the test never released the blocked capture"
        return self.result


def reader_with(
    server: CaptureServer,
    *,
    clock: FakeClock | None = None,
    resolver: FakeResolver | None = None,
    limits: CaptureLimits | None = None,
) -> TerminalReader:
    return TerminalReader(
        clock or FakeClock(),
        resolver or FakeResolver(),
        limits=limits,
        server_factory=lambda _socket: server,
    )


def wait_until(predicate: object, timeout: float = 5.0) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` passes."""
    assert callable(predicate)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


# -- a live pane ----------------------------------------------------------


def test_a_live_pane_returns_its_lines_and_is_not_stale() -> None:
    server = FakeServer()
    frame = reader_with(server).capture(TARGET)

    assert frame.lines == tuple(LIVE_LINES)
    assert frame.stale is False
    assert frame.error is None
    assert frame.source == "pane"
    assert frame.captured_at == NOW


def test_the_frame_carries_bounded_pane_facts_without_the_title() -> None:
    """``TerminalPaneFacts`` is narrower than tmux's: no title, no command."""
    frame = reader_with(FakeServer()).capture(TARGET)

    assert frame.facts is not None
    assert frame.facts.height == 3
    assert frame.facts.alternate_on is True
    assert not hasattr(frame.facts, "title")


def test_the_frame_reports_the_offset_tmux_honoured_not_the_one_requested() -> None:
    """A 10 000-line request against a 500-line history is a 500-line frame."""
    server = FakeServer(
        capture_of(scrollback=500, pane_facts=facts(history_size=500, alternate_on=False))
    )

    frame = reader_with(server).capture(TARGET, scrollback=10_000)

    assert frame.requested_scrollback == 10_000
    assert frame.scrollback == 500
    assert frame.history_size == 500


def test_a_viewport_hint_reaches_tmux_as_the_height_bound() -> None:
    server = FakeServer(
        capture_of(scrollback=40, pane_facts=facts(history_size=900, alternate_on=False))
    )

    reader_with(server).capture(TARGET, scrollback=40, viewport_height=60)

    assert server.calls == [("%14", 40, 60)]


def test_a_non_positive_viewport_hint_is_no_hint_at_all() -> None:
    server = FakeServer()

    reader_with(server).capture(TARGET, viewport_height=0)

    assert server.calls == [("%14", 0, None)]


def test_a_negative_offset_is_the_live_screen() -> None:
    server = FakeServer()

    frame = reader_with(server).capture(TARGET, scrollback=-5)

    assert server.calls == [("%14", 0, None)]
    assert frame.requested_scrollback == 0


def test_the_scrollback_method_is_the_same_read_with_an_offset() -> None:
    server = FakeServer(
        capture_of(scrollback=12, pane_facts=facts(history_size=900, alternate_on=False))
    )

    frame = reader_with(server).scrollback(TARGET, before=12, viewport_height=40)

    assert server.calls == [("%14", 12, 40)]
    assert frame.scrollback == 12


# -- the alternate screen: history that does not exist --------------------


def test_scrollback_on_an_alternate_screen_pane_reports_that_none_exists() -> None:
    """The finding the packet is built around, stated as a capability.

    A live Claude pane reports ``alternate_on=1`` and ``history_size=0``: the
    TUI owns tmux's alternate screen, which has no scrollback buffer. An empty
    result would invite tuning an offset that can never matter.
    """
    server = FakeServer(capture_of(pane_facts=facts(alternate_on=True, history_size=0)))

    frame = reader_with(server).scrollback(TARGET, before=500)

    assert frame.error is not None
    assert frame.error.code == "unsupported_capability"
    assert frame.error.detail == ALTERNATE_SCREEN_DETAIL
    assert frame.error.retryable is False


def test_an_alternate_screen_pane_still_returns_its_live_screen() -> None:
    """Honest, not empty: there is no history, and here is what there is."""
    server = FakeServer(capture_of(pane_facts=facts(alternate_on=True, history_size=0)))

    frame = reader_with(server).scrollback(TARGET, before=500)

    assert frame.lines == tuple(LIVE_LINES)
    assert frame.scrollback == 0
    assert frame.stale is False


def test_an_empty_history_on_the_ordinary_screen_is_a_clamp_not_a_capability() -> None:
    """A pane nothing has scrolled off yet may fill later; that is not a fault."""
    server = FakeServer(capture_of(pane_facts=facts(alternate_on=False, history_size=0)))

    frame = reader_with(server).scrollback(TARGET, before=500)

    assert frame.error is None
    assert frame.scrollback == 0


def test_reaching_real_history_carries_no_error() -> None:
    server = FakeServer(
        capture_of(scrollback=80, pane_facts=facts(history_size=900, alternate_on=False))
    )

    frame = reader_with(server).scrollback(TARGET, before=80)

    assert frame.error is None
    assert frame.scrollback == 80


def test_the_live_screen_of_an_alternate_pane_is_not_an_error() -> None:
    """Offset 0 asks for no history, so the absence of history is not news."""
    frame = reader_with(FakeServer()).capture(TARGET, scrollback=0)

    assert frame.error is None


# -- lifecycle: dead, gone, unknown ---------------------------------------


def test_a_dead_pane_keeps_its_final_lines_and_exit_status() -> None:
    server = FakeServer(
        capture_of(["build failed", "$ "], pane_facts=facts(dead=True, dead_status=2))
    )

    frame = reader_with(server).capture(TARGET)

    assert frame.lines == ("build failed", "$ ")
    assert frame.facts is not None
    assert frame.facts.dead is True
    assert frame.facts.dead_status == 2


def test_a_dead_pane_is_not_reported_as_a_service_error() -> None:
    """The process ended. That is news about the agent, not a failed read."""
    server = FakeServer(capture_of(pane_facts=facts(dead=True, dead_status=0)))

    frame = reader_with(server).capture(TARGET)

    assert frame.error is None
    assert frame.stale is False
    assert frame.source == "pane"


def test_a_pane_the_server_says_is_absent_is_gone() -> None:
    server = FakeServer(error=TmuxError("can't find pane %14"), answers=True)

    with pytest.raises(TargetGone):
        reader_with(server).capture(TARGET)

    assert server.probes == 1, "the reachability probe is the only way to tell gone from unknown"


def test_a_target_that_never_existed_is_gone_without_touching_tmux() -> None:
    server = FakeServer()
    reader = reader_with(server, resolver=FakeResolver(locations={}))

    with pytest.raises(TargetGone):
        reader.capture(TerminalTarget(agent_id="agt_nobody"))

    assert server.calls == []


def test_an_unreachable_server_is_unknown_rather_than_gone() -> None:
    """Stopping at a silent socket is not evidence that the agent exited."""
    server = FakeServer(error=TmuxError("error connecting"), answers=False)

    frame = reader_with(server).capture(TARGET)

    assert frame.error is not None
    assert frame.error.code == "service_unavailable"
    assert frame.source == "none"
    assert frame.lines == ()


def test_an_unreachable_server_shows_the_last_good_frame_as_stale() -> None:
    clock = FakeClock()
    server = FakeServer()
    reader = reader_with(server, clock=clock)
    first = reader.capture(TARGET)

    clock.advance(60.0)
    server.error = TmuxError("error connecting")
    server._answers = False
    second = reader.capture(TARGET)

    assert second.lines == first.lines
    assert second.stale is True
    assert second.error is not None


def test_a_stale_frame_keeps_the_original_capture_time() -> None:
    """A source timeout does not convert an old screen into a current one."""
    clock = FakeClock()
    server = FakeServer()
    reader = reader_with(server, clock=clock)
    reader.capture(TARGET)

    clock.advance(300.0)
    server.error = TmuxError("error connecting")
    server._answers = False
    stale = reader.capture(TARGET)

    assert stale.captured_at == NOW
    assert stale.captured_at != clock.wall


def test_a_last_good_frame_from_another_generation_is_never_shown() -> None:
    clock = FakeClock()
    server = FakeServer()
    resolver = FakeResolver({TARGET.agent_id: LOCATION})
    reader = reader_with(server, clock=clock, resolver=resolver)
    reader.capture(TARGET)

    recycled = TerminalTarget(agent_id=TARGET.agent_id, session_id=None, generation=4)
    resolver.locations[TARGET.agent_id] = PaneLocation(socket="asq", pane_id="%21", generation=4)
    clock.advance(60.0)
    server.error = TmuxError("error connecting")
    server._answers = False

    frame = reader.capture(recycled)

    assert frame.lines == ()
    assert frame.stale is False


def test_a_capture_past_the_deadline_is_unknown_rather_than_dead() -> None:
    server = BlockingServer()
    reader = reader_with(server, limits=CaptureLimits(deadline_s=0.05))

    frame = reader.capture(TARGET)
    server.release.set()

    assert frame.error is not None
    assert frame.error.code == "timeout"
    assert frame.source == "none"


def test_an_abandoned_capture_retires_its_worker_when_it_finishes() -> None:
    """The wait stops; the flight is retired by the work itself, not leaked."""
    server = BlockingServer()
    reader = reader_with(server, limits=CaptureLimits(deadline_s=0.05))

    reader.capture(TARGET)
    assert reader.in_flight == 1, "the abandoned capture should still be running"

    server.release.set()

    assert wait_until(lambda: reader.in_flight == 0), "the finished flight was never retired"


def test_a_second_request_joins_a_stuck_capture_instead_of_starting_another() -> None:
    """One abandoned tmux process per pane, not one per request."""
    server = BlockingServer()
    reader = reader_with(server, limits=CaptureLimits(deadline_s=0.05))

    reader.capture(TARGET)
    reader.capture(TARGET)
    server.release.set()

    assert len(server.calls) == 1


# -- generation fencing ---------------------------------------------------


def test_a_recycled_pane_refuses_a_capture_for_the_old_generation() -> None:
    resolver = FakeResolver({TARGET.agent_id: PaneLocation("asq", "%21", generation=9)})
    server = FakeServer()

    with pytest.raises(StaleGeneration):
        reader_with(server, resolver=resolver).capture(TARGET)

    assert server.calls == [], "a fenced request must not reach tmux"


def test_invalidating_an_agent_drops_its_cached_frames() -> None:
    reader = reader_with(FakeServer())
    reader.capture(TARGET)

    dropped = reader.invalidate(TARGET.agent_id)

    assert dropped == 1
    assert reader.latest(TARGET) is None


def test_invalidating_below_a_generation_keeps_the_current_one() -> None:
    reader = reader_with(FakeServer())
    reader.capture(TARGET)

    dropped = reader.invalidate(TARGET.agent_id, generation=TARGET.generation)

    assert dropped == 0
    assert reader.latest(TARGET) is not None


# -- the browser never names a pane ---------------------------------------


def test_an_agent_id_shaped_like_a_pane_id_is_refused_before_the_resolver() -> None:
    resolver = FakeResolver()
    server = FakeServer()

    with pytest.raises(TargetRejected):
        reader_with(server, resolver=resolver).capture(TerminalTarget(agent_id="%14"))

    assert resolver.asked == [], "the guard must run before any resolver is trusted"
    assert server.calls == []


@pytest.mark.parametrize(
    "identifier",
    ["=asq-zephyr", "/etc/passwd", "asq:0.1", "agt zephyr", "agt\tzephyr", "$0", "@3"],
)
def test_an_identifier_that_could_address_something_else_is_refused(identifier: str) -> None:
    with pytest.raises(TargetRejected):
        reader_with(FakeServer()).capture(TerminalTarget(agent_id=identifier))


def test_an_empty_agent_id_is_refused() -> None:
    with pytest.raises(TargetRejected):
        reader_with(FakeServer()).capture(TerminalTarget(agent_id=""))


def test_an_overlong_agent_id_is_refused() -> None:
    with pytest.raises(TargetRejected):
        reader_with(FakeServer()).capture(TerminalTarget(agent_id="a" * 129))


def test_a_session_id_shaped_like_a_tmux_target_is_refused() -> None:
    target = TerminalTarget(agent_id="agt_zephyr", session_id="%14", generation=3)

    with pytest.raises(TargetRejected):
        reader_with(FakeServer()).capture(target)


def test_a_negative_generation_is_refused() -> None:
    target = TerminalTarget(agent_id="agt_zephyr", generation=-1)

    with pytest.raises(TargetRejected):
        reader_with(FakeServer()).capture(target)


# -- validation happens before tmux ---------------------------------------


def test_an_oversized_scrollback_request_never_reaches_tmux() -> None:
    server = FakeServer()

    with pytest.raises(RequestTooLarge):
        reader_with(server).capture(TARGET, scrollback=10_001)

    assert server.calls == []


def test_an_oversized_viewport_request_never_reaches_tmux() -> None:
    server = FakeServer()

    with pytest.raises(RequestTooLarge):
        reader_with(server).capture(TARGET, viewport_height=501)

    assert server.calls == []


# -- bounds on what comes back --------------------------------------------


def test_a_frame_is_bounded_to_the_line_limit_from_the_bottom() -> None:
    lines = [f"line {index}" for index in range(50)]
    server = FakeServer(capture_of(lines, pane_facts=facts(height=50)))

    frame = reader_with(server, limits=CaptureLimits(max_lines=5)).capture(TARGET)

    assert frame.lines == ("line 45", "line 46", "line 47", "line 48", "line 49")


def test_a_viewport_hint_cannot_lift_the_line_limit() -> None:
    """The hint is advisory; the global bound is not."""
    lines = [f"line {index}" for index in range(50)]
    server = FakeServer(capture_of(lines, pane_facts=facts(height=50)))

    frame = reader_with(server, limits=CaptureLimits(max_lines=4)).capture(
        TARGET, viewport_height=40
    )

    assert len(frame.lines) == 4


def test_the_total_character_budget_drops_the_top_of_the_frame() -> None:
    server = FakeServer(capture_of(["aaaa", "bbbb", "cccc"], pane_facts=facts()))

    frame = reader_with(server, limits=CaptureLimits(max_total_chars=9)).capture(TARGET)

    assert frame.lines == ("bbbb", "cccc")


def test_control_bytes_are_stripped_and_sgr_colours_are_kept() -> None:
    server = FakeServer(capture_of(["\x00a\x1b[31mred\x1b[39m\r"], pane_facts=facts(height=1)))

    frame = reader_with(server).capture(TARGET)

    assert frame.lines == ("a\x1b[31mred\x1b[39m",)


def test_a_trailing_blank_row_is_part_of_the_pane_and_is_kept() -> None:
    server = FakeServer(capture_of(["top", "", ""], pane_facts=facts(height=3)))

    frame = reader_with(server).capture(TARGET)

    assert frame.lines == ("top", "", "")


def test_a_truncated_line_does_not_end_in_half_an_escape() -> None:
    server = FakeServer(capture_of(["abcde\x1b[31mZZZZ"], pane_facts=facts(height=1)))

    frame = reader_with(server, limits=CaptureLimits(max_line_chars=8)).capture(TARGET)

    assert frame.lines == ("abcde",)


def test_a_complete_escape_survives_truncation_at_the_boundary() -> None:
    server = FakeServer(capture_of(["abcde\x1b[31mZZ"], pane_facts=facts(height=1)))

    frame = reader_with(server, limits=CaptureLimits(max_line_chars=10)).capture(TARGET)

    assert frame.lines == ("abcde\x1b[31m",)


def test_dropping_a_dangling_escape_leaves_ordinary_text_alone() -> None:
    assert _drop_dangling_escape("plain text") == "plain text"
    assert _drop_dangling_escape("a\x1b[31mb") == "a\x1b[31mb"
    assert _drop_dangling_escape("a\x1b[3") == "a"


# -- shared work ----------------------------------------------------------


def test_two_clients_watching_one_pane_share_a_single_capture() -> None:
    server = BlockingServer()
    reader = reader_with(server, limits=CaptureLimits(deadline_s=5.0))
    frames: list[TerminalFrame] = []

    def watch() -> None:
        frames.append(reader.capture(TARGET))

    first = threading.Thread(target=watch)
    first.start()
    assert server.entered.wait(5.0), "the first capture never started"

    second = threading.Thread(target=watch)
    second.start()
    assert wait_until(lambda: reader.in_flight == 1)
    time.sleep(0.2)
    server.release.set()

    first.join(5.0)
    second.join(5.0)

    assert len(server.calls) == 1, "the same pane was captured twice for two clients"
    assert len(frames) == 2
    assert frames[0] is frames[1], "the shared result must be one immutable frame"


def test_a_second_request_inside_the_rate_window_is_served_from_the_cache() -> None:
    clock = FakeClock()
    server = FakeServer()
    reader = reader_with(server, clock=clock)

    first = reader.capture(TARGET)
    clock.advance(0.05)
    second = reader.capture(TARGET)

    assert len(server.calls) == 1
    assert first is second


def test_a_request_after_the_rate_window_captures_again() -> None:
    clock = FakeClock()
    server = FakeServer()
    reader = reader_with(server, clock=clock)

    reader.capture(TARGET)
    clock.advance(0.2)
    reader.capture(TARGET)

    assert len(server.calls) == 2


def test_distinct_offsets_are_distinct_work() -> None:
    """Two clients reading different parts of the screen do not collide."""
    clock = FakeClock()
    server = FakeServer(capture_of(pane_facts=facts(alternate_on=False, history_size=900)))
    reader = reader_with(server, clock=clock)

    reader.capture(TARGET)
    reader.capture(TARGET, scrollback=40)

    assert len(server.calls) == 2


# -- what P05 and P09 get -------------------------------------------------


def test_latest_returns_the_last_good_frame_without_capturing() -> None:
    server = FakeServer()
    reader = reader_with(server)
    captured = reader.capture(TARGET)

    assert reader.latest(TARGET) is captured
    assert len(server.calls) == 1


def test_latest_is_empty_before_anything_was_captured() -> None:
    assert reader_with(FakeServer()).latest(TARGET) is None


def test_latest_does_not_answer_for_another_generation() -> None:
    reader = reader_with(FakeServer())
    reader.capture(TARGET)

    other = TerminalTarget(agent_id=TARGET.agent_id, session_id=None, generation=4)

    assert reader.latest(other) is None


def test_a_fresh_capture_ignores_the_rate_window() -> None:
    """P09 answers a prompt under a lock; a 100 ms-old screen cannot settle that."""
    clock = FakeClock()
    server = FakeServer()
    reader = reader_with(server, clock=clock)
    reader.capture(TARGET)

    fresh = reader.fresh_capture(TARGET)

    assert len(server.calls) == 2
    assert fresh.stale is False


def test_a_fresh_capture_is_still_fenced_to_the_generation() -> None:
    resolver = FakeResolver({TARGET.agent_id: PaneLocation("asq", "%21", generation=9)})

    with pytest.raises(StaleGeneration):
        reader_with(FakeServer(), resolver=resolver).fresh_capture(TARGET)


# -- read-only, structurally ----------------------------------------------


def test_the_reader_never_reaches_for_a_mutating_tmux_method() -> None:
    """The double raises on any attribute but the two reads."""
    server = ReadOnlyServer()
    reader = reader_with(server)

    frame = reader.capture(TARGET)

    assert frame.lines == tuple(LIVE_LINES)


def test_the_capture_protocol_offers_nothing_that_changes_a_pane() -> None:
    declared = {name for name in vars(CaptureServer) if not name.startswith("_")}

    assert declared == {"capture", "answers"}


def test_the_reader_implements_the_terminal_source_port() -> None:
    required = {name for name in vars(TerminalSource) if not name.startswith("_")}
    offered = {name for name in dir(TerminalReader) if not name.startswith("_")}

    assert required, "the port declared nothing, so this comparison would be vacuous"
    assert required <= offered


# -- shutdown -------------------------------------------------------------


def test_a_closed_reader_starts_no_new_capture() -> None:
    server = FakeServer()
    reader = reader_with(server)
    reader.close()

    with pytest.raises(ReaderClosed):
        reader.capture(TARGET)

    assert server.calls == []


def test_closing_leaves_work_already_in_flight_alone() -> None:
    server = BlockingServer()
    reader = reader_with(server, limits=CaptureLimits(deadline_s=0.05))
    reader.capture(TARGET)

    reader.close()
    server.release.set()

    assert wait_until(lambda: reader.in_flight == 0), "the in-flight capture never finished"
    assert reader.closed is True


# -- limits ---------------------------------------------------------------


def test_the_capture_rate_comes_from_the_resolved_configuration(tmp_path: Path) -> None:
    config = OfficeConfig.from_mapping({"capture_fps": 4}, home=tmp_path)

    limits = CaptureLimits.from_config(config)

    assert limits.min_interval_s == pytest.approx(0.25)


def test_the_default_rate_is_the_ten_frames_per_second_ceiling(tmp_path: Path) -> None:
    limits = CaptureLimits.from_config(OfficeConfig.from_mapping({}, home=tmp_path))

    assert limits.min_interval_s == pytest.approx(0.1)


# -- the cache and the flight, directly -----------------------------------


def frame_for(agent_id: str = "agt_zephyr", *, generation: int = 3) -> TerminalFrame:
    return TerminalFrame(
        target=TerminalTarget(agent_id=agent_id, generation=generation),
        lines=("x",),
        requested_scrollback=0,
        scrollback=0,
        captured_at=NOW,
        source="pane",
    )


def key_for(agent_id: str = "agt_zephyr", *, generation: int = 3, scrollback: int = 0) -> FrameKey:
    return FrameKey(
        agent_id=agent_id,
        session_id=None,
        generation=generation,
        scrollback=scrollback,
        viewport_height=None,
    )


def test_the_cache_serves_a_frame_only_inside_its_age_window() -> None:
    cache = FrameCache()
    cache.put(key_for(), frame_for(), now=100.0)

    assert cache.get(key_for(), max_age_s=0.1, now=100.05) is not None
    assert cache.get(key_for(), max_age_s=0.1, now=100.5) is None


def test_the_cache_evicts_the_oldest_entry_when_full() -> None:
    cache = FrameCache(max_entries=2)
    for index in range(3):
        cache.put(key_for(scrollback=index), frame_for(), now=100.0)

    assert len(cache) == 2
    assert cache.get(key_for(scrollback=0), max_age_s=10.0, now=100.0) is None
    assert cache.get(key_for(scrollback=2), max_age_s=10.0, now=100.0) is not None


def test_a_failure_envelope_is_cached_but_is_never_last_good() -> None:
    """Backpressure may reuse it; a stale envelope may not be built from it."""
    cache = FrameCache()
    envelope = TerminalFrame(
        target=TerminalTarget(agent_id="agt_zephyr", generation=3),
        lines=(),
        requested_scrollback=0,
        scrollback=0,
        captured_at=NOW,
        source="none",
    )
    cache.put(key_for(), envelope, now=100.0)

    assert cache.get(key_for(), max_age_s=10.0, now=100.0) is envelope
    assert cache.latest_good("agt_zephyr", 3) is None


def test_a_cache_needs_room_for_at_least_one_entry() -> None:
    with pytest.raises(ValueError, match="max_entries"):
        FrameCache(max_entries=0)


def test_clearing_the_cache_removes_everything() -> None:
    cache = FrameCache()
    cache.put(key_for(), frame_for(), now=100.0)

    cache.clear()

    assert len(cache) == 0


def test_the_single_flight_runs_one_worker_for_two_waiters() -> None:
    flight = SingleFlight()
    started = threading.Event()
    release = threading.Event()
    runs: list[int] = []

    def work() -> str:
        runs.append(1)
        started.set()
        assert release.wait(5.0)
        return "done"

    results: list[str] = []

    def ask() -> None:
        results.append(flight.run("key", work, deadline_s=5.0))

    first = threading.Thread(target=ask)
    first.start()
    assert started.wait(5.0)
    second = threading.Thread(target=ask)
    second.start()
    time.sleep(0.2)
    release.set()
    first.join(5.0)
    second.join(5.0)

    assert len(runs) == 1
    assert results == ["done", "done"]


def test_the_single_flight_reraises_the_workers_failure() -> None:
    flight = SingleFlight()

    def work() -> str:
        raise TmuxError("no pane")

    with pytest.raises(TmuxError):
        flight.run("key", work, deadline_s=5.0)

    assert flight.in_flight == 0, "a failing worker must still retire its flight"


def test_the_single_flight_times_out_the_waiter_not_the_work() -> None:
    flight = SingleFlight()
    release = threading.Event()

    def work() -> str:
        assert release.wait(5.0)
        return "late"

    with pytest.raises(FlightTimeout):
        flight.run("key", work, deadline_s=0.05)

    assert flight.in_flight == 1, "the work is still running; only the wait ended"
    release.set()
    assert wait_until(lambda: flight.in_flight == 0)
