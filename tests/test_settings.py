"""End-to-end CLI behaviour for the config group."""

from __future__ import annotations

import json
from typing import Any

from typer.testing import CliRunner

from aisquare.cli.app import app


def _json(output: str) -> Any:
    return json.loads(output)


def test_list_shows_defaults(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--json", "config", "list"])
    assert result.exit_code == 0, result.output
    assert _json(result.stdout)["default_pool"] == "project"


def test_get_value(runner: CliRunner) -> None:
    result = runner.invoke(app, ["config", "get", "default_pool"])
    assert result.exit_code == 0, result.output
    assert "default_pool = project" in result.stdout


def test_get_nested_value(runner: CliRunner) -> None:
    result = runner.invoke(app, ["config", "get", "redaction.level"])
    assert "redaction.level = standard" in result.stdout


def test_get_unknown_key_fails(runner: CliRunner) -> None:
    result = runner.invoke(app, ["config", "get", "nope"])
    assert result.exit_code == 1
    assert "unknown config key" in result.output


def test_set_value_persists(runner: CliRunner) -> None:
    written = runner.invoke(app, ["config", "set", "default_pool", "user"])
    assert written.exit_code == 0, written.output
    assert "default_pool = user" in written.stdout
    read_back = runner.invoke(app, ["config", "get", "default_pool"])
    assert "default_pool = user" in read_back.stdout


def test_set_bool_value(runner: CliRunner) -> None:
    result = runner.invoke(app, ["config", "set", "capture.enabled", "false"])
    assert result.exit_code == 0, result.output
    assert "capture.enabled = false" in result.stdout


def test_set_invalid_value_fails(runner: CliRunner) -> None:
    result = runner.invoke(app, ["config", "set", "default_pool", "bogus"])
    assert result.exit_code == 1
    assert "invalid value" in result.output


def test_set_unknown_key_fails(runner: CliRunner) -> None:
    result = runner.invoke(app, ["config", "set", "nope", "x"])
    assert result.exit_code == 1
    assert "unknown config key" in result.output


def test_redaction_shortcut(runner: CliRunner) -> None:
    result = runner.invoke(app, ["config", "redaction", "strict"])
    assert result.exit_code == 0, result.output
    assert "redaction.level = strict" in result.stdout
    listed = runner.invoke(app, ["--json", "config", "list"])
    assert _json(listed.stdout)["redaction"]["level"] == "strict"
