"""A truncated board database must not rebuild itself in silence.

Five damage shapes were measured against ``aisquare status``. Four are loud —
a Rich traceback ending in ``DatabaseError``:

    non-sqlite bytes          exit 1, 62 lines, "file is not a database"
    truncated to 100 bytes    exit 1, 62 lines, "database disk image is malformed"
    truncated to half         exit 1, 62 lines, "database disk image is malformed"
    header intact, page 2 zeroed  exit 1, 39 lines, "…malformed"

The fifth is silent, and it is the dangerous one. SQLite treats a ZERO-LENGTH
file as a brand-new empty database, so the store is rebuilt, migrated to the
current schema, and everything it held is gone:

    before   1 user entry, 2 in project, 1 project registered, user_version 10
    after    status exit 0, 6 lines, "0 user, 0 in this project, 0 project(s)"
             doctor: "✓ database: context.db is readable (0 user entries)"

`doctor` — the command you run precisely when you are unsure what state you are
in — actively affirms health while the board's entire history is gone. Nothing
anywhere says a file that existed has been emptied.

WHY THIS SHAPE IS PLAUSIBLE RATHER THAN THEORETICAL: a crash or a full disk
during a write leaves a zero-length file, this box keeps six 9p/DrvFs mounts
whose truncation semantics are unmeasured (see ``core.paths.aisquare_home``),
and the documented wedge recovery is "copy it aside, remove it, re-create" —
an operator who reaches for a truncating redirect instead of ``rm`` lands here
exactly, with no signal that they did.

IT ALSO CONTRADICTS THE RECOGNITION GUIDANCE §0b NOW CARRIES. That section
teaches an operator to recognise a wedged store by "sixty lines of stack trace
against six lines healthy". Two of the five shapes do not match it: the
page-corruption case prints 39 lines, and this one prints six and exits 0.

WHAT IS NOT DONE, DELIBERATELY: nothing is repaired, refused, or deleted. An
empty store is legitimate on a new machine, and this cannot tell a truncated
file from a fresh one after the fact — only AT the moment of opening, which is
the only moment the evidence exists. So it TELLS, the same principle
``disable`` follows when it names an environment variable it cannot unset, and
the same one that stops ``doctor`` deleting the database.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from aisquare.core import paths
from aisquare.core.store import open_store


def _used_machine() -> Path:
    """A store that exists on disk, created and migrated as a real one is."""
    open_store().close()
    database = paths.db_path()
    assert database.stat().st_size > 0, "the fixture produced no store"
    return database


def test_a_truncated_store_says_so_on_stderr(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """THE defect: against the current build this is completely silent."""
    database = _used_machine()
    database.write_bytes(b"")
    capsys.readouterr()

    open_store().close()

    message = capsys.readouterr().err
    assert "context.db" in message, f"nothing named the file: {message!r}"
    assert "empty" in message or "truncated" in message, (
        f"nothing said the file had been emptied: {message!r}"
    )


def test_it_still_opens_and_still_works(isolated_home: Path) -> None:
    """Fail open. Saying so must not become refusing.

    An operator whose board has just been emptied needs a working CLI more than
    they need a gatekeeper, and an empty store is a perfectly usable one.
    """
    database = _used_machine()
    database.write_bytes(b"")

    open_store().close()

    version = sqlite3.connect(str(database)).execute("PRAGMA user_version").fetchone()[0]
    assert version > 0, "the store was not migrated after being truncated"


def test_a_healthy_store_says_nothing(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control, and the reason this is safe to put on a hot path.

    Every command opens the store. A warning that fires on a normal open would
    be printed thousands of times a day and learned as noise, which is how the
    real signal gets ignored.
    """
    _used_machine()
    capsys.readouterr()

    open_store().close()

    assert capsys.readouterr().err == "", "a healthy open is not silent"


def test_a_brand_new_machine_says_nothing(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The boundary that matters most: absent is not truncated.

    On a new machine there is no file at all, and creating one is correct. Only
    a file that EXISTS at zero length means something made it and it lost what
    it held.
    """
    assert not paths.db_path().exists()
    capsys.readouterr()

    open_store().close()

    assert capsys.readouterr().err == "", "a first-ever open warned about truncation"
