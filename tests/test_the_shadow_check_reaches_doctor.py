"""``root_package_shadowed()`` is wired to doctor, and that is now pinned.

THIS FILE EXISTS BECAUSE A TASK SAID THE OPPOSITE. tsk_01m07k2v — "Delete
root_package_shadowed(): a safety net wired to nothing" — states that the
function is "DEFINED AND CALLED BY NOTHING (grep finds no caller)" and "never
reached doctor". Every clause is false, and was false when the task was filed:

* ``sdk_presence()`` calls it and stores the result as ``SdkPresence.shadowing``
* ``_check_sdk()`` reads ``presence.shadowing`` and returns a **warn** with a
  symptom and a remediation line
* that check is in ``doctor``'s results list, and the row shows up as
  ``explainability sdk`` once tracing is configured — which is exactly the
  state an operator is in after §4 of the cutover runbook

The wiring landed in a5c8987 at 05:52 UTC on 2026-08-17. The task was filed at
10:08 UTC the same day, four hours later. So this was not a claim overtaken by
events; it was wrong when written.

HOW A CAREFUL PERSON GETS THIS WRONG, because the answer is not "they were
careless": THE ONLY CALLER IS FOUR LINES ABOVE THE DEFINITION, IN THE SAME
FILE. A search for importers — the natural way to ask "who uses this?" across a
package — finds nothing, and "no cross-module caller" is TRUE. The claim it was
used to support, "called by nothing", is false. That is this board's recurring
shape: a measurement whose scope is narrower than the sentence it is quoted for.

WHAT THIS PINS, AND WHY IT IS NOT CIRCULAR. The predicate is exercised for
real — the attribute it reads is removed, rather than the function being
replaced with ``lambda: True``. Patching the function would prove only that
``_check_sdk`` branches on a boolean, which nobody doubted; removing
``__version__`` proves the actual condition, the dataclass, the check and the
CLI's JSON surface are all still connected.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.services import explainability_ops as ops

_ROW = "explainability sdk"

#: Configured the way §1/§2 of the cutover runbook configures a machine, and by
#: running the real commands: the `explainability sdk` row does not appear at
#: all until tracing is configured, so an unconfigured home would let every
#: assertion below pass over a doctor that never ran this check.
_SETUP: tuple[tuple[str, ...], ...] = (
    ("init",),
    (
        "explainability",
        "enable",
        "--target",
        "tst",
        "--gateway-url",
        "https://gw.invalid",
        "--key-env",
        "TST_KEY_UNSET",
    ),
)


@pytest.fixture
def configured_home(isolated_home: Path, runner: CliRunner) -> Path:
    for argv in _SETUP:
        result = runner.invoke(app, list(argv))
        assert result.exit_code == 0, f"setup `aisquare {' '.join(argv)}` failed: {result.output}"
    return isolated_home


def _rows(runner: CliRunner) -> dict[str, dict[str, Any]]:
    """Every doctor row by name.

    The exit code is deliberately not asserted: a configured home with no key
    exported and no proxy running has two legitimate ``fail`` rows, so doctor
    exits 1 and that is the correct behaviour. Asserting 0 here would pin an
    unrelated property and fail for reasons that have nothing to do with the
    shadow check. What IS asserted is that the payload parses and is
    non-empty — a doctor that died would satisfy neither.
    """
    result = runner.invoke(app, ["--json", "doctor"], catch_exceptions=False)
    payload = json.loads(result.stdout)
    assert payload, "doctor produced no checks at all"
    return {row["name"]: row for row in payload}


def _shadow_the_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the REAL predicate true by removing the attribute it reads.

    ``root_package_shadowed`` imports ``aisquare`` and asks whether
    ``__version__`` is gone — that is precisely what an SDK install of the same
    package name does to us. ``monkeypatch`` puts it back at teardown.
    """
    import aisquare

    monkeypatch.delattr(aisquare, "__version__")


def test_the_predicate_discriminates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both directions, or the tests below cannot mean anything.

    A predicate stuck on one value would make every other assertion here pass
    for a reason unrelated to the wiring.
    """
    assert ops.root_package_shadowed() is False, "our own __version__ is missing"

    _shadow_the_root(monkeypatch)

    assert ops.root_package_shadowed() is True


def test_the_value_reaches_the_presence_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first hop the task said did not exist."""
    assert ops.sdk_presence().shadowing is False

    _shadow_the_root(monkeypatch)

    assert ops.sdk_presence().shadowing is True


def test_the_row_is_clean_on_a_healthy_install(
    configured_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control. Without it, a doctor that always warned would pass below.

    "Healthy" has to be STATED, not inherited from the interpreter. This test
    used to assert ``ok`` against whatever the ambient environment happened to
    hold, and it passed for a reason nobody chose: an earlier test in the run
    — ``test_doctor_does_not_create_state``, via ``doctor --fix --yes`` — really
    pip-installed the SDK into this interpreter, so by the time the letter T was
    collected the row genuinely read ``ok``. Alphabetical order was load-bearing.
    With that install refused (it shadowed the CLI and broke fifteen later
    tests), a clean environment has no SDK and the row correctly warns, so the
    control has to supply the presence it is a control for.
    """
    monkeypatch.setattr(
        ops,
        "sdk_presence",
        lambda: ops.SdkPresence(
            importable=True, script="explainability-doctor", version="1.1.0", shadowing=False
        ),
    )

    row = _rows(runner).get(_ROW)

    assert row is not None, f"`{_ROW}` is not a doctor check any more"
    assert row["status"] == "ok", row


def test_shadowing_reaches_doctor_with_a_symptom_and_a_fix(
    configured_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: the predicate, the record, the check, and the CLI surface.

    This is the assertion the task's acceptance criteria already describe —
    "wired into doctor with a stated symptom and a remediation line" — which is
    why the correct outcome of that task was a premise correction rather than a
    deletion.
    """
    _shadow_the_root(monkeypatch)

    row = _rows(runner)[_ROW]

    assert row["status"] == "warn", row
    assert "not aisquare-cli's" in row["detail"], row["detail"]
    assert row["fix"], "a warn with no remediation is a dead end for the operator"
    assert "pip install" in row["fix"], row["fix"]


def test_the_warning_says_who_it_matters_to(
    configured_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The task's real question, answered in the message rather than by deleting.

    "Name a symptom it would catch that MATTERS to someone." The message
    already does: it says the collision is harmless for the CLI, which reads
    its version from dist metadata, and that the person it matters to is
    whoever uses the SDK's own facade. A warning that did not say this would
    deserve deleting, because an operator cannot act on it.
    """
    _shadow_the_root(monkeypatch)

    row = _rows(runner)[_ROW]

    assert "Harmless for the CLI" in row["fix"], row["fix"]
    assert "facade" in row["fix"], row["fix"]
