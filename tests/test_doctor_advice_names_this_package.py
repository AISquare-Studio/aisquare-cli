"""Every install command the doctor prints must name a package that is us.

THE DEFECT THIS PINS. ``aisquare`` on PyPI is the *Explainability SDK* — a
different distribution (1.2.0, "Explainability SDK for tracing, graphing, and
policy auditing of AI agents"). This CLI is ``aisquare-cli``. Three of the
doctor's remediations named the SDK:

- ``install`` (both branches): ``pipx install aisquare``
- ``tiktoken``: ``pipx inject aisquare tiktoken``
- ``explainability sdk``: ``pip install "aisquare[explainability]"``

So the checks whose entire job is "this machine is not set up properly" answered
it with commands that install somebody else's package, or — for ``pipx inject``
— name a pipx environment that does not exist on any machine that followed the
documented install.

WHY IT IS WORSE THAN A TYPO, and why the guard is class-level rather than three
assertions on three strings. The SDK ships its own ``aisquare/__init__.py`` into
the directory this package occupies, and pip's RECORD for the two overlaps on
exactly that file, so the last writer wins it silently. ``pyproject.toml``'s
``explainability`` extra carries twelve lines about this, ending in the rule
that our advice must name the CLI and "never a bare ``pip install
aisquare[explainability]``". ``tests/test_insight_sweeper.py`` already asserts
that string never reaches an insight's ``reason``. Both existed while the
doctor printed it. A per-string test would have been satisfied by fixing the
three known instances; this one fails on the fourth.

THE ONE DELIBERATE EXCEPTION is recorded rather than special-cased away:
``_check_sdk``'s *shadowing* row says ``pip install --force-reinstall
aisquare``, and it means the SDK. That row is about repairing the SDK's own
package root after it lost the shared ``__init__.py`` — the subject of the
sentence is the SDK, so naming it is correct. ``--force-reinstall`` is the
discriminator, and it is a discriminator rather than an allowlist entry: any
*other* command that names the bare distribution is a finding.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aisquare.core.version import DISTRIBUTION
from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import diagnostics

#: An install-ish command and the first package-looking word after it. Written
#: to match the forms this project's own advice uses — ``pip install``,
#: ``pipx install``, ``pipx inject``, ``uv tool install`` — with any flags
#: between the verb and the package skipped, since ``pip install --upgrade X``
#: and ``uv tool install --with tiktoken X`` are both shapes we print.
_INSTALL = re.compile(
    r"""
    (?P<verb>pipx\s+install|pipx\s+inject|pip\s+install|uv\s+tool\s+install)
    (?P<flags>(?:\s+-{1,2}[\w-]+(?:[=\s]+[^\s"']+)?)*)
    \s+
    (?P<quote>["']?)
    (?P<package>[A-Za-z0-9_.\[\]-]+)
    """,
    re.VERBOSE,
)

#: The bare SDK distribution, with or without an extra: `aisquare`,
#: `aisquare[explainability]`. NOT `aisquare-cli` or `aisquare-cli[...]`.
_BARE_SDK = re.compile(r"^aisquare(?:\[[^\]]*\])?$")


def _install_commands(text: str) -> list[tuple[str, str]]:
    """``(verb, package)`` for every install command in ``text``."""
    return [
        (" ".join(match.group("verb").split()), match.group("package"))
        for match in _INSTALL.finditer(text)
    ]


def test_the_extractor_sees_the_shapes_this_project_prints() -> None:
    """The instrument, before it is trusted — a silent extractor proves nothing.

    Every line here is a real shape from this tree (or the pre-fix version of
    one), so a regex that stopped matching would fail here rather than passing
    the guard below by finding nothing.
    """
    assert _install_commands("Install as a global tool: pipx install aisquare-cli") == [
        ("pipx install", "aisquare-cli")
    ]
    assert _install_commands("pipx inject aisquare tiktoken") == [("pipx inject", "aisquare")]
    assert _install_commands('pip install --upgrade "aisquare-cli[explainability]"') == [
        ("pip install", "aisquare-cli[explainability]")
    ]
    assert _install_commands("uv tool install --with tiktoken aisquare-cli") == [
        ("uv tool install", "aisquare-cli")
    ]
    assert _install_commands("pip install --force-reinstall aisquare") == [
        ("pip install", "aisquare")
    ]
    assert _install_commands("npm install -g repomix") == []


def test_the_sdk_matcher_tells_the_two_distributions_apart() -> None:
    """`aisquare-cli` starts with `aisquare`, which is the whole trap."""
    assert _BARE_SDK.match("aisquare")
    assert _BARE_SDK.match("aisquare[explainability]")
    assert not _BARE_SDK.match("aisquare-cli")
    assert not _BARE_SDK.match("aisquare-cli[explainability]")
    assert not _BARE_SDK.match("aisquare-cli[serve]")


def _offenders(text: str) -> list[tuple[str, str]]:
    """Install commands in ``text`` that name the SDK where they should name us.

    ``--force-reinstall`` is exempt: see the module docstring. It is matched on
    the flag rather than on the check name, so the exemption travels with the
    command it is about instead of blessing a whole row.
    """
    found: list[tuple[str, str]] = []
    for match in _INSTALL.finditer(text):
        if not _BARE_SDK.match(match.group("package")):
            continue
        if "--force-reinstall" in match.group("flags"):
            continue
        found.append((" ".join(match.group("verb").split()), match.group("package")))
    return found


def test_the_offender_detector_finds_the_bug_that_shipped() -> None:
    """The pre-fix strings, verbatim, must be findings — else the sweep is blind."""
    assert _offenders("Install as a global tool: pipx install aisquare") == [
        ("pipx install", "aisquare")
    ]
    assert _offenders("Install it: pip install tiktoken (or: pipx inject aisquare tiktoken)") == [
        ("pipx inject", "aisquare")
    ]
    assert _offenders('Install it: pip install "aisquare[explainability]"') == [
        ("pip install", "aisquare[explainability]")
    ]
    # ...and the deliberate one is not.
    assert _offenders("repair the root: pip install --force-reinstall aisquare") == []
    # ...nor is the corrected advice.
    assert _offenders(f"pipx install {DISTRIBUTION} (or: uv tool install {DISTRIBUTION})") == []


def _force_the_rows_that_carry_install_advice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the sweep deterministic instead of a reading of this machine.

    ONLY FOUR ROWS in ``doctor()`` can emit an install command: ``install``'s
    two warn branches, ``tiktoken``'s, and the SDK's not-present branch. Every
    one of them is SILENT on a well-provisioned machine — aisquare on PATH
    outside a venv (exactly what ``_GLOBAL_INSTALL`` recommends), tiktoken
    importable, the SDK installed. So a sweep that simply ran ``doctor()`` was
    strongest on a broken laptop and vacuous on a correct one: it judged nothing
    and passed, and the control below would have gone red on a *healthy* machine
    rather than a wrong one. Both are wrong for the same reason, so both use
    this.

    Everything else in ``doctor()`` advertises ``apt``/``dnf``/``brew``,
    ``npm install -g`` or ``aisquare …``, which the extractor does not match.
    """
    monkeypatch.setattr(diagnostics, "_has_module", lambda _name: False)
    monkeypatch.setattr("aisquare.services.diagnostics.shutil.which", lambda _name: None)


def _sweep(checks: list[DoctorCheck]) -> list[str]:
    """Every offending install command in ``checks``, named by where it came from."""
    return [
        f"{check.name}.{field}: `{verb} {package}` — should be {DISTRIBUTION}"
        for check in checks
        for field, text in (("detail", check.detail), ("fix", check.fix or ""))
        for verb, package in _offenders(text)
    ]


def test_no_doctor_check_tells_a_user_to_install_the_sdk_instead_of_this_cli(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard: run the real doctor, sweep every detail and fix it produced.

    Through ``doctor()`` rather than the private check functions, because the
    string has to be wrong *where a user reads it*, and because a row assembled
    from a constant elsewhere in the tree is exactly how the fourth instance hid
    from the first three.
    """
    _force_the_rows_that_carry_install_advice(monkeypatch)
    offenders = _sweep(diagnostics.doctor())
    assert not offenders, (
        "a doctor remediation names the Explainability SDK (`aisquare`) where it "
        f"means this CLI (`{DISTRIBUTION}`). That command installs a different "
        "distribution, which shares our top-level package directory:\n  " + "\n  ".join(offenders)
    )


def test_the_sweep_actually_read_some_install_commands(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep that found nothing to judge is a green light for no reason.

    The control for the test above, and it is only meaningful now that both
    force the same state: as a reading of the ambient machine it would have
    failed on a correctly-provisioned one, which is the opposite of a control.
    """
    _force_the_rows_that_carry_install_advice(monkeypatch)
    seen = [
        command
        for check in diagnostics.doctor()
        for command in _install_commands(f"{check.detail} {check.fix or ''}")
    ]
    assert seen, "no doctor check produced an install command, so the guard judged nothing"


def test_the_explainability_sdk_rows_are_swept_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rows ``doctor()`` alone never reaches — and where the fourth bug was.

    ``explainability_ops.checks`` returns the switch ALONE until the feature has
    been touched, so on a fresh home the SDK row does not exist and the sweep
    above cannot see it. That is precisely the row whose stale constant said
    ``pip install "aisquare[explainability]"``, so leaving it to an untouched
    home would have been a guard that missed the bug it was written for.

    Both reachable states are swept: enabled (a ``warn`` carrying a ``fix``) and
    merely configured (an ``ok`` carrying the advice in its ``detail``).
    """
    from aisquare.core.config import ExplainabilitySettings
    from aisquare.services import explainability_ops as ops

    monkeypatch.setattr(
        ops,
        "sdk_presence",
        lambda: ops.SdkPresence(importable=False, script=None, version=None, shadowing=False),
    )
    rows = [
        *ops.checks(ExplainabilitySettings(enabled=True), env={}),
        *ops.checks(ExplainabilitySettings(), live=True, env={}),
    ]
    sdk_rows = [row for row in rows if row.name == "explainability sdk"]
    assert sdk_rows, "the SDK rows were not reached, so this guard judged nothing"
    assert not _sweep(sdk_rows), "\n  ".join(_sweep(sdk_rows))


def test_the_executed_sdk_install_goes_through_our_own_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The COMMAND RUN, not just the command printed.

    ``install_sdk`` shelled out to ``pip install aisquare[explainability]``
    while every printed hint was being corrected away from exactly that form, so
    the two halves of one code path disagreed: the row told the operator the safe
    command and this ran the other one for them.

    The floor is the checkable difference, and the reason this is a defect rather
    than a style point: our extra pins ``aisquare[explainability]>=1.1``, which
    is where ``AgentRunTracer`` accepts ``run_id``. The bare form carries no
    floor, so it can resolve an SDK too old for the lane the install exists to
    enable — and succeed while doing it.
    """
    from aisquare.services import explainability_ops as ops

    seen: list[list[str]] = []

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def capture(argv: list[str], **_kwargs: object) -> _Completed:
        seen.append(argv)
        return _Completed()

    monkeypatch.setattr("aisquare.services.explainability_ops.subprocess.run", capture)
    ok, detail = ops.install_sdk()

    assert ok, detail
    assert len(seen) == 1, seen
    requirement = seen[0][-1]
    assert requirement == f"{DISTRIBUTION}[explainability]", requirement
    assert requirement != "aisquare[explainability]"
    assert seen[0][1:4] == ["-m", "pip", "install"], seen[0]
    # Not --upgrade: consent was for installing the SDK, not for upgrading the
    # CLI underneath a running process.
    assert "--upgrade" not in seen[0], seen[0]


def test_install_hint_names_this_distribution_on_both_of_its_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`install`'s two warnings — absent from PATH, and inside a virtualenv."""
    monkeypatch.setattr("aisquare.services.diagnostics.shutil.which", lambda _name: None)
    absent = diagnostics._check_install()
    assert absent.status is CheckStatus.warn
    assert f"pipx install {DISTRIBUTION}" in (absent.fix or "")

    monkeypatch.setattr(
        "aisquare.services.diagnostics.shutil.which",
        lambda _name: "/home/u/proj/.venv/bin/aisquare",
    )
    in_venv = diagnostics._check_install()
    assert in_venv.status is CheckStatus.warn
    assert f"pipx install {DISTRIBUTION}" in (in_venv.fix or "")
    # The uv form is offered too, because that is what the installer will use.
    assert f"uv tool install {DISTRIBUTION}" in (in_venv.fix or "")


def test_tiktoken_hint_names_the_pipx_environment_that_actually_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`pipx inject` takes an ENVIRONMENT name, and ours is the distribution."""
    monkeypatch.setattr(diagnostics, "_has_module", lambda _name: False)
    check = diagnostics._check_tiktoken()
    assert check.status is CheckStatus.warn
    assert f"pipx inject {DISTRIBUTION} tiktoken" in (check.fix or "")
    assert f"uv tool install --with tiktoken {DISTRIBUTION}" in (check.fix or "")


def test_a_check_with_no_fix_is_not_a_crash() -> None:
    """`fix` is None on every ok check; the sweep must tolerate that."""
    ok = DoctorCheck(name="x", status=CheckStatus.ok, detail="fine")
    assert ok.fix is None
    assert _offenders(ok.fix or "") == []
