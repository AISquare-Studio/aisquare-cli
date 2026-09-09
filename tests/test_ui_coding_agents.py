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
from aisquare.cli.ui.views.spawn import SpawnScreen
from aisquare.core.config import load_config
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
