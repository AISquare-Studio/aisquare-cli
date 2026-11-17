"""The board names each agent as asq's left pane does, not as ``role·<id>`` (#140).

The owner, verbatim: "the board currently has persona.<id> sort of format which is impossible to
understand, can that be just the name shown on left pane". On the HUMAN board — asq's board view,
``board -w`` and ``aisquare board``'s text — a session is its fleet row's label, live or ended, as
a relabel left it, else the label it recorded, else its role (the manager has none). A line no
session wrote is "you" in the views and "owner" in ``aisquare board``'s text, which agents run
too. Two sessions of one name are told apart only then. The agent-facing text (hook deltas, the
start pack, the MCP board, ``team verify``) and every ``--json`` keep ids.

A session here records NO label, as the hooks write it: its name is its fleet row's
(``FLEET``, as ``team.fleet_labels`` reads it). The pins at the end go through a real store and a
real fleet: an agent that has ended keeps its name, and a stop with the board open renames none
of its lines (tester-rlx-board, #140).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.widgets import OptionList

from aisquare.cli import board_names, watch
from aisquare.cli.ui.board import BoardPanel, needs_you
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo, TeamEvent, TeamSession, TeamTask
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from tests import test_fleet_service as fleet_suite
from tests.test_fleet_service import FakeTmux, _add_task
from tests.ui_workers import settle_page

# The fleet suite's fixtures, bound here so pytest finds them for the real-store pins.
tmux = fleet_suite.tmux
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project

NOW = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
PROJECT = ProjectInfo(id="prj_names", root=Path("/tmp/names-project"))


def _session(
    sid: str,
    role: str,
    *,
    label: str | None = None,
    started: int = 60,
    ended: bool = False,
    state: str = "working",
) -> TeamSession:
    return TeamSession(
        id=sid,
        project_id=PROJECT.id,
        role=role,
        label=label,
        started_at=NOW - timedelta(minutes=started),
        last_seen_at=NOW - timedelta(minutes=1),
        ended_at=NOW - timedelta(minutes=5) if ended else None,
        state=state,
    )


def _event(seq: int, session_id: str | None, kind: str = "note", text: str = "hi") -> TeamEvent:
    return TeamEvent(
        seq=seq,
        id=f"evt_{seq}",
        project_id=PROJECT.id,
        session_id=session_id,
        kind=kind,
        text=text,
        created_at=NOW - timedelta(minutes=2),
    )


def _task(holder: str | None) -> TeamTask:
    return TeamTask(
        id="tsk_0001",
        project_id=PROJECT.id,
        number=1,
        key="wire-auth",
        title="wire auth",
        status="doing" if holder else "todo",
        claimed_by=holder,
        created_at=NOW - timedelta(minutes=30),
        updated_at=NOW - timedelta(minutes=5),
    )


MANAGER = _session("1111aaaa-manager", "manager")
CODER = _session("2222bbbb-coder", "coder")
FLEET = {CODER.id: "coder-auth"}
"""Each session's fleet row's label, as ``team.fleet_labels`` reads it: the manager here has no
row, as an owner's own session has none."""


def _hhmm(minutes_ago: int) -> str:
    return f"{(NOW - timedelta(minutes=minutes_ago)).astimezone():%H:%M}"


# --- the names --------------------------------------------------------------------------------


def test_a_session_is_its_fleet_row_s_label_and_the_unlabelled_manager_is_manager() -> None:
    recorded = _session("3333aaaa-recorded", "tester", label="tester-ui")

    assert board_names.board_names([MANAGER, CODER, recorded], FLEET) == {
        MANAGER.id: "manager",
        CODER.id: "coder-auth",
        recorded.id: "tester-ui",
    }, "no fleet row: the label the session recorded, else its role"
    assert board_names.board_names([CODER])[CODER.id] == "coder"


def test_a_relabelled_agent_is_named_by_its_fleet_row_as_the_left_pane_names_it() -> None:
    recorded = CODER.model_copy(update={"label": "coder-auth"})
    names = board_names.board_names([MANAGER, recorded], {CODER.id: "login-fix"})
    assert names[CODER.id] == "login-fix", "the row's label, over what the session recorded"


