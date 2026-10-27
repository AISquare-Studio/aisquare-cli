"""A turn's second notice is stamped after the event of its first (CI time, #29).

The needs feed tells the second of two parallel permission prompts, or an MCP server's form,
from the first by ``session.last_seen_at > event.created_at``
(``remote_needs._needs_later_notice``): the first notice leaves the event and each later one
moves ``last_seen_at`` past it. Two notices that read the same tick of a clock that ticks
every 15 ms (Windows') tied, and the second was taken for the first. The store stamps it one
microsecond past the event when the clock would not (``SqliteStore.mark_attention``), and a
third, fourth... notice in the same tick leaves it there rather than back at the tick.

The tick is frozen where it matters: the first event and the stamp of the notice that wrote it
are the same instant, which is what a Windows clock gives two writes in a row and a Linux one
almost never does.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

import pytest

from aisquare.core import store as store_module
from aisquare.core.store import store_session
from aisquare.services import remote_needs
from aisquare.services import team as team_service

SESSION = "cccc3333-0000-0000-0000-000000000000"
PROMPT = "Claude needs your permission to use mcp__deploy__release"


def _the_agent_asks_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, first_event_at: datetime
) -> tuple[object, object]:
    """Two notices of one turn, over the real store; the first one's event stamped
    ``first_event_at`` by the clock it read. Returns the session and the event after both."""
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("AISQUARE_TEAM", "1")
    project = team_service.activate(work)
    team_service.hook_session_start(SESSION, work, "startup")
    with monkeypatch.context() as patch:
        patch.setattr(team_service, "_now", lambda: first_event_at)
        team_service.hook_notification(SESSION, work, PROMPT, notification_type="permission_prompt")
    team_service.hook_notification(
        SESSION, work, "Claude Code needs your input", notification_type="elicitation_dialog"
    )
    with store_session() as store:
        [event] = [e for e in store.recent_events(project.id, limit=20) if e.kind == "attention"]
        return store.get_session(SESSION), event


def test_the_second_notice_is_after_the_event_of_the_first_even_when_the_clock_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ahead = datetime.now(tz=UTC) + timedelta(hours=1)  # the tick the first event was stamped in
    session, event = _the_agent_asks_twice(tmp_path, monkeypatch, first_event_at=ahead)
    assert session is not None
    assert session.last_seen_at > event.created_at  # type: ignore[attr-defined]
    assert event.created_at == ahead  # type: ignore[attr-defined]
    assert remote_needs._needs_later_notice(event, None, session)  # type: ignore[arg-type]


def test_the_first_notice_alone_is_not_a_later_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the pin: the stamp moves only past an event that is there."""
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("AISQUARE_TEAM", "1")
    project = team_service.activate(work)
    team_service.hook_session_start(SESSION, work, "startup")
    team_service.hook_notification(SESSION, work, PROMPT, notification_type="permission_prompt")
    with store_session() as store:
        [event] = [e for e in store.recent_events(project.id, limit=20) if e.kind == "attention"]
        session = store.get_session(SESSION)
    assert session is not None
    assert not remote_needs._needs_later_notice(event, None, session)


