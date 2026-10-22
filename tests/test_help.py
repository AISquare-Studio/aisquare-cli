"""``--help`` succeeds for the app, every group and every command."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from tests.cli_tree import all_command_paths


def _paths() -> list[object]:
    """Every command path; a ``captain`` command's own help needs the captain on.

    With it off (the captain-OFF run), ``aisquare captain <verb> --help`` is refused in
    the one line like the verb itself, as it ships; ``aisquare captain --help`` still
    prints the group's usage.
    """
    return [
        pytest.param(path, marks=pytest.mark.needs_captain)
        if path[:1] == ("captain",) and len(path) > 1
        else path
        for path in all_command_paths()
    ]


@pytest.mark.parametrize("path", _paths(), ids=lambda p: " ".join(p) or "(root)")
def test_help_succeeds(runner: CliRunner, path: tuple[str, ...]) -> None:
    result = runner.invoke(app, [*path, "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage" in result.output