def test_two_sessions_of_one_name_are_told_apart_and_only_then() -> None:
    """A restarted agent: the live one keeps the name, the earlier one gets the time it
    started. An agent alone under its name is never decorated."""
    earlier = _session("3333cccc-old", "coder", started=120, ended=True)
    names = board_names.board_names([MANAGER, earlier, CODER], {**FLEET, earlier.id: "coder-auth"})

    assert names[CODER.id] == "coder-auth"
    assert names[earlier.id] == f"coder-auth ({_hhmm(120)})"
    assert names[MANAGER.id] == "manager"


def test_the_live_one_keeps_the_name_even_when_an_ended_one_started_later() -> None:
    later_but_ended = _session("4444dddd-later", "coder", started=10, ended=True)
    names = board_names.board_names(
        [CODER, later_but_ended], {**FLEET, later_but_ended.id: "coder-auth"}
    )

    assert names[CODER.id] == "coder-auth"
    assert names[later_but_ended.id] == f"coder-auth ({_hhmm(10)})"


def test_older_sessions_started_in_one_minute_are_told_apart_by_their_short_ids() -> None:
    first = _session("5555eeee-first", "coder", started=90, ended=True)
    second = _session("6666ffff-second", "coder", started=90, ended=True)
    fleet = {**FLEET, first.id: "coder-auth", second.id: "coder-auth"}
    names = board_names.board_names([first, second, CODER], fleet)

    assert names[CODER.id] == "coder-auth"
    assert names[first.id] == "coder-auth (5555eeee)"
    assert names[second.id] == "coder-auth (6666ffff)"


class _Store:
    """``fleet_labels_for_sessions`` alone, as :func:`team.fleet_labels` reads it."""

    def __init__(self, rows: dict[str, str], *, fails: bool = False) -> None:
        self.rows = rows
        self.fails = fails
        self.asked: list[list[str]] = []

    def fleet_labels_for_sessions(self, session_ids: list[str]) -> dict[str, str]:
        self.asked.append(list(session_ids))
        if self.fails:
            raise RuntimeError("database is locked")
        return {sid: self.rows[sid] for sid in session_ids if sid in self.rows}


def test_fleet_labels_reads_every_session_s_row_and_survives_a_failure() -> None:
    """Ended sessions too: an agent that has ended keeps its name. A row a relabel retired
    (``~coder-auth``) reads as the label it had, told apart by the board as an earlier one."""
    ended = _session("7777aaaa-ended", "coder", ended=True)
    retired = _session("7777bbbb-retired", "coder", ended=True)
    rows = {CODER.id: "login-fix", ended.id: "coder-gamma", retired.id: "~coder-auth"}
    store = _Store(rows)
    sessions = [MANAGER, CODER, ended, retired]
    found = team_service.fleet_labels(store, sessions)  # type: ignore[arg-type]
    failing = team_service.fleet_labels(_Store({}, fails=True), [CODER])  # type: ignore[arg-type]

    assert found == {CODER.id: "login-fix", ended.id: "coder-gamma", retired.id: "coder-auth"}
    assert store.asked == [[s.id for s in sessions]], "one read, for every session"
    assert failing == {}, "a read that fails costs the names, never the board"


# --- the human board's surfaces --------------------------------------------------------------


def test_the_feed_names_its_author_and_the_person() -> None:
    roles = {MANAGER.id: "manager", CODER.id: "coder"}
    names = board_names.board_names([MANAGER, CODER], FLEET)

    by_coder = watch.feed_line(_event(1, CODER.id), roles, names).plain
    by_manager = watch.feed_line(_event(2, MANAGER.id), roles, names).plain
    by_you = watch.feed_line(_event(3, None), roles, names).plain

    assert " coder-auth 💬 noted: hi" in by_coder and "·" not in by_coder.split("💬")[0][5:]
    assert " manager 💬 noted: hi" in by_manager
    assert "⌨️ you 💬 noted: hi" in by_you and "cli" not in by_you
    assert "2222bbbb" not in by_coder, "the id is a detail away, not in the feed"


