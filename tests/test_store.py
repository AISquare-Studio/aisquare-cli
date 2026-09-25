"""SQLite store: CRUD, pool scoping, soft-delete, prefix lookup and search."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, tzinfo
from pathlib import Path

import pytest

from aisquare.core import store as store_module
from aisquare.core.ids import new_entry_id
from aisquare.core.store import (
    SCHEMA_VERSION,
    AmbiguousIdError,
    ContextStore,
    open_store,
    store_session,
)
from aisquare.models import CheckStatus, ContextEntry, Pool, ProjectInfo

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
    # A captured registration is reachable by id and in the full list, but not LISTED
    # until something adds it on purpose (#139).
    assert {project.id for project in store.list_projects(all=True)} == {PROJECT.id, "prj_other"}
    assert "prj_other" not in {project.id for project in store.list_projects()}
    stored = store.get_project("prj_other")
    assert stored is not None and stored.model_copy(update={"onboarded_at": None}) == other
    assert store.get_project("prj_missing") is None
    store.onboard_project(other)
    ids = {project.id for project in store.list_projects()}
    assert ids >= {"prj_other"}


def test_list_projects_hides_a_forgotten_registration_unless_asked(store: ContextStore) -> None:
    """``include_forgotten`` is for the one question a tombstone must not hide: a
    forgotten registration can still hold LIVE ``fleet_agent`` rows, whose panes are
    real processes. ``fleet shutdown`` asks this way so it cannot take a pane down
    while leaving its row live with nothing able to reconcile it.

    It is not ``all`` (#139), which adds the captured rows and still hides a
    tombstone. A forget clears ``onboarded_at``, so ``include_forgotten`` alone
    reads past the onboarded filter too — kept, that filter would drop the very
    tombstone the flag exists to find."""
    store.onboard_project(PROJECT)  # the fixture only captured it
    captured = ProjectInfo(id="prj_captured", root=Path("/tmp/captured-app"), linked_repos=[])
    store.ensure_project(captured)
    gone = ProjectInfo(id="prj_gone", root=Path("/tmp/gone-app"), linked_repos=[])
    store.onboard_project(gone)
    store.forget_project("prj_gone")

    assert [p.id for p in store.list_projects()] == [PROJECT.id], "the default still hides it"
    assert {p.id for p in store.list_projects(all=True)} == {PROJECT.id, "prj_captured"}, (
        "so does all"
    )
    everything = {PROJECT.id, "prj_captured", "prj_gone"}
    assert {p.id for p in store.list_projects(include_forgotten=True)} == everything
    assert {p.id for p in store.list_projects(all=True, include_forgotten=True)} == everything
    assert store.get_project("prj_gone") is None, "every OTHER read keeps the promise"


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


def test_add_and_list_prompts(store: ContextStore, monkeypatch: pytest.MonkeyPatch) -> None:
    # NO SLEEP. Both prompts are written inside one clock tick on purpose —
    # which is the case that used to sort by coin flip, because the tie-break
    # was an id whose tail is 128 random bits. `recent_prompts` breaks ties on
    # `rowid` now, so "written second" is what "sorts first" means, and this
    # asserts that rather than sleeping until the clock disambiguates it.
    #
    # The tick and the coin are both FIXED here rather than hoped for. On Linux
    # `created_at` has microsecond resolution and two inserts almost never
    # share it, so the tie never happened, and an `id DESC` tie-break passed
    # too. The clock is frozen, and the ids come out in the REVERSE of
    # insertion order, so only `rowid` puts the second prompt first (review of
    # #65, R5).
    frozen = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> _Frozen:
            return cls.fromtimestamp(frozen.timestamp(), tz)

    ids = iter(["prm_zzz_written_first", "prm_aaa_written_second"])
    monkeypatch.setattr(store_module, "datetime", _Frozen)
    monkeypatch.setattr(store_module, "new_prompt_id", lambda: next(ids))
    first = store.add_prompt("first prompt", PROJECT.id)
    second = store.add_prompt("second prompt", PROJECT.id)
    assert second.id < first.id, "the ids agree with insertion order; `id DESC` would pass too"
    prompts = store.recent_prompts(PROJECT.id)
    assert [p.created_at for p in prompts] == [frozen, frozen], "the tick was not shared"
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
    # v11 fleet, v12 metric, v13 converges, v14 forgotten_at, v15 the account registry
    # (#145), v16 usage readings and the limited state (#146), v17 onboarded_at (#139),
    # v18 the launch spec and ui_state (#144), v19 the project explainability key (#141),
    # v20 project groups, pins and manual order (#140), v21 project destinations (#142),
    # v22 the revocations owed for keys the CLI minted (#142), v23 the one-time repair of
    # old tombstones (#139, #140), v24 the destination a project's key was attached for (#142)
    assert version == SCHEMA_VERSION == 24


def test_the_metric_check_constraints_mirror_the_python_vocabularies() -> None:
    """Each closed vocabulary is spelled twice — once in SQL, once in Python —
    because SQLite cannot read an enum. Held equal here so neither can drift:
    a value the model accepts that the CHECK refuses would lose the row
    silently, on every prompt, with nothing raising."""
    import re
    from typing import get_args

    from aisquare.core.store import _SCHEMA_V12
    from aisquare.models import (
        BriefingStatus,
        CacheStatus,
        ClientReason,
        DeliverySource,
        HookAction,
        HookTrigger,
        RunKind,
    )

    def sql_set(column: str, schema: str = _SCHEMA_V12) -> set[str]:
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
    assert sql_set("delivery_source") == set(get_args(DeliverySource))


def test_the_metric_table_has_no_column_that_could_name_an_arm() -> None:
    from aisquare.core.store import _SCHEMA_V12, _SCHEMA_V13

    for forbidden in ("arm", "flags_hash", "architecture", "CREATE TABLE run"):
        assert forbidden not in _SCHEMA_V12 and forbidden not in _SCHEMA_V13, forbidden


def test_a_populated_v10_database_migrates_to_the_current_version_with_its_rows_intact() -> None:
    """The migration real machines take: every row that existed before the
    metric table survives it, the table arrives with the v2 columns and
    ``delivery_source``, and no ``run`` table comes along."""
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
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
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
    ``user_version`` stamped ``stamp`` (default: ``version``). Each step as the
    ladder runs it, so a step from v15 on brings its columns too."""
    from aisquare.core.store import _run_step

    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(db))
    try:
        for step in range(version):
            _run_step(raw, step)
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
CI_V11_METRIC_DDL = """
CREATE TABLE metric (
    trace_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    session_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    wall_ms INTEGER,
    run_id TEXT,
    run_kind TEXT CHECK (run_kind IN ('live', 'replay')),
    opaque_config_id TEXT,
    trigger TEXT CHECK (trigger IN ('session_start', 'prompt_submit', 'agent_request')),
    client_reason TEXT NOT NULL DEFAULT 'disabled',
    status TEXT,
    action TEXT,
    query_id TEXT,
    briefing_id TEXT,
    config_fingerprint TEXT,
    input_checkpoint TEXT,
    resolved_scope_version INTEGER,
    round_trip_ms INTEGER,
    server_ms INTEGER,
    deadline_breached INTEGER,
    token_count INTEGER,
    items_count INTEGER,
    cache_status TEXT,
    error_codes TEXT NOT NULL DEFAULT '[]',
    rendered_chars INTEGER,
    injected_chars INTEGER,
    frame_version TEXT,
    instruction_version TEXT,
    redaction_level TEXT,
    snapshot_ref TEXT,
    snapshot_untracked_excluded INTEGER,
    tokens_in INTEGER,
    tokens_out INTEGER,
    tool_calls INTEGER
);
CREATE INDEX metric_project_started ON metric (project_id, started_at);
CREATE INDEX metric_open_session ON metric (session_id, started_at) WHERE ended_at IS NULL;
"""
"""The ``metric`` table as THIS BRANCH stamped ``user_version 11`` before the
merge — no ``delivery_source``. It is spelled out here rather than taken from
``_SCHEMA_V11`` because that name now holds the fleet tables v0.6.0 shipped:
two branches claimed 11, which is the whole reason _SCHEMA_V13 exists."""


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
    metric table and no fleet tables. The merged ladder adds the column and the
    fleet tables; the rows stay and read back with a ``None`` source, which is
    the truth about them."""
    db = _at_version(10, after=CI_V11_METRIC_DDL, stamp=11)
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
    """``user_version`` 11 with no ``metric`` table — two ways to arrive: the
    PR body's own withdrawn advice for the v1-shaped table, and every v0.6.0
    install from PyPI, whose 11 is the fleet tables. The merged ladder creates
    the table before anything alters it, and a write lands afterwards."""
    from datetime import UTC, datetime

    from aisquare.models import TurnMetric

    db = _at_version(11)
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
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
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
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
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
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
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


# Every shape `user_version` 11 or 12 has in the wild, and what each must reach.
# Two branches claimed 11 at once — v0.6.0 shipped the fleet tables under it
# (and is on PyPI), this branch had the metric table — so the merged ladder is
# built to CONVERGE rather than to count, and this is the test that says so.
# Renumbering alone cannot pass this table: put the fleet first and every CI
# machine wedges on `table metric already exists`; put the metric first and
# every v0.6.0 install wedges on `table fleet_agent already exists`.
_COHORTS: tuple[tuple[str, str, int], ...] = (
    ("a fresh install", "", 10),
    ("v0.6.0 from PyPI: fleet at 11, no metric", "FLEET", 11),
    ("this branch at 11: metric, no fleet", "CI11", 11),
    ("this branch at 12: metric with the column", "CI12", 12),
    ("11 with no metric table at all", "", 11),
    ("11 with the v1-contract metric table", "V1", 11),
    ("11 with a v1 table and an orphan already aside", "V1ORPHAN", 11),
)


@pytest.mark.parametrize(("label", "shape", "stamp"), _COHORTS, ids=[c[0] for c in _COHORTS])
def test_every_shape_of_user_version_11_converges_on_one_schema(
    label: str, shape: str, stamp: int
) -> None:
    """No cohort is wedged, and none is left silently short of a table.

    "Silently short" is the half that is easy to miss: a store that opens but
    has no ``fleet_agent`` breaks the fleet UI at the first query, and one with
    no ``metric`` loses every CI row with nothing raising. So this asserts the
    end state by WRITING to both tables, not by reading the version.
    """
    from datetime import UTC, datetime

    from aisquare.core.store import _SCHEMA_V11
    from aisquare.models import TurnMetric

    after = {
        "": "",
        "FLEET": _SCHEMA_V11,
        "CI11": CI_V11_METRIC_DDL,
        "CI12": CI_V11_METRIC_DDL + "\nALTER TABLE metric ADD COLUMN delivery_source TEXT;",
        "V1": V1_METRIC_DDL,
        "V1ORPHAN": V1_METRIC_DDL + "\nCREATE TABLE metric_v1_orphaned (trace_id TEXT);",
    }[shape]
    db = _at_version(10, after=after, stamp=stamp)

    store = open_store()  # migrates on open; a wedge raises out of here
    try:
        store.open_turn(
            TurnMetric(
                trace_id="trc_cohort",
                project_id="prj_x",
                started_at=datetime.now(tz=UTC),
                delivery_source="descriptor",
            )
        )
        (row,) = store.turn_metrics(project_id="prj_x")
    finally:
        store.close()
    assert row.delivery_source == "descriptor", label

    raw = sqlite3.connect(str(db))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION, label
        # the fleet half must be there and usable too, whichever way in
        raw.execute(
            "INSERT INTO project (id, name, root, linked_repos, created_at, codename) "
            "VALUES ('prj_f', 'f', '/tmp/f', '[]', '2026-01-01T00:00:00+00:00', 'kestrel')"
        )
        raw.execute(
            "INSERT INTO fleet_agent (id, project_id, label, role, pane_id, cwd, created_at) "
            "VALUES ('agt_1', 'prj_f', 'a', 'dev', '%1', '/tmp/f', '2026-01-01T00:00:00+00:00')"
        )
        raw.commit()
        tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        raw.close()
    if shape.startswith("V1"):
        # the v1 rows are the developer's; renamed aside, never dropped
        assert any(t.startswith("metric_v1_orphaned") for t in tables), label
    if shape == "V1ORPHAN":
        assert "metric_v1_orphaned_2" in tables, "a taken orphan name must not wedge the rename"


# --- stores another line stamped 15 or 17 (the note above _SCHEMA_V15) ----------------------
#
# v15-v17 were claimed by other lines of development while the accounts stack held
# them. What each line's own steps above v14 left in its stores, verbatim from its
# branch: #136 (codex/native-personas-workflow), #201 (rc/hackathon-v1) and #113
# (feat/coding-agent-adapters), with a row in what each added.
WORK_BRIEF_AT_15 = """
CREATE TABLE IF NOT EXISTS work_brief (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS work_brief_project ON work_brief(project_id);
INSERT INTO work_brief (id, project_id, revision, data) VALUES ('wb_1', 'prj_used', 3, '{}');
"""
PERSONAS_AT_15 = """
ALTER TABLE team_session ADD COLUMN persona TEXT;
ALTER TABLE fleet_agent ADD COLUMN persona TEXT;
INSERT INTO team_session (id, project_id, started_at, last_seen_at, persona)
    VALUES ('ses_1', 'prj_used', '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00',
            'architect');
"""
CODING_AGENTS_AT_17 = """
ALTER TABLE team_session ADD COLUMN agent TEXT;
ALTER TABLE team_session ADD COLUMN native_session_id TEXT;
ALTER TABLE fleet_agent ADD COLUMN agent TEXT;
UPDATE team_session SET agent = 'claude-code', native_session_id = id
 WHERE account IS NOT NULL AND transcript_path LIKE '%/projects/%.jsonl';
UPDATE fleet_agent SET agent = 'claude-code' WHERE binary = 'claude';
ALTER TABLE team_meta ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0;
UPDATE team_meta SET updated_at = CAST(strftime('%s', 'now') AS INTEGER);
CREATE TRIGGER team_meta_insert_time AFTER INSERT ON team_meta BEGIN
 UPDATE team_meta SET updated_at = CAST(strftime('%s', 'now') AS INTEGER) WHERE key = NEW.key;
END;
CREATE TRIGGER team_meta_update_time AFTER UPDATE OF value ON team_meta BEGIN
 UPDATE team_meta SET updated_at = CAST(strftime('%s', 'now') AS INTEGER) WHERE key = NEW.key;
END;
UPDATE team_meta SET updated_at = CAST(strftime('%s', 'now') AS INTEGER)
 WHERE updated_at = 0;
INSERT INTO team_session (id, project_id, started_at, last_seen_at, agent, native_session_id)
    VALUES ('ses_1', 'prj_used', '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00',
            'codex', 'nat_1');
"""

# Two projects every cohort's store holds: one used on purpose (a context entry), which
# the v17 backfill adopts, and one only ever captured (a prompt), which it leaves hidden.
# The used one has a live fleet agent: a fleet read of it is what raised "no such
# column: account_slot" on a store that skipped v15 (review of #203).
_TWO_PROJECTS = """
INSERT INTO project (id, root, name, linked_repos, created_at) VALUES
    ('prj_used', '/w/used', 'used', '[]', '2026-09-01T00:00:00+00:00'),
    ('prj_seen', '/w/seen', 'seen', '[]', '2026-09-01T00:00:00+00:00');
INSERT INTO entry (id, pool, project_id, text, tags, source, created_at, updated_at)
    VALUES ('ent_1', 'project', 'prj_used', 'a fact', '[]', 'cli',
            '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00');
INSERT INTO prompt (id, project_id, text, source, created_at)
    VALUES ('prm_1', 'prj_seen', 'hello', 'claude-code', '2026-09-01T00:00:00+00:00');
INSERT INTO fleet_agent (id, project_id, label, role, pane_id, cwd, created_at)
    VALUES ('agt_1', 'prj_used', 'manager', 'manager', '%0', '/w/used',
            '2026-09-01T00:00:00+00:00');
"""

# (label, what the line's steps left, the stamp, a query over it, what it must still read)
_FOREIGN_COHORTS: tuple[tuple[str, str, int, str, list[tuple[object, ...]]], ...] = (
    (
        "#136 at 15: work_brief",
        WORK_BRIEF_AT_15,
        15,
        "SELECT id, revision FROM work_brief",
        [("wb_1", 3)],
    ),
    (
        "#201 at 15: persona columns",
        PERSONAS_AT_15,
        15,
        "SELECT persona FROM team_session",
        [("architect",)],
    ),
    # The maintainer's own store is this cohort: a backup of it (31 projects) holds
    # exactly these tables, indexes, triggers and columns, object for object.
    (
        "#113 at 17: coding-agent columns",
        CODING_AGENTS_AT_17,
        17,
        "SELECT session.agent, session.native_session_id, agent.agent"
        " FROM team_session AS session, fleet_agent AS agent",
        [("codex", "nat_1", "claude-code")],
    ),
)


def _shape(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """Every table, index and trigger by name, and every column as ``table.column``.

    SQLite's automatic indexes are left out: they come and go with their table."""
    shape: set[tuple[str, str]] = set()
    for kind, name in conn.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
    ).fetchall():
        shape.add((kind, name))
        if kind == "table":
            columns = conn.execute(f"PRAGMA table_info({name})").fetchall()
            shape |= {("column", f"{name}.{column[1]}") for column in columns}
    return shape


