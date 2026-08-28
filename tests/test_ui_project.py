"""The Project view, driven headless: its tabs, the manager button, the board, both forms.

Everything the view reaches for outside the store is monkeypatched at the
module the view imports — ``services.fleet`` for the manager, the two
explainability services for the tracing tab — so no test starts tmux, dials a
gateway or depends on ``claude``. What each test asserts is the artefact the
claim is about: the spawn call the button made, the bytes ``config.toml`` holds
after Save, the rows the board table shows for THIS project and not another.

Textual is imported at module level: the ``dev`` extra pins it, and every test
here is about widgets.
"""

from __future__ import annotations

import asyncio
import tomllib
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import pytest
from textual.app import App, ComposeResult
from textual.notifications import SeverityLevel
from textual.pilot import Pilot
from textual.widgets import Button, DataTable, Input, Select
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.ui.board import BoardPanel
from aisquare.cli.ui.terminal import TerminalPane
from aisquare.cli.ui.views.doctor import DoctorView
from aisquare.cli.ui.views.explainability import ExplainabilityView
from aisquare.cli.ui.views.project import ManagerTab, ProjectView
from aisquare.cli.ui.views.settings import SettingsView
from aisquare.core import paths
from aisquare.core.config import load_config
from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo
from aisquare.services import explainability as explainability_service
from aisquare.services import explainability_ops as ops
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service

T = TypeVar("T")


class Host(App[None]):
    """A bare app around one ``ProjectView`` that records every notification."""

    def __init__(self, project: ProjectInfo) -> None:
        super().__init__()
        self._project = project
        self.notices: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        yield ProjectView(self._project, id="project", refresh_seconds=60.0)

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        self.notices.append((message, severity))
        super().notify(message, title=title, severity=severity, timeout=timeout, markup=markup)


def drive(
    project: ProjectInfo, scenario: Callable[[Pilot[None], Host], Coroutine[Any, Any, T]]
) -> T:
    """Run ``scenario`` against a mounted Project view and return what it observed."""

    async def run() -> T:
        host = Host(project)
        async with host.run_test(size=(120, 50)) as pilot:
            await pilot.pause()
            return await scenario(pilot, host)

    return asyncio.run(run())


async def settle(pilot: Pilot[None]) -> None:
    """Let every worker finish and its state-change handler run."""
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def fake_agent(project: ProjectInfo, *, pane_id: str = "%7", label: str = "manager") -> FleetAgent:
    return FleetAgent(
        id="agt_01testmanager",
        project_id=project.id,
        label=label,
        role="manager" if label == "manager" else "coder",
        pane_id=pane_id,
        cwd=project.root,
        created_at=datetime.now(tz=UTC),
    )


# --- fixtures ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def project() -> ProjectInfo:
    return team_service.activate()


