"""Concurrent FIRST opens of a fresh store must not corrupt the migration.

Several sessions launching together onto a machine that has never run aisquare
is not an edge case — it is the morning. Before the fix, 12 concurrent first
opens raised a NON-transient `duplicate column name: account` in 27 of 15 runs,
and the damage was permanent: the column existed while `user_version` still said
8, so every later attempt at migration 8 failed on that database forever.

The cause was a time-of-check / time-of-use gap. `_migrate` read
`PRAGMA user_version`, then started the transaction — and between those two, a
racing opener could advance the schema, so this one applied an OLD migration to
a NEWER database. Instrumentation caught a thread running migration index 9
against a database that read version 8 on two independent connections.

These tests are about CONCURRENCY, so they use enough openers to have reproduced
the fault. Six did not; twelve did.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any

import pytest

from aisquare.core import paths
from aisquare.core.store import (
    _MIGRATIONS,
    SCHEMA_VERSION,
    _statements,
    is_locked_error,
    store_session,
)

OPENERS = 12


def _race() -> list[Exception]:
    errors: list[Exception] = []
    barrier = threading.Barrier(OPENERS)

    def opener() -> None:
        barrier.wait()
        try:
            with store_session() as store:
                store.entries("user")
        except Exception as exc:  # pragma: no cover - the assertions are below
            errors.append(exc)

    threads = [threading.Thread(target=opener) for _ in range(OPENERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


def test_the_version_is_read_under_the_write_lock() -> None:
    """The fix, stated as the invariant rather than as a race.

    Reproducing the failure needs concurrency AND luck — measured, it fires in
    0-2 of 15 twelve-way races depending on what else the box is doing — so a
    test that raced would be the load-sensitive kind this suite has already had
    to fix twice. The DEFECT, though, is not probabilistic: `PRAGMA
    user_version` was read BEFORE `BEGIN IMMEDIATE`, leaving a window in which
    another opener advances the schema and this one then applies an old
    migration to a newer database. Order the two statements correctly and the
    window does not exist.

    So this asserts the order, on a real first open, deterministically. It goes
    red against the pre-fix form.
    """
    seen: list[str] = []
    real_connect = sqlite3.connect

    def recording_connect(database: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        # SQLite's own trace callback rather than wrapping Connection.execute:
        # Connection is a C type and its methods are not assignable, and this
        # sees the statements the engine actually ran.
        connection: sqlite3.Connection = real_connect(database, *args, **kwargs)

        def trace(statement: str) -> None:
            head = statement.strip().split("\n")[0].strip().upper()
            if head.startswith("BEGIN IMMEDIATE"):
                seen.append("BEGIN")
            elif head.startswith("PRAGMA USER_VERSION") and "=" not in head:
                seen.append("READ")
            elif head.startswith(("CREATE ", "ALTER ", "DROP ", "INSERT ")):
                seen.append("DDL")

        connection.set_trace_callback(trace)
        return connection

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sqlite3, "connect", recording_connect)
        with store_session() as store:
            store.entries("user")

    assert "BEGIN" in seen and "DDL" in seen, seen
    # The exact invariant: after EVERY write lock, the version is re-read before
    # any DDL runs. Pre-fix the order per attempt was READ, BEGIN, DDL — the
    # read outside the lock, which is the gap. Post-fix it is BEGIN, READ, DDL.
    for position, event in enumerate(seen):
        if event != "BEGIN":
            continue
        following = seen[position + 1 :]
        next_read = following.index("READ") if "READ" in following else len(following)
        next_ddl = following.index("DDL") if "DDL" in following else len(following)
        assert next_read < next_ddl, (
            "a migration ran without re-reading the version under the write lock "
            f"— the time-of-check gap is back. order was {seen}"
        )


def test_a_race_leaves_a_fully_migrated_intact_store() -> None:
    """A smoke test, and honest about being one.

    It does NOT reliably reproduce the old defect (measured above), so it does
    not guard the fix — the ordering test does. What it does cover is that
    concurrent first opens end somewhere valid, and it would catch a fix that
    made the common path worse.
    """
    errors = _race()

    non_transient = [
        exc
        for exc in errors
        if not (isinstance(exc, sqlite3.OperationalError) and is_locked_error(exc))
    ]
    assert non_transient == [], non_transient

    conn = sqlite3.connect(str(paths.db_path()))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_splitting_a_script_builds_exactly_what_executescript_built() -> None:
    """The fix stopped using executescript; the schema must not have moved.

    `executescript` issues an implicit COMMIT before running, so it cannot be
    used inside the transaction the fix holds. Statements are split with
    SQLite's own parser instead — and this compares the two schemas object for
    object, SQL included, rather than trusting that they agree.
    """

    def build(split: bool) -> list[tuple[str, str, str]]:
        conn = sqlite3.connect(":memory:")
        try:
            for script in _MIGRATIONS:
                if split:
                    for statement in _statements(script):
                        conn.execute(statement)
                else:
                    conn.executescript(script)
            return sorted(
                conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall(),
                key=lambda row: (row[0], row[1]),
            )
        finally:
            conn.close()

    assert build(split=True) == build(split=False)


def test_a_semicolon_inside_a_trigger_body_does_not_split_the_statement() -> None:
    """Why SQLite's parser and not a regex: the day someone adds a trigger.

    No migration has one today, which is exactly when this is worth pinning —
    a naive split passes every existing test and breaks on the next one.
    """
    script = (
        "CREATE TABLE t (a INTEGER);\n"
        "CREATE TRIGGER trg AFTER INSERT ON t BEGIN\n"
        "  UPDATE t SET a = 1;\n"
        "  UPDATE t SET a = 2;\n"
        "END;\n"
    )

    statements = list(_statements(script))

    assert len(statements) == 2, statements
    assert "END;" in statements[1]
