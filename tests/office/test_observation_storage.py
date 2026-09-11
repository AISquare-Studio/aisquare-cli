"""What the sidecar promises, asserted one property at a time.

The scenarios are the ones P02 is accepted against: a first open that creates
an owner-only home, a path boundary that refuses to leave it, an all-or-nothing
merge, concurrent writers that do not erase each other, generation ordering,
survival across a restart, retention that spares live facts, bounds that refuse
oversized payloads, and compare-and-set checkpoints whose derived rows commit
with the cursor.

Every test builds its own database under ``tmp_path``. Nothing here reads a
real home, opens ``context.db``, starts a server or touches tmux — the sidecar
is a file and a schema, and that is the whole surface under test.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aisquare.models import TeamSession
from aisquare.office.config import OfficeConfig
from aisquare.office.models import (
    HookFact,
    LocalObservationBatch,
    ObservationUpdate,
    PaneObservation,
    PromptEvidence,
    QuestionOption,
    TerminalPaneFacts,
)
from aisquare.office.ports import ObservationStore
from aisquare.office.storage import (
    CorrelationRecord,
    ObservationDatabase,
    StorageBoundsError,
    StorageError,
    StoragePathError,
)
from aisquare.office.storage_schema import MIGRATIONS, StorageLimits

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="file modes need a POSIX filesystem")


class FrozenClock:
    """A clock a test drives, so nothing here sleeps to make time pass."""

    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


def _database(
    home: Path, limits: StorageLimits | None = None, *, now: datetime = NOW
) -> ObservationDatabase:
    return ObservationDatabase.from_config(OfficeConfig(home=home), FrozenClock(now), limits)


def _session(
    session_id: str = "ses-1",
    *,
    state: str = "working",
    ended_at: datetime | None = None,
    focus: str | None = None,
    started_at: datetime = NOW - timedelta(minutes=5),
    last_seen_at: datetime = NOW,
) -> TeamSession:
    return TeamSession(
        id=session_id,
        project_id="prj-1",
        role="coder",
        focus=focus,
        started_at=started_at,
        last_seen_at=last_seen_at,
        ended_at=ended_at,
        state=state,
        model="claude-opus-5",
        transcript_path="/home/someone/.claude/projects/x.jsonl",
    )


def _pane(agent_id: str = "agt-1", *, lines: tuple[str, ...] = ("hello",)) -> PaneObservation:
    return PaneObservation(
        agent_id=agent_id,
        alive=True,
        health="live",
        observed_at=NOW,
        lines=lines,
        facts=TerminalPaneFacts(
            width=80,
            height=24,
            cursor_x=3,
            cursor_y=4,
            cursor_visible=True,
            alternate_on=False,
            history_size=120,
            dead=False,
            dead_status=None,
            in_mode=False,
        ),
    )


def _prompt(
    *, generation: int = 1, raw: str | None = None, observed_at: datetime = NOW
) -> PromptEvidence:
    return PromptEvidence(
        agent_id="agt-1",
        provider="claude",
        prompt_id=f"p-{generation}",
        generation=generation,
        kind="permission",
        detected_by="frame",
        observed_at=observed_at,
        options=(QuestionOption(key="1", label="Yes"), QuestionOption(key="2", label="No")),
        raw=raw,
    )


# -- opening ---------------------------------------------------------------


def test_a_first_open_creates_the_schema_under_the_resolved_home(tmp_path: Path) -> None:
    home = tmp_path / "aisquare-home"
    database = _database(home)

    version = database.migrate()

    assert version == MIGRATIONS.latest_version()
    assert database.path == home / "office" / "observations.sqlite3"
    assert database.path.exists()
    assert database.schema_version() == MIGRATIONS.latest_version()


def test_an_open_leaves_the_cli_context_db_untouched(tmp_path: Path) -> None:
    """The sidecar is a separate file; opening it must not create or read one."""
    home = tmp_path / "aisquare-home"
    database = _database(home)

    database.migrate()

    assert not (home / "context.db").exists()
    assert sorted(p.name for p in (home / "office").iterdir() if p.suffix == ".sqlite3") == [
        "observations.sqlite3"
    ]


@POSIX_ONLY
def test_an_open_sets_owner_only_modes_on_the_directory_and_file(tmp_path: Path) -> None:
    home = tmp_path / "aisquare-home"
    database = _database(home)

    database.migrate()

    assert database.path.parent.stat().st_mode & 0o777 == 0o700
    assert database.path.stat().st_mode & 0o777 == 0o600


def test_a_path_outside_the_resolved_home_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "aisquare-home"
    escape = tmp_path / "elsewhere" / "observations.sqlite3"

    with pytest.raises(StoragePathError) as caught:
        ObservationDatabase(escape, FrozenClock(), home=home)

    assert "outside the resolved AISQUARE home" in str(caught.value)
    assert not escape.exists()


def test_the_path_may_never_be_the_cli_context_database(tmp_path: Path) -> None:
    with pytest.raises(StoragePathError) as caught:
        ObservationDatabase(tmp_path / "context.db", FrozenClock(), home=tmp_path)

    assert "context.db" in str(caught.value)


def test_a_relative_path_is_refused_so_it_cannot_follow_the_process_cwd(tmp_path: Path) -> None:
    with pytest.raises(StoragePathError):
        ObservationDatabase(Path("office/observations.sqlite3"), FrozenClock(), home=tmp_path)


def test_the_database_satisfies_the_observation_store_port(tmp_path: Path) -> None:
    """Typed against P01's port, so P03/P05 cannot be handed a near-miss."""
    database = _database(tmp_path / "aisquare-home")
    store: ObservationStore = database

    batch = store.read(now=NOW)

    assert batch.collected_at == NOW
    assert store.read_checkpoint("activity", "src-1") is None


