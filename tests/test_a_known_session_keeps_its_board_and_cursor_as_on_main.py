"""A known session that registers again keeps its board and its cursor, as on main.

Review of #240, F4. The captain line's #230 fix 2b (a60cfdb9) moved a known session id that
registered on ANOTHER board to that board, from that board's cursor: a repair for the
wrong-board sessions #230's own hub rule made. Hub rule A retired that rule and kept main's,
and fix 2b's pins went with it, so a change to main's session semantics shipped unpinned,
with the captain off: ``claude --resume`` from another project's directory switched boards
and lost its read cursor. main 0.8.2 keeps the first board and the cursor; so does this
branch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from aisquare.core.store import store_session
from aisquare.models import ProjectInfo, TeamSession

FIRST = ProjectInfo(id="prj_first", root=Path("/tmp/first-board"), linked_repos=[])
OTHER = ProjectInfo(id="prj_other", root=Path("/tmp/other-board"), linked_repos=[])
NOW = datetime(2026, 10, 20, tzinfo=UTC)


def _registered(project: ProjectInfo, cursor: int) -> TeamSession:
    return TeamSession(
        id="sess-1", project_id=project.id, started_at=NOW, last_seen_at=NOW, cursor=cursor
    )


def test_a_session_that_registers_on_another_board_keeps_its_first_board_and_cursor() -> None:
    with store_session() as store:
        store.ensure_project(FIRST)
        store.ensure_project(OTHER)
        store.upsert_session(_registered(FIRST, 5))

        again = store.upsert_session(_registered(OTHER, 99))

    assert (again.project_id, again.cursor) == (FIRST.id, 5)


def test_a_refresh_on_its_own_board_keeps_its_cursor() -> None:
    """The control: a session start on the same board does not rewind or advance the cursor."""
    with store_session() as store:
        store.ensure_project(FIRST)
        store.upsert_session(_registered(FIRST, 5))

        again = store.upsert_session(_registered(FIRST, 42))

    assert (again.project_id, again.cursor) == (FIRST.id, 5)