def _built(steps: int, after: str = "") -> set[tuple[str, str]]:
    """The shape of a database built in memory by the first ``steps`` steps, then ``after``."""
    from aisquare.core.store import _run_step

    conn = sqlite3.connect(":memory:")
    try:
        for step in range(steps):
            _run_step(conn, step)
        if after:
            conn.executescript(after)
        return _shape(conn)
    finally:
        conn.close()


def _contents(db: Path) -> tuple[list[tuple[object, ...]], dict[str, list[tuple[object, ...]]]]:
    """``db``'s whole schema, SQL included, and every row of every table, with its version."""
    raw = sqlite3.connect(str(db))
    try:
        schema = raw.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()
        rows = {
            name: sorted(raw.execute(f"SELECT * FROM {name}").fetchall(), key=repr)
            for kind, name, _, _ in schema
            if kind == "table"
        }
        rows["PRAGMA user_version"] = raw.execute("PRAGMA user_version").fetchall()
        return sorted(schema, key=repr), rows
    finally:
        raw.close()


def _open_a_foreign_cohort(db: Path, query: str) -> dict[str, object]:
    """Open ``db`` with this build, write through the store to the tables whose absence
    was "no such table", read from the ones a fleet read and ``accounts list`` failed
    on, open it once more, and read back what :func:`_converged` says it must hold."""
    store = open_store()  # a wedge raises out of here
    try:
        store.upsert_claude_account(1, Path("/h/.claude"))
        store.set_project_setting("prj_used", "claude_account", "1")
        read: dict[str, object] = {
            "listed": [p.id for p in store.list_projects()],
            "listed with --all": sorted(p.id for p in store.list_projects(all=True)),
            "account setting": store.project_setting("prj_used", "claude_account"),
            "accounts": [(r.slot, r.config_dir) for r in store.claude_accounts()],
            "fleet": [(a.id, a.account_slot) for a in store.fleet_agents("prj_used")],
            "live fleet": [a.id for a in store.fleet_agents("prj_used", live_only=True)],
            "missing": store.missing_schema(),
        }
    finally:
        store.close()
    schema, rows = _contents(db)
    open_store().close()
    schema_again, rows_again = _contents(db)
    read["a second open changed"] = sorted(
        name for name in rows.keys() | rows_again.keys() if rows.get(name) != rows_again.get(name)
    ) + (["the schema"] if schema_again != schema else [])
    raw = sqlite3.connect(str(db))
    try:
        read["version"] = raw.execute("PRAGMA user_version").fetchone()[0]
        read["shape"] = _shape(raw)
        read["their rows"] = raw.execute(query).fetchall()
    finally:
        raw.close()
    return read