def test_an_event_s_detail_keeps_the_short_id_reachable() -> None:
    roles = {CODER.id: "coder"}
    names = board_names.board_names([CODER], FLEET)

    detail = watch._event_detail(_event(1, CODER.id), roles, names).plain

    assert "note by coder-auth (2222bbbb)" in detail


def test_the_sessions_block_names_each_live_session() -> None:
    names = board_names.board_names([MANAGER, CODER], FLEET)
    block = watch._session_lines([MANAGER, CODER], {}, names=names).plain

    assert "manager  ▶ working" in block and "coder-auth  ▶ working" in block
    assert "·" not in block.split("▶")[0], "no role·id"


def test_a_claim_is_named_in_the_task_s_detail_with_its_short_id() -> None:
    names = board_names.board_names([CODER], FLEET)

    detail = watch._task_detail(_task(CODER.id), {}, names).plain

    assert "claimed by coder-auth (2222bbbb)" in detail


def test_the_strip_names_a_relabelled_waiting_agent_as_the_left_pane_does() -> None:
    """The strip's button finds the agent by the name it shows (#286), so it is the fleet row's
    plain label, as a relabel left it: never one told apart."""
    waiting = _session("8888bbbb-waiting", "coder", state="attention")
    fleet = {waiting.id: "login-fix"}

    found = needs_you(PROJECT, [MANAGER, waiting], [], [], seq=0, fleet_labels=fleet)
    no_row = needs_you(PROJECT, [MANAGER, waiting], [], [], seq=0)

    assert found.agents.get("waiting") == ["login-fix"]
    assert no_row.agents.get("waiting") == ["coder"]


def test_aisquare_board_s_text_names_sessions_claims_and_authors() -> None:
    names = board_names.board_names([MANAGER, CODER], FLEET)
    events = [_event(1, CODER.id, text="PR ready"), _event(2, None, text="ship it")]

    text = team_service.render_board(
        PROJECT, [MANAGER, CODER], [_task(CODER.id)], events, names=names
    )

    assert "  - manager — " in text and "  - coder-auth — " in text
    assert "[doing @coder-auth] wire auth" in text
    assert "  - coder-auth note: PR ready" in text
    # "owner", not "you": agents run aisquare board too, and would read "you" as their own.
    assert "  - owner note: ship it" in text and "you note" not in text
    assert "2222bbbb" not in text and "1111aaaa" not in text


# --- what stays: the agents' text and every --json ---------------------------------------------


def test_the_agent_facing_board_keeps_short_ids() -> None:
    """The MCP board and ``team verify`` render without names: as before, by short id and role,
    which ``--as`` takes."""
    events = [_event(1, CODER.id, text="PR ready"), _event(2, None, text="ship it")]

    text = team_service.render_board(PROJECT, [MANAGER, CODER], [_task(CODER.id)], events)
    line = team_service.event_line(events[0], {CODER.id: "coder"})

    assert "  - 2222bbbb coder — " in text and "[doing @2222bbbb]" in text
    assert "  - 2222bbbb (coder) note: PR ready" in text and "  - cli note: ship it" in text
    assert line == "2222bbbb (coder) note: PR ready"


def test_the_agents_own_lines_keep_their_authors() -> None:
    """``short_line`` (hook deltas) names its author by ``authors_of``, as before #140."""
    recorded = _session("3333aaaa-recorded", "tester", label="tester-ui")
    authors = team_service.authors_of([MANAGER, CODER, recorded])
    assert authors == {MANAGER.id: "manager", CODER.id: "coder", recorded.id: "tester-ui"}


def test_the_board_json_keeps_session_ids() -> None:
    from aisquare.cli.team import board_json

    payload: dict[str, Any] = board_json(PROJECT, [MANAGER, CODER], [_task(CODER.id)], [])

    assert [s["id"] for s in payload["sessions"]] == [MANAGER.id, CODER.id]
    assert payload["tasks"][0]["claimed_by"] == CODER.id


