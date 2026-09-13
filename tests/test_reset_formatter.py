"""#152 — a reset time is a distance and a date, from ONE formatter, on both surfaces.

``accounts usage`` and the Accounts page each had their own ``_resets()``,
both ``HH:MM`` only. For the seven-day window that is misinformation: the reset
can be six days out and ``resets 02:00`` reads as tonight; the five-hour window
has the same bug in miniature around midnight. ``cli.common.format_reset`` is
now the only formatter, and this file pins it against a fixed clock on the
five cases the issue names — reset in 20 minutes, later today, tomorrow at the
same clock time, six days out, and the midnight boundary — plus that both
surfaces really call it (an AST walk, so a third copy cannot quietly appear).
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

import pytest

from aisquare.cli import accounts as accounts_cli
from aisquare.cli.common import format_reset
from aisquare.cli.ui.views import accounts as accounts_view
from aisquare.models import ClaudeUsage

TORONTO = ZoneInfo("America/Toronto")
#: 14:00 local on a Tuesday (18:00 UTC in EDT).
NOW = datetime(2026, 9, 15, 14, 0, tzinfo=TORONTO)


@pytest.fixture(autouse=True)
def local_clock_is_toronto(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """``local_time`` uses the process zone; pin it so the expected strings hold everywhere."""
    monkeypatch.setenv("TZ", "America/Toronto")
    import time

    time.tzset()
    yield
    time.tzset()


@pytest.mark.parametrize(
    ("ahead", "expected"),
    [
        (timedelta(minutes=20), "in 20m"),  # within the hour: the clock adds nothing
        (timedelta(minutes=59, seconds=30), "in 59m"),
        (timedelta(hours=3, minutes=10), "in 3h 10m (17:10)"),  # later today
        (timedelta(hours=4), "in 4h (18:00)"),
        (timedelta(days=1), "in 1d (Wed 14:00)"),  # tomorrow, same clock time — NOT bare 14:00
        (timedelta(days=2, hours=4), "in 2d 4h (Thu 18:00)"),
        (timedelta(days=6, hours=12), "in 6d 12h (Tue 02:00)"),  # the weekly window
        (timedelta(hours=10, minutes=30), "in 10h 30m (Wed 00:30)"),  # past midnight: the day shows
        # The endpoint's resets_at jitters across the second boundary between calls
        # (measured: 08:59:59.86 / 09:00:00.26); the clock time rounds to the minute
        # so two refreshes seconds apart say the same thing.
        (timedelta(hours=3, minutes=14, seconds=59, microseconds=864818), "in 3h 14m (17:15)"),
        (timedelta(hours=3, minutes=15, microseconds=261933), "in 3h 15m (17:15)"),
        (timedelta(0), "now"),
        (timedelta(minutes=-5), "now"),  # a stale reading: the window has already lifted
    ],
)
def test_format_reset_says_how_far_and_when(ahead: timedelta, expected: str) -> None:
    assert format_reset(NOW + ahead, now=NOW) == expected


def test_format_reset_never_renders_a_weekly_reset_as_a_bare_clock_time() -> None:
    """The property the issue states outright, swept over a week of offsets."""
    for hours in range(25, 7 * 24, 7):
        rendered = format_reset(NOW + timedelta(hours=hours), now=NOW)
        assert rendered.startswith("in ") and "d" in rendered.split(" (")[0], rendered
        assert " (" in rendered and rendered.split(" (")[1][:3].isalpha(), rendered


def test_format_reset_handles_none_and_reads_the_wall_clock_by_default() -> None:
    assert format_reset(None) == ""
    soon = datetime.now(tz=UTC) + timedelta(minutes=30)
    assert format_reset(soon).startswith("in ")  # no `now` given: the wall clock


def test_both_surfaces_render_the_same_string() -> None:
    """The page and the table agree to the character — they call the same function."""
    usage = ClaudeUsage(
        available=True,
        session_percent=68,
        session_resets_at=NOW + timedelta(hours=3, minutes=10),
        week_percent=35,
        week_resets_at=NOW + timedelta(days=6, hours=12),
    )
    session, week = accounts_cli._usage_cells(usage, now=NOW)
    assert session == "68% · resets in 3h 10m (17:10)"
    assert week == "35% · resets in 6d 12h (Tue 02:00)"
    from tests.test_ui_accounts import _status

    line = accounts_view.account_line_text(_status(2, "two@example.com"), usage, now=NOW).plain
    assert "session ▮▮▮▯▯ 68% · resets in 3h 10m (17:10)" in line  # 68 % rounds to three cells
    assert "week ▮▮▯▯▯ 35% · resets in 6d 12h (Tue 02:00)" in line


def _formats_a_reset_itself(node: ast.FunctionDef) -> bool:
    """A function that strftime()s or f-formats ``%H:%M`` is a formatter of its own."""
    for inner in ast.walk(node):
        if (
            isinstance(inner, ast.Constant)
            and isinstance(inner.value, str)
            and "%H:%M" in inner.value
        ):
            return True
        if (
            isinstance(inner, ast.FormattedValue)
            and isinstance(inner.format_spec, ast.JoinedStr)
            and any(
                isinstance(part, ast.Constant) and "%H" in str(part.value)
                for part in inner.format_spec.values
            )
        ):
            return True
    return False


@pytest.mark.parametrize("module", [accounts_cli, accounts_view])
def test_neither_surface_keeps_a_formatter_of_its_own(module: ModuleType) -> None:
    """One formatter: a surface that spells ``%H:%M`` itself is the drift this fixes."""
    source = Path(module.__file__ or "").read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and _formats_a_reset_itself(node)
    ]
    assert offenders == [], f"{module.__name__} formats a reset time on its own: {offenders}"


def test_the_formatter_rule_sees_its_own_shape() -> None:
    """Control: the rule must fire on a hand-rolled copy and stay quiet on a caller."""
    copy = ast.parse('def f(when):\n    return f" · resets {when:%H:%M}"\n').body[0]
    strf = ast.parse('def g(when):\n    return when.strftime("%H:%M")\n').body[0]
    caller = ast.parse("def h(when):\n    return format_reset(when)\n").body[0]
    assert isinstance(copy, ast.FunctionDef) and isinstance(strf, ast.FunctionDef)
    assert isinstance(caller, ast.FunctionDef)
    assert _formats_a_reset_itself(copy) and _formats_a_reset_itself(strf)
    assert not _formats_a_reset_itself(caller)
