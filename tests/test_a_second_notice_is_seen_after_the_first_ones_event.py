"""A turn's second notice is stamped after the event of its first (CI time, #29).

The needs feed tells the second of two parallel permission prompts, or an MCP server's form,
from the first by ``session.last_seen_at > event.created_at``
(``remote_needs._needs_later_notice``): the first notice leaves the event and each later one
moves ``last_seen_at`` past it. Two notices that read the same tick of a clock that ticks
every 15 ms (Windows') tied, and the second was taken for the first. The store stamps it one
microsecond past the event when the clock would not (``SqliteStore.mark_attention``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
