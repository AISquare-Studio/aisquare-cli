"""What needs you, over the board (#286): the strip, the empty state, and what they read.

The strip counts with the captain's attention queue (``queue.observe``) over what the
board's own tick read, so these pin both halves: the counts it shows for a board, and
that showing them asks nothing of tmux or the store beyond the tick.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, DataTable

from aisquare.cli.ui import board as board_mod
from aisquare.cli.ui.board import EMPTY_ROW, NOTHING_NEEDS_YOU, BoardPanel, NeedsYou, needs_you
from aisquare.core import tmux as tmux_core
from aisquare.models import ProjectInfo, TaskStatus, TeamEvent, TeamSession, TeamTask
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from aisquare.services.captain import queue
from tests.ui_workers import settle_page

T = TypeVar("T")
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
PROJECT = ProjectInfo(id="prj_board", root=Path("/tmp/board-project"))


def _session(sid: str, role: str, label: str, state: str = "working") -> TeamSession:
    return TeamSession(
        id=sid,
        project_id=PROJECT.id,
        role=role,
        label=label,
        started_at=NOW - timedelta(minutes=20),
        last_seen_at=NOW - timedelta(minutes=1),
        state=state,
    )


def _task(number: int, title: str, status: TaskStatus) -> TeamTask:
    return TeamTask(
        id=f"tsk_{number:04d}",
        project_id=PROJECT.id,
        number=number,
        key=f"task-{number}",
        title=title,
        status=status,
        created_at=NOW - timedelta(minutes=30),
        updated_at=NOW - timedelta(minutes=5),
    )


def _event(seq: int, session_id: str, kind: str, text: str, **extra: Any) -> TeamEvent:
    return TeamEvent(
        seq=seq,
        id=f"evt_{seq}",
        project_id=PROJECT.id,
        session_id=session_id,
        kind=kind,
        text=text,
        created_at=NOW - timedelta(minutes=10) + timedelta(seconds=seq),
        **extra,
    )


MANAGER = _session("ses_manager", "manager", "manager")
CODER = _session("ses_coder", "coder", "coder-auth", state="attention")
BOARD_TASKS = [
    _task(1, "wire auth", "doing"),
    _task(2, "fix the login race", "blocked"),
    _task(3, "add the retry", "review"),
    _task(4, "write the docs", "todo"),
]


def test_a_board_counts_what_waits_on_you_and_nothing_else() -> None:
    """1 blocked, 1 in review and 1 pane on a permission prompt: those three, by the
    captain queue's own rules — a doing or todo card, and a working manager, are not."""
    found = needs_you(PROJECT, [MANAGER, CODER], BOARD_TASKS, [], seq=0, now=NOW)

    assert found.tasks == {"blocked": ["tsk_0002"], "review": ["tsk_0003"]}
    assert found.agents == {"waiting": ["coder-auth"]}
    assert found.line() == (
        "needs you: 🔔 1 waiting · 👀 1 in review · ⛔ 1 blocked — click one to go to it"
    )


def test_a_card_a_reviewer_gated_and_a_quiet_board_need_nothing() -> None:
    entered = {"created_at": NOW - timedelta(hours=2), "updated_at": NOW - timedelta(hours=1)}
    tasks = [_task(3, "add the retry", "review").model_copy(update=entered)]
    gated = [_event(1, "ses_reviewer", "result", "PASS", task_id="tsk_0003")]

    found = needs_you(PROJECT, [MANAGER], tasks, gated, seq=1, now=NOW)

    assert found == NeedsYou() and found.line() == NOTHING_NEEDS_YOU


def test_a_question_counts_until_its_asker_speaks_again() -> None:
    """The queue's question rows are resolved by the owner, which a board cannot see; the
    strip counts one while it is still the asker's last word and the asker is live."""
    asked = [_event(1, "ses_manager", "question", "staging or prod first?")]
    open_q = needs_you(PROJECT, [MANAGER], [], asked, seq=1, now=NOW)
    moved_on = [*asked, _event(2, "ses_manager", "note", "going with staging")]
    answered = needs_you(PROJECT, [MANAGER], [], moved_on, seq=2, now=NOW)
    gone = [MANAGER.model_copy(update={"ended_at": NOW})]
    ended = needs_you(PROJECT, gone, [], asked, seq=1, now=NOW)
    to_the_manager = [_event(1, "ses_coder", "question", "which branch?", to_role="manager")]
    not_mine = needs_you(PROJECT, [MANAGER, CODER], [], to_the_manager, seq=1, now=NOW)

    assert open_q.agents == {"question": ["manager"]} and open_q.count("question") == 1
    assert answered.count("question") == 0
    assert ended.count("question") == 0
    assert not_mine.count("question") == 0, "a coder's question to a live manager is its"


# --- the panel ------------------------------------------------------------------------


