"""typer 0.29.0 (PyPI, 2026-11-09) is a rewrite that every command of a fresh install died on,
``--version`` included, before it read its arguments:

- ``cli.common.AgentPath`` subclasses ``typer.models.TyperPath``, which 0.29.0 no longer has,
  so ``aisquare.cli.common`` failed to import;
- ``cli.global_flags`` builds the five shared flags as ``TyperOption(...)`` by hand, and
  0.29.0's wants a ``type_descriptor`` none of them pass (0.9.2 broke on this one alone).

pyproject caps typer below 0.29 until both are ported to the new typer. These pin the cap,
and what it keeps working: dropping the cap without the port lets an install (CI's
included) resolve 0.29, and the second test fails there."""

from __future__ import annotations

import re
import tomllib
from importlib.metadata import version
from pathlib import Path

from typer.testing import CliRunner

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _release(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def test_pyproject_caps_typer_below_0_29() -> None:
    dependencies = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]
    typer = [spec for spec in dependencies if re.match(r"typer\s*[<>=!~]", spec)]
    assert typer == ["typer>=0.26.8,<0.29"]


def test_every_command_builds_on_the_typer_installed(runner: CliRunner) -> None:
    assert _release(version("typer")) < (0, 29), version("typer")
    from typer.models import TyperPath

    from aisquare.cli import common, global_flags
    from aisquare.cli.app import app

    assert issubclass(common.AgentPath, TyperPath)
    assert [option.opts[0] for option in global_flags._shared_options()] == [
        "--verbose",
        "--quiet",
        "--json",
        "--profile",
        "--no-color",
    ]
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