# -- atomicity -------------------------------------------------------------


def test_a_merge_is_atomic_when_one_fact_is_out_of_bounds(tmp_path: Path) -> None:
    """A refused pane must not leave the session from the same batch behind."""
    database = _database(tmp_path / "aisquare-home", StorageLimits(max_json_bytes=40))
    batch = LocalObservationBatch(
        collected_at=NOW,
        board_seq=4,
        sessions=(_session(),),
        panes=(_pane(lines=tuple(f"line {index}" for index in range(50))),),
    )

    with pytest.raises(StorageBoundsError):
        database.merge(batch)

    restored = database.read(now=NOW)
    assert restored.sessions == ()
    assert restored.panes == ()


def test_an_atomic_merge_commits_every_fact_together(tmp_path: Path) -> None:
    database = _database(tmp_path / "aisquare-home")

    database.merge(
        LocalObservationBatch(
            collected_at=NOW,
            board_seq=11,
            sessions=(_session(),),
            panes=(_pane(),),
            prompts=(_prompt(),),
            hook_facts=(HookFact(agent_id="agt-1", name="tool", value="Edit", observed_at=NOW),),
        )
    )

    batch = database.read(now=NOW)
    assert batch.board_seq == 11
    assert [session.id for session in batch.sessions] == ["ses-1"]
    assert [pane.agent_id for pane in batch.panes] == ["agt-1"]
    assert [prompt.prompt_id for prompt in batch.prompts] == ["p-1"]
    assert [(fact.name, fact.value) for fact in batch.hook_facts] == [("tool", "Edit")]


def test_a_merge_never_stores_a_session_transcript_path(tmp_path: Path) -> None:
    """A path in, no path out: the column is deliberately not written."""
    database = _database(tmp_path / "aisquare-home")

    database.merge(LocalObservationBatch(collected_at=NOW, sessions=(_session(),)))

    assert database.read(now=NOW).sessions[0].transcript_path is None
    raw = database.path.read_bytes()
    assert b"/home/someone/.claude" not in raw


# -- concurrency -----------------------------------------------------------


def test_concurrent_writers_merge_independent_facts_rather_than_overwrite(
    tmp_path: Path,
) -> None:
    """Two writers, two facts, one database: both survive.

    Each thread opens its own :class:`ObservationDatabase` on the same path,
    which is the real shape — a hook process and the poll worker are not
    sharing an object.
    """
    home = tmp_path / "aisquare-home"
    _database(home).migrate()
    errors: list[BaseException] = []
    start = threading.Barrier(2)

    def write(index: int) -> None:
        database = _database(home)
        try:
            start.wait(timeout=5)
            for round_index in range(10):
                database.merge(
                    LocalObservationBatch(
                        collected_at=NOW + timedelta(seconds=round_index),
                        board_seq=round_index,
                        hook_facts=(
                            HookFact(
                                agent_id=f"agt-{index}",
                                name="tool",
                                value=f"Edit-{round_index}",
                                observed_at=NOW + timedelta(seconds=round_index),
                            ),
                        ),
                    )
                )
        except BaseException as exc:  # reported through `errors`, never swallowed
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(index,)) for index in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    facts = {fact.agent_id: fact.value for fact in _database(home).read(now=NOW).hook_facts}
    assert facts == {"agt-1": "Edit-9", "agt-2": "Edit-9"}


