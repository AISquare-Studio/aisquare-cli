"""Damage the open never sees still reached the operator as a stack trace.

``open_store`` raises ``StoreUnopenable`` and the root group translates it, so
a file that is not a database at all produces one legible line. That seam is at
the OPEN. Zero a page in the middle and leave the header intact and SQLite opens
the file happily; a ``SELECT`` finds the damage later, past the seam.

Measured at 3843ead, nine entries, pages 3 and 5 zeroed:

    status        exit 1, 52 lines, 4 source frames, DatabaseError
    context list  exit 1, 52 lines, 4 source frames
    log           exit 1, 51 lines, 4 source frames
    board         exit 1, ONE line — "context store error: database disk image
                  is malformed", naming no file and no next step

``board`` was legible only because ``_fail_team`` catches it, and it is the
same dead end the open-time message was before it carried a recovery.

WHAT MAY NOT BE SWEPT UP WITH IT. Three things arrive here as
``sqlite3.DatabaseError`` and they are not alike:

* **malformed / not a database** — the operator's file is damaged. Legible,
  named, with the recovery.
* **"database is locked"** — an ``OperationalError``, and it MUST keep meaning
  transient. Five sessions race this store nightly; telling contention to move
  the board aside would be the worst regression available here.
* **"no such table"** — our own schema bug, which keeps its traceback, because
  burying that costs whoever debugs it far more than a buried message costs an
  operator.

So the widening is keyed on ``is_corrupt_error`` — errorcode first, message as
a fallback — exactly as ``is_locked_error`` is. That helper was deleted as dead
code when the open-time seam replaced it; correct for that moment, wrong for
this one.

AND THE OPEN-TIME SENTENCE IS NOT REUSED. It says the store "cannot be opened",
which here is simply false — it opened. §0b of the cutover runbook also quotes
that sentence verbatim, so editing it in place would falsify an operator
document nobody touched. The two messages share one recovery function, which is
the thing that must not drift.
"""

from __future__ import annotations

import sqlite3

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.store import damaged_store_recovery

# The nine @8dd460fb measured, verbatim from their ratchet.
_QUERY_TIME_COMMANDS = [
    ["context", "export"],
    ["context", "list"],
    ["context", "preview"],
    ["ctx", "export"],
    ["ctx", "list"],
    ["ctx", "preview"],
    ["init"],
    ["inject"],
    ["status"],
]


def _damage_a_page(runner: CliRunner) -> None:
    """Zero page 2 with the header intact — the shape the open cannot see.

    Same recipe as tests/test_no_traceback_on_a_damaged_store.py rather than a
    second invention, so the two files cannot disagree about what "query-time
    damage" means.
    """
    runner.invoke(app, ["init", "--yes"], catch_exceptions=True)
    database = paths.db_path()
    good = database.read_bytes()
    assert len(good) > 8192, "fixture premise: store too small to corrupt a page of"
    database.write_bytes(good[:4096] + bytes(4096) + good[8192:])


@pytest.mark.parametrize("command", _QUERY_TIME_COMMANDS, ids=lambda c: " ".join(c))
def test_query_time_damage_is_one_legible_line(runner: CliRunner, command: list[str]) -> None:
    """The nine @8dd460fb measured, each naming the file and what to do.

    These use REAL page damage, unlike the classification pair below: the point
    here is that the seam is reached at all from a genuinely damaged file.
    """
    _damage_a_page(runner)

    result = runner.invoke(app, command)

    assert not isinstance(result.exception, sqlite3.DatabaseError), (
        f"{' '.join(command)} let the raw sqlite error escape"
    )
    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert "store.py" not in result.output, "a source frame reached the operator"
    assert str(paths.db_path()) in result.output, "name the file"
    assert "mv " in result.output, "legible but no recovery is still a dead end"