def test_a_view_caps_a_long_name_and_never_what_tells_it_apart() -> None:
    """``board -w`` and asq's board give a name at most ``BOARD_NAME_CELLS``: a long label ends in
    ``…`` and keeps a row's chip in view; a told-apart name keeps its time whole."""
    cap = board_names.BOARD_NAME_CELLS
    long_label = "reviewer-board-audit-of-the-whole-release"
    reviewer = _session("9999aaaa-rev", "reviewer")
    earlier = _session("9999bbbb-old", "reviewer", started=120, ended=True)
    fleet = {**FLEET, reviewer.id: long_label, earlier.id: long_label}

    capped = board_names.board_names([reviewer, earlier, CODER], fleet, cells=cap)
    whole = board_names.board_names([reviewer, earlier, CODER], fleet)

    assert capped[reviewer.id] == long_label[: cap - 1] + "…" and len(capped[reviewer.id]) == cap
    assert capped[earlier.id].endswith(f"… ({_hhmm(120)})") and len(capped[earlier.id]) == cap
    assert capped[CODER.id] == "coder-auth", "a name that fits is left whole"
    assert whole[reviewer.id] == long_label, "aisquare board's text keeps every name whole"


# --- through a real store and a real fleet ----------------------------------------------------
# The hooks record no label on a session (tester-rlx-board, #140: 7 sessions, 0 labels), so the
# name is the fleet row's, and an agent that has ended keeps the row it ended on. Read as the
# board reads it: ``team.board_read``, the panel, ``board -w``'s plain frame.


def _row(
    project: ProjectInfo,
    agent_id: str,
    label: str,
    session_id: str | None,
    *,
    created: datetime,
    ended: bool = True,
) -> None:
    with store_session() as store:
        store.upsert_fleet_agent(
            FleetAgent(
                id=agent_id,
                project_id=project.id,
                label=label,
                role="coder",
                pane_id=f"%{agent_id}",
                cwd=project.root,
                session_id=session_id,
                created_at=created,
            )
        )
        if ended:
            store.end_fleet_agent(agent_id, ended_by="stopped")


def test_the_store_names_each_session_by_its_newest_row_live_or_ended(project: ProjectInfo) -> None:
    now = datetime.now(UTC)
    _row(project, "agt_alpha", "coder-alpha", "ses-alpha", created=now - timedelta(hours=3))
    # A resume that kept the session: the newest row is the name.
    _row(project, "agt_beta1", "coder-beta-was", "ses-beta", created=now - timedelta(hours=2))
    _row(project, "agt_beta2", "coder-beta", "ses-beta", created=now - timedelta(hours=1))
    # A live row wins, as ``fleet_agent_for_session`` picks it.
    _row(
        project,
        "agt_gam1",
        "coder-gamma",
        "ses-gamma",
        created=now - timedelta(minutes=50),
        ended=False,
    )
    _row(project, "agt_gam2", "coder-gamma-was", "ses-gamma", created=now - timedelta(minutes=40))
    # A row whose own column is empty: the session its launch bound (Codex's).
    _row(project, "agt_delta", "codex-delta", None, created=now - timedelta(minutes=30))
    with store_session() as store:
        store.set_meta("fleet-session:agt_delta", "ses-delta")
        # Asked past one chunk of 500: these sort first, the rows' sessions after them.
        padding = [f"ses-0{n:03d}" for n in range(600)]
        found = store.fleet_labels_for_sessions(
            [*padding, "ses-alpha", "ses-beta", "ses-gamma", "ses-delta", "ses-none"]
        )

    assert found == {
        "ses-alpha": "coder-alpha",
        "ses-beta": "coder-beta",
        "ses-gamma": "coder-gamma",
        "ses-delta": "codex-delta",
    }


def _hooked(agent: FleetAgent, *, started_ago: timedelta = timedelta(0)) -> str:
    """The board row an agent's hooks write: its role and its times, and no label."""
    assert agent.session_id is not None
    now = datetime.now(UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=agent.session_id,
                project_id=agent.project_id,
                role=agent.role,
                started_at=now - started_ago,
                last_seen_at=now,
            )
        )
    return agent.session_id