def _converged(ddl: str, their_rows: list[tuple[object, ...]]) -> dict[str, object]:
    """What a foreign store reads once this build has opened it: the project used on
    purpose listed (the v17 backfill ran) and the captured one not, the account and the
    fleet agent read back, nothing ``doctor`` would report missing, a second open that
    changes no row and no schema, every table, index and column of this build and of
    the other line and nothing else, and the other line's rows as it left them."""
    theirs = _built(14, _TWO_PROJECTS + ddl) - _built(14, _TWO_PROJECTS)
    return {
        "listed": ["prj_used"],
        "listed with --all": ["prj_seen", "prj_used"],
        "account setting": "1",
        "accounts": [(1, Path("/h/.claude"))],
        "fleet": [("agt_1", None)],
        "live fleet": ["agt_1"],
        "missing": [],
        "a second open changed": [],
        "version": SCHEMA_VERSION,
        "shape": _built(SCHEMA_VERSION) | theirs,
        "their rows": their_rows,
    }


@pytest.mark.parametrize(
    ("label", "ddl", "stamp", "query", "expected"),
    _FOREIGN_COHORTS,
    ids=[c[0] for c in _FOREIGN_COHORTS],
)
def test_a_store_another_line_stamped_converges_on_this_schema_and_keeps_its_own(
    label: str, ddl: str, stamp: int, query: str, expected: list[tuple[object, ...]]
) -> None:
    """A store stamped 15 or 17 by another line never ran this line's steps below its
    stamp. Counted positionally, it had no ``claude_account``, so every accounts
    command failed with "no such table". Stamped 17, it had no ``onboarded_at`` either:
    v23 failed on that column and the store stopped opening. It converges instead:
    every table and column this build makes, the v17 backfill (so its projects stay
    listed), and the other line's tables, columns, triggers and rows left alone."""
    db = _at_version(14, after=_TWO_PROJECTS + ddl, stamp=stamp)

    assert _open_a_foreign_cohort(db, query) == _converged(ddl, expected), label


