"""The board names each agent as asq's left pane does, not as ``role·<id>`` (#140).

The owner, verbatim: "the board currently has persona.<id> sort of format which is impossible to
understand, can that be just the name shown on left pane". On the HUMAN board — asq's board view,
``board -w`` and ``aisquare board``'s text — a session is its fleet row's label, as a relabel left
it, else the label it started under, else its role (the manager has none). A line no session
wrote is "you" in the views and "owner" in ``aisquare board``'s text, which agents run too.
Two sessions of one name are told apart only then. The agent-facing text (hook deltas, the
start pack, the MCP board, ``team verify``) and every ``--json`` keep ids.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aisquare.cli import board_names, watch
from aisquare.cli.ui.board import needs_you
from aisquare.models import FleetAgent, ProjectInfo, TeamEvent, TeamSession, TeamTask
from aisquare.services import team as team_service

NOW = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
PROJECT = ProjectInfo(id="prj_names", root=Path("/tmp/names-project"))


def _session(
    sid: str,
    role: str,
    label: str | None,
    *,
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


MANAGER = _session("1111aaaa-manager", "manager", None)
CODER = _session("2222bbbb-coder", "coder", "coder-auth")


def _hhmm(minutes_ago: int) -> str:
    return f"{(NOW - timedelta(minutes=minutes_ago)).astimezone():%H:%M}"


# --- the names --------------------------------------------------------------------------------


def test_a_session_is_its_label_and_the_unlabelled_manager_is_manager() -> None:
    assert board_names.board_names([MANAGER, CODER]) == {
        MANAGER.id: "manager",
        CODER.id: "coder-auth",
    }


def test_a_relabelled_agent_is_named_by_its_fleet_row_as_the_left_pane_names_it() -> None:
    names = board_names.board_names([MANAGER, CODER], {CODER.id: "login-fix"})
    assert names[CODER.id] == "login-fix"


def test_two_sessions_of_one_name_are_told_apart_and_only_then() -> None:
    """A restarted agent: the live one keeps the name, the earlier one gets the time it
    started. An agent alone under its name is never decorated."""
    earlier = _session("3333cccc-old", "coder", "coder-auth", started=120, ended=True)
    names = board_names.board_names([MANAGER, earlier, CODER])

    assert names[CODER.id] == "coder-auth"
    assert names[earlier.id] == f"coder-auth ({_hhmm(120)})"
    assert names[MANAGER.id] == "manager"


def test_the_live_one_keeps_the_name_even_when_an_ended_one_started_later() -> None:
    later_but_ended = _session("4444dddd-later", "coder", "coder-auth", started=10, ended=True)
    names = board_names.board_names([CODER, later_but_ended])

    assert names[CODER.id] == "coder-auth"
    assert names[later_but_ended.id] == f"coder-auth ({_hhmm(10)})"


def test_older_sessions_started_in_one_minute_are_told_apart_by_their_short_ids() -> None:
    first = _session("5555eeee-first", "coder", "coder-auth", started=90, ended=True)
    second = _session("6666ffff-second", "coder", "coder-auth", started=90, ended=True)
    names = board_names.board_names([first, second, CODER])

    assert names[CODER.id] == "coder-auth"
    assert names[first.id] == "coder-auth (5555eeee)"
    assert names[second.id] == "coder-auth (6666ffff)"


class _Store:
    """``fleet_agent_for_session`` alone, as :func:`team.live_labels` reads it."""

    def __init__(self, rows: dict[str, str], *, fails: bool = False) -> None:
        self.rows = rows
        self.fails = fails
        self.asked: list[str] = []

    def fleet_agent_for_session(self, session_id: str) -> FleetAgent | None:
        self.asked.append(session_id)
        if self.fails:
            raise RuntimeError("database is locked")
        label = self.rows.get(session_id)
        if label is None:
            return None
        return FleetAgent(
            id="agt_x",
            project_id=PROJECT.id,
            label=label,
            role="coder",
            pane_id="%1",
            cwd=Path("/w"),
            created_at=NOW,
        )


def test_live_labels_reads_the_live_sessions_fleet_rows_and_survives_a_failure() -> None:
    ended = _session("7777aaaa-ended", "coder", "old", ended=True)
    store = _Store({CODER.id: "login-fix"})
    found = team_service.live_labels(store, [MANAGER, CODER, ended])  # type: ignore[arg-type]
    failing = team_service.live_labels(_Store({}, fails=True), [CODER])  # type: ignore[arg-type]

    assert found == {CODER.id: "login-fix"}
    assert store.asked == [MANAGER.id, CODER.id], "an ended session's row is not looked up"
    assert failing == {}, "a read that fails costs the relabels, never the board"


# --- the human board's surfaces --------------------------------------------------------------


def test_the_feed_names_its_author_and_the_person() -> None:
    roles = {MANAGER.id: "manager", CODER.id: "coder"}
    names = board_names.board_names([MANAGER, CODER])

    by_coder = watch.feed_line(_event(1, CODER.id), roles, names).plain
    by_manager = watch.feed_line(_event(2, MANAGER.id), roles, names).plain
    by_you = watch.feed_line(_event(3, None), roles, names).plain

    assert " coder-auth 💬 noted: hi" in by_coder and "·" not in by_coder.split("💬")[0][5:]
    assert " manager 💬 noted: hi" in by_manager
    assert "⌨️ you 💬 noted: hi" in by_you and "cli" not in by_you
    assert "2222bbbb" not in by_coder, "the id is a detail away, not in the feed"


def test_an_event_s_detail_keeps_the_short_id_reachable() -> None:
    roles = {CODER.id: "coder"}
    names = board_names.board_names([CODER])

    detail = watch._event_detail(_event(1, CODER.id), roles, names).plain

    assert "note by coder-auth (2222bbbb)" in detail


def test_the_sessions_block_names_each_live_session() -> None:
    names = board_names.board_names([MANAGER, CODER])
    block = watch._session_lines([MANAGER, CODER], {}, names=names).plain

    assert "manager  ▶ working" in block and "coder-auth  ▶ working" in block
    assert "·" not in block.split("▶")[0], "no role·id"


def test_a_claim_is_named_in_the_task_s_detail_with_its_short_id() -> None:
    names = board_names.board_names([CODER])

    detail = watch._task_detail(_task(CODER.id), {}, names).plain

    assert "claimed by coder-auth (2222bbbb)" in detail


def test_the_strip_names_a_relabelled_waiting_agent_as_the_left_pane_does() -> None:
    """The strip's button finds the agent by the name it shows (#286), so it is the fleet row's
    plain label, as a relabel left it: never one told apart."""
    waiting = _session("8888bbbb-waiting", "coder", "coder-auth", state="attention")

    found = needs_you(PROJECT, [MANAGER, waiting], [], [], seq=0, live={waiting.id: "login-fix"})
    before = needs_you(PROJECT, [MANAGER, waiting], [], [], seq=0)

    assert found.agents.get("waiting") == ["login-fix"]
    assert before.agents.get("waiting") == ["coder-auth"]


def test_aisquare_board_s_text_names_sessions_claims_and_authors() -> None:
    names = board_names.board_names([MANAGER, CODER])
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
    authors = team_service.authors_of([MANAGER, CODER])
    assert authors == {MANAGER.id: "manager", CODER.id: "coder-auth"}


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
    reviewer = _session("9999aaaa-rev", "reviewer", long_label)
    earlier = _session("9999bbbb-old", "reviewer", long_label, started=120, ended=True)

    capped = board_names.board_names([reviewer, earlier, CODER], cells=cap)
    whole = board_names.board_names([reviewer, earlier, CODER])

    assert capped[reviewer.id] == long_label[: cap - 1] + "…" and len(capped[reviewer.id]) == cap
    assert capped[earlier.id].endswith(f"… ({_hhmm(120)})") and len(capped[earlier.id]) == cap
    assert capped[CODER.id] == "coder-auth", "a name that fits is left whole"
    assert whole[reviewer.id] == long_label, "aisquare board's text keeps every name whole"
