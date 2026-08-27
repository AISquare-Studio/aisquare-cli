"""doctor sent an operator to a command that crashed and repaired nothing.

Measured at 5200357 in a throwaway home with a configured explainability
section and a corrupt ``context.db``:

* ``aisquare doctor`` → exit 1, "✗ database: context.db is unreadable: file is
  not a database", remediation "→ Re-initialise: aisquare init".
* ``aisquare init`` — exactly what it said — → exit 1 and **59 lines** of Rich
  traceback ending ``DatabaseError: file is not a database``, with frames in
  ``store.py``. The store is not repaired.
* ``aisquare init --reinit`` → refuses, correctly, because the section is
  configured. That refusal is not weakened here.
* Moving the file aside and re-creating it → works, and the configured section
  survives. So a recovery existed and doctor did not name it.

THE FIX IS THAT THE REMEDIATION AND THE ERROR ARE NOW ONE STRING.
``damaged_store_recovery()`` is the single source: doctor prints it as its fix,
and every command that hits a corrupt store prints it as its error. They were
different sentences and only one of them worked, which is the whole defect —
two copies can drift again, one cannot.

WHAT THE RECOVERY MAY NOT BE. Not repair: sqlite corruption is not something
this CLI should attempt to reconstruct. Not deletion by us: ``context.db``
holds the board's whole history and a diagnostic destroying it is the surprise
you cannot take back — so the file is MOVED aside, by the operator, and what is
lost is stated in the sentence. Tell, do not do.

The traceback assertions here are on ``result.exception`` as well as on the
text: at the real CLI the crash renders as a *Rich panel* whose header contains
"Traceback", but a check keyed only on that word is one output-format change
away from passing vacuously. The exception escaping the command is the property
that actually matters, and it is the one an operator experiences.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.config import AppConfig, ExplainabilityTarget, load_config, save_config
from aisquare.core.store import damaged_store_recovery
from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import diagnostics

_CORRUPT = b"this is not a database, not even a little"


def _corrupt_the_store() -> Path:
    """Byte-for-byte what an interrupted write or a bad restore leaves behind."""
    db = paths.db_path()
    db.write_bytes(_CORRUPT)
    return db


def _configured_cutover() -> None:
    """A machine mid-cutover: the state that makes `--reinit` refuse."""
    config = AppConfig()
    config.explainability.enabled = True
    config.explainability.target = "prod"
    config.explainability.targets["prod"] = ExplainabilityTarget(
        gateway_url="https://gateway.invalid", api_key_env="PROD_KEY_VAR"
    )
    save_config(config)


def _database_check() -> DoctorCheck:
    return next(c for c in diagnostics.doctor() if c.name == "database")


# --------------------------------------------------------------------------
# (b) the crash becomes legible
# --------------------------------------------------------------------------


def test_init_on_a_corrupt_store_is_legible_not_a_crash(runner: CliRunner) -> None:
    """The command doctor recommends, on the state doctor diagnosed."""
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    db = _corrupt_the_store()

    result = runner.invoke(app, ["init"])

    assert not isinstance(result.exception, sqlite3.DatabaseError), (
        "the raw sqlite error escaped the command — that is the traceback"
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "store.py" not in result.output, "a source frame reached the operator"
    assert str(db) in result.output, "name the file, or they cannot act on it"


def test_the_error_carries_the_recovery_not_just_the_diagnosis(runner: CliRunner) -> None:
    """``fail()`` prints ``message`` and nothing else.

    ``hint`` reaches ``--json`` callers only, so a recovery placed there is
    invisible to the human being told their store is broken. This asserts the
    recovery is on the surface an operator actually reads.
    """
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    _corrupt_the_store()

    result = runner.invoke(app, ["init"])

    assert "mv " in result.output, "no executable step reached the human surface"
    assert "aisquare init" in result.output


def test_status_is_legible_too(runner: CliRunner) -> None:
    """Measured crashing identically (62 lines, store.py frames) and it is the
    command the runbook's own wedge-recovery step tells you to run."""
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    _corrupt_the_store()

    result = runner.invoke(app, ["status"])

    assert not isinstance(result.exception, sqlite3.DatabaseError)
    assert result.exit_code == 1
    assert "Traceback" not in result.output


