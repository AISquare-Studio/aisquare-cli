"""``doctor`` diagnoses; it must not build the thing it is diagnosing.

Run against a never-used ``AISQUARE_HOME`` it reported "home is missing" and
exited 1 — while having already created that directory, ``cache/``, ``log/``
and ``context.db``. The second run against the same path exited 0, because the
first run had fixed what it complained about.

Three separate costs, and the third is the one that reaches an operator:

* a diagnostic that mutates the machine it inspects. ``doctor`` is what you run
  when you are unsure what state you are in, which is exactly when you least
  want the tool to change it.
* the same machine gives two exit codes depending on whether anyone has run
  ``doctor`` before — the "two runs, one number" hazard, produced by the tool
  rather than by a harness, for anyone scripting the cutover.
* runbook §6 is "PROVE IT: ``aisquare doctor --live``". On a fresh machine,
  run before ``init``, that exits 1 naming a home it has just built.

The creation was never in the home check, which reports honestly what it found.
Four checks open the store, and ``store_session`` calls ``ensure_home``. They
now decline to open it when there is no home yet and say so, following the
convention already in this file: a verdict that ``home`` and ``config`` own is
reported at ok status elsewhere rather than repeated as a second failure.

``init`` creating these directories is correct and untouched.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths


def test_a_doctor_run_leaves_a_fresh_home_absent(runner: CliRunner) -> None:
    """The property the whole file exists for, asserted on the filesystem.

    Not "the database check does not open the store" — that is the mechanism
    today and could change. What must hold is that looking does not create.
    """
    home = paths.aisquare_home()
    assert not home.exists(), "the fixture must start from a home that does not exist"

    runner.invoke(app, ["doctor"], catch_exceptions=False)

    assert not home.exists(), (
        f"doctor created {home} while reporting on it — a diagnostic must not "
        "build the thing it is diagnosing"
    )


def test_two_doctor_runs_on_the_same_fresh_machine_agree(runner: CliRunner) -> None:
    """A machine's diagnosis must not depend on whether it has been diagnosed.

    This failed before: exit 1 then exit 0, same path, nothing else changed —
    the first run had created what the second one found.
    """
    first = runner.invoke(app, ["doctor"], catch_exceptions=False)
    second = runner.invoke(app, ["doctor"], catch_exceptions=False)

    assert first.exit_code == second.exit_code, (
        f"first run exited {first.exit_code}, second {second.exit_code} — "
        "doctor changed the machine it was asked to describe"
    )


def test_doctor_still_names_a_missing_home(runner: CliRunner) -> None:
    """Declining to create it must not cost the diagnosis."""
    result = runner.invoke(app, ["doctor"], catch_exceptions=False)

    assert "home" in result.output
    assert result.exit_code == 1, "a machine with no home is not healthy"


def test_the_store_backed_checks_report_rather_than_repeat_the_failure(
    runner: CliRunner,
) -> None:
    """They say "not created yet", not "unreadable".

    ``home`` owns the verdict that there is no home. A store check that also
    failed would report a second problem where there is one, and would send an
    operator looking at the database.
    """
    result = runner.invoke(app, ["doctor"], catch_exceptions=False)

    assert "unreadable" not in result.output, result.output
    for check in ("database", "snapshot", "brain", "harness"):
        line = next(row for row in result.output.splitlines() if f"{check}:" in row)
        assert "not created yet" in line, f"{check} check opened the store anyway: {line}"


def test_an_initialised_machine_is_unaffected(runner: CliRunner, tmp_path: Path) -> None:
    """The boundary: do not make doctor fail where it currently passes.

    Once ``init`` has run, every store-backed check opens the store exactly as
    before — the guard is on absence, not a permanent narrowing.
    """
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)

    result = runner.invoke(app, ["doctor"], catch_exceptions=False)

    assert "not created yet" not in result.output, result.output
    assert "context.db is readable" in result.output