@pytest.mark.parametrize(
    ("label", "ddl", "stamp", "query", "expected"),
    _FOREIGN_COHORTS,
    ids=[c[0] for c in _FOREIGN_COHORTS],
)
def test_a_foreign_store_a_build_without_the_pass_carried_on_converges_too(
    label: str, ddl: str, stamp: int, query: str, expected: list[tuple[object, ...]]
) -> None:
    """A foreign store that a build without the presence pass has already opened ran
    this line's steps from its stamp on and none below it. Stamped 15, it reached the
    current version without v15's tables, and the ladder never runs for it again, so
    only the pass after the ladder can give them back. Stamped 17, it stopped at 22:
    v23 failed on the missing ``onboarded_at``, and the pass before v23 adds it."""
    from aisquare.core.store import _run_step

    carried_to = SCHEMA_VERSION if stamp == 15 else 22  # v23 is the step it failed on
    db = _at_version(14, after=_TWO_PROJECTS + ddl, stamp=stamp)
    raw = sqlite3.connect(str(db))
    try:
        for step in range(stamp, carried_to):
            _run_step(raw, step)
        raw.execute(f"PRAGMA user_version = {carried_to}")
        raw.commit()
        tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        raw.close()
    assert "claude_account" not in tables, "the fixture is the store that build left"

    assert _open_a_foreign_cohort(db, query) == _converged(ddl, expected), label


# --- doctor's database row reads the schema, not only the file (review of #203) ---------------


