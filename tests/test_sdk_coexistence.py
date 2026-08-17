"""The explainability SDK and this CLI share one ``aisquare/`` directory.

The SDK is distributed as ``aisquare`` and installs a REGULAR package —
``aisquare/__init__.py`` — into the same directory this package occupies. pip's
RECORD for the two distributions overlaps on exactly that one file, so whichever
installs last wins it, silently, with no warning:

    pip install aisquare-cli && pip install 'aisquare[explainability]'
    aisquare --version
    ImportError: cannot import name '__version__' from 'aisquare'

Every ``aisquare`` command dies at import time. The subpackages never collide
(``aisquare/cli`` and ``aisquare/explainability`` are different directories), and
a *missing* ``__init__.py`` merely makes ``aisquare`` a PEP 420 namespace package
in which ``aisquare.cli`` still imports fine. So the whole failure reduces to one
thing: reading a name OUT of the top-level ``__init__``.

These tests pin that reduction. If they fail, ``aisquare-cli[explainability]``
has become a self-destruct button again.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
PACKAGE = SRC / "aisquare"

#: Reproduces the installed shape where the SDK's ``__init__.py`` won: the
#: top-level module exists and its ``__path__`` still finds our subpackages, but
#: it carries none of OUR names. Identical in shape to the post-``pip uninstall
#: aisquare`` case, where no ``__init__.py`` survives at all and the interpreter
#: synthesises a namespace package with exactly these attributes.
_FOREIGN_TOP_LEVEL = """
import sys, types
pkg = types.ModuleType("aisquare")
pkg.__path__ = [{package!r}]
sys.modules["aisquare"] = pkg
{body}
"""


def _run_under_foreign_top_level(body: str) -> subprocess.CompletedProcess[str]:
    """Import our modules with a top-level ``aisquare`` we do not own."""
    return subprocess.run(
        [sys.executable, "-c", _FOREIGN_TOP_LEVEL.format(package=str(PACKAGE), body=body)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_starts_when_the_sdk_owns_the_top_level_package() -> None:
    """``aisquare --version`` must survive the SDK owning ``aisquare/__init__``."""
    result = _run_under_foreign_top_level(
        "from typer.testing import CliRunner\n"
        "from aisquare.cli.app import app\n"
        "out = CliRunner().invoke(app, ['--version'])\n"
        "assert out.exit_code == 0, out.output\n"
        "print(out.output.strip())\n"
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert result.stdout.strip().startswith("aisquare ")


def test_version_resolves_without_the_top_level_package() -> None:
    """The version comes from distribution metadata, not from our ``__init__``."""
    result = _run_under_foreign_top_level(
        "from aisquare.core.version import __version__\nprint(__version__)\n"
    )
    assert result.returncode == 0, f"stderr={result.stderr}"
    assert result.stdout.strip()


def _package_modules() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if p.name != "__init__.py")


def test_the_module_sweep_actually_finds_modules() -> None:
    """Guard the guard, and here the vacuous case is a SKIP rather than a pass.

    The sweep below is a `parametrize` over `_package_modules()`. If that list is
    ever empty — `PACKAGE` renamed, the layout moved, a glob typo — pytest does
    not fail: it emits ONE SKIPPED test for the empty parameter set and exits 0.
    Measured: 60 cases become `1 skipped`, and one skip inside a thousand-test
    suite is invisible in a summary everyone reads as a pass count.

    That matters more here than for most guards, because the parametrized sweep
    below is the thing actually holding the SDK-collision invariant. Losing it
    silently would leave `aisquare-cli[explainability]` a self-destruct button
    with a green suite over it.

    Landmarks as well as a floor: a count alone passes if the glob is pointed at
    the wrong tree and happens to find enough files there.
    """
    modules = _package_modules()

    assert len(modules) >= 40, (
        f"the module sweep found only {len(modules)} files under {PACKAGE} — it is "
        "the parameter source for the collision guard below, and an empty or "
        "truncated list makes that guard skip rather than fail"
    )
    found = {path.relative_to(PACKAGE).as_posix() for path in modules}
    for landmark in ("cli/app.py", "core/version.py"):
        assert landmark in found, (
            f"{landmark} is missing from the sweep, so it is not walking this "
            f"package — {PACKAGE} may have moved"
        )


def _top_level_reads(tree: ast.AST) -> list[int]:
    """Lines importing a NAME out of the top-level ``aisquare`` package."""
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "aisquare" and node.level == 0
    ]


def test_the_import_matcher_recognises_the_fatal_shape() -> None:
    """Positive control: the yield floor above cannot catch a blind PREDICATE.

    `test_the_module_sweep_actually_finds_modules` proves the parametrize source
    is populated — "walked nothing" is covered. It says nothing about whether the
    matcher still recognises anything, and a matcher that recognises nothing
    produces an empty offender list exactly like a clean package does. Measured
    on the sibling guard in test_console_markup: blinding the predicate while
    leaving the walk intact left every module visited and the suite green.

    The fatal shape and the safe shapes are both asserted, because the whole
    value of this guard is telling them apart: `from aisquare import X` dies when
    the SDK owns that ``__init__``, while submodule imports resolve through
    ``__path__`` and are fine.
    """
    fatal = ast.parse("from aisquare import __version__\n")
    submodule = ast.parse("from aisquare.core.version import __version__\n")
    relative = ast.parse("from . import version\n")
    plain = ast.parse("import aisquare\n")

    assert _top_level_reads(fatal) == [1], "the matcher no longer sees `from aisquare import X`"
    assert _top_level_reads(submodule) == [], "submodule imports must not be flagged"
    assert _top_level_reads(relative) == [], "a relative import is level>0, not the fatal shape"
    assert _top_level_reads(plain) == [], "`import aisquare` binds no name out of __init__"


@pytest.mark.parametrize("module", _package_modules(), ids=lambda p: str(p.name))
def test_no_module_reads_a_name_out_of_the_top_level_package(module: Path) -> None:
    """``from aisquare import X`` is the one import shape the pairing cannot survive.

    Submodule imports (``from aisquare.core import paths``) are fine — those
    resolve through ``__path__``, which every candidate top-level provides.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    offenders = _top_level_reads(tree)
    assert not offenders, (
        f"{module.relative_to(SRC)} reads a name out of the top-level 'aisquare' package "
        f"at line(s) {offenders}. The explainability SDK overwrites that __init__.py, so "
        "this import raises ImportError and every aisquare command dies. Import from a "
        "submodule instead (e.g. aisquare.core.version)."
    )
