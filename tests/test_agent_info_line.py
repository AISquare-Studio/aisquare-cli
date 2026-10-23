"""The agent info line: one builder for the Agent view's header and the Manager tab's first line.

The two were separate builders that had drifted apart (the Manager tab named the model and
the tmux command, the Agent view the role, the task and the working directory). Now there is
one, :func:`~aisquare.cli.ui.views.agent.info_line`, in the manager line's shape with the
effort and the account added, in the order the owner ruled on #267 — what matters most at a
glance first, the unbounded detail last but the pane id:
``🔨 coder-auth  ▶ working  dev · 87%  claude-opus-5-5 high  %4``. The account is its NAME (no
``#slot``) and the figure beside it the five-hour window, percent USED, never the weekly one.
The sidebar's rows say ``#<slot>·<pct>%`` with the same colours, from the same helper
(:func:`~aisquare.cli.ui.sidebar.session_percent_text`).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest
from rich.text import Text
from textual.widgets import Static

from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.sidebar import (
    BADGE_KEEP,
    GREEN,
    RED,
    YELLOW,
    agent_row_text,
    session_percent_text,
)
from aisquare.cli.ui.views.agent import info_line
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.agent_adapters.types import AgentEvent
from aisquare.models import (
    AccountsOverview,
    ClaudeAccount,
    ClaudeAccountStatus,
    ClaudeInstall,
    ClaudeUsage,
    FleetAgent,
    FleetAgentStatus,
    HeadroomSummary,
    TeamSession,
)
from aisquare.services import claude_accounts as accounts_service
from tests import test_ui_shell as ui_suite
from tests.test_codex_contract import ROLLOUT as CODEX_ROLLOUT
from tests.test_codex_contract import TURN_CONTEXT
from tests.test_ui_shell import Script, on_screen, on_slot, row_for, seed, settle

# The shell suite's fixtures, bound here so pytest finds them for this module's tests
# (``no_real_tmux`` is autouse there: every tmux call held to a private socket).
no_real_tmux = ui_suite.no_real_tmux
script = ui_suite.script

FIXTURES = Path(__file__).parent / "fixtures"

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
    persona: str | None = None,
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
        persona=persona,
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
#
# Acceptance (a) — the Manager tab's line is the Agent view's for the same status — holds by
# construction: ``ManagerTab`` paints :func:`info_line` itself. It is checked mounted, text
# and styles, in tests/test_ui_shell.py
# (``test_at_90_columns_the_manager_tab_cuts_its_info_line_and_shows_its_hint_whole``).


def test_the_info_line_reads_in_the_owners_order_with_effort_and_account() -> None:
    """``<icon> <label>  <chip> <state>[(exit)]  <nickname> · <pct>%  <model> <effort>
    [detail]  <pane id>`` (the owner's rulings on #267): the account and its use right after
    the state, the model and effort after them, the detail last but the pane id."""
    manager = _status(label="manager", role="manager", state="waiting", effort="xhigh", slot=2)
    coder = _status()
    assert info_line(manager, LABELS, {2: 42.0}).plain == (
        "🧭 manager  ⏸ waiting  work · 42%  claude-opus-5-5 xhigh  %4"
    )
    assert info_line(coder, LABELS, {3: 87.0}).plain == (
        "🔨 coder-auth  ▶ working  dev · 87%  claude-opus-5-5 high  %4"
    )
    full = _status(state="attention", detail="Claude needs your permission", slot=2)
    assert info_line(full, LABELS, {2: 42.0}).plain == (
        "🔨 coder-auth  🔔 attention  work · 42%  claude-opus-5-5 high  "
        "Claude needs your permission  %4"
    )


def _cut(line: Text, width: int) -> str:
    """What a ``nowrap`` / ``text-overflow: ellipsis`` row of ``width`` cells shows of ``line``."""
    if line.cell_len <= width:
        return line.plain
    shown = line.copy()
    shown.truncate(width, overflow="ellipsis")
    return shown.plain


def test_a_line_too_wide_gives_way_in_its_label_first() -> None:
    """The owner's ruling 3 on #267: at about 37 cells (the Agent view in a 90-column
    terminal) a spawned agent's label pushed ``max · 42%`` off the line. The label is cut
    first, as the sidebar's rows cut theirs, so ``<icon> <label…>  <chip> <state>  <account>
    · <pct>%`` and the ``…`` that cuts the rest fit; model, effort, detail and pane id are
    cut after that, at the line's end. A line that fits keeps its whole label."""
    usage = {1: 42.0, 2: 42.0, 3: 42.0, 4: 42.0}
    labels = {2: "work", 3: "max"}  # 1 and 4 unaliased: "plain claude", "account 4"
    coder = _status(label="coder-01k8zq7m", slot=3)
    line = info_line(coder, labels, usage, width=37)
    assert _cut(line, 37) == "🔨 coder-01k8…  ▶ working  max · 42%…"
    assert line.plain.endswith("max · 42%  claude-opus-5-5 high  %4")  # the rest, there to cut
    wide = info_line(coder, labels, usage, width=87)
    assert wide.plain == "🔨 coder-01k8zq7m  ▶ working  max · 42%  claude-opus-5-5 high  %4"
    assert info_line(coder, labels, usage).plain == wide.plain  # no width: the whole label
    # A label that fits its room is not cut, though the rest is.
    assert _cut(info_line(_status(slot=3), labels, usage, width=37), 37) == (
        "🔨 coder-auth  ▶ working  max · 42% …"
    )


