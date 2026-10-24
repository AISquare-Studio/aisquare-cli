"""One root for the project persona layer, ``core.personas.project_root`` (review of #201).

The project layer's root was worked out in seven places: launch, the session-start
briefing, the owed briefing, spawn, the Personas tab, ``aisquare persona`` and the Spawn
dialog's import. F1, F6 and F8 of the first review were one of them out of step, and the
R1 review found two more:

- R1-F6: the tab and the ``persona`` command asked git (``git_common_root``), while spawn
  and launch read ``<project root>/.aisquare/personas``. A project that is not a git
  repository (a folder marked ``.aisquare`` holding several repositories) showed no
  project personas there, though spawn and launch used them; a project registered at a
  repository's subdirectory showed the repository's, not its own.
- R1-F7: one helper, asked everywhere.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from textual.pilot import Pilot
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.cli.ui.spawn import SpawnDialog
from aisquare.cli.ui.views.personas_tab import PersonasTab
from aisquare.core import orchestrator, personas
from aisquare.core.agent_adapters import claude_code
from aisquare.core.config import FleetRoleSettings
from aisquare.models import FleetAgent, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from tests.test_ui_personas import Host, drive, rows


def _persona(root: Path, name: str) -> None:
    skill = root / ".aisquare" / "personas" / name / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        f"---\nname: {name}\ndescription: Only this project has it.\n---\nYou are {name}.\n",
        encoding="utf-8",
    )


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def _block(name: str) -> str:
    return f'<aisquare-persona name="{name}"'


def _cli(runner: CliRunner, *argv: str) -> tuple[int, Any]:
    result = runner.invoke(app, ["--json", "persona", *argv])
    return result.exit_code, json.loads(result.stdout)


def _tab_rows(project: ProjectInfo) -> list[tuple[str, ...]]:
    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, ...]]:
        return rows(host)

    return drive(scenario, project=project)


@pytest.fixture
def folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project that is no git repository: a folder marked ``.aisquare``, two repositories
    inside, the working directory, with a persona in its own layer."""
    root = tmp_path / "multi"
    (root / ".aisquare").mkdir(parents=True)
    for name in ("api", "web"):
        _git_init(root / name)
    _persona(root, "multirev")
    monkeypatch.chdir(root)
    return root.resolve()


# --- the helper -------------------------------------------------------------------------


def test_a_project_is_its_own_root_and_git_is_not_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_git(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"git was asked: {args}")

    monkeypatch.setattr(subprocess, "run", no_git)
    project = ProjectInfo(id="prj_own", root=tmp_path / "anywhere")

    assert personas.project_root(project) == tmp_path / "anywhere"


def test_a_directory_is_the_project_it_is_in_and_never_the_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    (repo / "src" / "deep").mkdir(parents=True)
    _git_init(repo)
    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.setenv(orchestrator.TEAM_HUB_ENV_VAR, str(hub))
    monkeypatch.chdir(repo / "src")

    assert personas.project_root(repo / "src" / "deep") == repo.resolve()
    assert personas.project_root() == repo.resolve(), "None is the working directory"


def test_a_folder_marked_as_a_project_is_its_root(folder: Path) -> None:
    assert personas.project_root(folder) == folder
    assert personas.project_root() == folder


def test_a_directory_in_no_project_has_no_project_layer(tmp_path: Path) -> None:
    """The walk names an unmarked directory itself; ``<dir>/.aisquare/personas`` could not
    exist there without marking it, so no read changes, and a write is refused."""
    loose = tmp_path / "loose"
    loose.mkdir()

    assert personas.project_root(loose) is None


# --- R1-F6: a project that is no git repository ------------------------------------------


def test_persona_list_and_show_read_a_marked_folders_layer(runner: CliRunner, folder: Path) -> None:
    code, listed = _cli(runner, "list")
    assert code == 0
    layers = {entry["name"]: entry["layer"] for entry in listed["personas"]}
    assert layers.get("multirev") == "project"

    code, shown = _cli(runner, "show", "multirev")
    assert (code, shown["layer"]) == (0, "project")


def test_persona_new_project_writes_the_marked_folders_layer(
    runner: CliRunner, folder: Path
) -> None:
    code, made = _cli(runner, "new", "pair", "--project")

    assert code == 0, made
    assert Path(made["path"]) == folder / ".aisquare" / "personas" / "pair"