@pytest.fixture(autouse=True)
def no_fleet(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fleet with no manager and no tmux: the view must never reach the real service."""
    monkeypatch.setattr(fleet_service, "manager_of", lambda project: None)
    monkeypatch.setattr(
        fleet_service, "status_of", lambda agent: FleetAgentStatus(agent=agent, state="waiting")
    )


@pytest.fixture(autouse=True)
def quiet_explainability(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """No probe dials anything; the client lane answers from a fake; calls are counted."""
    calls = {"shipping_state": 0}

    def shipping_state(target_name: str | None = None) -> explainability_service.ShippingState:
        calls["shipping_state"] += 1
        return explainability_service.ShippingState(
            configured=False,
            gateway_url="",
            has_key=False,
            sdk_installed=False,
            queued=0,
            sent=0,
            dead=0,
            reason="off — nothing is captured (fake)",
        )

    monkeypatch.setattr(explainability_service, "shipping_state", shipping_state)
    monkeypatch.setattr(
        ops,
        "probe_proxy",
        lambda url, timeout=1.5: explainability_service.ProxyProbe(False, "not dialled in tests"),
    )
    return calls


def _config_toml() -> dict[str, Any]:
    return tomllib.loads(paths.config_path().read_text(encoding="utf-8"))


# --- the tabs -----------------------------------------------------------------------------


def test_project_view_has_the_five_tabs_with_their_widgets(project: ProjectInfo) -> None:
    async def scenario(pilot: Pilot[None], host: Host) -> tuple[int, list[str], str]:
        view = host.query_one(ProjectView)
        for pane_id, widget_type in (
            ("#tab-manager", ManagerTab),
            ("#tab-board", BoardPanel),
            ("#tab-doctor", DoctorView),
            ("#tab-explainability", ExplainabilityView),
            ("#tab-settings", SettingsView),
        ):
            assert view.query_one(pane_id).query_one(widget_type)
        ids = [pane.id or "" for pane in view.query("TabPane")]
        return view.tab_count, ids, view.active

    count, ids, active = drive(project, scenario)
    assert count == 5
    assert ids == list(ProjectView.TAB_IDS)
    assert active == "tab-manager"  # the manager first: that is where the goal goes


# --- the Manager tab ----------------------------------------------------------------------


def test_start_manager_button_spawns_once_and_attaches_the_pane(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet: dict[str, FleetAgent | None] = {"manager": None}
    spawns: list[tuple[str, str]] = []

    def spawn(target: ProjectInfo, role: str, **_: object) -> fleet_service.SpawnReceipt:
        spawns.append((target.id, role))
        fleet["manager"] = fake_agent(target)
        return fleet_service.SpawnReceipt(
            agent=fake_agent(target), asked_label=None, tmux_session="asq-amber-otter"
        )

    monkeypatch.setattr(fleet_service, "spawn", spawn)
    monkeypatch.setattr(fleet_service, "manager_of", lambda target: fleet["manager"])

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[bool, bool, bool, bool, str | None]:
        button = host.query_one("#start-manager", Button)
        pane = host.query_one("#manager-pane", TerminalPane)
        before = (button.display, pane.display)
        await pilot.click("#start-manager")
        await settle(pilot)
        return before[0], before[1], button.display, pane.display, pane.pane_id

    button_before, pane_before, button_after, pane_after, pane_id = drive(project, scenario)
    assert (button_before, pane_before) == (True, False)  # no manager: the button, no pane
    assert spawns == [(project.id, "manager")]  # exactly one spawn, of the manager role
    assert (button_after, pane_after) == (False, True)  # now the pane, no button
    assert pane_id == "%7"  # attached to the pane the receipt named


def test_no_button_and_no_spawn_when_a_manager_already_runs(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative half: a present manager means the pane, and the button never fires."""
    spawns: list[str] = []
    monkeypatch.setattr(
        fleet_service, "manager_of", lambda target: fake_agent(target, pane_id="%3")
    )

    def never(*args: object, **kwargs: object) -> fleet_service.SpawnReceipt:
        spawns.append("spawned")
        pytest.fail("spawn was called although a manager already runs")

    monkeypatch.setattr(fleet_service, "spawn", never)

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[bool, str | None, str]:
        button = host.query_one("#start-manager", Button)
        pane = host.query_one("#manager-pane", TerminalPane)
        await pilot.press("enter")  # whatever has focus, the hidden button must not be it
        await settle(pilot)
        header = str(host.query_one("#manager-header").render())
        return button.display, pane.pane_id, header

    displayed, pane_id, header = drive(project, scenario)
    assert displayed is False
    assert pane_id == "%3"
    assert spawns == []
    assert "waiting" in header  # the state the (patched) service reported


def test_start_manager_reports_a_fleet_error_and_stays_available(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawns: list[str] = []

    def refuse(target: ProjectInfo, role: str, **_: object) -> fleet_service.SpawnReceipt:
        spawns.append(role)
        raise fleet_service.FleetUnavailable("tmux is not installed (or not on PATH)")

    monkeypatch.setattr(fleet_service, "spawn", refuse)

    async def scenario(
        pilot: Pilot[None], host: Host
    ) -> tuple[bool, bool, bool, list[tuple[str, str]]]:
        button = host.query_one("#start-manager", Button)
        await pilot.click("#start-manager")
        await settle(pilot)
        pane_shown = host.query_one("#manager-pane").display
        return button.disabled, button.display, pane_shown, host.notices

    disabled, displayed, pane_shown, notices = drive(project, scenario)
    assert spawns == ["manager"]
    assert disabled is False  # the button is usable again after the refusal
    assert displayed is True and pane_shown is False  # still no manager
    errors = [m for m, severity in notices if severity == "error"]
    assert errors and "tmux is not installed" in errors[0]  # the reason, verbatim


def test_refresh_status_pushes_the_managers_state(project: ProjectInfo) -> None:
    manager = fake_agent(project, pane_id="%11")
    coder = fake_agent(project, pane_id="%12", label="coder-auth")

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[str, str | None, bool, bool]:
        view = host.query_one(ProjectView)
        view.refresh_status(
            [
                FleetAgentStatus(agent=coder, state="working"),
                FleetAgentStatus(agent=manager, state="attention", tmux_session="asq-ruby-fox"),
            ]
        )
        await pilot.pause()
        pane = host.query_one("#manager-pane", TerminalPane)
        header = str(host.query_one("#manager-header").render())
        attached = pane.pane_id
        view.refresh_status([FleetAgentStatus(agent=coder, state="working")])  # coder only
        await pilot.pause()
        return header, attached, host.query_one("#start-manager", Button).display, pane.display

    header, attached, button_back, pane_after = drive(project, scenario)
    assert "NEEDS YOU" in header and "asq-ruby-fox" in header
    assert attached == "%11"  # the manager's pane, not the coder's
    assert button_back is True and pane_after is False  # no manager in the snapshot → button


def test_refresh_routes_a_snapshot_and_a_bare_refresh_only_repaints(project: ProjectInfo) -> None:
    """The plan spells the push ``refresh(snapshot)``; Textual's own ``refresh()`` must survive."""
    manager = fake_agent(project, pane_id="%21")

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[str, str | None, str | None, str]:
        view = host.query_one(ProjectView)
        view.refresh([FleetAgentStatus(agent=manager, state="working")])  # the plan's spelling
        await pilot.pause()
        pane = host.query_one("#manager-pane", TerminalPane)
        header, attached = str(host.query_one("#manager-header").render()), pane.pane_id
        view.refresh()  # the framework's spelling: a repaint, no snapshot
        view.refresh(layout=True)
        await pilot.pause()
        # A view handed a snapshot BEFORE it mounted applies it once the tabs exist.
        early = ProjectView(project, id="early", refresh_seconds=60.0)
        early.refresh_status([FleetAgentStatus(agent=manager, state="attention")])
        await host.mount(early)
        await pilot.pause()
        early_header = str(early.query_one("#manager-header").render())
        return header, attached, pane.pane_id, early_header

    header, attached, after_bare, early_header = drive(project, scenario)
    assert "working" in header and attached == "%21"  # routed to the manager tab
    assert after_bare == "%21"  # a bare refresh() changed nothing
    assert "NEEDS YOU" in early_header  # the pre-mount push landed at mount


# --- the Board tab --------------------------------------------------------------------------


def test_board_tab_shows_this_projects_tasks_and_not_anothers(
    project: ProjectInfo, runner: CliRunner, tmp_path: Path
) -> None:
    runner.invoke(app, ["task", "add", "build the API"])
    runner.invoke(app, ["task", "add", "wire the UI", "--detail", "hook Atlas chat to v3"])
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = team_service.activate(other_dir)
    assert other.id != project.id

    async def rows(pilot: Pilot[None], host: Host) -> int:
        return host.query_one("#tasks", DataTable).row_count

    assert drive(project, rows) == 2
    assert drive(other, rows) == 0  # the panel reads the project it was given, not the cwd


# --- the Settings tab -----------------------------------------------------------------------


def test_settings_saves_a_changed_permission_mode_to_config_toml(project: ProjectInfo) -> None:
    assert not paths.config_path().exists()  # nothing written before the user saves

    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, str]]:
        host.query_one(ProjectView).active = "tab-settings"
        await pilot.pause()
        host.query_one("#perm-coder", Select).value = "plan"
        host.query_one("#worktree-dir", Input).value = ".fleet-trees"
        host.query_one("#save-settings", Button).press()
        await pilot.pause()
        return host.notices

    notices = drive(project, scenario)
    assert any(m.startswith("✓ fleet settings saved") for m, _ in notices), notices
    fleet_on_disk = _config_toml()["fleet"]
    on_disk = fleet_on_disk["roles"]
    assert on_disk["coder"]["permission_mode"] == "plan"  # the bytes, not the widget
    assert on_disk["coder"]["worktree"] is True  # untouched fields survive the write
    assert on_disk["manager"]["permission_mode"] == "auto"  # only the changed role changed
    assert fleet_on_disk["worktree_dir"] == ".fleet-trees"  # the worktree root (§4.2)
    assert fleet_on_disk["escape_key"] == "f12"  # an untouched fleet field keeps its default
    assert load_config().fleet.roles["coder"].permission_mode == "plan"