def _stop(project: ProjectInfo, agent: FleetAgent) -> None:
    """``fleet stop``, then the SessionEnd hook its agent's exit fires."""
    assert agent.session_id is not None
    fleet_service.stop(project, agent.label, force=True)
    team_service.hook_session_end(agent.session_id, None, reason="prompt_input_exit")
    with store_session() as store:
        row = store.get_fleet_agent(agent.id)
        session = store.get_session(agent.session_id)
    assert row is not None and row.ended_at is not None, "the row ended"
    assert session is not None and session.ended_at is not None, "the session ended"
    assert session.label is None, "as production: the session records no label"


def _names(project: ProjectInfo) -> dict[str, str]:
    read = team_service.board_read(project=project)
    return board_names.board_names(read.sessions, read.fleet_labels)


def test_an_agent_that_ended_keeps_its_name_and_a_respawn_under_it_is_told_apart(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The tester's coder-alpha and coder-delta: a stopped agent read "coder", and its earlier
    round, once a respawn took the label, "coder (<id>)". It keeps its name, told apart by the
    time it started."""
    first = fleet_service.spawn(project, "coder", label="coder-alpha", worktree=False).agent
    earlier = _hooked(first, started_ago=timedelta(minutes=30))
    live = _names(project)
    _stop(project, first)
    stopped = _names(project)
    second = fleet_service.spawn(project, "coder", label="coder-alpha", worktree=False).agent
    later = _hooked(second)
    respawned = _names(project)
    with store_session() as store:
        started = store.get_session(earlier)
    assert started is not None

    assert live[earlier] == "coder-alpha"
    assert stopped[earlier] == "coder-alpha", "stopped, it keeps its name"
    assert respawned[later] == "coder-alpha", "the live one keeps the plain name"
    assert respawned[earlier] == f"coder-alpha ({started.started_at.astimezone():%H:%M})"


class _Board(App[None]):
    def __init__(self, project: ProjectInfo) -> None:
        super().__init__()
        self.project = project

    def compose(self) -> ComposeResult:
        yield BoardPanel(self.project, interval=3600, id="board")


def test_a_stop_with_the_board_open_renames_none_of_the_agent_s_lines(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The tester's ``fleet stop coder-gamma-longest-name`` with ``board -w`` open: the panel
    redrew gamma's lines as "coder (74dde549)". Its lines keep its name."""
    label = "coder-gamma-longest-name"  # 24 cells: the cap, whole
    gamma = fleet_service.spawn(project, "coder", label=label, worktree=False).agent
    sid = _hooked(gamma)
    note = team_service.add_note("tests pass", session_ref=sid, project_id=project.id)

    async def go() -> tuple[str, str]:
        app = _Board(project)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await settle_page(app)
            panel = app.query_one(BoardPanel)
            feed = panel.query_one("#feed", OptionList)
            before = str(feed.get_option(note.id).prompt)
            _stop(project, gamma)
            panel.refresh_data()
            await settle_page(app)
            return before, str(feed.get_option(note.id).prompt)

    before, after = asyncio.run(go())

    assert f" {label} 💬 noted: tests pass" in before
    assert f" {label} 💬 noted: tests pass" in after, "the stop renamed the agent's lines"


def test_board_w_s_plain_frame_names_a_claim_and_an_ended_agent_s_lines(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """``board -w`` without Textual (``watch.board_frame``) names a claim's holder and a line's
    author by the fleet row too, before the agent ends and after."""
    agent = fleet_service.spawn(project, "coder", label="coder-auth", worktree=False).agent
    sid = _hooked(agent)
    task = _add_task(project, "wire auth")
    team_service.claim_task(task.id, session_ref=sid)
    team_service.add_note("PR ready", session_ref=sid, project_id=project.id)

    live = watch.board_frame(40, 160, project).plain
    _stop(project, agent)
    ended = watch.board_frame(40, 160, project).plain

    assert "[doing @coder-auth] wire auth" in live
    assert " coder-auth 💬 noted: PR ready" in live
    assert " coder-auth 💬 noted: PR ready" in ended, "the stop renamed the agent's lines"
    assert sid[:8] not in live and sid[:8] not in ended