def _team_store_raises(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """Make the store method a team command reads raise, deterministically.

    Physical page damage is NOT used for this pair. Which page is fatal depends
    on what the store holds — @dfd9a883 measured pages 3 and 5 fatal where
    pages 1, 2, 10 and 20 were silent, and my own attempt zeroed a page
    `board`'s query never reads, so it printed twelve tasks quite happily. A
    fixture whose damage may or may not be seen cannot pin which BRANCH ran.
    The nine commands above are exercised with real damage; this pair is about
    the classification, so it injects the error the damage would have produced.
    """
    from aisquare.core import store as store_module

    def boom(*_a: object, **_kw: object) -> None:
        raise exc

    # Named from the store's real surface rather than guessed: my first attempt
    # patched "tasks", which does not exist, and every test then failed with an
    # AttributeError that looked nothing like the behaviour under test.
    for method in ("team_sessions", "team_tasks", "entries"):
        if hasattr(store_module.SqliteStore, method):
            monkeypatch.setattr(store_module.SqliteStore, method, boom)


def test_a_team_command_gains_the_recovery_too(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``board`` was already one line, and still a dead end: no file, no step."""
    runner.invoke(app, ["init", "--yes"], catch_exceptions=True)
    _team_store_raises(monkeypatch, sqlite3.DatabaseError("database disk image is malformed"))

    result = runner.invoke(app, ["board"])

    assert "Traceback" not in result.output
    assert str(paths.db_path()) in result.output, "name the file"
    assert "mv " in result.output, "legible but no recovery is still a dead end"


def test_contention_is_still_transient_not_damage(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression that would matter most.

    ``StoreUnopenable`` and this widening both key on ``sqlite3.DatabaseError``,
    and ``OperationalError`` IS one. Five sessions race this store nightly; a
    careless widening tells contention to move the board aside. Ordering is the
    whole fix: ``is_locked_error`` is consulted before ``is_corrupt_error``.
    """
    runner.invoke(app, ["init", "--yes"], catch_exceptions=True)
    _team_store_raises(monkeypatch, sqlite3.OperationalError("database is locked"))

    result = runner.invoke(app, ["board"])

    assert "mv " not in result.output, "contention was reported as damage"
    assert "busy" in result.output or "retry" in result.output, result.output


def test_the_message_does_not_claim_the_store_would_not_open(runner: CliRunner) -> None:
    """It opened. Saying otherwise sends the reader looking for the wrong thing.

    The open-time sentence is quoted verbatim in §0b of the cutover runbook, so
    this also pins that the two messages stayed distinct rather than one being
    edited into the other.
    """
    _damage_a_page(runner)

    result = runner.invoke(app, ["status"])

    assert "cannot be opened" not in result.output
    assert damaged_store_recovery() in result.output, "both messages share one recovery"


def test_a_schema_bug_still_escapes_with_its_traceback(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only corruption is translated. Our own bugs stay bug reports."""
    from aisquare.core import store as store_module

    runner.invoke(app, ["init", "--yes"], catch_exceptions=True)

    def missing(*_a: object, **_kw: object) -> None:
        raise sqlite3.OperationalError("no such table: entries")

    monkeypatch.setattr(store_module.SqliteStore, "entries", missing)

    result = runner.invoke(app, ["status"])

    assert isinstance(result.exception, sqlite3.DatabaseError)
    assert "mv " not in result.output, "a schema bug must not be sold as damage"


@pytest.mark.parametrize(
    ("code", "message", "corrupt"),
    [
        (sqlite3.SQLITE_CORRUPT, "database disk image is malformed", True),
        (sqlite3.SQLITE_NOTADB, "file is not a database", True),
        (sqlite3.SQLITE_BUSY, "database is locked", False),
        (sqlite3.SQLITE_ERROR, "no such table: entries", False),
    ],
)
def test_corruption_is_classified_by_code_not_by_hope(
    code: int, message: str, corrupt: bool
) -> None:
    """Errorcode first, message only as the fallback — same as is_locked_error.

    Both families that must NOT match are asserted, because the cost of a false
    positive here is an operator moving a healthy board aside.
    """
    from aisquare.core.store import is_corrupt_error

    plain = sqlite3.DatabaseError(message)
    assert is_corrupt_error(plain) is corrupt, "message fallback disagrees"

    coded = sqlite3.DatabaseError(message)
    coded.sqlite_errorcode = code
    assert is_corrupt_error(coded) is corrupt