def test_a_concurrent_write_lock_fails_within_the_finite_busy_timeout(tmp_path: Path) -> None:
    """A wedged sidecar must fail, not hang: the poll's own budget is 500 ms."""
    home = tmp_path / "aisquare-home"
    database = _database(home, StorageLimits(busy_timeout_ms=200))
    database.migrate()
    blocker = sqlite3.connect(str(database.path), isolation_level=None)
    blocker.execute("PRAGMA busy_timeout = 0")
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute(
        "INSERT INTO office_hook_observation("
        "agent_id, name, value, metadata_json, generation, observed_at, expires_at, source) "
        "VALUES ('agt-lock', 'tool', 'x', NULL, 1, '2026-09-11T12:00:00+00:00', NULL, 'hook')"
    )

    started = time.monotonic()
    try:
        with pytest.raises(StorageError):
            database.merge(LocalObservationBatch(collected_at=NOW, sessions=(_session(),)))
        elapsed = time.monotonic() - started
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert elapsed < 10, f"the busy timeout did not bound the wait ({elapsed:.1f}s)"


# -- ordering --------------------------------------------------------------


def test_an_older_generation_cannot_overwrite_a_newer_fact(tmp_path: Path) -> None:
    database = _database(tmp_path / "aisquare-home")
    database.merge(
        LocalObservationBatch(
            collected_at=NOW, board_seq=9, sessions=(_session(focus="the new truth"),)
        )
    )

    database.merge(
        LocalObservationBatch(
            collected_at=NOW - timedelta(minutes=1),
            board_seq=2,
            sessions=(_session(focus="a late arrival from an older poll"),),
        )
    )

    assert database.read(now=NOW).sessions[0].focus == "the new truth"


def test_an_equal_generation_resolves_by_observation_time(tmp_path: Path) -> None:
    database = _database(tmp_path / "aisquare-home")
    database.merge(
        LocalObservationBatch(collected_at=NOW, board_seq=5, sessions=(_session(focus="first"),))
    )

    database.merge(
        LocalObservationBatch(
            collected_at=NOW + timedelta(seconds=30),
            board_seq=5,
            sessions=(_session(focus="second, same board sequence"),),
        )
    )

    assert database.read(now=NOW).sessions[0].focus == "second, same board sequence"


def test_an_older_generation_cannot_lower_the_board_sequence(tmp_path: Path) -> None:
    database = _database(tmp_path / "aisquare-home")
    database.merge(LocalObservationBatch(collected_at=NOW, board_seq=42))

    database.merge(LocalObservationBatch(collected_at=NOW - timedelta(minutes=1), board_seq=7))

    assert database.read(now=NOW).board_seq == 42


def test_a_newer_pane_generation_replaces_the_stored_tail(tmp_path: Path) -> None:
    database = _database(tmp_path / "aisquare-home")
    database.merge(
        LocalObservationBatch(collected_at=NOW, board_seq=1, panes=(_pane(lines=("old",)),))
    )

    database.merge(
        LocalObservationBatch(collected_at=NOW, board_seq=2, panes=(_pane(lines=("new",)),))
    )

    assert database.read(now=NOW).panes[0].lines == ("new",)


# -- restart ---------------------------------------------------------------


def test_facts_and_freshness_survive_a_restart_on_a_new_connection(tmp_path: Path) -> None:
    home = tmp_path / "aisquare-home"
    first = _database(home)
    first.merge(
        LocalObservationBatch(
            collected_at=NOW,
            board_seq=13,
            sessions=(_session(),),
            panes=(_pane(),),
            prompts=(_prompt(generation=4),),
            hook_facts=(HookFact(agent_id="agt-1", name="model", value="opus", observed_at=NOW),),
        )
    )
    first.close()

    second = _database(home)
    batch = second.read(now=NOW)

    assert batch.board_seq == 13
    assert batch.sessions[0].started_at == NOW - timedelta(minutes=5)
    assert batch.sessions[0].last_seen_at == NOW
    assert batch.panes[0].facts is not None
    assert batch.panes[0].facts.width == 80
    assert batch.prompts[0].generation == 4
    assert [option.label for option in batch.prompts[0].options] == ["Yes", "No"]
    assert batch.hook_facts[0].observed_at == NOW


