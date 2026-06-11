"""``--version`` prints the installed package version."""

from __future__ import annotations

from typer.testing import CliRunner

from aisquare import __version__
from aisquare.cli.app import app


def test_version_flag(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert f"aisquare {__version__}" in result.output


def test_version_short_flag(runner: CliRunner) -> None:
    result = runner.invoke(app, ["-V"])
    assert result.exit_code == 0
    assert __version__ in result.output