def _the_clock_stops_at(monkeypatch: pytest.MonkeyPatch, tick: datetime) -> None:
    """Every reading of the wall clock, the store's and the team service's, is ``tick``."""

    class _Stopped(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> _Stopped:
            return tick.astimezone(tz)  # type: ignore[return-value]

    monkeypatch.setattr(store_module, "datetime", _Stopped)
    monkeypatch.setattr(team_service, "_now", lambda: tick)


def test_every_notice_after_the_first_in_one_tick_is_a_later_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three parallel permission prompts inside one tick: the second moved ``last_seen_at`` to
    the microsecond after the event, and the third, asking for no event past that, wrote the
    tick back, which read as a first notice again (the card of the wrong call). The tick is
    stopped before the first notice, so its event and the stamp it leaves are one instant."""
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("AISQUARE_TEAM", "1")
    tick = datetime.now(tz=UTC)
    _the_clock_stops_at(monkeypatch, tick)
    project = team_service.activate(work)
    team_service.hook_session_start(SESSION, work, "startup")
    kinds = ["permission_prompt", "elicitation_dialog", "permission_prompt", "elicitation_dialog"]
    later: list[bool] = []
    stamps: list[datetime] = []
    for number, kind in enumerate(kinds, 1):
        team_service.hook_notification(SESSION, work, f"notice {number}", notification_type=kind)
        with store_session() as store:
            [event] = [
                e for e in store.recent_events(project.id, limit=50) if e.kind == "attention"
            ]
            session = store.get_session(SESSION)
        assert session is not None
        assert event.created_at == tick, "the turn's first notice left the one event"
        later.append(remote_needs._needs_later_notice(event, None, session))
        stamps.append(session.last_seen_at)
    assert stamps[0] == tick, "the first notice is stamped in the tick of its own event"
    assert later == [False, True, True, True]
    assert stamps[1:] == [tick + timedelta(microseconds=1)] * 3, "past the event, and not further"


def test_the_stamp_is_after_this_sessions_attention_event_and_no_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The newest event asked for is the session's own, and an ``attention`` one: another
    session's, or another kind of the same session's, in a later tick, is not what its second
    notice follows."""
    other_session = "dddd4444-0000-0000-0000-000000000000"
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("AISQUARE_TEAM", "1")
    tick = datetime.now(tz=UTC)
    _the_clock_stops_at(monkeypatch, tick)
    project = team_service.activate(work)
    team_service.hook_session_start(SESSION, work, "startup")
    team_service.hook_session_start(other_session, work, "startup")
    team_service.hook_notification(SESSION, work, PROMPT, notification_type="permission_prompt")
    later_tick = tick + timedelta(milliseconds=16)
    monkeypatch.setattr(team_service, "_now", lambda: later_tick)
    team_service.hook_notification(
        other_session, work, "another agent asks", notification_type="permission_prompt"
    )
    with store_session() as store:
        team_service._emit(store, project.id, "note", "a line", session_id=SESSION)
    monkeypatch.setattr(team_service, "_now", lambda: tick)
    team_service.hook_notification(
        SESSION, work, "Claude Code needs your input", notification_type="elicitation_dialog"
    )
    with store_session() as store:
        [event] = [
            e
            for e in store.recent_events(project.id, limit=50)
            if e.kind == "attention" and e.session_id == SESSION
        ]
        session = store.get_session(SESSION)
    assert session is not None
    assert event.created_at == tick
    assert session.last_seen_at == tick + timedelta(microseconds=1)
    assert remote_needs._needs_later_notice(event, None, session)


def test_a_heartbeat_in_the_tick_of_the_notices_does_not_step_the_stamp_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``touch_session`` wrote the clock's reading, which in the tick of the first notice is the
    event's instant: after the second notice a same-tick heartbeat (the next tool call, a
    prompt) stepped ``last_seen_at`` back to the event, and the third notice read as the
    first. The stamp stays one microsecond past the event, and a heartbeat in a later tick
    still moves it on."""
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("AISQUARE_TEAM", "1")
    tick = datetime.now(tz=UTC)
    _the_clock_stops_at(monkeypatch, tick)
    project = team_service.activate(work)
    team_service.hook_session_start(SESSION, work, "startup")
    team_service.hook_notification(SESSION, work, PROMPT, notification_type="permission_prompt")
    team_service.hook_notification(
        SESSION, work, "Claude Code needs your input", notification_type="elicitation_dialog"
    )

    def stamp_after(heartbeat: bool) -> tuple[datetime, bool]:
        with store_session() as store:
            if heartbeat:
                store.touch_session(SESSION, cursor=3, state="working")
            [event] = [
                e for e in store.recent_events(project.id, limit=50) if e.kind == "attention"
            ]
            session = store.get_session(SESSION)
        assert session is not None
        return session.last_seen_at, remote_needs._needs_later_notice(event, None, session)

    assert stamp_after(heartbeat=False) == (tick + timedelta(microseconds=1), True)
    assert stamp_after(heartbeat=True) == (tick + timedelta(microseconds=1), True)

    later_tick = tick + timedelta(milliseconds=16)
    _the_clock_stops_at(monkeypatch, later_tick)
    assert stamp_after(heartbeat=True)[0] == later_tick, "a heartbeat later on still lands"
