"""The aisquare home is hidden by default and a project only when the owner adds it.

The owner's decision D5 (2026-10-23), closing #240 R1-F8 and item (c) of the #267 review:
``onboard_project`` refused the home whatever asked, so ``aisquare init`` there and
``project onboard`` succeeded and did nothing, and the home never showed. Now:

- nothing adds it by itself: a spawn, a launch's activation, a capture (capture on too);
- ``init`` and ``project onboard`` (and asq's ``+``, which runs ``init``) add it, as any
  folder, and say once what the folder holds;
- its card never shows the captain's row, which stays in the captain's section;
- removing it from the fleet (a forget) hides it again, and the captain's own capture of
  its home board does not bring it back.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.ui import app as ui_app
from aisquare.core import paths
from aisquare.core.store import store_session
from aisquare.core.workspace import project_id_for
from aisquare.models import FleetAgent, FleetAgentStatus, LaunchSpec, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import lifecycle
from aisquare.services import project as project_service


@pytest.fixture
def home() -> ProjectInfo:
    root = paths.aisquare_home().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return ProjectInfo(id=project_id_for(root), root=root)


def _listed(project: ProjectInfo) -> bool:
    with store_session() as store:
        return project.id in {p.id for p in store.list_projects()}


def test_nothing_adds_the_home_by_itself(home: ProjectInfo) -> None:
    with store_session() as store:
        store.ensure_project(home)  # a capture, the captain's own included
        store.onboard_project(home, codename="amber-otter")  # a spawn's, a launch's
    assert not _listed(home)


def test_project_onboard_adds_the_home_and_warns_once(home: ProjectInfo) -> None:
    first = project_service.onboard(home.root, refresh=False)
    again = project_service.onboard(home.root, refresh=False)

    assert _listed(home)
    assert first.warning == project_service.HOME_WARNING
    assert again.warning is None, "said once, when it becomes a project"
    for words in ("settings", "fleet database", "login data", "read and change"):
        assert words in project_service.HOME_WARNING


def test_an_ordinary_folder_is_added_without_a_warning(tmp_path: Path) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    assert project_service.onboard(folder, refresh=False).warning is None


def test_init_in_the_home_adds_it_and_says_so_once(home: ProjectInfo) -> None:
    report = lifecycle.initialize(
        home.root,
        api_key=None,
        local=True,
        agents=[],
        onboard=True,
        reinit=False,
        assume_yes=True,
        explainability=False,
    )
    assert _listed(home)
    said = [note for note in report.notes if project_service.HOME_WARNING in note]
    assert len(said) == 1, report.notes


def test_the_cli_warns_on_stderr_and_succeeds(home: ProjectInfo) -> None:
    result = CliRunner().invoke(app, ["project", "onboard", str(home.root)])

    assert result.exit_code == 0, result.output
    assert "warning: this folder is aisquare's own home" in result.output + (
        result.stderr if result.stderr_bytes is not None else ""
    )


def test_a_forget_hides_it_again_and_the_captains_capture_keeps_it_hidden(
    home: ProjectInfo,
) -> None:
    project_service.onboard(home.root, refresh=False)
    with store_session() as store:
        store.forget_project(home.id)
        store.ensure_project(home)  # the captain's home board, captured again
    assert not _listed(home)


def _captain_row(project: ProjectInfo) -> FleetAgent:
    return FleetAgent(
        id="agt_the_captain",
        project_id=project.id,
        label="captain",
        role="captain",
        pane_id="%1",
        cwd=project.root / "captain" / "brain",
        launch_spec=LaunchSpec(binary="claude", extra_args=["--strict-mcp-config", "--tools", ""]),
        created_at=datetime.now(tz=UTC),
    )


def _coder_row(project: ProjectInfo) -> FleetAgent:
    return FleetAgent(
        id="agt_a_coder",
        project_id=project.id,
        label="coder-1",
        role="coder",
        pane_id="%2",
        cwd=project.root,
        created_at=datetime.now(tz=UTC),
    )


@pytest.mark.parametrize("captain", [True, False], ids=["captain on", "captain off"])
def test_the_homes_card_leaves_the_captain_to_its_own_section(
    home: ProjectInfo, monkeypatch: pytest.MonkeyPatch, captain: bool
) -> None:
    project_service.onboard(home.root, refresh=False)
    with store_session() as store:
        store.upsert_fleet_agent(_captain_row(home))
        store.upsert_fleet_agent(_coder_row(home))

    def listed(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        with store_session() as store:
            rows = store.fleet_agents(project.id, live_only=True)
        return [FleetAgentStatus(agent=row, state="waiting") for row in rows]

    monkeypatch.setattr(fleet_service, "list_agents", listed)

    read = ui_app._read_frame(show_captured=False, captain=captain)

    assert home.id in {p.id for p in read.projects}, "its own card"
    assert [s.agent.label for s in read.agents[home.id]] == ["coder-1"], "no captain there"
    if captain:
        assert read.captain is not None and read.captain.agent.id == "agt_the_captain"


def test_with_capture_on_the_captured_home_is_still_no_card(
    home: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(project_service, "capture_on", lambda: True)
    with store_session() as store:
        store.ensure_project(home)

    read = ui_app._read_frame(show_captured=True, captain=False)

    assert home.id not in {p.id for p in read.projects}
