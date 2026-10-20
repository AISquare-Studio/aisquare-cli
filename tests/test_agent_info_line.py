"""The agent info line: one builder for the Agent view's header and the Manager tab's first line.

The two were separate builders that had drifted apart (the Manager tab named the model and
the tmux command, the Agent view the role, the task and the working directory). Now there is
one, :func:`~aisquare.cli.ui.views.agent.info_line`, in the manager line's shape with the
effort and the account added: ``🔨 coder-auth  ▶ working  claude-opus-5-5 high  dev · 87%  %4``.
The account is its NAME (no ``#slot``) and the figure beside it the five-hour window, percent
USED, never the weekly one. The sidebar's rows say ``#<slot>·<pct>%`` with the same colours,
from the same helper (:func:`~aisquare.cli.ui.sidebar.session_percent_text`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import pytest
from rich.text import Text

from aisquare.cli.ui.sidebar import GREEN, RED, YELLOW, agent_row_text, session_percent_text
from aisquare.cli.ui.views.agent import info_line
from aisquare.cli.ui.views.project import MANAGER_HINT, manager_text
from aisquare.models import ClaudeAccount, ClaudeUsage, FleetAgent, FleetAgentStatus, TeamSession
from aisquare.services import claude_accounts as accounts_service

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
CWD = Path("/home/me/[archive]/repo")
LABELS = {1: "plain claude", 2: "work", 3: "dev"}


def _status(
    *,
    label: str = "coder-auth",
    role: str = "coder",
    state: str = "working",
    model: str | None = "claude-opus-5-5",
    effort: str | None = "high",
    slot: int | None = 3,
    account: str | None = None,
    detail: str | None = None,
    exit_status: int | None = None,
    session: bool = True,
) -> FleetAgentStatus:
    agent = FleetAgent(
        id="agt_1",
        project_id="prj_1",
        label=label,
        role=role,
        pane_id="%4",
        cwd=CWD,
        task_id="tsk_0123456789abcdef",
        worktree=True,
        created_at=NOW,
        exit_status=exit_status,
        account_slot=slot,
    )
    team = TeamSession(
        id="sess-1",
        project_id="prj_1",
        role=role,
        started_at=NOW,
        last_seen_at=NOW,
        model=model,
        effort=effort,
        account=account,
    )
    return FleetAgentStatus(
        agent=agent,
        state=state,
        detail=detail,
        session=team if session else None,
        tmux_session="asq-amber-otter",
    )


def _segments(text: Text) -> list[tuple[str, str]]:
    """Each styled field in order, as ``(text, style)`` — the separators carry no style."""
    return [
        (text.plain[span.start : span.end], str(span.style))
        for span in sorted(text.spans, key=lambda span: span.start)
    ]


def _style_of(text: Text, field: str) -> str:
    """The style of the one span whose text is ``field``."""
    (style,) = [style for shown, style in _segments(text) if shown == field]
    return style


# --- one builder for both views ---------------------------------------------------------


def test_the_manager_tabs_first_line_is_the_agent_views_info_line() -> None:
    """Acceptance (a): the same status gives the same line, text and styles, on both."""
    for status in (
        _status(label="manager", role="manager", state="waiting", slot=2),
        _status(state="exited", exit_status=1, detail="exit 1"),
        _status(slot=None, account=None, model=None, effort=None, session=False),
    ):
        usage = {2: 42.0, 3: 87.0}
        line = info_line(status, LABELS, usage)
        first, hint = manager_text(status, LABELS, usage).split("\n")
        assert first.plain == line.plain
        assert first.spans == line.spans
        assert hint.plain == MANAGER_HINT and _segments(hint) == [(MANAGER_HINT, "dim")]


def test_the_info_line_reads_in_the_manager_lines_shape_with_effort_and_account() -> None:
    manager = _status(label="manager", role="manager", state="waiting", effort="xhigh", slot=2)
    coder = _status()
    assert info_line(manager, LABELS, {2: 42.0}).plain == (
        "🧭 manager  ⏸ waiting  claude-opus-5-5 xhigh  work · 42%  %4"
    )
    assert info_line(coder, LABELS, {3: 87.0}).plain == (
        "🔨 coder-auth  ▶ working  claude-opus-5-5 high  dev · 87%  %4"
    )


def test_the_effort_follows_the_model_and_is_left_out_while_unknown() -> None:
    assert "claude-opus-5-5 high  dev" in info_line(_status(), LABELS).plain
    without = info_line(_status(effort=None), LABELS).plain
    assert "claude-opus-5-5  dev" in without and "high" not in without
    # An effort with no model still shows, in its own place.
    alone = info_line(_status(model=None, effort="low"), LABELS)
    assert "▶ working  low  dev" in alone.plain
    assert _style_of(alone, "low") == _style_of(info_line(_status(), LABELS), "high")


def test_the_account_is_its_name_and_the_session_use_beside_it() -> None:
    status = _status(slot=2)
    assert "  work · 42%  %4" in info_line(status, LABELS, {2: 42.0}).plain
    # Another slot's figure is not this one's, and no figure is no " · ".
    assert info_line(status, LABELS, {3: 42.0}).plain.endswith("  work  %4")
    assert info_line(status, LABELS).plain.endswith("  work  %4")
    # Without the registry's labels, the built-in name — never "#2".
    assert info_line(status, None, {2: 42.0}).plain.endswith("  account 2 · 42%  %4")


def test_with_no_slot_the_sessions_account_alone_and_with_neither_no_account() -> None:
    by_hand = _status(slot=None, account="/somewhere/.claude-work")
    line = info_line(by_hand, LABELS, {1: 10.0, 2: 20.0, 3: 30.0}).plain
    assert line.endswith("  .claude-work  %4"), line  # the label alone: no figure, no " · "
    neither = info_line(_status(slot=None, account=None), LABELS, {3: 30.0}).plain
    assert neither == "🔨 coder-auth  ▶ working  claude-opus-5-5 high  %4"


def test_the_figure_is_the_session_window_never_the_weekly_one() -> None:
    """The figure comes from the headroom read's ``session_by_slot``: an account that has
    spent most of its week but little of its five hours reads its five hours."""
    account = ClaudeAccount(slot=3, config_dir=Path("/accounts/3"))
    reading = ClaudeUsage(available=True, session_percent=12.0, week_percent=95.0, fetched_at=NOW)
    summary = accounts_service.fleet_headroom([account], {3: reading})
    assert summary.session_by_slot == {3: 12.0}
    assert summary.left_by_slot == {3: 5.0}  # the pick still ranks on the fuller window
    line = info_line(_status(), LABELS, summary.session_by_slot).plain
    assert "dev · 12%" in line and "95" not in line and "88" not in line


@pytest.mark.parametrize(
    ("percent", "shown", "colour"),
    [
        (0.0, "0%", GREEN),
        (69.0, "69%", GREEN),
        (69.4, "69%", GREEN),
        (69.6, "70%", YELLOW),  # the colour is the shown figure's
        (70.0, "70%", YELLOW),
        (89.0, "89%", YELLOW),
        (90.0, "90%", RED),
        (100.0, "100%", RED),
    ],
)
def test_the_session_figure_is_green_then_yellow_from_70_then_red_from_90(
    percent: float, shown: str, colour: str
) -> None:
    figure = session_percent_text(percent)
    assert (figure.plain, str(figure.style)) == (shown, colour)
    # The same figure, the same colour, in both places it is shown.
    assert _style_of(info_line(_status(), LABELS, {3: percent}), shown) == colour
    row = agent_row_text(_status(), percent)
    assert _style_of(row, shown) == colour and row.plain.rstrip().endswith(f"▶ #3·{shown}")


def test_no_hash_tmux_command_role_task_or_working_directory_in_the_info_line() -> None:
    for state, exit_status in (("working", None), ("exited", 2)):
        line = info_line(
            _status(state=state, exit_status=exit_status, slot=2), LABELS, {2: 55.0}
        ).plain
        assert "#" not in line
        assert "tmux" not in line and "attach" not in line and "asq-amber-otter" not in line
        assert " coder " not in f"{line} " and "  coder" not in line  # the role word
        assert "task" not in line and "89abcdef" not in line
        assert str(CWD) not in line and "⎇" not in line


def test_an_exited_row_says_its_exit_once() -> None:
    line = info_line(_status(state="exited", exit_status=1, detail="exit 1"), LABELS).plain
    assert "💤 exited(1)" in line
    assert "exit 1" not in line and "exited 1" not in line  # not a second time
    # Another detail on an exited row still shows.
    gone = info_line(_status(state="exited", exit_status=None, detail="exit 3"), LABELS).plain
    assert "💤 exited  exit 3" in gone


def test_no_two_neighbouring_fields_share_a_style() -> None:
    """Each field reads apart from the next: the label bold, the state its chip's colour,
    the model dim cyan, then the effort, the account and the pane id each a style of their
    own — in every state, including the dim ``💤`` and ``·`` chips beside a dim field."""
    cases = [
        _status(),
        _status(state="attention", detail="Claude needs your permission"),
        _status(state="limited", detail="resets 14:00", model=None),
        _status(state="exited", exit_status=1, detail="exit 1"),
        _status(state="exited", session=False, slot=None),  # the chip, then the pane id
        _status(state="unknown", detail="tmux unavailable", session=False, slot=None),
        _status(state="lost", detail="pane gone", effort=None),
        _status(model=None, effort=None),
    ]
    for status in cases:
        for percent in (None, 10.0, 75.0, 95.0):
            usage = {} if percent is None else {3: percent}
            segments = _segments(info_line(status, LABELS, usage))
            for (left, left_style), (right, right_style) in pairwise(segments):
                assert left_style != right_style, (status.state, left, right, left_style)
    full = info_line(_status(), LABELS, {3: 50.0})
    assert _style_of(full, "coder-auth") == "bold"
    assert _style_of(full, "▶ working") == "green"
    assert _style_of(full, "claude-opus-5-5") == "dim cyan"
    distinct = {_style_of(full, field) for field in ("high", "dev", "%4", "claude-opus-5-5")}
    assert len(distinct) == 4, distinct
    assert "dim" in _style_of(full, "%4").split()


def test_bracketed_labels_and_model_names_render_literally() -> None:
    status = _status(
        label="[bold]coder[/bold]",
        model="[red]opus[/]",
        effort="[x]",
        slot=2,
        detail="[link=x]y[/link]",
        state="attention",
    )
    labels = {2: "[i]work[/i]"}
    line = info_line(status, labels, {2: 30.0}).plain
    for literal in ("[bold]coder[/bold]", "[red]opus[/]", "[x]", "[i]work[/i]", "[link=x]y[/link]"):
        assert literal in line
    # …and through manager_text, which a Static renders as it is handed.
    assert "[bold]coder[/bold]" in manager_text(status, labels).plain
