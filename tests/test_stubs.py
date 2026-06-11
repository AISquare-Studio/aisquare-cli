"""Every leaf command is wired to the shared stub: exit 70, consistent message.

``result.output`` is the mixed stdout+stderr stream, so the stub's stderr
message is visible there.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.stubs import EXIT_NOT_IMPLEMENTED
from tests.cli_tree import leaf_invocations


@pytest.mark.parametrize("argv", leaf_invocations(), ids=lambda a: " ".join(a))
def test_leaf_commands_are_stubbed(runner: CliRunner, argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == EXIT_NOT_IMPLEMENTED, result.output
    assert "is not implemented yet" in result.output


def test_stub_message_goes_to_stderr(runner: CliRunner) -> None:
    result = runner.invoke(app, ["remember", "always use uv"])
    assert result.exit_code == EXIT_NOT_IMPLEMENTED
    assert "⚠ aisquare remember is not implemented yet (planned: v0)" in result.stderr
    assert result.stdout == ""


def test_alias_reports_canonical_command(runner: CliRunner) -> None:
    result = runner.invoke(app, ["ctx", "list"])
    assert result.exit_code == EXIT_NOT_IMPLEMENTED
    assert "aisquare context list is not implemented yet" in result.output


def test_workspace_alias_works(runner: CliRunner) -> None:
    result = runner.invoke(app, ["workspace", "info"])
    assert result.exit_code == EXIT_NOT_IMPLEMENTED
    assert "aisquare project info is not implemented yet" in result.output