def test_a_board_command_gains_the_recovery_it_lacked(runner: CliRunner) -> None:
    """``board`` was already legible — "✗ context store error: file is not a
    database" — and still a dead end: no file, no next step. Same string now."""
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    db = _corrupt_the_store()

    result = runner.invoke(app, ["board"])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert str(db) in result.output
    assert "mv " in result.output


def test_an_unexpected_database_error_keeps_its_traceback(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only corruption is translated; everything else stays a bug report.

    "no such table" is a schema defect in OUR code, and dressing it up as an
    operator-actionable corruption message would send them to delete a healthy
    database to fix our migration.
    """
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)

    def _boom(*_a: object, **_kw: object) -> None:
        raise sqlite3.DatabaseError("no such table: entries")

    monkeypatch.setattr("aisquare.services.lifecycle.store_session", _boom)

    result = runner.invoke(app, ["init"])

    assert isinstance(result.exception, sqlite3.DatabaseError)
    assert "mv " not in result.output, "a schema bug must not be sold as corruption"


# --------------------------------------------------------------------------
# (a) + (c) the remediation works, walked end to end
# --------------------------------------------------------------------------


def test_doctor_remediation_is_the_string_that_also_reports_the_error(
    runner: CliRunner,
) -> None:
    """One source, so the two can never disagree again — the defect itself."""
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    _corrupt_the_store()

    assert _database_check().fix == damaged_store_recovery()


def test_the_whole_chain_walks_from_corrupt_to_green(runner: CliRunner) -> None:
    """Corrupt store + configured section → doctor → ITS OWN remediation → green.

    The remediation is parsed out of doctor's output and executed rather than
    retyped here: a test that hardcodes the recovery would still pass if doctor
    started printing a different one.
    """
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    _configured_cutover()
    db = _corrupt_the_store()

    check = _database_check()
    assert check.status is CheckStatus.fail
    remediation = check.fix
    assert remediation is not None, "a failing check with no remediation is a dead end"

    move = re.search(r"mv (\S+) (\S+)", remediation)
    assert move is not None, f"remediation is not executable: {remediation}"
    source, destination = Path(move.group(1)), Path(move.group(2))
    assert source == db, "the remediation moves a file that is not the broken one"

    source.rename(destination)
    assert "aisquare init" in remediation
    recovered = runner.invoke(app, ["init"], catch_exceptions=False)

    assert recovered.exit_code == 0, recovered.output
    assert _database_check().status is CheckStatus.ok
    assert destination.read_bytes() == _CORRUPT, "the broken file is kept for forensics"


def test_the_recovery_does_not_cost_the_configured_cutover(runner: CliRunner) -> None:
    """The gateway URL and key-env name are configured out of band.

    If recovering a database silently reset them, this fix would have re-opened
    the hole ``--reinit``'s refusal was added to close.
    """
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    _configured_cutover()
    db = _corrupt_the_store()
    db.rename(db.with_suffix(".db.broken"))

    runner.invoke(app, ["init"], catch_exceptions=False)

    surviving = load_config().explainability
    assert surviving.targets["prod"].gateway_url == "https://gateway.invalid"
    assert surviving.enabled is True


def test_the_recovery_says_what_is_lost(runner: CliRunner) -> None:
    """An operator moving a file aside is entitled to know what goes with it."""
    text = damaged_store_recovery().lower()

    assert "lost" in text or "loses" in text
    assert "history" in text


# --------------------------------------------------------------------------
# boundaries the task set explicitly
# --------------------------------------------------------------------------


def test_doctor_never_deletes_or_edits_the_database(runner: CliRunner) -> None:
    """Tell, do not do. Diagnosis may not destroy the board's whole history."""
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    db = _corrupt_the_store()

    runner.invoke(app, ["doctor"])
    runner.invoke(app, ["doctor", "--fix"])

    assert db.exists(), "doctor deleted the database it was asked to diagnose"
    assert db.read_bytes() == _CORRUPT, "doctor rewrote the database"


def test_the_reinit_refusal_is_untouched(runner: CliRunner) -> None:
    """A corrupt store must not become a way around the consent gate."""
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    _configured_cutover()
    _corrupt_the_store()

    result = runner.invoke(app, ["init", "--reinit"])

    assert result.exit_code != 0
    assert "prod" in load_config().explainability.targets


def test_a_lock_timeout_is_not_dressed_up_as_damage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Contention is transient and must keep routing to "retry shortly".

    ``StoreUnopenable`` subclasses ``DatabaseError``, so a careless wrap would
    have swallowed the lock path too and told five sessions racing a first open
    that their board was damaged and should be moved aside. Driven through the
    real ``open_store`` — asserting on a hand-built exception would prove
    nothing about the branch that actually decides this.

    The damage case runs in the same harness as its control: if both came out
    the same type the probe would not be discriminating.
    """
    from aisquare.core import store

    monkeypatch.setenv("AISQUARE_DB_BUSY_MS", "1")  # the deadline passes at once

    def locked(_connection: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_migrate", locked)
    with pytest.raises(sqlite3.OperationalError) as contention:
        store.open_store()

    assert not isinstance(contention.value, store.StoreUnopenable)
    assert store.is_locked_error(contention.value) is True

    def damaged(_connection: object) -> None:
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(store, "_migrate", damaged)
    with pytest.raises(store.StoreUnopenable):
        store.open_store()


def _wedge_the_store() -> None:
    """A store left mid-migration: the §0b race, reproduced by rewinding the
    version so a migration re-applies onto its own DDL."""
    connection = sqlite3.connect(str(paths.db_path()))
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    connection.execute(f"PRAGMA user_version = {version - 1}")
    connection.commit()
    connection.close()


# Every command measured printing a traceback at 8bdc636, plus the three that
# were already legible — kept in the list so a regression in either direction
# is caught. `log`/`inject`/`context list`/`project`/`workspace` came from
# coder2's sweep; `status` is §0b's own command.
_STORE_COMMANDS = [
    ["status"],
    ["log"],
    ["inject"],
    ["context", "list"],
    ["project", "list"],
    ["project", "info"],
    ["workspace", "list"],
    ["board"],
    ["init"],
]


@pytest.mark.parametrize("damage", ["corrupt", "wedged"])
@pytest.mark.parametrize("command", _STORE_COMMANDS, ids=lambda c: "-".join(c))
def test_no_command_prints_a_traceback_against_a_damaged_store(
    runner: CliRunner, command: list[str], damage: str
) -> None:
    """They all die in one place, so they are all fixed in one place.

    Measured before the seam existed: 59-75 lines and 2-3 ``store.py`` frames
    from every command in this list bar three. Parametrised over BOTH damaged
    states because the first fix covered corruption only, and the wedged store
    — the one §0b exists for and the one the runbook documents — was still
    crashing.
    """
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    if damage == "corrupt":
        _corrupt_the_store()
    else:
        _wedge_the_store()

    result = runner.invoke(app, command)

    assert not isinstance(result.exception, sqlite3.DatabaseError), (
        f"{' '.join(command)} let the raw sqlite error escape"
    )
    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert "store.py" not in result.output
    assert "mv " in result.output, "legible but no recovery is still a dead end"


def test_json_callers_get_the_recovery_too(runner: CliRunner) -> None:
    """Five agent sessions hammer this store and they all read ``--json``.

    ``fail`` puts ``message`` on the human surface and ``hint``/``detail`` in
    the payload, so without an explicit hint a ``--json`` caller learns only
    that something is wrong. It needs the same next step a human gets.
    """
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    _corrupt_the_store()

    result = runner.invoke(app, ["--json", "board"])

    payload = json.loads(result.output.strip().splitlines()[0])
    assert payload["error"] == "store_unopenable"
    assert "mv " in payload["hint"]
    assert "aisquare init" in payload["hint"]
