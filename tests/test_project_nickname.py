"""A project's nickname: free text a person names it by, beside the constrained codename."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.store import store_session
from aisquare.core.workspace import project_id_for
from aisquare.models import ProjectInfo
from aisquare.services import project as project_service


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return workdir


def _added(root: Path) -> ProjectInfo:
    root.mkdir(parents=True, exist_ok=True)
    resolved = root.resolve()
    with store_session() as store:
        return store.onboard_project(ProjectInfo(id=project_id_for(resolved), root=resolved))


def _stored(project: ProjectInfo) -> ProjectInfo:
    with store_session() as store:
        found = store.get_project(project.id)
    assert found is not None
    return found


# --- the service ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given",
    [
        "Auth rewrite",
        "fix: the login loop (again!) — #42 [wip]",
        "日本語のプロジェクト 🚀",
        "x" * project_service.NICKNAME_MAX,
    ],
)
def test_any_text_up_to_the_limit_is_kept(work_dir: Path, given: str) -> None:
    project = _added(work_dir)
    assert project_service.set_nickname(project, given).nickname == given
    assert _stored(project).nickname == given


def test_a_nickname_is_trimmed_and_a_blank_one_clears_it(work_dir: Path) -> None:
    project = _added(work_dir)
    assert project_service.set_nickname(project, "  billing v2 \t").nickname == "billing v2"
    assert project_service.set_nickname(project, "   ").nickname is None
    project_service.set_nickname(project, "again")
    assert project_service.set_nickname(project, None).nickname is None
    assert _stored(project).nickname is None


def test_a_nickname_over_the_limit_is_refused_and_nothing_is_written(work_dir: Path) -> None:
    project = _added(work_dir)
    project_service.set_nickname(project, "kept")
    too_long = "y" * (project_service.NICKNAME_MAX + 1)
    with pytest.raises(ValueError, match="at most 255 characters; this one has 256"):
        project_service.set_nickname(project, too_long)
    # Trimmed first: what is measured is what would be kept.
    padded = "  " + "z" * project_service.NICKNAME_MAX + "  "
    assert project_service.set_nickname(project, padded).nickname == padded.strip()
    project_service.set_nickname(project, "kept")
    with pytest.raises(ValueError):
        project_service.set_nickname(project, too_long)
    assert _stored(project).nickname == "kept"


@pytest.mark.parametrize("given", ["two\nlines", "tab\tinside", "esc\x1b[31m"])
def test_a_nickname_is_one_line(work_dir: Path, given: str) -> None:
    project = _added(work_dir)
    with pytest.raises(ValueError, match="one line"):
        project_service.set_nickname(project, given)
    assert _stored(project).nickname is None


def test_a_forgotten_project_is_not_renamed(work_dir: Path) -> None:
    project = _added(work_dir)
    project_service.forget_project(project)
    with pytest.raises(KeyError):
        project_service.set_nickname(project, "ghost")


def test_display_name_is_the_nickname_then_the_folder_then_the_id() -> None:
    plain = ProjectInfo(id="prj_x", root=Path("/w/api"))
    assert project_service.display_name(plain) == "api"
    assert project_service.display_name(plain.model_copy(update={"nickname": "Q3"})) == "Q3"
    assert project_service.display_name(ProjectInfo(id="prj_root", root=Path("/"))) == "prj_root"


def test_resolve_finds_a_project_by_its_nickname(tmp_path: Path) -> None:
    api = _added(tmp_path / "api")
    web = _added(tmp_path / "web")
    project_service.set_nickname(api, "payments / refunds")
    assert project_service.resolve("payments / refunds").id == api.id
    # A folder name still wins over a nickname spelled like it, rather than both refused.
    project_service.set_nickname(api, "web")
    assert project_service.resolve("web").id == web.id
    project_service.set_nickname(api, "same")
    project_service.set_nickname(web, "same")
    with pytest.raises(ValueError, match="matches multiple projects: api, web"):
        project_service.resolve("same")


# --- the CLI --------------------------------------------------------------------------------


def test_rename_names_the_active_project(runner: CliRunner, work_dir: Path) -> None:
    project = _added(work_dir)
    result = runner.invoke(app, ["project", "rename", "Login [wip] — día 2"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "✓ work is now called Login [wip] — día 2"
    assert _stored(project).nickname == "Login [wip] — día 2"

    listed = runner.invoke(app, ["project", "list"])
    assert "Login [wip] — día 2" in listed.stdout
    rows = json.loads(runner.invoke(app, ["--json", "project", "list"]).stdout)
    assert [(row["name"], row["nickname"]) for row in rows] == [
        ("Login [wip] — día 2", "Login [wip] — día 2")
    ]


def test_rename_clear_shows_the_folder_again(runner: CliRunner, work_dir: Path) -> None:
    project = _added(work_dir)
    project_service.set_nickname(project, "Q3 launch")
    result = runner.invoke(app, ["project", "rename", "--clear"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "✓ Q3 launch is now called work"
    assert _stored(project).nickname is None


def test_rename_takes_another_project_by_any_of_its_names(
    runner: CliRunner, tmp_path: Path
) -> None:
    other = _added(tmp_path / "api")
    first = runner.invoke(app, ["project", "rename", "payments", "-P", "api"])
    assert first.exit_code == 0, first.output
    again = runner.invoke(app, ["--json", "project", "rename", "refunds", "-P", "payments"])
    assert again.exit_code == 0, again.output
    payload = json.loads(again.stdout)
    assert (payload["id"], payload["nickname"]) == (other.id, "refunds")


def test_rename_refuses_what_it_cannot_do(runner: CliRunner, work_dir: Path) -> None:
    project = _added(work_dir)
    project_service.set_nickname(project, "kept")

    neither = runner.invoke(app, ["project", "rename"])
    assert neither.exit_code == 1 and "pass a nickname, or --clear" in neither.output
    both = runner.invoke(app, ["project", "rename", "x", "--clear"])
    assert both.exit_code == 1 and "not both" in both.output
    too_long = runner.invoke(app, ["--json", "project", "rename", "n" * 256])
    assert too_long.exit_code == 1
    assert json.loads(too_long.stdout)["error"] == "bad_nickname"
    unknown = runner.invoke(app, ["project", "rename", "x", "-P", "ghost"])
    assert unknown.exit_code == 1 and "no project matches 'ghost'" in unknown.output
    assert _stored(project).nickname == "kept"


def test_rename_of_a_directory_never_added_says_how_to_add_it(
    runner: CliRunner, work_dir: Path
) -> None:
    result = runner.invoke(app, ["project", "rename", "x"])
    assert result.exit_code == 1
    assert "is not one of your projects — add it first: aisquare project onboard" in (result.output)


def test_fleet_titles_and_the_sidebar_show_the_nickname(
    work_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from aisquare.cli import fleet as fleet_cli
    from aisquare.cli.ui import sidebar

    project = project_service.set_nickname(_added(work_dir), "Q3 [launch]")
    assert fleet_cli._project_json(project)["name"] == "Q3 [launch]"
    fleet_cli._emit_agents(project, [])
    assert capsys.readouterr().out.splitlines()[0] == "Q3 [launch]", "text, not markup"
    assert sidebar.project_name(project) == "Q3 [launch]"
    assert "🗂 Q3 [launch]" in sidebar.project_title_text(project).plain
