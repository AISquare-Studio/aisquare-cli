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

import pytest
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


def test_fix_is_the_one_that_may_build_the_home(runner: CliRunner) -> None:
    """The other half of the ruling, and the reason the plain guard is safe to keep.

    Declining to create state is right for a DIAGNOSTIC. It would be wrong for
    an opt-in repair — `--fix` exists to "repair what can be repaired", and a
    repair that refuses to create a missing home repairs nothing. The pair is
    the actual rule: looking does not create, asking to fix does.

    MORNING-HANDOFF.md listed "`doctor --fix` state creation is unmeasured"
    under what was deliberately left. This is that measurement.
    """
    home = paths.aisquare_home()
    assert not home.exists()

    runner.invoke(app, ["doctor", "--fix", "--yes"], catch_exceptions=False)

    assert home.exists(), (
        "--fix left the home missing — an opt-in repair that repairs nothing is "
        "worse than no flag, because the operator believes it tried"
    )


def test_fix_writes_nothing_outside_the_aisquare_home(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boundary that makes `--fix` safe to hand a stranger at 08:00.

    `doctor` reports on the Claude config directory too, and the obvious reading
    of "repair what can be repaired" would have it install the missing hooks.
    It does not: it prints the `agents connect` line and leaves the directory
    alone. That is the right call — hooks live in the operator's own agent
    config, outside anything this CLI owns, and a diagnostic command silently
    editing them is the surprise you cannot take back.

    Measured rather than assumed, because the check that reports the hooks is
    one line away from the code that could install them.
    """
    claude_config = tmp_path / "claude-config"
    claude_config.mkdir()
    # A sentinel, so the comparison below is not empty-against-empty: an rglob
    # that saw nothing would satisfy an assertion over two empty lists.
    (claude_config / "settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_config))
    before = sorted(path.name for path in claude_config.rglob("*"))
    assert before == ["settings.json"], "the sentinel is not visible to the check"

    result = runner.invoke(app, ["doctor", "--fix", "--yes"], catch_exceptions=False)

    assert sorted(path.name for path in claude_config.rglob("*")) == before, (
        "--fix wrote into the operator's agent config directory"
    )
    assert "agents connect claude-code" in result.output, (
        "the hook repair must still be OFFERED, or declining to do it silently "
        "leaves the operator with no way to find the fix"
    )


def test_fix_does_not_rewrite_a_configured_machine(runner: CliRunner) -> None:
    """`--fix` must not be a second way to lose a configured section.

    `init --reinit` was found this shift to discard a configured
    [explainability] section at exit 0 (@9bbc8ed7 made it refuse). `--fix` is
    the other command an operator reaches for when something looks wrong, and it
    runs at exactly the moment they can least afford to lose the targets table —
    a gateway URL and a key-variable name, both configured out of band.

    Measured on a configured home: config.toml is byte-identical afterwards.
    """
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    runner.invoke(
        app,
        [
            "explainability",
            "enable",
            "--target",
            "stg",
            "--gateway-url",
            "https://gw.example",
            "--key-env",
            "FOO_KEY",
        ],
        catch_exceptions=False,
    )
    config = paths.config_path()
    before = config.read_bytes()
    assert b"gw.example" in before, "the fixture did not configure anything to lose"

    runner.invoke(app, ["doctor", "--fix", "--yes"], catch_exceptions=False)

    assert config.read_bytes() == before, "--fix rewrote config.toml on a configured machine"
