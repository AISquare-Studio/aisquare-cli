"""Usage-limit notices whose reset is a set distance from the clock the code under test reads.
Not a test module.

``You've hit your weekly limit · resets Mon 12:00am (America/Toronto)`` names a time of day,
and the hook reads it against the wall clock: a reset ``wait_if_reset_within_minutes`` (15) or
closer is waited for, not handed over. A test that fires that fixed text meant "a reset far
away", and it was one for all but the quarter hour before Monday 00:00 in Toronto (the weekly
text), and the quarter hour before 00:30 every night (the session text): CI run 40265482611, at
23:36 to 23:59 on Sunday in Toronto, lost 24 of its shards to nine tests of
``tests/test_usage_aware_accounts.py`` and ``tests/test_fleet_service.py`` for that alone.

These are the same texts, made for the minute the test runs, so what a test sees does not
depend on when it is run. The fixed texts stay where a test hands the parser its own ``now``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

TORONTO = ZoneInfo("America/Toronto")
FAR_WEEKLY = timedelta(days=3, hours=1)
FAR_SESSION = timedelta(hours=5)
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _clock(at: datetime) -> str:
    """``9:30am``, as Claude Code prints a reset (no leading zero, lowercase)."""
    hour = at.hour % 12 or 12
    return f"{hour}:{at.minute:02d}{'pm' if at.hour >= 12 else 'am'}"


def weekly(resets_in: timedelta = FAR_WEEKLY) -> str:
    """The weekly limit's notice, resetting ``resets_in`` from now (Toronto's clock, a weekday)."""
    at = (datetime.now(tz=UTC) + resets_in).astimezone(TORONTO)
    return (
        f"You've hit your weekly limit · resets {DAYS[at.weekday()]} {_clock(at)} (America/Toronto)"
    )


def session(resets_in: timedelta = FAR_SESSION) -> str:
    """The session limit's notice, resetting ``resets_in`` (under a day) from now."""
    assert resets_in < timedelta(days=1), "a session text names a time of day, never a day"
    at = (datetime.now(tz=UTC) + resets_in).astimezone(TORONTO)
    return f"You've hit your session limit · resets {_clock(at)} (America/Toronto)"
