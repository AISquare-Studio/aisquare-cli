"""The base install must not gain the Office serving stack.

Every hook and every ordinary command runs in a plain ``pip install
aisquare-cli``. The Office server needs Starlette, uvicorn, websockets and an
HTTP client; the Office *foundation* — models, config, ports — needs none of
them, and that is what lets P02 through P14 import the typed seam in an
environment that never installed the extra.

The checks run in subprocesses with those four names made unimportable. That is
the only form of the assertion that stays honest: a developer environment has
them installed (``[serve]`` pulls starlette and uvicorn in transitively), so
merely observing that an import succeeded would prove nothing at all.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

OFFICE_EXTRA_MODULES = ("starlette", "uvicorn", "websockets", "httpx2", "httpx")
"""What an Office server needs and the base CLI must never require."""

_BLOCKER = f"""
import sys

BLOCKED = {OFFICE_EXTRA_MODULES!r}


class _Refuse:
    \"\"\"Make the Office serving stack unimportable, as a base install has it.\"\"\"

    def find_spec(self, name, path=None, target=None):
        root = name.split(".")[0]
        if root in BLOCKED:
            raise ImportError(f"{{root}} is blocked: the base install has no Office extra")
        return None


sys.meta_path.insert(0, _Refuse())
for _name in list(sys.modules):
    if _name.split(".")[0] in BLOCKED:
        del sys.modules[_name]
"""


def _run_without_office_extras(body: str) -> subprocess.CompletedProcess[str]:
    """Run ``body`` in a fresh interpreter where the extra cannot be imported."""
    return subprocess.run(
        [sys.executable, "-c", _BLOCKER + body],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT,
    )


def test_the_base_cli_imports_without_the_office_extra() -> None:
    """``aisquare`` and its command tree must not need a serving dependency."""
    result = _run_without_office_extras(
        "import aisquare\nimport aisquare.cli.app\nimport aisquare.core.paths\nprint('ok')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_the_base_cli_runs_without_the_office_extra() -> None:
    """Not just importable: ``aisquare --version`` has to actually answer.

    An import check alone would pass a CLI whose every command died on first
    use, which is precisely the shape the hook path cannot afford.
    """
    result = _run_without_office_extras(
        "from typer.testing import CliRunner\n"
        "from aisquare.cli.app import app\n"
        "outcome = CliRunner().invoke(app, ['--version'])\n"
        "assert outcome.exit_code == 0, outcome.output\n"
        "print('version-ok')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "version-ok" in result.stdout


def test_the_office_foundation_imports_without_the_office_extra() -> None:
    """The typed seam is importable in a base install, so P02-P14 can build on it."""
    result = _run_without_office_extras(
        "import aisquare.office\n"
        "from aisquare.office import config, models, ports\n"
        "cfg = config.OfficeConfig(home=__import__('pathlib').Path('/tmp/home'))\n"
        "assert cfg.port == config.DEFAULT_PORT\n"
        "assert models.CONTRACT_REVISION == '1.4'\n"
        "assert ports.Clock is not None\n"
        "print('foundation-ok')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "foundation-ok" in result.stdout


def test_importing_the_foundation_pulls_in_no_serving_module() -> None:
    """Even where the extra IS installed, importing the seam must not load it.

    The blocked-import tests prove the foundation can live without the stack;
    this one proves it does not quietly drag it in on a machine that has it,
    which is how a base-install regression hides during development.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import aisquare.cli.app\n"
            "from aisquare.office import config, models, ports\n"
            f"blocked = {OFFICE_EXTRA_MODULES!r}\n"
            "loaded = sorted(n for n in sys.modules if n.split('.')[0] in blocked)\n"
            "print(':'.join(loaded))\n",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", f"the foundation imported: {result.stdout.strip()}"


def test_the_office_extra_is_declared_and_the_base_dependencies_are_untouched() -> None:
    """The serving stack is an extra, not a base dependency."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = data["project"]["optional-dependencies"]
    assert "office" in extras, "the office extra must exist for `pip install aisquare-cli[office]`"

    declared = {_distribution(spec) for spec in extras["office"]}
    assert declared == {"starlette", "uvicorn", "websockets", "httpx2"}

    base = {_distribution(spec) for spec in data["project"]["dependencies"]}
    assert base == {"typer", "rich", "pydantic", "tomli-w", "textual"}, (
        "the Office packet must not change what a plain `pip install aisquare-cli` pulls in"
    )


def _distribution(spec: str) -> str:
    """``"starlette>=0.48,<2"`` -> ``"starlette"``."""
    import re

    return re.split(r"[<>=!~\[; ]", spec)[0].strip()
