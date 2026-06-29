"""``--json`` switches stub output to a machine-readable error object."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from aisquare.cli.app import app


def test_json_error_shape(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--json", "sync"])
    assert result.exit_code == 70  # pinned: the documented not-implemented exit code
    payload = json.loads(result.stdout)
    assert payload == {"error": "not_implemented", "command": "sync"}


def test_json_top_level_command(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--json", "open"])
    assert result.exit_code == 70
    assert json.loads(result.stdout) == {"error": "not_implemented", "command": "open"}
