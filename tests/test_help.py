"""``--help`` succeeds for the app, every group and every command."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from tests.cli_tree import all_command_paths


@pytest.mark.parametrize("path", all_command_paths(), ids=lambda p: " ".join(p) or "(root)")
def test_help_succeeds(runner: CliRunner, path: tuple[str, ...]) -> None:
    result = runner.invoke(app, [*path, "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage" in result.output