def test_the_label_keeps_six_characters_and_a_narrower_row_is_cut_at_its_end() -> None:
    """Ruling 3's floor, with the labels ``fleet spawn`` gives (``<role>-<task id[:8]>``) and
    the built-in names of unaliased accounts: ``plain claude · 42%`` cannot stay whole beside
    even ``coder-…``, so the label stops at six characters and the line is cut at its end."""
    usage = {1: 42.0, 2: 42.0, 4: 42.0}
    labels = {2: "work"}

    def at_37(**fields: object) -> str:
        return _cut(info_line(_status(**fields), labels, usage, width=37), 37)  # type: ignore[arg-type]

    assert at_37(label="coder-01m5d34v", slot=1) == "🔨 coder-…  ▶ working  plain claude …"
    assert at_37(label="tester-01m5d34v", role="tester", state="waiting", slot=4) == (
        "🧪 tester…  ⏸ waiting  account 4 · 4…"
    )
    assert at_37(label="coder-01m5d34v", state="attention", slot=2) == (
        "🔨 coder-…  🔔 attention  work · 42%…"
    )
    # The manager's label is shorter than the floor: never cut.
    manager = _status(label="manager", role="manager", state="waiting", slot=1)
    assert info_line(manager, labels, usage, width=37).plain.startswith("🧭 manager  ⏸")


def test_the_effort_follows_the_model_and_is_left_out_while_unknown() -> None:
    assert "dev  claude-opus-5-5 high  %4" in info_line(_status(), LABELS).plain
    without = info_line(_status(effort=None), LABELS).plain
    assert "dev  claude-opus-5-5  %4" in without and "high" not in without
    # An effort with no model still shows, in its own place.
    alone = info_line(_status(model=None, effort="low"), LABELS)
    assert "▶ working  dev  low  %4" in alone.plain
    assert _style_of(alone, "low") == _style_of(info_line(_status(), LABELS), "high")


def test_the_account_is_its_name_and_the_session_use_beside_it() -> None:
    status = _status(slot=2)
    assert "▶ working  work · 42%  claude-opus-5-5" in info_line(status, LABELS, {2: 42.0}).plain
    # Another slot's figure is not this one's, and no figure is no " · ".
    assert "▶ working  work  claude-opus-5-5" in info_line(status, LABELS, {3: 42.0}).plain
    assert "▶ working  work  claude-opus-5-5" in info_line(status, LABELS).plain
    # Without the registry's labels, the built-in name — never "#2".
    assert "▶ working  account 2 · 42%  claude" in info_line(status, None, {2: 42.0}).plain


def test_with_no_slot_the_sessions_account_alone_and_with_neither_no_account() -> None:
    by_hand = _status(slot=None, account="/somewhere/.claude-work")
    line = info_line(by_hand, LABELS, {1: 10.0, 2: 20.0, 3: 30.0}).plain
    assert "▶ working  .claude-work  claude-opus-5-5" in line, line  # no figure, no " · "
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


