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


# --- role bindings, edited headless (#98) ------------------------------------------------


def _bind(runner: CliRunner, *argv: str) -> None:
    result = runner.invoke(app, ["team", "bind", *argv])
    assert result.exit_code == 0, result.output


def _config_bytes() -> bytes:
    from aisquare.core import paths

    return paths.config_path().read_bytes()


def test_an_edit_writes_what_team_bind_clear_then_bind_writes(runner: CliRunner) -> None:
    """App settings' Edit replaces bin, env and args, as `--clear` then a fresh `team bind`
    does: through `bind_role`, which merges env and appends args, an edit doubled every
    arg it kept."""
    from aisquare.core import paths
    from aisquare.services import settings as settings_service

    first = ("coder", "--bin", "claude2", "--env", "A=1", "--env", "B=2", "--arg", "--model")
    _bind(runner, *first, "--arg", "opus")
    _bind(runner, "coder", "--clear")
    _bind(runner, "coder", "--bin", "claude3", "--env", "B=3", "--arg", "-v")
    by_the_cli = _config_bytes()

    paths.config_path().unlink()
    _bind(runner, *first, "--arg", "opus")
    settings_service.replace_role_binding("coder", agent_bin="claude3", env={"B": "3"}, args=["-v"])
    assert _config_bytes() == by_the_cli


def test_an_edit_keeps_what_the_form_does_not_show(runner: CliRunner, tmp_path: Any) -> None:
    """A variable bound to a file is a secret the form never shows, and the account has its
    own select: an edit leaves both as they were."""
    from aisquare.services import settings as settings_service

    token = tmp_path / "gh-token"
    token.write_text("t\n", encoding="utf-8")
    _bind(runner, "reviewer", "--env-file", f"GH_TOKEN={token}", "--env", "X=1")
    settings_service.bind_role("reviewer", account="2")
    profile = settings_service.replace_role_binding(
        "reviewer", agent_bin=None, env={"Y": "2"}, args=[]
    )
    assert profile.env == {"Y": "2"} and profile.env_files == {"GH_TOKEN": str(token)}
    assert profile.account == "2" and profile.bin is None