def test_a_restored_batch_reports_itself_partial_and_carries_no_projects(tmp_path: Path) -> None:
    """The sidecar is a cache; a consumer must not read it as a full collection."""
    database = _database(tmp_path / "aisquare-home")
    database.merge(LocalObservationBatch(collected_at=NOW, board_seq=3, sessions=(_session(),)))

    batch = database.read(now=NOW)

    assert batch.partial is True
    assert batch.projects == ()
    assert batch.tasks == ()
    assert batch.events == ()
    assert batch.fleet == ()


def test_a_closed_database_refuses_further_work(tmp_path: Path) -> None:
    database = _database(tmp_path / "aisquare-home")
    database.migrate()
    database.close()
    database.close()  # idempotent

    with pytest.raises(StorageError):
        database.read(now=NOW)


# -- expiry ----------------------------------------------------------------


def test_expiry_deletes_expired_rows_and_reports_the_count(tmp_path: Path) -> None:
    database = _database(tmp_path / "aisquare-home")
    assert database.write_checkpoint(
        "activity",
        "src-1",
        0,
        {"offset": 1},
        1,
        NOW,
        derived_updates=(
            ObservationUpdate(
                category="activity",
                key="agt-1:gone",
                payload={"turns": 1},
                generation=1,
                observed_at=NOW,
                expires_at=NOW + timedelta(minutes=1),
            ),
            ObservationUpdate(
                category="activity",
                key="agt-1:kept",
                payload={"turns": 2},
                generation=1,
                observed_at=NOW,
            ),
        ),
    )

    removed = database.expire(now=NOW + timedelta(minutes=5))

    keys = [update.key for update in database.derived_observations(category="activity")]
    assert removed == 1
    assert keys == ["agt-1:kept"]


def test_expiry_keeps_a_live_session_when_its_source_goes_quiet(tmp_path: Path) -> None:
    """Absence of a new batch is not evidence that anything ended."""
    database = _database(tmp_path / "aisquare-home")
    database.merge(LocalObservationBatch(collected_at=NOW, sessions=(_session(),)))

    database.expire(now=NOW + timedelta(days=365))

    assert [session.id for session in database.read(now=NOW).sessions] == ["ses-1"]


def test_expiry_ages_out_an_ended_session_past_its_retention_window(tmp_path: Path) -> None:
    database = _database(tmp_path / "aisquare-home", StorageLimits(session_retention_s=60.0))
    database.merge(
        LocalObservationBatch(
            collected_at=NOW,
            sessions=(
                _session("ses-live"),
                _session("ses-done", state="ended", ended_at=NOW - timedelta(hours=1)),
            ),
        )
    )

    removed = database.expire(now=NOW)

    assert removed == 1
    assert [session.id for session in database.read(now=NOW).sessions] == ["ses-live"]


def test_expiry_keeps_a_checkpoint_a_consumer_still_needs(tmp_path: Path) -> None:
    """Dropping a cursor silently restarts a consumer from the beginning."""
    database = _database(tmp_path / "aisquare-home", StorageLimits(max_rows_per_table=1))
    assert database.write_checkpoint("cost", "src-1", 0, {"offset": 5}, 1, NOW)

    database.expire(now=NOW + timedelta(days=400))

    record = database.read_checkpoint("cost", "src-1")
    assert record is not None
    assert record.state["offset"] == 5


# -- bounds ----------------------------------------------------------------


def test_an_oversized_checkpoint_state_is_refused_before_it_is_written(tmp_path: Path) -> None:
    database = _database(tmp_path / "aisquare-home", StorageLimits(max_text_chars=16))

    with pytest.raises(StorageBoundsError) as caught:
        database.write_checkpoint("activity", "src-1", 0, {"blob": "x" * 64}, 1, NOW)

    assert "character limit" in str(caught.value)
    assert database.read_checkpoint("activity", "src-1") is None


def test_a_nested_checkpoint_state_is_refused_rather_than_flattened(tmp_path: Path) -> None:
    """Nesting is the shape that carries a transcript, a body or a path."""
    database = _database(tmp_path / "aisquare-home")

    with pytest.raises(StorageBoundsError) as caught:
        database.write_checkpoint("activity", "src-1", 0, {"nested": {"deep": 1}}, 1, NOW)

    assert "must be a scalar" in str(caught.value)


