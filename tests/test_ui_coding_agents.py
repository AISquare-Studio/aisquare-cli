"""Agent choice survives the UI -> configuration/service boundaries."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from textual.app import App
from textual.pilot import Pilot
from textual.widgets import Button, Select

from aisquare.cli.ui.views.project import ProjectView
from aisquare.cli.ui.views.settings import SettingsView
from aisquare.cli.ui.views.spawn import SpawnScreen
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.config import RoleLaunchProfile, load_config, save_config
from aisquare.core.orchestrator import team_project
from aisquare.models import FleetAgent
from aisquare.services import fleet
from tests.test_ui_project import Host, drive


def test_ui_settings_persist_mixed_agents_and_native_permissions(tmp_path: Path) -> None:
    project = team_project(tmp_path)

    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, str]]:
        host.query_one(ProjectView).active = "tab-settings"
        await pilot.pause()
        host.query_one("#default-agent", Select).value = "codex"
        host.query_one("#family-reviewer", Select).value = "claude-code"
        host.query_one("#sandbox-coder", Select).value = "workspace-write"
        host.query_one("#approval-coder", Select).value = "on-request"
        host.query_one("#save-settings", Button).press()
        await pilot.pause()
        return host.notices

    assert any("saved" in message for message, _ in drive(project, scenario))
    config = load_config()
    assert config.agents.default == "codex"
    assert config.team.profiles["reviewer"].agent == "claude-code"
    assert config.fleet.roles["coder"].approval_policy == "on-request"
    assert config.fleet.roles["coder"].sandbox == "workspace-write"


def test_settings_preserve_unknown_choices_on_mount_reload_and_save(tmp_path: Path) -> None:
    config = load_config()
    config.agents.default = "future-agent"
    config.team.profiles["coder"] = RoleLaunchProfile(agent="future-role-agent")
    config.fleet.roles["coder"].sandbox = "future-scope"
    config.fleet.roles["coder"].approval_policy = "future-approval"
    save_config(config)

    async def scenario(pilot: Pilot[None], host: Host) -> None:
        host.query_one(ProjectView).active = "tab-settings"
        await pilot.pause()
        view = host.query_one(SettingsView)
        assert view.query_one("#default-agent", Select).value == "future-agent"
        assert view.query_one("#family-coder", Select).value == "future-role-agent"
        assert view.query_one("#sandbox-coder", Select).value == "future-scope"
        assert view.query_one("#approval-coder", Select).value == "future-approval"
        config.agents.default = "newer-agent"
        config.team.profiles["coder"].agent = "newer-role-agent"
        config.fleet.roles["coder"].sandbox = "newer-scope"
        config.fleet.roles["coder"].approval_policy = "newer-approval"
        save_config(config)
        view.reload_form()
        await pilot.pause()
        view.query_one("#save-settings", Button).press()
        await pilot.pause()
        saved = load_config()
        assert saved.agents.default == "newer-agent"
        assert saved.team.profiles["coder"].agent == "newer-role-agent"
        assert saved.fleet.roles["coder"].sandbox == "newer-scope"
        assert saved.fleet.roles["coder"].approval_policy == "newer-approval"

    drive(team_project(tmp_path), scenario)


def test_settings_never_persist_an_uninitialized_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config()
    config.agents.default = "codex"
    config.team.profiles["coder"] = RoleLaunchProfile(agent="codex")
    config.fleet.roles["coder"].sandbox = "read-only"
    config.fleet.roles["coder"].approval_policy = "on-request"
    config.fleet.roles["coder"].permission_mode = "plan"
    save_config(config)

    async def scenario(pilot: Pilot[None], host: Host) -> None:
        host.query_one(ProjectView).active = "tab-settings"
        await pilot.pause()
        view = host.query_one(SettingsView)
        lookup = view.query_one
        selectors = (
            "#default-agent",
            "#family-coder",
            "#sandbox-coder",
            "#approval-coder",
            "#perm-coder",
        )
        uninitialized = {
            key: Select([("valid", "valid")], value="missing", allow_blank=False)
            for key in selectors
        }
        assert all(control.value is Select.NULL for control in uninitialized.values())
        reload = view.reload_form

        def restore_and_reload() -> None:
            monkeypatch.setattr(view, "query_one", lookup)
            reload()

        monkeypatch.setattr(view, "reload_form", restore_and_reload)
        monkeypatch.setattr(
            view,
            "query_one",
            lambda query, expect_type=None: (
                uninitialized[query] if query in uninitialized else lookup(query, expect_type)
            ),
        )
        view._save_fleet_settings()
        saved = load_config()
        assert saved.agents.default == "codex"
        assert saved.team.profiles["coder"].agent == "codex"
        assert saved.fleet.roles["coder"].sandbox == "read-only"
        assert saved.fleet.roles["coder"].approval_policy == "on-request"
        assert saved.fleet.roles["coder"].permission_mode == "plan"

    drive(team_project(tmp_path), scenario)


def test_spawn_dialog_uses_selected_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = team_project(tmp_path)
    receipt = fleet.SpawnReceipt(
        agent=FleetAgent(
            id="fixture",
            project_id=project.id,
            label="coder-1",
            role="coder",
            agent="codex",
            binary="codex",
            cwd=tmp_path,
            tmux_socket="test",
            pane_id="%1",
            created_at=datetime.now(UTC),
        ),
        asked_label=None,
        tmux_session="test",
    )
    started = Mock(return_value=receipt)
    monkeypatch.setattr(fleet, "spawn", started)

    async def scenario() -> None:
        host: App[None] = App()
        async with host.run_test(size=(100, 36)) as pilot:
            host.push_screen(SpawnScreen(project))
            await pilot.pause()
            host.screen.query_one("#spawn-family", Select).value = "codex"
            host.screen.query_one("#spawn-submit", Button).press()
            await pilot.pause()
            assert started.call_count == 1
            assert started.call_args.args == (project, "coder")
            assert started.call_args.kwargs["agent"] == "codex"
            assert not isinstance(host.screen, SpawnScreen)

    asyncio.run(scenario())


def test_settings_round_trip_the_empty_native_permission_mode(tmp_path: Path) -> None:
    config = load_config()
    config.fleet.roles["coder"].permission_mode = ""
    save_config(config)

    async def scenario(pilot: Pilot[None], host: Host) -> None:
        host.query_one(ProjectView).active = "tab-settings"
        await pilot.pause()
        view = host.query_one(SettingsView)
        control = view.query_one("#perm-coder", Select)
        assert control.value == "" and control.value is not Select.NULL
        view.query_one("#save-settings", Button).press()
        await pilot.pause()
        saved = load_config().fleet.roles["coder"].permission_mode
        assert saved == ""
        assert "--permission-mode" not in get_adapter("claude-code").fleet_args(
            "coder", "coder", saved
        )
        view.reload_form()
        await pilot.pause()
        assert view.query_one("#perm-coder", Select).value == ""

    drive(team_project(tmp_path), scenario)
