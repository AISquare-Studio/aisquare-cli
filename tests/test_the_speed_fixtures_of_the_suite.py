"""The two fixtures that keep the suite fast on a slow disk and a slow CPU (CI time, #29).

``the_board_does_not_wait_for_the_disk`` and ``the_command_tree_is_built_once`` (conftest)
change how a test runs, never what it asserts, and only while each does exactly what it says:
the first sets ``synchronous = OFF`` on the board's own connection and on nothing else, the
second hands back one tree for the app until its registrations move and never keeps the tree
of an app a test makes. Each pin below goes red when the fixture drops its half of that.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import typer
import typer.testing
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.store import store_session

#: SQLite's own numbers for ``PRAGMA synchronous``.
OFF, FULL = 0, 2


def _tree(target: typer.Typer) -> object:
    """What ``CliRunner.invoke`` asks for, as it asks."""
    return typer.testing._get_command(target)  # type: ignore[attr-defined]


def test_the_board_a_test_opens_does_not_wait_for_the_disk_and_is_still_in_wal() -> None:
    with store_session() as store:
        connection = store._conn  # type: ignore[attr-defined]
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == OFF
        assert str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"


def test_the_product_opens_its_board_with_sqlites_own_durability(tmp_path: Path) -> None:
    """In an interpreter that never loaded conftest: ``synchronous = OFF`` is the suite's
    alone. On a real ``~/.aisquare`` it would risk the board's last writes on a crash, so a
    store opened by the product keeps SQLite's default, ``FULL``."""
    code = (
        "from aisquare.core.store import store_session\n"
        "with store_session() as store:\n"
        "    print(store._conn.execute('PRAGMA synchronous').fetchone()[0])\n"
    )
    env = {**os.environ, "AISQUARE_HOME": str(tmp_path / "product-home")}
    done = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == str(FULL), done.stdout


def test_a_connection_that_is_not_the_board_waits_for_the_disk_as_sqlite_does(
    tmp_path: Path,
) -> None:
    connection = sqlite3.connect(str(tmp_path / "other.db"))
    try:
        # A statement first: the pragma is the board's alone, set once ITS journal mode took.
        connection.execute("CREATE TABLE t (a INTEGER)")
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == FULL
    finally:
        connection.close()


def test_a_factory_the_caller_names_is_kept(tmp_path: Path) -> None:
    class Mine(sqlite3.Connection):
        pass

    connection = sqlite3.connect(str(tmp_path / "mine.db"), factory=Mine)
    try:
        assert type(connection) is Mine
    finally:
        connection.close()


def test_the_pragma_follows_the_journal_mode_and_never_leads_it(tmp_path: Path) -> None:
    """What the order is for: a store another connection holds, or a damaged file, fails at
    ``PRAGMA journal_mode`` exactly as it did, because nothing ran before it."""
    connection = sqlite3.connect(str(tmp_path / "board.db"))
    try:
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        connection.execute("PRAGMA busy_timeout = 50")
        connection.execute("PRAGMA journal_mode = WAL")
        asked = [s for s in statements if s.startswith("PRAGMA")]
        assert asked == [
            "PRAGMA busy_timeout = 50",
            "PRAGMA journal_mode = WAL",
            "PRAGMA synchronous = OFF",
        ], asked
    finally:
        connection.close()


def test_a_file_that_is_not_a_database_fails_at_the_journal_statement_as_before(
    tmp_path: Path,
) -> None:
    """The pragma comes after the journal mode took, so a damaged file is refused by the
    statement that always refused it, not by one added in front of it."""
    damaged = tmp_path / "damaged.db"
    damaged.write_bytes(b"this is not a database" * 100)
    connection = sqlite3.connect(str(damaged))
    try:
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError as exc:
            assert "not a database" in str(exc)
        else:
            raise AssertionError("a file that is not a database took the journal mode")
        assert not any("synchronous" in statement for statement in statements), statements
    finally:
        connection.close()


def test_the_app_has_one_tree_until_its_registrations_move() -> None:
    first = _tree(app)
    assert _tree(app) is first

    @app.command("zz-speed-probe")
    def probe() -> None:
        """Registered by a test, and removed by it."""

    try:
        with_probe = _tree(app)
        assert with_probe is not first, "a command registered later is not in the kept tree"
        assert "zz-speed-probe" in with_probe.commands  # type: ignore[attr-defined]
    finally:
        app.registered_commands.pop()
    without = _tree(app)
    assert "zz-speed-probe" not in without.commands  # type: ignore[attr-defined]


def test_an_app_a_test_makes_for_itself_is_built_afresh_every_time() -> None:
    small = typer.Typer()

    @small.command()
    def one() -> None:
        """One."""

    assert _tree(small) is not _tree(small)


def test_the_kept_tree_runs_the_same_command_every_time() -> None:
    runner = CliRunner()
    first, second = runner.invoke(app, ["--version"]), runner.invoke(app, ["--version"])
    assert first.exit_code == second.exit_code == 0
    assert first.output == second.output != ""