def test_a_pane_tail_is_bounded_to_the_configured_line_budget(tmp_path: Path) -> None:
    database = _database(
        tmp_path / "aisquare-home", StorageLimits(max_pane_lines=3, max_line_chars=8)
    )

    database.merge(
        LocalObservationBatch(
            collected_at=NOW,
            panes=(_pane(lines=tuple(f"line-{index}-and-more" for index in range(20))),),
        )
    )

    lines = database.read(now=NOW).panes[0].lines
    assert len(lines) == 3, "only the last max_pane_lines lines are kept"
    assert lines == ("line-17-", "line-18-", "line-19-"), (
        "each kept line is truncated to max_line_chars"
    )


def test_prompt_evidence_is_redacted_and_bounded_before_persistence(tmp_path: Path) -> None:
    database = _database(tmp_path / "aisquare-home", StorageLimits(max_raw_chars=80))

    database.merge(
        LocalObservationBatch(
            collected_at=NOW,
            prompts=(
                _prompt(raw="Edit /home/someone/secrets/app/main.py to add a flag? " + "x" * 200),
            ),
        )
    )

    raw = database.read(now=NOW).prompts[0].raw
    assert raw is not None
    assert "/home/someone/secrets" not in raw
    assert "<path>" in raw
    assert len(raw) <= 80


def test_an_unknown_checkpoint_consumer_is_refused(tmp_path: Path) -> None:
    database = _database(tmp_path / "aisquare-home")

    with pytest.raises(StorageBoundsError) as caught:
        database.write_checkpoint("transcripts", "src-1", 0, {"offset": 1}, 1, NOW)

    assert "activity, cost" in str(caught.value)


def test_an_unknown_observation_category_is_refused(tmp_path: Path) -> None:
    database = _database(tmp_path / "aisquare-home")
    update = ObservationUpdate(
        category="project", key="k", payload={"a": 1}, generation=1, observed_at=NOW
    )
    smuggled = ObservationUpdate.__new__(ObservationUpdate)
    object.__setattr__(smuggled, "category", "secrets")
    for name in ("key", "payload", "generation", "observed_at", "expires_at"):
        object.__setattr__(smuggled, name, getattr(update, name))

    with pytest.raises(StorageBoundsError) as caught:
        database.write_checkpoint("activity", "src-1", 0, {}, 1, NOW, derived_updates=(smuggled,))

    assert "unknown observation category" in str(caught.value)


# -- checkpoints -----------------------------------------------------------


def test_a_checkpoint_and_its_derived_rows_commit_in_one_transaction(tmp_path: Path) -> None:
    database = _database(tmp_path / "aisquare-home")

    moved = database.write_checkpoint(
        "cost",
        "src-1",
        0,
        {"offset": 12, "state_version": 3},
        1,
        NOW,
        derived_updates=(
            ObservationUpdate(
                category="cost",
                key="agt-1:usd",
                payload={"usd": 1.25},
                generation=1,
                observed_at=NOW,
            ),
        ),
    )

    record = database.read_checkpoint("cost", "src-1")
    derived = database.derived_observations(category="cost")
    assert moved is True
    assert record is not None
    assert record.generation == 1
    assert record.state_version == 3
    assert [(update.key, update.payload["usd"]) for update in derived] == [("agt-1:usd", 1.25)]


def test_a_stale_checkpoint_write_changes_nothing_at_all(tmp_path: Path) -> None:
    """False means the loser wrote neither the cursor nor one derived row."""
    database = _database(tmp_path / "aisquare-home")
    assert database.write_checkpoint("activity", "src-1", 0, {"offset": 10}, 5, NOW)

    lost = database.write_checkpoint(
        "activity",
        "src-1",
        0,
        {"offset": 99},
        6,
        NOW + timedelta(minutes=1),
        derived_updates=(
            ObservationUpdate(
                category="activity",
                key="must-not-exist",
                payload={"turns": 99},
                generation=6,
                observed_at=NOW,
            ),
        ),
    )

    record = database.read_checkpoint("activity", "src-1")
    assert lost is False
    assert record is not None
    assert record.generation == 5
    assert record.state["offset"] == 10
    assert database.derived_observations(category="activity") == ()


def test_the_activity_and_cost_checkpoints_are_independent_namespaces(tmp_path: Path) -> None:
    database = _database(tmp_path / "aisquare-home")

    assert database.write_checkpoint("activity", "src-1", 0, {"offset": 3}, 3, NOW)
    assert database.write_checkpoint("cost", "src-1", 0, {"offset": 99}, 1, NOW)

    activity = database.read_checkpoint("activity", "src-1")
    cost = database.read_checkpoint("cost", "src-1")
    assert activity is not None and activity.generation == 3
    assert cost is not None and cost.generation == 1
    assert activity.state["offset"] == 3
    assert cost.state["offset"] == 99