def test_a_persona_badge_gives_way_before_the_session_figure_never_after() -> None:
    """Review of #267's merge, M-F1: the badge came after a label and figure already sized to
    fill the row, so the row's own ellipsis ate the ``%`` and the badge was never seen. Now
    the label's padding gives way first, then the badge is cut, and dropped below
    ``BADGE_KEEP`` cells; the row is never wider than its width, so the figure stays whole."""
    for width in (25, 30, 44, 60):
        for persona in ("skeptic", "a-very-long-persona-name"):
            status = _status(
                label="manager", role="manager", state="waiting", slot=1, persona=persona
            )
            row = agent_row_text(status, 42.0, width=width)
            plain = row.plain
            assert row.cell_len <= width, (width, plain)
            assert plain.endswith("⏸ #1·42%") or "⏸ #1·42% · " in plain, (width, plain)
            badge = plain.partition("#1·42%")[2]
            assert badge.startswith(" · ") and len(badge) >= BADGE_KEEP, (width, plain)
            if persona in badge:
                assert plain.endswith(persona)
            else:
                assert badge.endswith("…"), (width, plain)
    # A row with room for everything shows the whole badge, the label still padded.
    wide = agent_row_text(_status(slot=1, persona="skeptic"), 42.0, width=60).plain
    assert wide == "🔨 coder-auth    ▶ #1·42% · skeptic"
    # Too narrow even for a cut badge: it is dropped, the figure is not.
    tight = agent_row_text(_status(slot=1, persona="skeptic"), 42.0, width=25).plain
    assert tight.rstrip().endswith("▶ #1·42%") and "·sk" not in tight and " · " not in tight


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
    # Another detail on an exited row still shows, in the detail's place.
    gone = info_line(_status(state="exited", exit_status=None, detail="exit 3"), LABELS).plain
    assert gone == "🔨 coder-auth  💤 exited  dev  claude-opus-5-5 high  exit 3  %4"


def test_the_detail_comes_after_the_account_and_the_model_before_the_pane() -> None:
    """The owner's ruling on #267: a detail has no bound, and a line too long for its row is
    cut at its end, so the detail goes after ``account · pct%`` and the model and effort —
    what gives way first."""
    status = _status(state="attention", detail="Claude needs your permission", slot=2)
    assert info_line(status, LABELS, {2: 42.0}).plain.endswith(
        "work · 42%  claude-opus-5-5 high  Claude needs your permission  %4"
    )
    # Without an account, the detail follows the effort; without a session, the state.
    alone = _status(state="unknown", detail="tmux unavailable", session=False, slot=None)
    assert info_line(alone, LABELS).plain == "🔨 coder-auth  · unknown  tmux unavailable  %4"
    bare = _status(state="lost", detail="pane gone", slot=None)
    assert info_line(bare, LABELS).plain.endswith("claude-opus-5-5 high  pane gone  %4")


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


# --- a Codex row (SCOPE #113 0.8.2, A8) ------------------------------------------------------
#
# Codex's hooks carry no effort, and its account's use is the rollout's own primary window
# (``token_count.rate_limits``, measured on 0.162.1 as 42 % of a 300-minute window, M9). The
# readings below are those fixtures, read through the code that reads them in the app:
# ``CodexAdapter.decode_event`` for the model and effort of the turn, the Codex accounts'
# ``usage`` and ``fleet_headroom`` for the slot's figure, and ``session_by_slot`` for the one
# map every agent's slots share with Claude Code's.

CODEX_SLOT = 5


def _codex_slot(home: Path) -> ClaudeAccount:
    """A Codex slot whose one session recorded 42 % of its five-hour window (M9)."""
    rollout = home / "sessions" / "2026" / "10" / "23" / "rollout-2026-10-23T15-52-00-a.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    resets = int((datetime.now(tz=UTC) + timedelta(hours=2)).timestamp())
    records = [
        {"type": "session_meta", "payload": {"id": "a", "cli_version": "0.162.1"}},
        {
            "timestamp": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": None,
                "rate_limits": {
                    "limit_id": "codex",
                    "primary": {
                        "used_percent": 42.0,
                        "window_minutes": 300,
                        "resets_at": resets,
                    },
                    "secondary": None,
                },
            },
        },
    ]
    rollout.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return ClaudeAccount(slot=CODEX_SLOT, config_dir=home, managed=True, agent="codex")


def _codex_turn(tmp_path: Path) -> AgentEvent:
    """Codex's prompt as its hook hands it over, over a rollout whose newest turn ran with
    ``model_reasoning_effort="high"`` (the recorded fixtures)."""
    rollout = tmp_path / "rollout.jsonl"
    head = CODEX_ROLLOUT.read_text(encoding="utf-8").splitlines()[:74]
    rollout.write_text("".join(f"{line}\n" for line in [*head, TURN_CONTEXT]), encoding="utf-8")
    events = json.loads((FIXTURES / "agent_events" / "codex.json").read_text("utf-8"))["events"]
    payload = {**events["UserPromptSubmit"]["payload"], "transcript_path": str(rollout)}
    event = get_adapter("codex").decode_event({**payload, "hook_event_name": "UserPromptSubmit"})
    assert event is not None
    return event


