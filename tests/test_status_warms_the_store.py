"""`aisquare status` creates and migrates the store, and that is deliberate.

The cutover runbook depends on it in two places, both quoted here because the
dependency is invisible from the code:

    §0b line 76   aisquare status >/dev/null    # creates and migrates ~/.aisquare/context.db
    line 630      aisquare status > /dev/null   # ONE process, alone — this re-migrates

The first removes a whole crew's exposure to the migration race by warming the
store once before several sessions launch together. The second is the recovery
step for a store wedged mid-migration, which takes every aisquare command with
it until one process alone re-migrates.

NOTHING PINNED EITHER, and that matters now rather than in the abstract, because
a doctrine folded a few commits ago says a DIAGNOSTIC MUST NOT BUILD THE MACHINE
IT DIAGNOSES — `doctor` was made read-only for exactly that reason. Status is
read-SHAPED and sits right beside it, so the obvious extension of a correct
doctrine is to make status read-only too, and that would silently delete both
runbook steps.

THE DISCRIMINATOR IS NOT READ-SHAPED VERSUS WRITE-SHAPED, IT IS DIAGNOSTIC
VERSUS ORDINARY. Doctor's contract is to tell you what is true, so two runs must
agree and it may not change anything. Status is an ordinary command that happens
to print; initialising the store on first use is what every ordinary command
does, and one of them has to be first.

`log` also creates the home. That is INCIDENTAL — nothing depends on it and
nothing forbids it — so it is deliberately not pinned here. A test that froze it
would make a later decision to change `log` look like a regression.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.paths import db_path
from aisquare.core.store import SCHEMA_VERSION


def test_status_creates_and_migrates_the_store_on_a_fresh_home(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The §0b contract, asserted as a MIGRATED store rather than a directory.

    "A directory appeared" would pass against a status that created an empty
    file and left it unmigrated — which is precisely the state §0b exists to
    prevent, since the race it defuses is several processes migrating at once.
    So this asserts the schema version and the integrity of what was left
    behind, which is what the runbook is actually relying on.
    """
    home = tmp_path / "fresh"
    monkeypatch.setenv("AISQUARE_HOME", str(home))
    assert not home.exists(), "the fixture must start from a home that does not exist"

    result = runner.invoke(app, ["status"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert home.exists(), (
        "status no longer creates the home — runbook §0b line 76 warms the store "
        "with this command, and line 630 recovers a wedged one with it"
    )
    database = db_path()
    assert database.exists(), "the store was not created"

    connection = sqlite3.connect(database)
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()

    assert version == SCHEMA_VERSION, (
        f"status left the store at schema {version}, not {SCHEMA_VERSION} — §0b "
        "warms the store so that later sessions do not migrate concurrently, and "
        "an unmigrated store defeats the whole point of running it first"
    )
    assert integrity == "ok", f"integrity_check returned {integrity!r}"


def test_a_second_status_leaves_the_migrated_store_alone(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Warming is idempotent, which is what makes §0b safe to put in a runbook.

    A step an operator is told to run before starting must not do something
    different the second time, and line 630 has them run it again on a machine
    that is already initialised.
    """
    monkeypatch.setenv("AISQUARE_HOME", str(tmp_path / "twice"))
    assert runner.invoke(app, ["status"], catch_exceptions=False).exit_code == 0
    first = db_path().stat().st_ino

    result = runner.invoke(app, ["status"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert db_path().stat().st_ino == first, "the second status replaced the store file"
    connection = sqlite3.connect(db_path())
    try:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
    finally:
        connection.close()
