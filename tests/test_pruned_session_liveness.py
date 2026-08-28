"""#47: a pruned-but-alive session stays invisible while its write path works.

The scenario, from the field: a live session stretches its wakeup cadence past
the stale threshold, ``team prune`` retires its row, and the session keeps
working — notes land with verifiable receipts, ``team role`` succeeds, claims
hold. But ``board``, ``team status``, ``watch`` and ``doctor`` all read liveness
as ``ended_at IS NULL``, so the session is gone from every view. Operators read
row-absence as death and make wrong calls; on the board that filed this, a
healthy session was pruned on a cadence artifact and then presumed dead a second
time *because* the severed row masked its own recovery.

``end_session`` already documents the intended repair in its own docstring — "a
wrongly retired presence row is repaired by the session's next heartbeat" — and
that repair does not happen: ``upsert_session`` clears ``ended_at``, but
``touch_session`` and ``mark_attention``, the two writes every subsequent
heartbeat actually goes through, do not.

The asymmetry that makes this the right fix rather than a display change: a
heartbeat is PROOF OF LIFE. A prompt was submitted, a turn ended, a permission
prompt was raised — the process is there. Prune's own judgement was a guess from
silence, and the guess is now contradicted by evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.store import store_session
from aisquare.core.workspace import active_project
from aisquare.models import TeamSession
from aisquare.services import team as team_service


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An activated board in a temp repo — the state every hook path assumes."""
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("AISQUARE_TEAM", "1")
    team_service.activate(work)
    return work


def _session(session_id: str) -> TeamSession | None:
    with store_session() as store:
        return store.get_session(session_id)


def _live_ids() -> set[str]:
    """Exactly what every liveness view computes: rows with no ``ended_at``."""
    with store_session() as store:
        project = active_project(store)
        return {s.id for s in store.team_sessions(project.id) if s.ended_at is None}


def _register(session_id: str, *, role: str = "coder", cwd: Path | None = None) -> None:
    team_service.hook_session_start(session_id, cwd, "startup")
    team_service.set_role(role, session_id, cwd)


def _age_out(session_id: str) -> None:
    """Make the row look untouched for hours — what prune actually judges on.

    Through ``upsert_session`` rather than raw SQL: the store is a Protocol and
    a test that reaches past it is a test that survives the implementation
    changing underneath the thing it claims to check.
    """
    stored = _session(session_id)
    assert stored is not None
    with store_session() as store:
        store.upsert_session(
            stored.model_copy(update={"last_seen_at": datetime.now(tz=UTC) - timedelta(hours=6)})
        )


def _prune_to_death(session_id: str) -> None:
    """Retire the row the way ``team prune`` does — presence only, claims kept."""
    with store_session() as store:
        store.end_session(session_id, release_claims=False)


def test_the_bug_a_pruned_session_can_still_write_but_is_invisible(work_dir: Path) -> None:
    """Pins the shape of the defect, so a fix cannot be declared without it."""
    _register("sess-alive")
    _prune_to_death("sess-alive")

    assert "sess-alive" not in _live_ids(), "prune retires the row — that part works"

    # The write path is unaffected: this is what makes the invisibility dangerous
    # rather than merely wrong. An operator sees no row and infers death.
    team_service.set_role("runner", "sess-alive", work_dir)
    session = _session("sess-alive")
    assert session is not None and session.role == "runner"


def test_a_prompt_heartbeat_restores_a_wrongly_pruned_row(work_dir: Path) -> None:
    """The repair `end_session`'s docstring already promises."""
    _register("sess-alive")
    _prune_to_death("sess-alive")

    team_service.hook_prompt_heartbeat("sess-alive", work_dir)

    assert "sess-alive" in _live_ids()


def test_the_end_of_a_turn_also_proves_life(work_dir: Path) -> None:
    """`Stop` fires every turn — waiting for input is not being dead."""
    _register("sess-alive")
    _prune_to_death("sess-alive")

    team_service.hook_stop("sess-alive", work_dir)

    assert "sess-alive" in _live_ids()


def test_asking_the_human_for_permission_also_proves_life(work_dir: Path) -> None:
    """A session parked on a permission prompt is the MOST alive it ever is.

    It is also the one a human is most likely to be hunting for on the board.
    """
    _register("sess-alive")
    _prune_to_death("sess-alive")

    team_service.hook_notification("sess-alive", work_dir, "needs your attention")

    assert "sess-alive" in _live_ids()


def test_restoring_keeps_who_the_session_WAS(work_dir: Path) -> None:
    """Restore, not re-register — otherwise a planner comes back as 'unassigned'.

    This is the whole reason to repair the row instead of letting the session
    rejoin as a stranger: role, label and focus are what the board is FOR.
    """
    _register("sess-alive", role="planner")
    team_service.set_focus("folding the RC train", "sess-alive", work_dir)
    _prune_to_death("sess-alive")

    team_service.hook_prompt_heartbeat("sess-alive", work_dir)

    session = _session("sess-alive")
    assert session is not None
    assert session.role == "planner"
    assert session.focus == "folding the RC train"


def test_a_session_that_really_ended_stays_ended(work_dir: Path) -> None:
    """Nothing resurrects on its own: only a live signal clears ``ended_at``."""
    _register("sess-gone")
    team_service.hook_session_end("sess-gone", work_dir)

    assert "sess-gone" not in _live_ids()

    # A pruning pass, a board read, another session's traffic — none of it is
    # proof of life for this row.
    team_service.prune_sessions(cwd=work_dir)
    team_service.board_data(cwd=work_dir)

    assert "sess-gone" not in _live_ids()


def test_the_board_shows_a_restored_session_again(runner: CliRunner, work_dir: Path) -> None:
    """End to end through the command an operator actually runs."""
    _register("sess-alive", role="coder")
    _prune_to_death("sess-alive")

    before = runner.invoke(app, ["board"], catch_exceptions=False).output
    team_service.hook_prompt_heartbeat("sess-alive", work_dir)
    after = runner.invoke(app, ["board"], catch_exceptions=False).output

    assert "sess-ali" not in before
    assert "sess-ali" in after, after


def test_prune_still_retires_a_session_that_has_gone_quiet(work_dir: Path) -> None:
    """The fix must not make prune useless — silence still retires a row."""
    _register("sess-quiet")
    _age_out("sess-quiet")

    team_service.prune_sessions(cwd=work_dir)

    assert "sess-quiet" not in _live_ids()
