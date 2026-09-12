"""Persona UI uses the real terminal widget without sending it local commands."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, Static

from aisquare.cli.ui.persona_activity import PersonaActivity
from aisquare.cli.ui.personas import PersonaScreen
from aisquare.core.tmux import TmuxServer
from aisquare.models import ProjectInfo, TeamEvent, TeamSession, TeamTask
from aisquare.services import personas
from aisquare.services import team as team_service
from tests.test_terminal_pane import FakePane, FakeTmux, Host, synced, wait_until


class TerminalHost(Host):
    def __init__(self, server: TmuxServer, project: ProjectInfo) -> None:
        super().__init__(server, "%1")
        self.project = project

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield PersonaActivity(self.project)


def test_persona_button_returns_to_terminal_and_keeps_local_commands_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    project = team_service.activate()
    fake = FakeTmux()
    fake.panes["%1"] = FakePane(screen=["An unfinished agent draft"], cursor=(5, 0))

    async def run() -> None:
        host = TerminalHost(fake.server(tmp_path), project)
        async with host.run_test(size=(120, 40)) as pilot:
            await wait_until(pilot, lambda: synced(host.pane))
            host.pane.focus()
            await pilot.pause()
            await pilot.click(".open-persona")
            await host.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(host.screen, PersonaScreen)
            fake.input.clear()
            field = host.screen.query_one("#persona-command", Input)
            field.focus()
            await pilot.press(*list("/persona use studio"), "enter")
            await host.workers.wait_for_complete()
            await pilot.pause()
            assert personas.persona_status(project)["default"] == "studio@1.0.0"
            assert not [item for item in fake.input if item[0] in {"send-keys", "paste-buffer"}]
            await pilot.click("#persona-close")
            await pilot.pause()
            assert host.focused is host.pane
            assert fake.panes["%1"].screen == ["An unfinished agent draft"]
            await pilot.press("x")
            await pilot.pause()
            assert any(item[0] == "send-keys" and item[-1] == "x" for item in fake.input)

    asyncio.run(run())


def test_role_preview_and_slash_completion(project: ProjectInfo) -> None:
    """The simplified modal is one command box: per-role preview and completion both
    run through it, with no picker or role field to fill in first."""
    personas.select("use", project, reference="mission-control")

    async def run() -> None:
        host: App[None] = App()
        async with host.run_test(size=(120, 40)) as pilot:
            host.push_screen(PersonaScreen(project))
            await host.workers.wait_for_complete()
            await pilot.pause()
            field = host.screen.query_one("#persona-command", Input)
            field.focus()
            # Per-role preview via the one command box — the role rides on the command.
            field.value = "/persona preview mission-control --role ui-tester"
            await pilot.press("enter")
            await host.workers.wait_for_complete()
            await pilot.pause()
            assert '"role": "ui-tester"' in str(
                host.screen.query_one("#persona-output", Static).render()
            )
            # Previewing never disturbs the project's active default.
            assert personas.persona_status(project)["default"] == "mission-control@1.0.0"
            # Slash completion still works: type a prefix, press Right to accept it.
            field.value = "/persona sta"
            field.cursor_position = len(field.value)
            await host.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("right")
            assert field.value == "/persona status"

    asyncio.run(run())


@pytest.fixture
def project(tmp_path: Path) -> ProjectInfo:
    return ProjectInfo(id="ui-project", root=tmp_path)


def test_display_truncation_is_labelled_and_recovery_restores_unchanged_record(
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = TeamEvent(
        id="e1",
        project_id=project.id,
        kind="task_blocked",
        text="x" * 1700 + "IMPORTANT FAILURE",
        created_at=datetime.now(UTC),
    )
    answer: tuple[ProjectInfo, list[TeamSession], list[TeamTask], list[TeamEvent]] = (
        project,
        [],
        [],
        [event],
    )
    monkeypatch.setattr(team_service, "board_data", lambda **kwargs: answer)

    class ActivityHost(App[None]):
        def compose(self) -> ComposeResult:
            yield PersonaActivity(project)

    async def run() -> None:
        host = ActivityHost()
        async with host.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            activity = host.query_one(PersonaActivity)
            output = activity.query_one(".persona-events", Static)
            activity.refresh_activity()
            assert "Display shortened" in str(output.render())
            assert json.loads(activity.original)[0]["text"].endswith("IMPORTANT FAILURE")
            original = output.update
            updates: list[str] = []

            def record_update(content: Any = "") -> None:
                updates.append(str(content))
                original(content)

            monkeypatch.setattr(output, "update", record_update)
            activity.refresh_activity()
            activity.refresh_activity()
            assert not updates, "identical records should not redraw or disturb selection"

            def unavailable(**kwargs: Any) -> Any:
                raise OSError("temporary store failure")

            monkeypatch.setattr(team_service, "board_data", unavailable)
            activity.refresh_activity()
            assert "unavailable" in str(output.render())
            monkeypatch.setattr(team_service, "board_data", lambda **kwargs: answer)
            activity.refresh_activity()
            assert "Display shortened" in str(output.render())
            assert len(updates) == 2

    asyncio.run(run())