class Host(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.wanted: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        yield BoardPanel(PROJECT, interval=3600, id="board")

    def on_board_panel_agent_wanted(self, event: BoardPanel.AgentWanted) -> None:
        self.wanted.append((event.project_id, event.label))


@pytest.fixture
def board(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """The board's one read scripted; any other read recorded and refused: a store
    session opened by ``services.team`` or the captain's queue, the fleet's agents (which
    ask tmux), or tmux itself. The strip may ask for nothing beyond the tick."""
    frame: dict[str, Any] = {"sessions": [MANAGER, CODER], "tasks": BOARD_TASKS, "events": []}
    reads: list[str] = []

    def board_read(**_kwargs: Any) -> team_service.BoardRead:
        reads.append("board_read")
        return team_service.BoardRead(
            PROJECT, frame["sessions"], frame["tasks"], frame["events"], {}
        )

    def refused(name: str) -> Callable[..., Any]:
        def read(*_a: Any, **_k: Any) -> Any:
            reads.append(name)
            raise AssertionError(f"the board read {name}")

        return read

    monkeypatch.setattr(team_service, "board_read", board_read)
    monkeypatch.setattr(team_service, "store_session", refused("team.store_session"))
    monkeypatch.setattr(queue, "store_session", refused("queue.store_session"))
    monkeypatch.setattr(fleet_service, "list_agents", refused("fleet.list_agents"))
    monkeypatch.setattr(tmux_core, "_tmux", refused("tmux"))
    frame["reads"] = reads
    yield frame


def drive(fn: Callable[[Host, BoardPanel], Awaitable[T]]) -> T:
    async def run() -> T:
        app = Host()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await settle_page(app)
            return await fn(app, app.query_one(BoardPanel))

    return asyncio.run(run())


def strip_line(panel: BoardPanel) -> str:
    """The strip as it shows: its lead, each button shown, its tail."""
    parts: list[str] = []
    for widget in panel.query_one(board_mod.NeedsYouStrip).children:
        if not widget.display:
            continue
        if isinstance(widget, Button):
            parts.append(str(widget.label))
        else:
            text = getattr(getattr(widget, "visual", None), "plain", "")
            if text.strip():
                parts.append(text.strip())
    return " ".join(parts)


def test_the_strip_shows_the_counts_and_reads_nothing_beyond_the_tick(
    board: dict[str, Any],
) -> None:
    async def go(app: Host, panel: BoardPanel) -> tuple[str, list[str]]:
        before = len(board["reads"])
        panel.refresh_data()
        await settle_page(app)
        return strip_line(panel), board["reads"][before:]

    line, reads = drive(go)
    assert line == "needs you: 🔔 1 waiting 👀 1 in review ⛔ 1 blocked — click one to go to it"
    assert reads == ["board_read"], "one tick, one read: the strip asked for nothing more"


def test_nothing_needs_you_and_no_open_tasks_say_so(board: dict[str, Any]) -> None:
    board["sessions"] = [MANAGER]
    board["tasks"] = [_task(1, "shipped", "done")]

    async def go(app: Host, panel: BoardPanel) -> tuple[str, list[Any], str]:
        table = panel.query_one("#tasks", DataTable)
        rows = [key.value for key in table.rows]
        body = table.get_row(EMPTY_ROW)[2].plain
        return strip_line(panel), rows, body

    line, rows, body = drive(go)
    assert line == NOTHING_NEEDS_YOU
    assert rows == [EMPTY_ROW]
    assert " ".join(body.split()) == board_mod.EMPTY_TASKS


def test_a_count_goes_to_the_first_thing_it_counts(board: dict[str, Any]) -> None:
    async def go(app: Host, panel: BoardPanel) -> tuple[Any, str, list[tuple[str, str]]]:
        panel.query_one("#needs-you-blocked", Button).press()
        await settle_page(app)
        table = panel.query_one("#tasks", DataTable)
        cursor = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        detail = panel.detail_text
        panel.query_one("#needs-you-waiting", Button).press()
        await settle_page(app)
        return cursor, detail, app.wanted

    cursor, detail, wanted = drive(go)
    assert cursor == "tsk_0002" and "fix the login race" in detail
    assert wanted == [(PROJECT.id, "coder-auth")], "the pane's agent, for the shell to select"


def drive_at(width: int, fn: Callable[[Host, BoardPanel], Awaitable[T]]) -> T:
    async def run() -> T:
        app = Host()
        async with app.run_test(size=(width, 30)) as pilot:
            await pilot.pause()
            await settle_page(app)
            await pilot.pause()
            return await fn(app, app.query_one(BoardPanel))

    return asyncio.run(run())


WORDED = "needs you: 🔔 1 waiting ❓ 1 question 👀 1 in review ⛔ 1 blocked"


@pytest.mark.parametrize(
    ("width", "line"),
    [
        (140, f"{WORDED} — click one to go to it"),
        (80, WORDED),
        (50, "needs you: 🔔 1 ❓ 1 👀 1 ⛔ 1"),
    ],
)
def test_the_strip_fits_the_width_it_has(board: dict[str, Any], width: int, line: str) -> None:
    """Inside `asq` at 80 columns the board is about 50 wide: the whole line ran off it,
    two counts with it. It drops its tail, then its words, before anything is cut."""
    board["events"] = [_event(1, "ses_manager", "question", "staging or prod first?")]

    async def go(app: Host, panel: BoardPanel) -> tuple[str, list[str]]:
        strip = panel.query_one(board_mod.NeedsYouStrip)
        cut = [
            str(child.id)
            for child in strip.children
            if child.display and child.region.right > strip.region.right
        ]
        return strip_line(panel), cut

    shown, cut = drive_at(width, go)
    assert shown == line
    assert cut == [], f"run past the strip at {width} columns: {cut}"


def test_nothing_needs_you_shortens_rather_than_runs_off(board: dict[str, Any]) -> None:
    board["sessions"] = [MANAGER]
    board["tasks"] = []

    async def go(app: Host, panel: BoardPanel) -> str:
        return strip_line(panel)

    assert drive_at(50, go) == board_mod.NOTHING_SHORT
    assert drive_at(140, go) == NOTHING_NEEDS_YOU
