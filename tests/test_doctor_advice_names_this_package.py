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


def test_no_doctor_check_tells_a_user_to_install_the_sdk_instead_of_this_cli(
    isolated_home: Path,
) -> None:
    """The guard: run the real doctor, sweep every detail and fix it produced.

    Through ``doctor()`` rather than the private check functions, because the
    string has to be wrong *where a user reads it*, and because a row assembled
    from a constant elsewhere in the tree is exactly how the fourth instance
    hid from the first three.
    """
    offenders: list[str] = []
    for check in diagnostics.doctor():
        for field, text in (("detail", check.detail), ("fix", check.fix or "")):
            for verb, package in _offenders(text):
                offenders.append(
                    f"{check.name}.{field}: `{verb} {package}` — should be {DISTRIBUTION}"
                )
    assert not offenders, (
        "a doctor remediation names the Explainability SDK (`aisquare`) where it "
        f"means this CLI (`{DISTRIBUTION}`). That command installs a different "
        "distribution, which shares our top-level package directory:\n  " + "\n  ".join(offenders)
    )


def test_the_sweep_actually_read_some_install_commands(isolated_home: Path) -> None:
    """A sweep that found nothing to judge is a green light for no reason.

    The control for the test above. If `doctor()` ever stops emitting install
    advice — or this environment stops reaching the checks that carry it — that
    test passes vacuously, which is the failure mode the sweep exists to avoid.
    """
    seen = [
        command
        for check in diagnostics.doctor()
        for command in _install_commands(f"{check.detail} {check.fix or ''}")
    ]
    assert seen, "no doctor check produced an install command, so the guard judged nothing"


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
