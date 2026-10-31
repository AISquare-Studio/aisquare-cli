"""``aisquare fleet top`` as a table: the same one asq's *Agent resources* page draws.

One row per live agent (``services.fleet_top``), heaviest first by the sort, and a totals
row. An agent whose processes could not be read shows why in its CPU cell (``gone``: its
pane is not on its server; ``exited``: its pane's process ended; ``?``: tmux or ``ps`` did
not answer) and adds nothing to the totals but its spend.
"""

from __future__ import annotations

from rich.table import Table
from rich.text import Text

from aisquare.cli import usage_format as fmt
from aisquare.services.fleet_top import AgentUse, SortKey, TopReport, Totals

SORT_LABELS: dict[SortKey, str] = {"cpu": "CPU", "mem": "memory", "tokens": "tokens"}

_SORTED_COLUMN: dict[SortKey, str] = {"cpu": "CPU%", "mem": "MEM", "tokens": "TOKENS"}

_NOT_READ = {"gone": "gone", "exited": "exited", "unknown": "?"}

UNPRICED_NOTE = "* a model with no list price: its tokens are counted, its cost is not"


def memory(rss_kb: int) -> str:
    """Resident memory, KiB in: ``812M``, ``5.4G``."""
    mib = rss_kb / 1024
    return f"{mib:.0f}M" if mib < 1024 else f"{mib / 1024:.1f}G"


def duration(seconds: int) -> str:
    """How long something has run: ``45s``, ``12m``, ``3h05m``, ``2d03h``."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    return f"{seconds // 86400}d{seconds % 86400 // 3600:02d}h"


def _cost(amount: float | None, priced: bool) -> str:
    if amount is None:
        return "—"
    return fmt.money(amount) + ("" if priced else "*")


def _row(agent: AgentUse) -> list[str | Text]:
    running = agent.state == "running"
    return [
        agent.project,
        agent.label,
        agent.role,
        agent.account or "—",
        f"{agent.cpu:.1f}"
        if running and agent.cpu is not None
        else Text(_NOT_READ[agent.state], style="dim"),
        memory(agent.rss_kb) if running and agent.rss_kb is not None else "—",
        str(agent.processes) if running else "—",
        duration(agent.uptime) if running and agent.uptime is not None else "—",
        fmt.tokens(agent.tokens) if agent.tokens is not None else "—",
        _cost(agent.cost_usd, agent.priced),
    ]


def _totals_row(totals: Totals) -> list[str | Text]:
    agents = f"{totals.agents} agent{'' if totals.agents == 1 else 's'}"
    if totals.running != totals.agents:
        agents += f" ({totals.running} read)"
    return [
        "total",
        agents,
        "",
        "",
        f"{totals.cpu:.1f}",
        memory(totals.rss_kb),
        str(totals.processes),
        "",
        fmt.tokens(totals.tokens),
        _cost(totals.cost_usd, totals.priced),
    ]


def top_table(report: TopReport, by: SortKey = "cpu") -> Table:
    """The report as a table, sorted by ``by`` (its column marked ``▼``), with a totals row."""
    table = Table(box=None, pad_edge=False, header_style="bold", show_footer=False)
    for name, right in (
        ("PROJECT", False),
        ("AGENT", False),
        ("ROLE", False),
        ("ACCOUNT", False),
        ("CPU%", True),
        ("MEM", True),
        ("PROCS", True),
        ("UP", True),
        ("TOKENS", True),
        ("COST", True),
    ):
        header = f"{name} ▼" if name == _SORTED_COLUMN[by] else name
        table.add_column(
            header,
            justify="right" if right else "left",
            no_wrap=True,
            overflow="ellipsis",
            max_width=None if right else 24,
        )
    for agent in report.sorted(by):
        table.add_row(*_row(agent))
    if report.agents:
        table.add_section()
        table.add_row(*_totals_row(report.totals()), style="bold")
    return table


def summary(report: TopReport, by: SortKey = "cpu") -> str:
    """The line over the table: what was read, how fast, and how it is sorted."""
    totals = report.totals()
    line = (
        f"{totals.agents} live agent{'' if totals.agents == 1 else 's'} · "
        f"sampled in {report.took_ms / 1000:.2f} s · sorted by {SORT_LABELS[by]}"
    )
    if not report.ps_read:
        line += " · ps could not be read: no process figures"
    if report.unanswered:
        line += f" · tmux did not answer at {', '.join(report.unanswered)}"
    return line


def notes(report: TopReport) -> list[str]:
    """What the table's figures are, under it."""
    lines = [
        "CPU% is of one core, the pane's whole process tree summed; tokens and cost are the "
        "session's so far, at list prices."
    ]
    if not report.totals().priced:
        lines.append(UNPRICED_NOTE)
    return lines
