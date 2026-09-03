"""SQLite store: CRUD, pool scoping, soft-delete, prefix lookup and search."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aisquare.core.ids import new_entry_id
from aisquare.core.store import AmbiguousIdError, ContextStore, open_store, store_session
from aisquare.models import ContextEntry, Pool, ProjectInfo

PROJECT = ProjectInfo(id="prj_test", root=Path("/tmp/example-project"), linked_repos=[])


@pytest.fixture
def store() -> Iterator[ContextStore]:
    with store_session() as opened:
        opened.ensure_project(PROJECT)
        yield opened


def _entry(
    text: str = "a fact",
    *,
    pool: Pool = "user",
    project_id: str | None = None,
    tags: list[str] | None = None,
) -> ContextEntry:
    now = datetime.now(tz=UTC)
    return ContextEntry(
        id=new_entry_id(),
        pool=pool,
        project_id=project_id,
        text=text,
        tags=tags or [],
        source="test",
        created_at=now,
        updated_at=now,
    )


def test_add_returns_entry_and_get_round_trips(store: ContextStore) -> None:
    entry = store.add(_entry("prefer tabs", tags=["style", "python"]))
    fetched = store.get(entry.id)
    assert fetched == entry
    assert fetched is not None and fetched.tags == ["style", "python"]


def test_get_resolves_unambiguous_prefix(store: ContextStore) -> None:
    entry = store.add(_entry())
    assert store.get(entry.id[:28]) == entry


def test_get_unknown_returns_none(store: ContextStore) -> None:
    store.add(_entry())
    assert store.get("ctx_doesnotexist") is None


def test_get_ambiguous_prefix_raises(store: ContextStore) -> None:
    store.add(_entry("one"))
    store.add(_entry("two"))
    with pytest.raises(AmbiguousIdError):
        store.get("ctx")  # the shared prefix matches every entry


def test_list_filters_by_pool(store: ContextStore) -> None:
    store.add(_entry("global", pool="user"))
    store.add(_entry("local", pool="project", project_id=PROJECT.id))
    assert [e.text for e in store.entries("user")] == ["global"]
    assert [e.text for e in store.entries("project", project_id=PROJECT.id)] == ["local"]


def test_list_in_scope_is_user_plus_current_project(store: ContextStore) -> None:
    store.add(_entry("global", pool="user"))
    store.add(_entry("local", pool="project", project_id=PROJECT.id))
    store.ensure_project(ProjectInfo(id="prj_other", root=Path("/tmp/other"), linked_repos=[]))
    store.add(_entry("elsewhere", pool="project", project_id="prj_other"))
    in_scope = {e.text for e in store.entries(project_id=PROJECT.id)}
    assert in_scope == {"global", "local"}  # excludes the other project


def test_update_changes_fields_and_bumps_timestamp(store: ContextStore) -> None:
    entry = store.add(_entry("typo heer", tags=["old"]))
    updated = store.update(entry.id, text="typo here", tags=["new"])
    assert updated.text == "typo here"
    assert updated.tags == ["new"]
    assert updated.created_at == entry.created_at
    assert updated.updated_at >= entry.updated_at


def test_update_unknown_raises(store: ContextStore) -> None:
    with pytest.raises(KeyError):
        store.update("ctx_missing", text="x")


def test_delete_is_a_soft_tombstone(store: ContextStore) -> None:
    entry = store.add(_entry("temporary"))
    store.delete(entry.id)
    assert store.get(entry.id) is None
    assert store.entries("user") == []
    # The row survives as a tombstone so the deletion can sync later.
    raw = sqlite3.connect(str(_db_path()))
    try:
        deleted_at = raw.execute(
            "SELECT deleted_at FROM entry WHERE id = ?", (entry.id,)
        ).fetchone()[0]
    finally:
        raw.close()
    assert deleted_at is not None


def test_search_matches_prefix_tokens(store: ContextStore) -> None:
    store.add(_entry("prefer pytest over unittest"))
    store.add(_entry("use ruff for linting"))
    assert [e.text for e in store.search("pytest")] == ["prefer pytest over unittest"]
    assert [e.text for e in store.search("lint")] == ["use ruff for linting"]
    assert store.search("nonexistent") == []
    assert store.search("") == []


def test_search_excludes_deleted(store: ContextStore) -> None:
    entry = store.add(_entry("findable token"))
    assert len(store.search("findable")) == 1
    store.delete(entry.id)
    assert store.search("findable") == []


def test_search_respects_pool(store: ContextStore) -> None:
    store.add(_entry("alpha keyword", pool="user"))
    store.add(_entry("beta keyword", pool="project", project_id=PROJECT.id))
    assert [e.text for e in store.search("keyword", pool="user")] == ["alpha keyword"]
    project_hits = store.search("keyword", pool="project", project_id=PROJECT.id)
    assert [e.text for e in project_hits] == ["beta keyword"]


def test_search_in_scope_is_user_plus_current_project(store: ContextStore) -> None:
    store.add(_entry("alpha keyword", pool="user"))
    store.add(_entry("beta keyword", pool="project", project_id=PROJECT.id))
    store.ensure_project(ProjectInfo(id="prj_other", root=Path("/tmp/other"), linked_repos=[]))
    store.add(_entry("gamma keyword", pool="project", project_id="prj_other"))
    hits = {e.text for e in store.search("keyword", project_id=PROJECT.id)}
    assert hits == {"alpha keyword", "beta keyword"}  # excludes the other project


def test_promote_moves_project_entry_to_user_pool(store: ContextStore) -> None:
    entry = store.add(_entry("ship it", pool="project", project_id=PROJECT.id, tags=["t"]))
    promoted = store.promote(entry.id)
    assert promoted.id == entry.id  # moved in place
    assert promoted.pool == "user"
    assert promoted.project_id is None
    assert promoted.tags == ["t"]
    assert promoted.updated_at >= entry.updated_at
    assert [e.text for e in store.entries("user")] == ["ship it"]
    assert store.entries("project", project_id=PROJECT.id) == []


def test_promote_rejects_user_entry(store: ContextStore) -> None:
    entry = store.add(_entry("already global", pool="user"))
    with pytest.raises(ValueError, match="already in the user pool"):
        store.promote(entry.id)


def test_promote_unknown_raises(store: ContextStore) -> None:
    with pytest.raises(KeyError):
        store.promote("ctx_missing")


def test_schema_rejects_inconsistent_pool(store: ContextStore) -> None:
    bad = _entry("oops", pool="user", project_id=PROJECT.id)
    with pytest.raises(sqlite3.IntegrityError):
        store.add(bad)


def test_list_and_get_projects(store: ContextStore) -> None:
    other = ProjectInfo(id="prj_other", root=Path("/tmp/another-app"), linked_repos=[])
    store.ensure_project(other)  # PROJECT is already registered by the fixture
    ids = {project.id for project in store.list_projects()}
    assert ids == {PROJECT.id, "prj_other"}
    assert store.get_project("prj_other") == other
    assert store.get_project("prj_missing") is None


def test_find_projects_by_name_and_id_prefix(store: ContextStore) -> None:
    assert [p.id for p in store.find_projects("example-project")] == [PROJECT.id]  # by name
    assert [p.id for p in store.find_projects(PROJECT.id[:8])] == [PROJECT.id]  # by id prefix
    assert store.find_projects("nope") == []


def test_add_linked_repo_is_idempotent(store: ContextStore) -> None:
    updated = store.add_linked_repo(PROJECT.id, "git@github.com:acme/app.git")
    assert updated.linked_repos == ["git@github.com:acme/app.git"]
    again = store.add_linked_repo(PROJECT.id, "git@github.com:acme/app.git")
    assert again.linked_repos == ["git@github.com:acme/app.git"]  # no duplicate


def test_add_linked_repo_unknown_project_raises(store: ContextStore) -> None:
    with pytest.raises(KeyError):
        store.add_linked_repo("prj_missing", "repo")


def test_add_and_list_prompts(store: ContextStore) -> None:
    store.add_prompt("first prompt", PROJECT.id)
    store.add_prompt("second prompt", PROJECT.id)
    prompts = store.recent_prompts(PROJECT.id)
    assert [p.text for p in prompts] == ["second prompt", "first prompt"]  # newest first


def test_recent_prompts_are_scoped_to_project(store: ContextStore) -> None:
    store.ensure_project(ProjectInfo(id="prj_other", root=Path("/tmp/other"), linked_repos=[]))
    store.add_prompt("here", PROJECT.id)
    store.add_prompt("there", "prj_other")
    assert [p.text for p in store.recent_prompts(PROJECT.id)] == ["here"]


def test_migrations_reach_the_current_schema_version() -> None:
    from aisquare.core.store import SCHEMA_VERSION

    open_store().close()  # creates and migrates the database
    raw = sqlite3.connect(str(_db_path()))
    try:
        version = raw.execute("PRAGMA user_version").fetchone()[0]
    finally:
        raw.close()
    assert version == SCHEMA_VERSION == 12


def test_the_metric_check_constraints_mirror_the_python_vocabularies() -> None:
    """Each closed vocabulary is spelled twice — once in SQL, once in Python —
    because SQLite cannot read an enum. Held equal here so neither can drift:
    a value the model accepts that the CHECK refuses would lose the row
    silently, on every prompt, with nothing raising."""
    import re
    from typing import get_args

    from aisquare.core.store import _SCHEMA_V11, _SCHEMA_V12
    from aisquare.models import (
        BriefingStatus,
        CacheStatus,
        ClientReason,
        DeliverySource,
        HookAction,
        HookTrigger,
        RunKind,
    )

    def sql_set(column: str, schema: str = _SCHEMA_V11) -> set[str]:
        match = re.search(rf"{column} TEXT[^,]*?IN \(([^)]*)\)", schema, re.DOTALL)
        assert match is not None, column
        return {value.strip().strip("'") for value in match.group(1).split(",")}

    # Against the Python vocabulary itself, never a third copy typed here: a
    # Literal that moves in models.py must move the SQL or fail this test.
    assert sql_set("client_reason") == {reason.value for reason in ClientReason}
    assert sql_set("status") == set(get_args(BriefingStatus))
    assert sql_set("action") == set(get_args(HookAction))
    assert sql_set("trigger") == set(get_args(HookTrigger))
    assert sql_set("cache_status") == set(get_args(CacheStatus))
    assert sql_set("run_kind") == set(get_args(RunKind))
    assert sql_set("delivery_source", _SCHEMA_V12) == set(get_args(DeliverySource))


def test_the_metric_table_has_no_column_that_could_name_an_arm() -> None:
    from aisquare.core.store import _SCHEMA_V11, _SCHEMA_V12

    for forbidden in ("arm", "flags_hash", "architecture", "CREATE TABLE run"):
        assert forbidden not in _SCHEMA_V11 and forbidden not in _SCHEMA_V12, forbidden


def test_a_populated_v10_database_migrates_to_the_current_version_with_its_rows_intact() -> None:
    """The migration real machines take: every row that existed before the
    metric table survives it, the table arrives with the v2 columns and the
    v12 one, and no ``run`` table comes along."""
    from aisquare.core.store import _MIGRATIONS

    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(db))
    try:
        for migration in _MIGRATIONS[:10]:
            raw.executescript(migration)
        raw.execute("PRAGMA user_version = 10")
        raw.execute(
            "INSERT INTO project (id, root, name, linked_repos, created_at) VALUES (?, ?, ?, ?, ?)",
            ("prj_old", "/tmp/old", "old", "[]", "2026-01-01T00:00:00+00:00"),
        )
        raw.execute(
            "INSERT INTO entry (id, pool, project_id, text, tags, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "ctx_old",
                "project",
                "prj_old",
                "survives",
                "[]",
                "test",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        raw.commit()
    finally:
        raw.close()

    open_store().close()

    raw = sqlite3.connect(str(db))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 12
        assert (
            raw.execute("SELECT text FROM entry WHERE id = 'ctx_old'").fetchone()[0] == "survives"
        )
        columns = {row[1] for row in raw.execute("PRAGMA table_info(metric)")}
        tables = {
            row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        raw.close()
    assert {
        "client_reason",
        "status",
        "action",
        "query_id",
        "opaque_config_id",
        "run_kind",
        "delivery_source",
    } <= columns
    assert "arm" not in columns and "run" not in tables


def _at_version(version: int, *, after: str = "", stamp: int | None = None) -> Path:
    """A database migrated by hand to ``version``, with ``after`` run last and
    ``user_version`` stamped ``stamp`` (default: ``version``)."""
    from aisquare.core.store import _MIGRATIONS

    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(db))
    try:
        for migration in _MIGRATIONS[:version]:
            raw.executescript(migration)
        if after:
            raw.executescript(after)
        raw.execute(f"PRAGMA user_version = {stamp if stamp is not None else version}")
        raw.commit()
    finally:
        raw.close()
    return db


# The metric table the branch's v1 contract created (7557751..31956f2), stamped
# user_version 11 like the v2 one that replaced it in place. Column names as
# they were; the arm and flags_hash columns are the reason it must not stay.
V1_METRIC_DDL = """
CREATE TABLE metric (
    trace_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    session_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    wall_ms INTEGER,
    ci_action TEXT NOT NULL DEFAULT 'allow',
    degradation_reason TEXT NOT NULL DEFAULT 'disabled',
    cache_hit INTEGER NOT NULL DEFAULT 0,
    server_ms INTEGER,
    round_trip_ms INTEGER,
    budget_breach INTEGER NOT NULL DEFAULT 0,
    injected_chars INTEGER,
    run_id TEXT,
    arm TEXT,
    flags_hash TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    tool_calls INTEGER
);
CREATE INDEX metric_project_started ON metric (project_id, started_at);
CREATE INDEX metric_open_session ON metric (session_id, started_at) WHERE ended_at IS NULL;
CREATE TABLE run (
    id TEXT PRIMARY KEY,
    arm TEXT,
    flags_hash TEXT,
    started_at TEXT NOT NULL,
    note TEXT
);
INSERT INTO metric (trace_id, project_id, started_at, arm) VALUES ('trc_v1', 'prj_old', 't', 'A');
"""


def _metric_columns(db: Path) -> set[str]:
    raw = sqlite3.connect(str(db))
    try:
        return {row[1] for row in raw.execute("PRAGMA table_info(metric)")}
    finally:
        raw.close()


def test_a_v11_database_gains_the_column_and_keeps_its_rows() -> None:
    """The machine that ran the branch's stub smoke: at 11 with a populated
    metric table. v12 adds the column; the rows stay and read back with a
    ``None`` source, which is the truth about them."""
    db = _at_version(11)
    raw = sqlite3.connect(str(db))
    try:
        raw.execute(
            "INSERT INTO metric (trace_id, project_id, started_at, client_reason) "
            "VALUES ('trc_old', 'prj_old', '2026-09-01T00:00:00+00:00', 'disabled')"
        )
        raw.commit()
    finally:
        raw.close()
    store = open_store()
    try:
        (old,) = store.turn_metrics(project_id="prj_old")
    finally:
        store.close()
    assert old.trace_id == "trc_old" and old.delivery_source is None
    assert "delivery_source" in _metric_columns(db)


def test_a_v11_database_whose_metric_table_was_deleted_by_hand_heals() -> None:
    """The state the PR body's own advice for the v1-shaped table leaves behind
    — and the state this was written on: ``user_version`` 11, no ``metric``
    table, every row silently lost. v12 creates the table before it alters it,
    and a write lands afterwards."""
    from datetime import UTC, datetime

    from aisquare.models import TurnMetric

    db = _at_version(11, after="DROP TABLE metric;")
    assert _metric_columns(db) == set(), "the precondition: no metric table at all"
    store = open_store()
    try:
        store.open_turn(
            TurnMetric(
                trace_id="trc_healed",
                project_id="prj_x",
                started_at=datetime.now(tz=UTC),
                delivery_source="override",
            )
        )
        (row,) = store.turn_metrics(project_id="prj_x")
    finally:
        store.close()
    assert row.delivery_source == "override"
    raw = sqlite3.connect(str(db))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 12
    finally:
        raw.close()


def test_a_v11_database_with_the_v1_shaped_metric_table_is_moved_aside_and_rebuilt() -> None:
    """The third real v11 state. Every v12 statement would succeed on the v1
    shape and stamp it 12 — a table that can never take a v2 row and can never
    be migrated by version again. It is renamed (never dropped), its arm-shaped
    sibling with it, and the v2 shape is built in its place."""
    from datetime import UTC, datetime

    from aisquare.core.store import V1_ORPHAN_SUFFIX
    from aisquare.models import TurnMetric

    db = _at_version(10, after=V1_METRIC_DDL, stamp=11)
    assert "arm" in _metric_columns(db), "the precondition: the v1 shape"
    store = open_store()
    try:
        store.open_turn(
            TurnMetric(trace_id="trc_v2", project_id="prj_x", started_at=datetime.now(tz=UTC))
        )
        (row,) = store.turn_metrics(project_id="prj_x")
    finally:
        store.close()
    assert row.trace_id == "trc_v2" and row.delivery_source is None
    columns = _metric_columns(db)
    assert {"run_kind", "delivery_source"} <= columns and "arm" not in columns
    raw = sqlite3.connect(str(db))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 12
        tables = {
            row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert f"metric{V1_ORPHAN_SUFFIX}" in tables and f"run{V1_ORPHAN_SUFFIX}" in tables
        assert "run" not in tables, "nothing arm-shaped stays live"
        kept = raw.execute(f"SELECT trace_id, arm FROM metric{V1_ORPHAN_SUFFIX}").fetchall()
        assert kept == [("trc_v1", "A")], "renamed, never dropped"
        indexes = {row[1] for row in raw.execute("PRAGMA index_list(metric)")}
        assert {"metric_project_started", "metric_open_session"} <= indexes, (
            "the indexes follow the live table, not the orphan"
        )
    finally:
        raw.close()


def test_a_v1_shaped_table_is_moved_aside_even_when_an_orphan_already_exists() -> None:
    """The review's wedge: a fixed orphan name that already exists makes the
    rename raise, the transaction roll back and user_version stay 11 — so every
    later open fails identically until someone drops a table by hand."""
    from aisquare.core.store import V1_ORPHAN_SUFFIX

    prior = (
        f"CREATE TABLE metric{V1_ORPHAN_SUFFIX} (x TEXT);"
        f" CREATE TABLE run{V1_ORPHAN_SUFFIX} (x TEXT);"
    )
    db = _at_version(10, after=V1_METRIC_DDL + prior, stamp=11)
    open_store().close()
    raw = sqlite3.connect(str(db))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 12
        tables = {
            row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        raw.close()
    assert {f"metric{V1_ORPHAN_SUFFIX}", f"metric{V1_ORPHAN_SUFFIX}_2"} <= tables
    assert {f"run{V1_ORPHAN_SUFFIX}", f"run{V1_ORPHAN_SUFFIX}_2"} <= tables
    assert {"run_kind", "delivery_source"} <= _metric_columns(db)


def test_the_close_turn_compare_and_set_loses_to_a_stop_that_landed_first() -> None:
    """The review found the CAS uncovered: the racing test's second call never
    reached the UPDATE, and removing ``AND ended_at IS NULL`` left 74 tests
    green. Here the other Stop lands between this call's SELECT and its UPDATE,
    on the same connection, so the guard alone decides the outcome."""
    from datetime import UTC, datetime, timedelta
    from typing import Any

    from aisquare.models import TurnMetric

    store: Any = open_store()  # the concrete store, whose connection we interpose on
    try:
        started = datetime.now(tz=UTC) - timedelta(seconds=5)
        store.open_turn(
            TurnMetric(
                trace_id="trc_cas", project_id="prj_x", session_id="ses_cas", started_at=started
            )
        )
        first_close = (started + timedelta(seconds=1)).isoformat()
        real_conn = store._conn

        class RacingConnection:
            """The other Stop writes the row the instant before our UPDATE."""

            def __getattr__(self, name: str) -> object:
                return getattr(real_conn, name)

            def execute(self, sql: str, *params: object) -> object:
                if sql.lstrip().startswith("UPDATE metric SET ended_at"):
                    real_conn.execute(
                        "UPDATE metric SET ended_at = ?, wall_ms = ? WHERE trace_id = ?",
                        (first_close, 1000, "trc_cas"),
                    )
                return real_conn.execute(sql, *params)

        store._conn = RacingConnection()
        loser = store.close_turn("ses_cas", ended_at=started + timedelta(seconds=4))
        store._conn = real_conn
        (row,) = store.turn_metrics(session_id="ses_cas")
    finally:
        store.close()
    assert loser is None, "the second Stop reports that it closed nothing"
    assert row.ended_at is not None and row.ended_at.isoformat() == first_close
    assert row.wall_ms == 1000, "the first close is never overwritten"


def test_a_bad_delivery_source_is_refused_at_the_row() -> None:
    from datetime import UTC, datetime
    from typing import cast

    from aisquare.models import DeliverySource, TurnMetric

    store = open_store()
    try:
        # model_construct skips validation, so the CHECK is the only thing in
        # the way; the cast keeps mypy from refusing the value first.
        bad = TurnMetric.model_construct(
            trace_id="trc_bad",
            project_id="prj_x",
            started_at=datetime.now(tz=UTC),
            delivery_source=cast(DeliverySource, "server"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.open_turn(bad)
    finally:
        store.close()


def test_data_persists_across_reopen() -> None:
    first = open_store()
    first.ensure_project(PROJECT)
    entry = first.add(_entry("durable", pool="project", project_id=PROJECT.id))
    first.close()

    second = open_store()
    try:
        assert second.get(entry.id) == entry
    finally:
        second.close()


def _db_path() -> Path:
    from aisquare.core.paths import db_path

    return db_path()