def test_doctor_names_what_a_store_lacks_instead_of_calling_it_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measured on #203 by the crew: a store #201 stamped 15, opened by a build that
    trusted the stamp, reached the current version with no ``claude_account`` and no
    ``fleet_agent.account_slot``. Every fleet read failed on the column, and doctor's
    database row said "context.db is readable". That build is this one with the
    presence pass taken out. The row fails and names each thing missing once (the
    table, not its indexes as well). Its remedy is not the corrupt-store move: the
    history in the file is intact."""
    from aisquare.services import diagnostics

    _at_version(14, after=_TWO_PROJECTS + PERSONAS_AT_15, stamp=15)
    monkeypatch.setattr(store_module, "_converge_by_presence", lambda connection, below: None)
    with store_session() as store, pytest.raises(sqlite3.OperationalError, match="account_slot"):
        store.fleet_agents("prj_used")

    row = diagnostics._check_database()

    assert row.status is CheckStatus.fail, row
    assert (
        "schema: column fleet_agent.account_slot, table claude_account, table project_setting;"
        in row.detail
    ), row.detail
    assert "claude_account_alias" not in row.detail, "an index of a missing table is noise"
    assert "persona" not in row.detail, "another line's columns are not this build's to report"
    assert row.fix is not None and f"cp {_db_path()}" in row.fix, row.fix
    assert "mv " not in row.fix, "the corrupt-store move would drop an intact history"
    assert "github.com/AISquare-Studio/aisquare-cli/issues" in row.fix, "report it where?"


def test_doctor_fails_on_what_no_step_of_this_build_puts_back() -> None:
    """The open restores only what a step from v15 on makes. A table or an index from
    before that, gone from a store another build or a hand edit changed, stays gone
    whatever the open does, and doctor's database row is where it shows. It names six
    and counts the rest, so the row stays one line an operator can read. It says what
    each kind the store lacks costs, and it still counts the notes, whose table is
    whole."""
    from aisquare.services import diagnostics

    indexes = (
        "entry_pool_project",
        "metric_open_session",
        "metric_project_started",
        "team_event_project_seq",
        "team_session_project",
        "team_task_project_status",
    )
    with store_session() as store:
        store.add(_entry())
    raw = sqlite3.connect(str(_db_path()))
    try:
        raw.executescript("DROP TABLE prompt;" + "".join(f"DROP INDEX {i};" for i in indexes))
    finally:
        raw.close()

    with store_session() as store:
        missing = store.missing_schema()
    row = diagnostics._check_database()

    assert missing[0] == "table prompt", "a missing table is named before any index"
    assert sorted(missing[1:]) == [f"index {i}" for i in indexes], missing
    assert row.status is CheckStatus.fail, row
    assert f"schema: {', '.join(missing[:6])} and 1 more;" in row.detail, row.detail
    assert row.detail.startswith("context.db opens (1 user entries) but"), row.detail
    assert row.detail.endswith(
        "; a command that reads a missing table or column fails with 'no such table' or "
        "'no such column'; a missing index that is not unique only slows the reads it served"
    ), row.detail


def test_a_store_without_its_full_text_index_lacks_one_table_not_five() -> None:
    """``entry_fts`` is an FTS5 table, and SQLite keeps what it indexes in shadow tables
    the module makes and drops with it (``entry_fts_data``, ``entry_fts_idx`` …). Read as
    this build's tables, a store without ``entry_fts`` lacked five, and they took five of
    the six names doctor's database row shows, crowding out what else the store lacked
    (review of the #203 side merges, F7). It lacks one table."""
    from aisquare.services import diagnostics

    open_store().close()
    raw = sqlite3.connect(str(_db_path()))
    try:
        raw.executescript("DROP TABLE entry_fts; DROP INDEX team_session_project;")
    finally:
        raw.close()

    with store_session() as store:
        missing = store.missing_schema()
    row = diagnostics._check_database()

    assert missing == ["table entry_fts", "index team_session_project"], missing
    assert row.status is CheckStatus.fail, row
    assert f"schema: {', '.join(missing)};" in row.detail, row.detail


@pytest.mark.parametrize(
    "shadow", ["entry_fts_data", "entry_fts_idx", "entry_fts_docsize", "entry_fts_config"]
)
def test_doctor_fails_on_a_full_text_index_that_lost_a_shadow_table(shadow: str) -> None:
    """A shadow table gone with ``entry_fts`` is that table's absence, named once. Gone
    alone, the index is damaged: no note can be added, and SQLite's answer reads as a
    damaged store. Left out of this build's schema whatever else was there, a store
    without ``entry_fts_data`` lacked nothing and doctor's row said "context.db is
    readable"; without ``entry_fts_config`` the row's own column read raised and called
    the store unreadable, with the corrupt-store move (review of the #203 side merges,
    R2-F1). The row fails naming the shadow table, says what it costs, and still counts
    the notes, which are intact."""
    from aisquare.services import diagnostics

    with store_session() as store:
        store.add(_entry())
    raw = sqlite3.connect(str(_db_path()))
    try:
        raw.executescript(f"DROP TABLE {shadow};")
    finally:
        raw.close()

    with store_session() as store:
        missing = store.missing_schema()
        with pytest.raises(sqlite3.DatabaseError):
            store.add(_entry())
    row = diagnostics._check_database()

    assert missing == [f"shadow table {shadow}"], missing
    assert row.status is CheckStatus.fail, row
    assert row.detail == (
        "context.db opens (1 user entries) but lacks part of this build's schema: shadow "
        f"table {shadow}; without a shadow table the notes' full-text index can be neither "
        "written nor searched: adding a note and `aisquare context search` fail with an "
        "error that reads as a damaged store, though the notes are intact"
    ), row.detail


def test_a_table_named_after_the_full_text_index_is_not_taken_for_its_shadow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shadow tables are told apart by FTS5's suffixes (``_data``, ``_idx`` …). Told apart
    by the prefix ``entry_fts_`` alone, a table of this build's named that way was left
    out of its schema and never reported missing (review of the #203 side merges,
    R2-F3). A step that makes ``entry_fts_meta`` stands in for one."""
    ladder = store_module._MIGRATIONS
    monkeypatch.setattr(
        store_module,
        "_MIGRATIONS",
        (*ladder[:-1], ladder[-1] + "CREATE TABLE entry_fts_meta (note TEXT);"),
    )
    open_store().close()
    raw = sqlite3.connect(str(_db_path()))
    try:
        raw.executescript("DROP TABLE entry_fts; DROP TABLE entry_fts_meta;")
    finally:
        raw.close()

    with store_session() as store:
        missing = store.missing_schema()

    assert missing == ["table entry_fts", "table entry_fts_meta"], missing


def test_doctor_says_what_a_trigger_it_only_counts_costs() -> None:
    """The row names six missing objects and counts the rest, but says what each kind
    the store lacks costs, a counted one too: what it costs is what the operator will
    meet. A trigger's sentence names the trigger, so a note trigger that falls into
    "and 1 more" still comes with its warning, and says which it is."""
    from aisquare.services import diagnostics

    tables = ("prompt", "team_event", "team_task", "team_meta", "metric")
    open_store().close()
    raw = sqlite3.connect(str(_db_path()))
    try:
        raw.executescript(
            "ALTER TABLE project DROP COLUMN linked_repos;"
            + "".join(f"DROP TABLE {table};" for table in tables)
            + "DROP TRIGGER entry_ai;"
        )
    finally:
        raw.close()

    row = diagnostics._check_database()

    assert row.status is CheckStatus.fail, row
    assert row.detail.startswith(
        "context.db opens (0 user entries) but lacks part of this build's schema: column "
        f"project.linked_repos, {', '.join(f'table {table}' for table in tables)} and 1 "
        "more; a command that reads a missing table or column fails"
    ), row.detail
    assert row.detail.endswith(f"; {diagnostics._TRIGGER_COSTS['entry_ai']}"), row.detail


@pytest.mark.parametrize(
    ("named", "script"),
    [
        ("table entry", "DROP TABLE entry;"),
        (
            "column entry.deleted_at",
            "DROP INDEX entry_pool_project; ALTER TABLE entry DROP COLUMN deleted_at;",
        ),
    ],
)
def test_doctor_names_a_store_without_its_notes_table_instead_of_calling_it_unreadable(
    named: str, script: str
) -> None:
    """The row counted the notes before it read the schema, and the count reads
    ``entry``. A store without that table, or a column of it, raised "no such table:
    entry" there and read as unreadable, with the corrupt-store move for its remedy: an
    intact history moved aside over a table the file merely lacks. The schema is read
    first, the count is left out when ``entry`` is what is missing, and the row names
    the gap like any other."""
    from aisquare.services import diagnostics

    open_store().close()
    raw = sqlite3.connect(str(_db_path()))
    try:
        raw.executescript(script)
    finally:
        raw.close()

    row = diagnostics._check_database()

    assert row.status is CheckStatus.fail, row
    assert row.detail.startswith(
        f"context.db opens but lacks part of this build's schema: {named}"
    ), row.detail
    assert row.fix is not None and "mv " not in row.fix, row.fix


@pytest.mark.parametrize(
    ("script", "said", "not_said"),
    [
        (
            "DROP TRIGGER entry_ai;",
            "schema: trigger entry_ai; without trigger entry_ai a new note is not indexed: "
            "`aisquare context search` misses it, and editing or removing it, or purging its "
            "project, fails as 'database disk image is malformed', which the CLI calls a "
            "damaged store though the notes are intact",
            ("stays indexed", "duplicates", "slows"),
        ),
        (
            "DROP TRIGGER entry_ad;",
            "schema: trigger entry_ad; without trigger entry_ad a note purged with its "
            "project stays indexed, and `aisquare context search` can match a later note on "
            "the purged one's words",
            ("malformed", "old text", "duplicates", "slows"),
        ),
        (
            "DROP TRIGGER entry_au;",
            "schema: trigger entry_au; without trigger entry_au an edited note stays indexed "
            "under its old text, so `aisquare context search` matches what it said, not what "
            "it says",
            ("malformed", "purged", "duplicates", "slows"),
        ),
        (
            "DROP INDEX fleet_agent_live_label;",
            "schema: unique index fleet_agent_live_label; a missing unique index raises "
            "nothing and lets in the duplicates it refused",
            ("context search", "slows"),
        ),
        (
            "DROP INDEX prompt_project;",
            "schema: index prompt_project; a missing index that is not unique only slows the "
            "reads it served",
            ("context search", "duplicates"),
        ),
    ],
    ids=["entry_ai", "entry_ad", "entry_au", "unique index", "index"],
)
def test_doctor_says_what_a_missing_index_or_trigger_costs(
    script: str, said: str, not_said: tuple[str, ...]
) -> None:
    """A missing table or column fails its readers with "no such table" or "no such
    column"; nothing raises on a missing index or trigger, so the row says what each
    costs, and only of the kinds the store lacks. It once said a missing index or
    trigger "fails nothing", and then gave every trigger the cost of `entry_ai`: a note
    it did not index hands FTS5 a 'delete' for text it never held when that note is
    edited, removed or purged, and SQLite answers "database disk image is malformed",
    which the CLI calls a damaged store and answers with the corrupt-store move. Without
    `entry_ad` or `entry_au` nothing fails, and search goes stale in a way of its own,
    so each trigger gets its own sentence (the tests after this one measure each)."""
    from aisquare.services import diagnostics

    open_store().close()
    raw = sqlite3.connect(str(_db_path()))
    try:
        raw.executescript(script)
    finally:
        raw.close()

    row = diagnostics._check_database()

    assert row.status is CheckStatus.fail, row
    assert row.detail.endswith(said), row.detail
    assert "fails nothing" not in row.detail and "no such" not in row.detail, row.detail
    assert not [cost for cost in not_said if cost in row.detail], row.detail


def test_doctor_reports_a_schema_gap_where_the_package_says_issues_go() -> None:
    """The database row's remedy says where to report a gap it cannot close. The address
    is a copy of pyproject's ``[project.urls] Issues``, so a tracker that moves there
    must move here too."""
    import tomllib

    from aisquare.services import diagnostics

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    urls = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["urls"]

    assert urls["Issues"] == diagnostics._ISSUES_URL


def _drop_trigger(name: str) -> None:
    """A store whose ``name`` trigger a hand edit or another build dropped."""
    open_store().close()
    raw = sqlite3.connect(str(_db_path()))
    try:
        raw.execute(f"DROP TRIGGER {name}")
        raw.commit()
    finally:
        raw.close()


def test_without_entry_ai_a_new_note_is_unsearchable_and_changing_it_is_malformed() -> None:
    """What doctor's row says of a missing `entry_ai`, measured: the new note never
    reaches the search index, and each later change hands FTS5 a 'delete' for text it
    never held, which SQLite answers as a corrupt file though nothing in it is."""
    _drop_trigger("entry_ai")
    with store_session() as store:
        store.ensure_project(PROJECT)
        note = store.add(_entry("alpha beta", pool="project", project_id=PROJECT.id))

        assert store.search("alpha", project_id=PROJECT.id) == []
        for change in (
            lambda: store.update(note.id, text="gamma delta"),
            lambda: store.delete(note.id),
            lambda: store.purge_project(PROJECT.id),
        ):
            with pytest.raises(sqlite3.DatabaseError, match="database disk image is malformed"):
                change()


def test_without_entry_ad_a_later_note_matches_a_purged_notes_words() -> None:
    """What doctor's row says of a missing `entry_ad`, measured: a purge deletes the
    project's notes for real, their text stays in the search index, and a note that
    takes the freed rowid is found by words it does not hold. Nothing fails."""
    _drop_trigger("entry_ad")
    with store_session() as store:
        store.ensure_project(PROJECT)
        store.add(_entry("alpha beta", pool="project", project_id=PROJECT.id))
        store.purge_project(PROJECT.id)
        later = store.add(_entry("gamma delta"))

        assert [entry.id for entry in store.search("alpha")] == [later.id]


def test_without_entry_au_an_edited_note_is_found_by_its_old_text() -> None:
    """What doctor's row says of a missing `entry_au`, measured: an edit leaves the
    search index on the text the note had. Nothing fails, a removal included."""
    _drop_trigger("entry_au")
    with store_session() as store:
        note = store.add(_entry("alpha beta"))
        store.update(note.id, text="gamma delta")

        assert [entry.text for entry in store.search("alpha")] == ["gamma delta"]
        assert store.search("gamma") == []
        store.delete(note.id)


def test_doctor_has_a_cost_for_every_trigger_of_this_build() -> None:
    """Doctor's database row says what a missing trigger costs from a sentence per
    trigger, in the ladder's order. A trigger the ladder makes without one would be
    named in the row with no word of what its absence does, so a new trigger fails
    here until it has its sentence."""
    from aisquare.services import diagnostics

    connection = sqlite3.connect(":memory:")
    try:
        store_module._migrate(connection)
        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY rowid"
        ).fetchall()
    finally:
        connection.close()

    assert [name for (name,) in triggers] == list(diagnostics._TRIGGER_COSTS)


def test_each_step_from_v15_on_declares_what_it_builds_and_builds_nothing_twice() -> None:
    """``_PRODUCTS`` is what the presence pass looks for: a step whose entry drifts from
    its script is a table the pass never restores, or one it restores on every open.
    Checked by building: each step's new tables, indexes and columns are exactly its
    entry, and applying the step again, as the pass does, changes nothing."""
    from aisquare.core.store import _PRODUCTS, _Products, _run_step

    assert all(14 <= step < SCHEMA_VERSION for step in _PRODUCTS)
    conn = sqlite3.connect(":memory:")
    try:
        for step in range(SCHEMA_VERSION):
            before = _shape(conn)
            _run_step(conn, step)
            after = _shape(conn)
            if step < 14:
                continue  # v1-v14 run once, below every stamp another line made
            new = after - before
            objects = {name for kind, name in new if kind != "column"}
            columns = {
                column
                for kind, column in new
                if kind == "column" and column.split(".")[0] not in objects
            }
            declared = _PRODUCTS.get(step, _Products())
            assert objects == set(declared.objects), f"v{step + 1}"
            assert columns == {f"{t}.{c}" for t, c, _ in declared.columns}, f"v{step + 1}"
            _run_step(conn, step)
            assert _shape(conn) == after, f"v{step + 1} applied twice"
    finally:
        conn.close()


def test_the_ladder_from_v15_run_again_over_a_current_store_changes_nothing() -> None:
    """Every step from v15 on is idempotent, so a store that meets them twice (stamped
    back to 14 here) opens with the same schema and the same rows. That includes the
    rows the v17 backfill reads: a captured project with a context entry stays
    captured, because the backfill runs only when it adds the column."""
    with store_session() as store:
        store.onboard_project(PROJECT)
        store.ensure_project(ProjectInfo(id="prj_captured", root=Path("/w/captured")))
        store.add(_entry("a fact", pool="project", project_id="prj_captured"))
        store.upsert_claude_account(1, Path("/h/.claude"))
        store.set_project_setting(PROJECT.id, "claude_account", "1")
        assert [p.id for p in store.list_projects()] == [PROJECT.id]

    def dump() -> tuple[list[tuple[object, ...]], dict[str, list[tuple[object, ...]]]]:
        raw = sqlite3.connect(str(_db_path()))
        try:
            schema = raw.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()
            rows = {
                name: sorted(raw.execute(f"SELECT * FROM {name}").fetchall(), key=repr)
                for kind, name, _, _ in schema
                if kind == "table"
            }
            return sorted(schema, key=repr), rows
        finally:
            raw.close()

    before = dump()
    raw = sqlite3.connect(str(_db_path()))
    try:
        raw.execute("PRAGMA user_version = 14")
        raw.commit()
    finally:
        raw.close()

    with store_session() as store:
        assert [p.id for p in store.list_projects()] == [PROJECT.id]
    raw = sqlite3.connect(str(_db_path()))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        raw.close()
    assert dump() == before


# --- the Claude account registry and per-project settings (v15, #145) ----------------------


def test_claude_account_registry_keeps_one_default_unique_aliases_and_a_dense_order(
    store: ContextStore,
) -> None:
    """The two invariants are the schema's: a second default and a reused alias are refused
    by an index, not by a caller remembering to check."""
    first = store.upsert_claude_account(1, Path("/h/.claude"))
    second = store.upsert_claude_account(2, Path("/h/.aisquare/claude-accounts/2"))
    assert (first.position, second.position) == (1, 2)  # queued at the end, in order
    assert not first.is_default and not second.is_default and first.alias is None

    store.set_claude_account_default(2)
    store.set_claude_account_default(1)
    assert [r.slot for r in store.claude_accounts() if r.is_default] == [1]  # moved, never two
    with pytest.raises(KeyError):
        store.set_claude_account_default(9)
    assert [r.slot for r in store.claude_accounts() if r.is_default] == [1]  # refused, unchanged
    store.set_claude_account_default(None)
    assert not any(r.is_default for r in store.claude_accounts())

    store.set_claude_account_alias(2, "work")
    with pytest.raises(sqlite3.IntegrityError):
        store.set_claude_account_alias(1, "work")
    assert [r.alias for r in store.claude_accounts()] == [None, "work"]
    store.set_claude_account_alias(2, None)
    store.set_claude_account_alias(1, "work")  # free again once released
    assert [r.alias for r in store.claude_accounts()] == ["work", None]

    store.set_claude_account_disabled(2, True)
    assert [r.disabled for r in store.claude_accounts()] == [False, True]

    # An upsert of a known slot refreshes the directory and touches nothing else.
    refreshed = store.upsert_claude_account(1, Path("/elsewhere/.claude"))
    assert refreshed.config_dir == Path("/elsewhere/.claude")
    assert refreshed.alias == "work" and refreshed.position == 1

    store.upsert_claude_account(3, Path("/h/.aisquare/claude-accounts/3"))
    store.order_claude_accounts([3, 9])  # 9 does not exist and is ignored
    assert [(r.slot, r.position) for r in store.claude_accounts()] == [(3, 1), (1, 2), (2, 3)]
    store.order_claude_accounts([2, 2, 3])  # a repeated reference left position 1 unused
    assert [(r.slot, r.position) for r in store.claude_accounts()] == [(2, 1), (3, 2), (1, 3)]
    store.order_claude_accounts([3, 1, 2])  # back to the order the rest of the test reads
    assert store.delete_claude_account(1) is True
    assert store.delete_claude_account(1) is False
    assert [(r.slot, r.position) for r in store.claude_accounts()] == [(3, 1), (2, 2)]  # dense


def test_project_settings_round_trip_per_project_and_clear(store: ContextStore) -> None:
    other = ProjectInfo(id="prj_other", root=Path("/tmp/other"), linked_repos=[])
    store.ensure_project(other)

    assert store.project_setting(PROJECT.id, "claude_account") is None
    store.set_project_setting(PROJECT.id, "claude_account", "2")
    store.set_project_setting(other.id, "claude_account", "3")
    store.set_project_setting(PROJECT.id, "claude_account", "4")  # an update, not a second row
    assert store.project_setting(PROJECT.id, "claude_account") == "4"
    assert store.project_setting(other.id, "claude_account") == "3"
    assert store.project_settings("claude_account") == {PROJECT.id: "4", other.id: "3"}
    assert store.project_setting(PROJECT.id, "something_else") is None

    assert store.clear_project_setting(PROJECT.id, "claude_account") is True
    assert store.clear_project_setting(PROJECT.id, "claude_account") is False
    assert store.project_settings("claude_account") == {other.id: "3"}
    with pytest.raises(sqlite3.IntegrityError):  # a foreign key: no setting for a ghost project
        store.set_project_setting("prj_ghost", "claude_account", "1")


# --- captured versus onboarded (#139) ------------------------------------------------------


def test_ensure_project_captures_and_only_onboard_project_shows() -> None:
    from aisquare.models import ProjectInfo

    store = open_store()
    try:
        quiet = ProjectInfo(id="prj_quiet", root=Path("/w/quiet"))
        store.ensure_project(quiet)  # what a hook does
        assert store.list_projects() == []  # captured, not shown
        [captured] = store.list_projects(all=True)
        assert captured.id == "prj_quiet" and captured.onboarded_at is None
        assert [p.id for p in store.captured_projects()] == ["prj_quiet"]
        assert store.get_project("prj_quiet") is not None  # reachable by id, as before
        store.ensure_project(quiet)  # again: still nothing changes
        assert store.list_projects() == []

        shown = store.onboard_project(quiet)  # what init / onboard / link / + / team on do
        assert shown.onboarded_at is not None
        assert [p.id for p in store.list_projects()] == ["prj_quiet"]
        assert store.captured_projects() == []
        first = shown.onboarded_at
        assert store.onboard_project(quiet).onboarded_at == first  # set once, kept

        # forget clears the mark and hides; a hook's capture afterwards brings the row
        # back CAPTURED — reachable, with its history, but not listed…
        store.forget_project("prj_quiet")
        assert store.list_projects(all=True) == [] and store.get_project("prj_quiet") is None
        store.ensure_project(quiet)
        assert store.list_projects() == [], "forget sticks against a capture"
        [back] = store.list_projects(all=True)
        assert back.id == "prj_quiet" and back.onboarded_at is None
        assert store.get_project("prj_quiet") is not None, "not a tombstone prompts vanish into"
        # …and a deliberate add lists it again, with a fresh mark.
        again = store.onboard_project(quiet)
        assert again.onboarded_at is not None and again.onboarded_at >= first
        assert [p.id for p in store.list_projects()] == ["prj_quiet"]
    finally:
        store.close()


def test_v23_repairs_the_tombstones_older_cuts_left_holding_a_mark_or_a_place() -> None:
    """The first cut of the v17 backfill (c716094) had no ``forgotten_at`` guard, so a
    store migrated by it holds forgotten rows stamped onboarded, and a forget written
    before #171's first round left the group, the position and the pin on its tombstone.
    Revived as they were, the next prompt there put the project back on the list, pinned
    and grouped (the bug #139 is about), and an onboarding kept the stale mark. Both were
    answered by a CASE on every capture; v23 clears them once, as a forget does now (review
    of #168 at the fold). A live row keeps its mark and its place."""
    legacy = "'2026-09-01T00:00:00+00:00'"
    _at_version(
        22,
        after=f"""
        INSERT INTO project_group (id, name, position, created_at)
            VALUES ('grp_1', 'tools', 0, {legacy});
        INSERT INTO project (id, root, name, linked_repos, created_at, onboarded_at,
                             forgotten_at, group_id, position, pinned_at)
        VALUES
            ('prj_old', '/w/old', 'old', '[]', {legacy}, {legacy},
             '2026-09-02T00:00:00+00:00', 'grp_1', 0, {legacy}),
            ('prj_gone', '/w/gone', 'gone', '[]', {legacy}, {legacy},
             '2026-09-02T00:00:00+00:00', NULL, NULL, {legacy}),
            ('prj_live', '/w/live', 'live', '[]', {legacy}, {legacy}, NULL, 'grp_1', 1, {legacy});
    """,
    )

    store = open_store()
    try:
        raw = sqlite3.connect(str(_db_path()))
        try:
            tombstones = raw.execute(
                "SELECT onboarded_at, group_id, position, pinned_at FROM project "
                "WHERE forgotten_at IS NOT NULL"
            ).fetchall()
        finally:
            raw.close()
        assert tombstones == [(None, None, None, None)] * 2, "repaired as a forget clears them"

        store.ensure_project(ProjectInfo(id="prj_old", root=Path("/w/old")))  # a prompt there
        store.ensure_project(ProjectInfo(id="prj_live", root=Path("/w/live")))  # one here
        onboarded = store.onboard_project(ProjectInfo(id="prj_gone", root=Path("/w/gone")))

        assert {p.id for p in store.list_projects()} == {"prj_live", "prj_gone"}
        revived = store.get_project("prj_old")
        assert revived is not None, "captured, not a tombstone"
        assert (revived.onboarded_at, revived.group_id, revived.position) == (None, None, None)
        assert revived.pinned_at is None
        assert onboarded.onboarded_at is not None
        assert onboarded.onboarded_at.isoformat() > "2026-09-02", "marked now, not the old mark"
        kept = store.get_project("prj_live")
        assert kept is not None and kept.onboarded_at is not None
        assert (kept.group_id, kept.position) == ("grp_1", 1) and kept.pinned_at is not None
    finally:
        store.close()


def test_v24_marks_the_keys_bound_to_their_destinations_deployment() -> None:
    """A binding named its deployment by target name alone, and the destination's ``stg``
    and the machine's ``stg`` can be two deployments (review of #203). v24 records the
    destination's API on a key bound to the deployment its destination names, as ``use``
    and ``key set`` bind it; a key bound to another target stays one of the machine's."""
    legacy = "'2026-09-01T00:00:00+00:00'"
    _at_version(
        23,
        after=f"""
        INSERT INTO project (id, root, name, linked_repos, created_at) VALUES
            ('prj_dest', '/w/dest', 'dest', '[]', {legacy}),
            ('prj_elsewhere', '/w/elsewhere', 'elsewhere', '[]', {legacy}),
            ('prj_machine', '/w/machine', 'machine', '[]', {legacy});
        INSERT INTO project_destination (project_id, api_url, environment, workspace_id,
                                         workspace_name, set_at)
        VALUES
            ('prj_dest', 'https://stg-api.aisquare.studio', 'stg', 42, 'acme', {legacy}),
            ('prj_elsewhere', 'https://api.aisquare.studio', 'prod', 42, 'acme', {legacy});
        INSERT INTO project_explainability (project_id, target, key_path, set_at) VALUES
            ('prj_dest', 'stg', '/k/dest', {legacy}),
            ('prj_elsewhere', 'stg', '/k/elsewhere', {legacy}),
            ('prj_machine', 'stg', '/k/machine', {legacy});
    """,
    )

    with store_session() as store:
        marked = {row.project_id: row.api_url for row in store.project_explainability_all()}
    assert marked == {
        "prj_dest": "https://stg-api.aisquare.studio",
        "prj_elsewhere": None,
        "prj_machine": None,
    }


def test_the_v17_migration_adopts_the_rows_already_used_on_purpose(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows with context entries, a codename, linked repos, board activity, a fleet
    agent or a snapshot on disk become onboarded; captured-only rows stay hidden."""
    from aisquare.core import snapshot as snapshot_core

    rows = [
        ("prj_entries", "/w/entries"),
        ("prj_named", "/w/named"),
        ("prj_linked", "/w/linked"),
        ("prj_board", "/w/board"),
        ("prj_agent", "/w/agent"),
        ("prj_snap", "/w/snap"),
        ("prj_quiet", "/w/quiet"),
        ("prj_gone", "/w/gone"),
        ("prj_gone_used", "/w/gone-used"),
    ]

    def insert(pid: str, root: str) -> str:
        repos = '["git@x:y.git"]' if pid == "prj_linked" else "[]"
        name = root.rsplit("/", 1)[1]
        return (
            "INSERT INTO project (id, root, name, linked_repos, created_at) VALUES "
            f"('{pid}', '{root}', '{name}', '{repos}', '2026-09-01T00:00:00+00:00');\n"
        )

    inserts = "".join(insert(pid, root) for pid, root in rows)
    after = (
        inserts
        + """
        UPDATE project SET codename = 'amber-otter' WHERE id = 'prj_named';
        UPDATE project SET forgotten_at = '2026-09-02T00:00:00+00:00'
            WHERE id IN ('prj_gone', 'prj_gone_used');
        INSERT INTO entry (id, pool, project_id, text, tags, source, created_at, updated_at)
            VALUES ('ent_1', 'project', 'prj_entries', 'a fact', '[]', 'cli',
                    '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00');
        INSERT INTO entry (id, pool, project_id, text, tags, source, created_at, updated_at)
            VALUES ('ent_2', 'project', 'prj_gone_used', 'kept by forget', '[]', 'cli',
                    '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00');
        INSERT INTO team_event (id, project_id, session_id, kind, text, created_at)
            VALUES ('evt_1', 'prj_board', NULL, 'activate', 'on', '2026-09-01T00:00:00+00:00');
        INSERT INTO fleet_agent (id, project_id, label, role, tmux_socket, pane_id, cwd,
                                 worktree, created_at)
            VALUES ('agt_1', 'prj_agent', 'coder-1', 'coder', 'asq', '%1', '/w/agent', 0,
                    '2026-09-01T00:00:00+00:00');
        INSERT INTO prompt (id, project_id, text, source, created_at)
            VALUES ('prm_1', 'prj_quiet', 'hello', 'claude-code', '2026-09-01T00:00:00+00:00');
    """
    )
    _at_version(16, after=after)
    for with_snapshot in ("prj_snap", "prj_gone_used"):  # forget keeps the snapshot
        snapshot_core.meta_path(with_snapshot).parent.mkdir(parents=True, exist_ok=True)
        snapshot_core.meta_path(with_snapshot).write_text("{}", encoding="utf-8")

    store = open_store()
    try:
        shown = {p.id for p in store.list_projects()}
        everything = {p.id for p in store.list_projects(all=True)}
        # The next prompt in a directory forgotten before v17 captures it; it must
        # not be re-listed by the entries and snapshot its forget left behind.
        store.ensure_project(ProjectInfo(id="prj_gone_used", root=Path("/w/gone-used")))
        revived = store.get_project("prj_gone_used")
        listed_after_prompt = {p.id for p in store.list_projects()}
    finally:
        store.close()
    assert shown == {"prj_entries", "prj_named", "prj_linked", "prj_board", "prj_agent", "prj_snap"}
    assert everything == shown | {"prj_quiet"}, "the prompt-only row is captured, not shown"
    assert "prj_gone" not in everything, "forgotten stays forgotten"
    assert "prj_gone_used" not in everything
    assert revived is not None and revived.onboarded_at is None, "a forgotten row is not adopted"
    assert "prj_gone_used" not in listed_after_prompt


def test_an_unreadable_snapshot_is_no_evidence_and_the_store_still_opens(
    isolated_home: Path,
) -> None:
    """The v17 backfill looks for each captured row's snapshot on disk, and ``Path.exists``
    raises for a directory this user cannot search. The ``PermissionError`` escaped the
    migration, which rolls back only on ``sqlite3.Error``, so the open failed with a raw
    traceback and the write transaction left open, on every command until the directory
    was readable again (review of #203). It is no evidence: the row stays captured."""
    from aisquare.core import snapshot as snapshot_core

    after = "".join(
        "INSERT INTO project (id, root, name, linked_repos, created_at) VALUES "
        f"('{pid}', '/w/{pid}', '{pid}', '[]', '2026-09-01T00:00:00+00:00');\n"
        for pid in ("prj_locked", "prj_snap")
    )
    _at_version(16, after=after)
    for pid in ("prj_locked", "prj_snap"):
        snapshot_core.meta_path(pid).parent.mkdir(parents=True, exist_ok=True)
        snapshot_core.meta_path(pid).write_text("{}", encoding="utf-8")
    locked = snapshot_core.snapshot_dir("prj_locked")
    locked.chmod(0o000)
    try:
        try:
            snapshot_core.exists("prj_locked")
        except PermissionError:
            pass
        else:
            pytest.skip("this user can search a directory with no permissions (root, or no modes)")
        store = open_store()
        try:
            shown = {p.id for p in store.list_projects()}
            everything = {p.id for p in store.list_projects(all=True)}
        finally:
            store.close()
    finally:
        locked.chmod(0o700)
    assert shown == {"prj_snap"}, "a readable snapshot still adopts its row"
    assert everything == {"prj_snap", "prj_locked"}, "the unreadable one stays captured"


# --- the launch spec and ui_state (#144) ----------------------------------------------------


def test_a_fleet_agents_launch_spec_round_trips_and_an_old_row_has_none(
    store: ContextStore,
) -> None:
    from aisquare.models import FleetAgent, LaunchSpec

    spec = LaunchSpec(
        binary="claude",
        permission_mode="auto",
        extra_args=["--chrome", "--effort", "high"],
        account_slot=2,
        worktree=True,
        command=["python", "-P", "-m", "aisquare", "launch", "coder"],
    )
    row = FleetAgent(
        id="agt_spec", project_id=PROJECT.id, label="coder-1", role="coder", pane_id="%1",
        cwd=Path("/w"), created_at=datetime.now(tz=UTC), launch_spec=spec,
    )  # fmt: skip
    stored = store.upsert_fleet_agent(row)
    assert stored.launch_spec == spec
    bare = store.upsert_fleet_agent(
        row.model_copy(update={"id": "agt_old", "label": "coder-2", "launch_spec": None})
    )
    assert bare.launch_spec is None
    # A spec that no longer parses (a future field renamed) costs the replay, not the row.
    with sqlite3.connect(str(_db_path())) as raw:
        raw.execute("UPDATE fleet_agent SET launch_spec = '{not json' WHERE id = 'agt_spec'")
    broken = store.get_fleet_agent("agt_spec")
    assert broken is not None and broken.launch_spec is None


def test_ui_state_is_a_key_value_memory(store: ContextStore) -> None:
    assert store.ui_state("fleet.selected") is None
    store.set_ui_state("fleet.selected", "project:prj_test")
    assert store.ui_state("fleet.selected") == "project:prj_test"
    store.set_ui_state("fleet.selected", "agent:prj_test/agt_1")  # every change is the save
    assert store.ui_state("fleet.selected") == "agent:prj_test/agt_1"
    store.set_ui_state("fleet.selected", None)
    assert store.ui_state("fleet.selected") is None
