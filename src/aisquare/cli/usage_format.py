"""How the usage numbers are written: one set of helpers for ``aisquare metrics stats`` and the
Usage & stats page, so a figure reads the same in both."""

from __future__ import annotations

from datetime import date

from aisquare.models import UsageRange

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
RANGE_LABELS: dict[UsageRange, str] = {
    "today": "Today",
    "7d": "7 days",
    "30d": "30 days",
    "lifetime": "Lifetime",
}
_STEPS = " ▁▂▃▄▅▆▇█"


def tokens(count: int) -> str:
    """``count`` in 3 significant figures with a unit: ``842``, ``12.4K``, ``1.4M``, ``2.1B``."""
    value = float(count)
    if abs(value) < 1000:
        return f"{int(value)}"
    for unit in ("K", "M", "B", "T"):
        value /= 1000
        digits = 1 if abs(value) < 100 else 0
        if round(abs(value), digits) < 1000 or unit == "T":
            return f"{value:.{digits}f}{unit}"
    return str(count)  # unreachable: the loop returns at "T"


def money(amount: float) -> str:
    """List-price dollars: cents under $100, whole dollars above."""
    return f"${amount:,.2f}" if amount < 100 else f"${amount:,.0f}"


def percent(share: float) -> str:
    return f"{share * 100:.0f}%" if share >= 0.01 or share == 0 else "<1%"


def bar(value: int, peak: int, width: int) -> str:
    """A bar ``width`` cells wide drawn in eighths: full when ``value`` is ``peak``."""
    if peak <= 0 or value <= 0 or width <= 0:
        return ""
    eighths = max(1, round(value / peak * width * 8))
    full, part = divmod(eighths, 8)
    return "█" * full + (" ▏▎▍▌▋▊▉"[part] if part else "")


def spark(values: list[int]) -> str:
    """One character per value, as tall as it is against the largest."""
    peak = max(values, default=0)
    if peak <= 0:
        return " " * len(values)
    return "".join(_STEPS[max(1, round(v / peak * 8))] if v > 0 else _STEPS[0] for v in values)


def short_date(text: str) -> str:
    """``2026-10-25`` as ``Sun 25 Oct``."""
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return text
    return f"{WEEKDAYS[day.weekday()]} {day.day} {day.strftime('%b')}"


#: How many days one bar stands for, tried in turn until the series fits the chart's width.
_BAR_SPANS = (1, 7, 30, 90, 365)


def rebucket(values: list[int], max_bars: int) -> tuple[list[int], int]:
    """``values`` (one per day) summed into bars of 1, 7, 30, 90 or 365 days — the first span
    that fits ``max_bars`` — and how many days each bar is."""
    for span in _BAR_SPANS:
        if -(-len(values) // span) <= max_bars:
            break
    bars = [sum(values[i : i + span]) for i in range(0, len(values), span)]
    return bars, span


def column_chart(values: list[int], *, height: int = 6, gap: int = 1) -> list[str]:
    """A column chart, top row first: one column per value (``gap`` blank cells between), each
    as tall as it is against the largest, in eighths of a row. Blank when all are zero."""
    peak = max(values, default=0)
    rows: list[str] = []
    for row in range(height, 0, -1):
        line = ""
        for value in values:
            filled = (value / peak * height - (row - 1)) if peak > 0 else 0.0
            if filled >= 1:
                cell = "█"
            elif filled <= 0:
                cell = " "
            else:
                cell = _STEPS[max(1, round(filled * 8))]
            line += cell + " " * gap
        rows.append(line.rstrip())
    return rows