def test_settings_rejects_a_bad_agent_cap_and_writes_nothing(project: ProjectInfo) -> None:
    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, str]]:
        host.query_one(ProjectView).active = "tab-settings"
        await pilot.pause()
        host.query_one("#max-agents", Input).value = "0"
        host.query_one("#save-settings", Button).press()
        await pilot.pause()
        return host.notices

    notices = drive(project, scenario)
    assert any("at least 1" in m and severity == "error" for m, severity in notices), notices
    assert not paths.config_path().exists()  # a refused form never reaches the writer


def test_settings_rejects_an_invalid_codename_and_renames_a_valid_one(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    renames: list[tuple[str, str]] = []

    def rename(target: ProjectInfo, codename: str) -> ProjectInfo:
        renames.append((target.id, codename))
        return target.model_copy(update={"codename": codename})

    monkeypatch.setattr(fleet_service, "rename", rename)

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[list[tuple[str, str]], int, str]:
        host.query_one(ProjectView).active = "tab-settings"
        await pilot.pause()
        field = host.query_one("#codename", Input)
        field.value = "Not Valid!"
        host.query_one("#rename-codename", Button).press()
        await pilot.pause()
        after_invalid = len(renames)
        field.value = "amber-otter"
        host.query_one("#rename-codename", Button).press()
        await pilot.pause()
        return host.notices, after_invalid, field.value

    notices, after_invalid, shown = drive(project, scenario)
    assert after_invalid == 0  # the invalid name never reached the service
    assert any("not a valid codename" in m and s == "error" for m, s in notices), notices
    assert renames == [(project.id, "amber-otter")]  # the valid one did, once
    assert shown == "amber-otter"
    assert any("is now amber-otter" in m for m, _ in notices)


# --- the Explainability tab -----------------------------------------------------------------


def test_explainability_enable_writes_the_switch_and_disable_clears_it(
    project: ProjectInfo, quiet_explainability: dict[str, int]
) -> None:
    async def scenario(
        pilot: Pilot[None], host: Host
    ) -> tuple[bool, str, bool, list[tuple[str, str]]]:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        view = host.query_one(ExplainabilityView)
        await pilot.click("#explainability-enable")
        await settle(pilot)
        enabled = _config_toml()["explainability"]["enabled"]
        status_after_enable = view.status_text
        await pilot.click("#explainability-disable")
        await settle(pilot)
        return (
            enabled,
            status_after_enable,
            _config_toml()["explainability"]["enabled"],
            host.notices,
        )

    enabled, status, disabled, notices = drive(project, scenario)
    assert enabled is True  # the artefact: config.toml on disk
    assert "enabled:   on" in status  # …and the tab re-read it through the services
    assert disabled is False
    assert quiet_explainability["shipping_state"] >= 3  # mount, after enable, after disable
    assert any(m.startswith("✓ tracing enabled") for m, _ in notices)
    assert any(m.startswith("✓ tracing disabled") for m, _ in notices)
    assert _config_toml()["explainability"]["target"] == "stg"  # nothing else was touched


def test_explainability_ship_drains_through_the_service_and_register_refuses_unconfigured(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    drains: list[int] = []
    rosters: list[object] = []

    def ship_once(limit: int = 500) -> explainability_service.ShipReport:
        drains.append(limit)
        return explainability_service.ShipReport(
            sent=3, runs=("run-1",), reason="shipped 3 records"
        )

    monkeypatch.setattr(explainability_service, "ship_once", ship_once)

    def never(*args: object, **kwargs: object) -> ops.HttpVerdict:
        rosters.append(args)
        pytest.fail("register_roster was called with no gateway configured")

    monkeypatch.setattr(ops, "register_roster", never)

    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, str]]:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        await pilot.click("#explainability-ship")
        await settle(pilot)
        await pilot.click("#explainability-register")
        await settle(pilot)
        return host.notices

    notices = drive(project, scenario)
    assert drains == [500]  # one press, one drain, the CLI's default limit
    assert ("shipped 3 records\nruns: run-1", "information") in notices
    assert rosters == []  # no gateway configured → refused before any request
    assert any("has no gateway URL" in m and s == "error" for m, s in notices), notices
