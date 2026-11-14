"""A Claude-only machine's agent info line is 0.9.2's, cell for cell, at every width (C1).

The Agent view's header and the Manager tab's first line (``views.agent.info_line``,
painted by ``InfoLine``). #113 put the agent's name (``claude-code``) right after the label
for every agent it spawned. That name counted in what a narrow row keeps whole, so in the
Agent view of a 90-column terminal (about 38 cells) the label was cut to six characters
and then the account and its use were cut too, which 0.9.2 keeps: the owner's #267 ruling
put them right after the state for this row (tester's C1, seq 11708 on #120).

The name is shown now only where the Accounts page marks its rows with their agent, with
accounts of more than one agent listed (``named``: ``FleetApp.agents_named``), and it
follows the account and its use, outside what a narrow row keeps whole.

:data:`LINES_0_9_2` is what 0.9.2's own ``info_line`` (6a77e650's views/agent.py, loaded
beside this package) painted for these rows. These are fixed strings, not a live import of
0.9.2, and they cover text, styles and the label's cut. The rows carry ``agent="claude-code"``,
as every spawn records it since #113; a row with no agent recorded hid C1 from the tests
before this one."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from rich.text import Text

from aisquare.cli.ui.views.agent import info_line
from aisquare.models import FleetAgent, FleetAgentStatus, TeamSession

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
LABELS = {2: "work", 3: "account 3"}
USAGE = {2: 7.0, 3: 0.0}
WIDTHS = (38, 45, 60, 200)


def _status(
    label: str,
    role: str,
    state: str,
    slot: int,
    model: str | None = None,
    effort: str | None = None,
    pane: str = "%3",
) -> FleetAgentStatus:
    agent = FleetAgent(
        id="agt_1",
        project_id="prj_1",
        label=label,
        role=role,
        pane_id=pane,
        cwd=Path("/w"),
        created_at=NOW,
        account_slot=slot,
        agent="claude-code",
    )
    session = (
        TeamSession(
            id="s",
            project_id="prj_1",
            role=role,
            started_at=NOW,
            last_seen_at=NOW,
            model=model,
            effort=effort,
        )
        if model
        else None
    )
    return FleetAgentStatus(agent=agent, state=state, session=session)


ROWS = {
    # The tester's three (seq 11708): the manager, a short coder label, a spawned one's.
    "manager": _status("manager", "manager", "waiting", 3),
    "coder-r2l": _status("coder-r2l", "coder", "waiting", 3, "claude-haiku-5-5", "medium"),
    "coder-01m78wvz": _status(
        "coder-01m78wvz", "coder", "working", 2, "claude-opus-5-5", "high", pane="%7"
    ),
}

#: 0.9.2's painted line for each row at each width (a nowrap, ellipsis row of that width).
LINES_0_9_2 = {
    ("manager", 38): "🧭 manager  ⏸ waiting  account 3 · 0%…",
    ("manager", 45): "🧭 manager  ⏸ waiting  account 3 · 0%  %3",
    ("manager", 60): "🧭 manager  ⏸ waiting  account 3 · 0%  %3",
    ("manager", 200): "🧭 manager  ⏸ waiting  account 3 · 0%  %3",
    ("coder-r2l", 38): "🔨 coder-…  ⏸ waiting  account 3 · 0%…",
    ("coder-r2l", 45): "🔨 coder-r2l  ⏸ waiting  account 3 · 0%  cla…",
    ("coder-r2l", 60): "🔨 coder-r2l  ⏸ waiting  account 3 · 0%  claude-haiku-5-5 m…",
    ("coder-r2l", 200): "🔨 coder-r2l  ⏸ waiting  account 3 · 0%  claude-haiku-5-5 medium  %3",
    ("coder-01m78wvz", 38): "🔨 coder-01m78…  ▶ working  work · 7%…",
    ("coder-01m78wvz", 45): "🔨 coder-01m78wvz  ▶ working  work · 7%  cla…",
    ("coder-01m78wvz", 60): "🔨 coder-01m78wvz  ▶ working  work · 7%  claude-opus-5-5 hi…",
    ("coder-01m78wvz", 200): "🔨 coder-01m78wvz  ▶ working  work · 7%  claude-opus-5-5 high  %7",
}

#: 0.9.2's line with its styles (``Text.markup``) where the label is cut and where it is not.
MARKUP_0_9_2 = {
    ("coder-r2l", 38): (
        "🔨 [bold]coder-…[/bold]  [yellow]⏸ waiting[/yellow]  [blue]account 3[dim][/blue] · "
        "[#3fb950][/dim]0%[/#3fb950]  [dim cyan]claude-haiku-5-5[/dim cyan] [cyan]medium"
        "[/cyan]  [dim italic]%3[/dim italic]"
    ),
    ("coder-r2l", 200): (
        "🔨 [bold]coder-r2l[/bold]  [yellow]⏸ waiting[/yellow]  [blue]account 3[dim][/blue] · "
        "[#3fb950][/dim]0%[/#3fb950]  [dim cyan]claude-haiku-5-5[/dim cyan] [cyan]medium"
        "[/cyan]  [dim italic]%3[/dim italic]"
    ),
    ("coder-01m78wvz", 38): (
        "🔨 [bold]coder-01m78…[/bold]  [green]▶ working[/green]  [blue]work[dim][/blue] · "
        "[#3fb950][/dim]7%[/#3fb950]  [dim cyan]claude-opus-5-5[/dim cyan] [cyan]high[/cyan]"
        "  [dim italic]%7[/dim italic]"
    ),
}


def _painted(line: Text, width: int) -> str:
    """What a ``nowrap`` / ``text-overflow: ellipsis`` row of ``width`` cells shows."""
    if line.cell_len <= width:
        return line.plain
    shown = line.copy()
    shown.truncate(width, overflow="ellipsis")
    return shown.plain


@pytest.mark.parametrize(("row", "width"), sorted(LINES_0_9_2))
def test_a_claude_only_fleet_sees_0_9_2s_line_at_every_width(row: str, width: int) -> None:
    line = info_line(ROWS[row], LABELS, USAGE, width=width)
    assert _painted(line, width) == LINES_0_9_2[row, width]
    assert "claude-code" not in line.plain


@pytest.mark.parametrize(("row", "width"), sorted(MARKUP_0_9_2))
def test_a_claude_only_fleets_line_has_0_9_2s_styles(row: str, width: int) -> None:
    assert info_line(ROWS[row], LABELS, USAGE, width=width).markup == MARKUP_0_9_2[row, width]


@pytest.mark.parametrize("row", sorted(ROWS))
@pytest.mark.parametrize("width", [38, 45])
def test_beside_another_agents_accounts_a_narrow_row_keeps_0_9_2s_account_and_use(
    row: str, width: int
) -> None:
    """``named``: the agent follows the account and its use, so a narrow row keeps
    everything 0.9.2 keeps up to the use, cut in the same place: the name is what gives way."""
    named = _painted(info_line(ROWS[row], LABELS, USAGE, width=width, named=True), width)
    before = LINES_0_9_2[row, width]
    use = before.index("%") + 1  # the end of `<account> · <pct>%`
    assert named[:use] == before[:use], named
    if "claude" in named[use:]:  # what of the name fits, after the use
        assert named[use:].startswith("  c"), named


@pytest.mark.parametrize(
    ("row", "line"),
    [
        ("manager", "🧭 manager  ⏸ waiting  account 3 · 0%  claude-code  %3"),
        (
            "coder-r2l",
            "🔨 coder-r2l  ⏸ waiting  account 3 · 0%  claude-code  claude-haiku-5-5 medium  %3",
        ),
        (
            "coder-01m78wvz",
            "🔨 coder-01m78wvz  ▶ working  work · 7%  claude-code  claude-opus-5-5 high  %7",
        ),
    ],
)
def test_beside_another_agents_accounts_a_wide_row_names_the_agent_after_its_account(
    row: str, line: str
) -> None:
    named = info_line(ROWS[row], LABELS, USAGE, width=200, named=True)
    assert named.plain == line
    assert ("claude-code", "dim") in [
        (named.plain[span.start : span.end], str(span.style)) for span in named.spans
    ]