def _codex_status(turn: AgentEvent, state: str = "working") -> FleetAgentStatus:
    agent = FleetAgent(
        id="agt_codex",
        project_id="prj_1",
        label="coder-1",
        role="coder",
        agent="codex",
        binary="codex",
        pane_id="%7",
        cwd=CWD,
        created_at=NOW,
        account_slot=CODEX_SLOT,
    )
    team = TeamSession(
        id="sess-codex",
        project_id="prj_1",
        role="coder",
        started_at=NOW,
        last_seen_at=NOW,
        model=turn.model,
        effort=turn.effort,
        agent="codex",
    )
    return FleetAgentStatus(agent=agent, state=state, session=team)


def test_a_codex_rows_line_and_sidebar_figure_come_from_its_own_readings(tmp_path: Path) -> None:
    account = _codex_slot(tmp_path / "codex-5")
    reading = accounts_service.usage(account)
    codex = accounts_service.fleet_headroom([account], {CODEX_SLOT: reading})
    claude = accounts_service.fleet_headroom(
        [ClaudeAccount(slot=2, config_dir=tmp_path / "claude-2", managed=True)],
        {2: ClaudeUsage(available=True, session_percent=87.0, week_percent=10.0)},
    )
    # One map for every agent's slots: Claude Code's summary first, as the app reads them.
    usage = accounts_service.session_by_slot([claude, codex])
    assert usage == {2: 87.0, CODEX_SLOT: 42.0}
    status = _codex_status(_codex_turn(tmp_path))
    line = info_line(status, {CODEX_SLOT: "codex-1"}, usage)
    assert line.plain == "🔨 coder-1  codex  ▶ working  codex-1 · 42%  gpt-5.5-codex high  %7"
    assert agent_row_text(status, usage.get(CODEX_SLOT)).plain.endswith("▶ #5·42%")
    # Control: Claude Code's summary alone — what the line read before — has no figure for it.
    alone = info_line(status, {CODEX_SLOT: "codex-1"}, claude.session_by_slot).plain
    assert "codex-1  gpt-5.5-codex high" in alone and "42%" not in alone


def test_the_sidebar_and_the_info_line_show_a_codex_rows_figure_beside_claudes(
    tmp_path: Path, script: Script
) -> None:
    """Mounted: the app reads one headroom summary per agent, and the figures the rows and
    the info lines show are every agent's slots — the first summary alone (Claude Code's)
    left a Codex row on ``#5`` with no figure."""
    codex_account = _codex_slot(tmp_path / "codex-5")
    claude_account = ClaudeAccount(slot=2, config_dir=tmp_path / "claude-2", managed=True)
    overview = AccountsOverview(
        claude=ClaudeInstall(installed=True),
        accounts=[
            ClaudeAccountStatus(account=account, label=f"account {account.slot}", signed_in=True)
            for account in (claude_account, codex_account)
        ],
    )

    def headroom(accounts: Sequence[ClaudeAccount] | None) -> HeadroomSummary:
        listed = list(accounts or [])
        readings = {
            account.slot: (
                accounts_service.usage(account)
                if account.agent == "codex"
                else ClaudeUsage(available=True, session_percent=87.0)
            )
            for account in listed
        }
        return accounts_service.fleet_headroom(listed, readings)

    seed(tmp_path, ("prj_a", "alpha", None))
    codex_row = on_slot(ui_suite.status("prj_a", "coder-1", "coder", "working", minute=1), 5)
    codex_row = codex_row.model_copy(
        update={"agent": codex_row.agent.model_copy(update={"agent": "codex"})}
    )
    script["prj_a"] = [on_slot(ui_suite.status("prj_a", "manager", "manager", "waiting"), 2)]
    script["prj_a"].append(codex_row)

    async def run() -> tuple[str, str, str]:
        app = FleetApp(
            refresh_seconds=3600, doctor=lambda: [], accounts=lambda: overview, headroom=headroom
        )
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(app)
            await pilot.click(row_for(app, "agt_a_coder-1"))
            await settle(app)
            header = app.query_one("#agent-agt_a_coder-1 #agent-header", Static)
            return (
                on_screen(row_for(app, "agt_a_manager")),
                on_screen(row_for(app, "agt_a_coder-1")),
                str(header.render()),
            )

    manager, coder, header = asyncio.run(run())
    assert manager.rstrip().endswith("⏸ #2·87%"), manager
    assert coder.rstrip().endswith("▶ #5·42%"), coder
    assert "account 5 · 42%" in header, header
