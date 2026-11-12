"""typer stays below 0.29, so a fresh install gets a typer the CLI can build its flags on.

typer 0.29.0 (PyPI, 2026-11-09) vendors click and gave ``TyperOption.__init__`` a required
keyword-only ``type_descriptor``. ``cli/global_flags.py`` builds the five shared flags as
``TyperOption(...)`` by hand, so ``GlobalFlagsGroup`` raised while the command tree was
built: every command of every fresh 0.9.2 install failed, ``aisquare --version``
included. pyproject asked ``typer>=0.26.8`` with no ceiling, and pip, ``uv tool install``,
install.sh and CI all resolved 0.29.0. Only ``uv.lock`` held 0.27.3.

The cap is what a wheel's metadata carries, so it is what every installer reads. These
tests read it from pyproject, as written, so dropping it is a red test even while the
environment still has an older typer. They also check the typer that runs: under a
typer the cap excludes, the suite's own CLI tests fail as well (test_global_flags
builds the tree), and this says why.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import version
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

#: The first typer the CLI cannot start on.
BROKEN = Version("0.29.0")


def _typer() -> Requirement:
    dependencies = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]
    found = [Requirement(line) for line in dependencies if Requirement(line).name == "typer"]
    assert len(found) == 1, f"pyproject names typer {len(found)} times: {found}"
    return found[0]


def test_pyproject_caps_typer_below_0_29() -> None:
    specifier = _typer().specifier
    ceilings = [Version(spec.version) for spec in specifier if spec.operator == "<"]
    assert ceilings and min(ceilings) <= BROKEN, (
        f"typer{specifier} has no ceiling at or below {BROKEN}: typer 0.29.0 breaks every "
        "command (TyperOption's type_descriptor), so pyproject must say typer<0.29"
    )
    for later in ("0.29.0", "0.29.1", "0.30.0", "1.0.0"):
        assert not specifier.contains(later, prereleases=True), f"typer{specifier} admits {later}"
    # The release the fix was measured on, and the one uv.lock holds, stay in range.
    for works in ("0.28.0", "0.27.3"):
        assert specifier.contains(works), f"typer{specifier} excludes {works}, which works"


def test_the_typer_that_runs_is_in_range() -> None:
    installed = version("typer")
    assert _typer().specifier.contains(installed, prereleases=True), (
        f"typer {installed} is installed, outside pyproject's typer{_typer().specifier}: "
        "reinstall with `uv pip install -c ci/constraints.txt -e '.[dev]'`"
    )