def test_the_tab_shows_a_marked_folders_personas(folder: Path) -> None:
    project = ProjectInfo(id="prj_multi", root=folder)

    found = {row[0]: row[1] for row in _tab_rows(project)}

    assert found.get("multirev") == "project"


def test_the_tab_of_a_subdirectory_project_shows_its_own_layer_as_spawn_checks_it(
    tmp_path: Path,
) -> None:
    """A project registered at ``repo/svc`` has its layer at ``repo/svc``: git named the
    repository around it, so the tab showed ``repo``'s personas and hid the ones spawn uses."""
    repo = tmp_path / "repo"
    (repo / "svc").mkdir(parents=True)
    _git_init(repo)
    _persona(repo, "outer")
    _persona(repo / "svc", "inner")
    project = ProjectInfo(id="prj_svc", root=(repo / "svc").resolve())
    notes: list[str] = []

    found = {row[0]: row[1] for row in _tab_rows(project)}

    chosen = fleet_service._chosen_persona(project, "coder", "inner", FleetRoleSettings(), notes)
    assert chosen == "inner"
    assert found.get("inner") == "project"
    assert "outer" not in found


# --- R1-F7: every place asks the one helper ---------------------------------------------


@pytest.fixture
def elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The helper answers a root nothing else would name, holding the one persona ``solo``:
    a place that works its root out itself does not find it."""
    root = tmp_path / "elsewhere"
    _persona(root, "solo")
    asked: list[object] = []

    def project_root(where: object = None) -> Path:
        asked.append(where)
        return root

    monkeypatch.setattr(personas, "project_root", project_root)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return root


@pytest.fixture
def project(tmp_path: Path) -> ProjectInfo:
    root = tmp_path / "proj"
    root.mkdir()
    return ProjectInfo(id="prj_proj", root=root)


def test_the_cli_asks_the_helper(runner: CliRunner, elsewhere: Path) -> None:
    code, shown = _cli(runner, "show", "solo")

    assert (code, shown["path"]) == (0, str(elsewhere / ".aisquare" / "personas" / "solo"))


def test_launch_asks_the_helper(
    runner: CliRunner, elsewhere: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(launch_cli, "_exec", lambda binary, argv, env: captured.update(argv=argv))
    monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/local/bin/{cmd}")

    result = runner.invoke(app, ["launch", "coder", "--persona", "solo"])

    assert result.exit_code == 0, result.output
    assert captured["argv"][1] == claude_code.SYSTEM_PROMPT_FLAG
    assert "You are solo." in Path(captured["argv"][2]).read_text(encoding="utf-8")


def test_spawn_attach_and_a_replay_ask_the_helper(elsewhere: Path, project: ProjectInfo) -> None:
    notes: list[str] = []
    chosen = fleet_service._chosen_persona(project, "coder", "solo", FleetRoleSettings(), notes)
    agent = FleetAgent(
        id="agt_1",
        project_id=project.id,
        label="coder",
        role="coder",
        pane_id="%1",
        cwd=project.root,
        created_at=datetime.now(UTC),
        persona="solo",
    )

    assert chosen == "solo"
    assert fleet_service._persona_for_replay(project, agent, notes) == "solo"
    assert notes == []
    with pytest.raises(fleet_service.FleetError) as refused:
        fleet_service.attach_persona(project, "nobody", "solo")
    assert "no persona named" not in str(refused.value), "resolved; only the agent is missing"


def test_the_briefings_ask_the_helper(elsewhere: Path, project: ProjectInfo) -> None:
    for where in (project, project.root, None):
        lines = team_service._persona_briefing("solo", where)
        assert _block("solo") in "\n".join(lines), lines


def test_the_tab_and_the_spawn_dialog_ask_the_helper(elsewhere: Path, project: ProjectInfo) -> None:
    dialog = SimpleNamespace(project=project)
    offered, _unavailable = SpawnDialog._read_personas(dialog)  # type: ignore[arg-type]

    assert "solo" in [persona.name for persona in offered]
    assert PersonasTab(project).root == elsewhere
    assert "solo" in {row[0] for row in _tab_rows(project)}