def test_a_checkpoint_generation_may_not_move_backwards(tmp_path: Path) -> None:
    database = _database(tmp_path / "aisquare-home")
    assert database.write_checkpoint("activity", "src-1", 0, {"offset": 1}, 4, NOW)

    with pytest.raises(StorageBoundsError):
        database.write_checkpoint("activity", "src-1", 4, {"offset": 0}, 2, NOW)


def test_only_one_of_two_racing_checkpoint_writers_wins(tmp_path: Path) -> None:
    home = tmp_path / "aisquare-home"
    _database(home).migrate()
    results: list[bool] = []
    lock = threading.Lock()
    start = threading.Barrier(2)

    def advance(offset: int) -> None:
        database = _database(home)
        start.wait(timeout=5)
        won = database.write_checkpoint("activity", "src-1", 0, {"offset": offset}, 1, NOW)
        with lock:
            results.append(won)

    threads = [threading.Thread(target=advance, args=(offset,)) for offset in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(results) == [False, True], results


# -- correlations ----------------------------------------------------------


def _correlation(**overrides: object) -> CorrelationRecord:
    payload: dict[str, object] = {
        "project_id": "prj-1",
        "binding_id": "bnd-1",
        "binding_revision": 2,
        "pipeline_marker": "pm-1",
        "observed_at": NOW,
        "status": "verified",
        "session_id": "ses-1",
        "run_id": "run-9",
        "join_evidence": {"how": "pipeline marker"},
    }
    payload.update(overrides)
    return CorrelationRecord(**payload)  # type: ignore[arg-type]


def test_a_correlation_survives_a_restart_with_its_status_and_evidence(tmp_path: Path) -> None:
    home = tmp_path / "aisquare-home"
    first = _database(home)
    first.upsert_correlation(_correlation())
    first.upsert_correlation(
        _correlation(session_id="ses-2", run_id=None, status="unjoined", pipeline_marker="pm-2")
    )
    first.close()

    found = _database(home).correlations_for(project_id="prj-1")

    assert {record.status for record in found} == {"verified", "unjoined"}
    verified = next(record for record in found if record.status == "verified")
    assert verified.run_id == "run-9"
    assert verified.join_evidence["how"] == "pipeline marker"


def test_a_repeated_join_updates_in_place_rather_than_duplicating(tmp_path: Path) -> None:
    database = _database(tmp_path / "aisquare-home")
    database.upsert_correlation(_correlation(status="unjoined", run_id=None))

    database.upsert_correlation(_correlation(status="unjoined", run_id=None, source="derived"))

    found = database.correlations_for(project_id="prj-1", session_id="ses-1")
    assert len(found) == 1
    assert found[0].source == "derived"


def test_a_resumed_session_records_its_own_correlation(tmp_path: Path) -> None:
    database = _database(tmp_path / "aisquare-home")
    database.upsert_correlation(_correlation())

    database.upsert_correlation(_correlation(session_id="ses-resumed", run_id="run-10"))

    assert len(database.correlations_for(project_id="prj-1")) == 2
    assert len(database.correlations_for(project_id="prj-1", session_id="ses-resumed")) == 1


def test_a_verified_correlation_must_name_the_run_it_verified(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="verified correlation names the run"):
        _correlation(run_id=None)


def test_a_correlation_never_stores_a_workspace_key(tmp_path: Path) -> None:
    """The binding's identity and revision are stored; its credential is not."""
    database = _database(tmp_path / "aisquare-home")
    database.upsert_correlation(_correlation())

    stored = database.correlations_for(project_id="prj-1")[0]

    assert not hasattr(stored, "workspace_key")
    assert b"workspace_key" not in database.path.read_bytes()


# -- project facts ---------------------------------------------------------


def test_a_project_observation_keeps_a_label_and_never_a_root(tmp_path: Path) -> None:
    from aisquare.models import ProjectInfo

    database = _database(tmp_path / "aisquare-home")
    database.merge(
        LocalObservationBatch(
            collected_at=NOW,
            board_seq=2,
            projects=(
                ProjectInfo(
                    id="prj-1", root=Path("/home/someone/work/secret-project"), codename="amber"
                ),
            ),
        )
    )

    observations = database.project_observations()

    assert [observation.root_label for observation in observations] == ["secret-project"]
    assert observations[0].codename == "amber"
    assert b"/home/someone/work" not in database.path.read_bytes()
