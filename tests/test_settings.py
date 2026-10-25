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


def test_the_no_role_agents_defaults_can_be_read_and_set(runner: CliRunner) -> None:
    """The Spawn dialog's *(none)* role is ``agent``, which is not one of the built-in roles:
    ``config set fleet.roles.agent.*`` was refused as an unknown key, so its permission mode
    and worktree could not be set. It reads as spawn's fallback and sets as any role does."""
    from aisquare.core.harness import PLAIN_ROLE
    from aisquare.services import fleet as fleet_service

    before = runner.invoke(app, ["config", "get", f"fleet.roles.{PLAIN_ROLE}.worktree"])
    assert before.exit_code == 0, before.output
    assert f"fleet.roles.{PLAIN_ROLE}.worktree = false" in before.stdout
    for key, value in (("permission_mode", "acceptEdits"), ("worktree", "true")):
        written = runner.invoke(app, ["config", "set", f"fleet.roles.{PLAIN_ROLE}.{key}", value])
        assert written.exit_code == 0, written.output
        assert f"fleet.roles.{PLAIN_ROLE}.{key} = {value}" in written.stdout
    shape = fleet_service.role_settings(PLAIN_ROLE)  # what a spawn of it reads
    assert shape.permission_mode == "acceptEdits" and shape.worktree is True
    assert fleet_service.role_settings("coder").permission_mode == "auto"  # its table alone


def test_a_role_nobody_has_is_still_an_unknown_key(runner: CliRunner) -> None:
    result = runner.invoke(app, ["config", "set", "fleet.roles.coderr.worktree", "true"])
    assert result.exit_code == 1
    assert "unknown config key" in result.output


def test_an_unrelated_set_and_unset_leaves_no_agent_table_and_holds_no_setting(
    runner: CliRunner,
) -> None:
    """Review of #405, M1: ``set`` seeded ``fleet.roles.agent`` into every save, so a file set
    and set back held ``[fleet.roles.agent]`` and ``holds_a_setting()`` read True — the
    reset/backup rule of #261 then backed up and reset a file that changed nothing."""
    from aisquare.core import config as core_config
    from aisquare.core import paths
    from aisquare.core.harness import PLAIN_ROLE

    for value in ("user", "project"):  # project is the default: set, then unset
        result = runner.invoke(app, ["config", "set", "default_pool", value])
        assert result.exit_code == 0, result.output
    written = paths.config_path().read_text(encoding="utf-8")
    assert f"[fleet.roles.{PLAIN_ROLE}]" not in written
    assert core_config.holds_a_setting() is False
    # The control: the agent's own table is written once it is set, and holds a setting.
    runner.invoke(app, ["config", "set", f"fleet.roles.{PLAIN_ROLE}.worktree", "true"])
    assert f"[fleet.roles.{PLAIN_ROLE}]" in paths.config_path().read_text(encoding="utf-8")
    assert core_config.holds_a_setting() is True
    runner.invoke(app, ["config", "set", f"fleet.roles.{PLAIN_ROLE}.worktree", "false"])
    assert f"[fleet.roles.{PLAIN_ROLE}]" not in paths.config_path().read_text(encoding="utf-8")
    assert core_config.holds_a_setting() is False


def test_config_list_shows_the_agent_role_as_get_does(runner: CliRunner) -> None:
    """Review of #405, m1: ``get`` knew ``fleet.roles.agent`` and ``list`` did not."""
    from aisquare.core.config import FleetRoleSettings
    from aisquare.core.harness import PLAIN_ROLE

    result = runner.invoke(app, ["--json", "config", "list"])
    assert result.exit_code == 0, result.output
    roles = _json(result.stdout)["fleet"]["roles"]
    assert roles[PLAIN_ROLE] == FleetRoleSettings().model_dump(mode="json")  # spawn's fallback
    assert set(roles) >= {"coder", "reviewer"}  # beside the built-in roles, not instead
