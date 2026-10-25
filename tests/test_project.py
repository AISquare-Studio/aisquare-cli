"""End-to-end CLI behaviour for the project group (and the workspace alias)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Default to a neutral temp working directory; tests may chdir elsewhere."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return workdir


def _json(output: str) -> Any:
    return json.loads(output)


def test_info_describes_the_cwd_project(runner: CliRunner, work_dir: Path) -> None:
    result = runner.invoke(app, ["--json", "project", "info"])
    assert result.exit_code == 0, result.output
    assert _json(result.stdout)["root"] == str(work_dir.resolve())


def test_list_empty_then_registered(runner: CliRunner) -> None:
    empty = runner.invoke(app, ["project", "list"])
    assert "No projects registered yet" in empty.stdout
    runner.invoke(app, ["context", "add", "a note", "--project"])  # registers the cwd project
    listed = runner.invoke(app, ["--json", "project", "list"])
    assert len(_json(listed.stdout)) == 1


def test_switch_pins_the_active_project_across_directories(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha, beta = tmp_path / "alpha", tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    monkeypatch.chdir(alpha)
    runner.invoke(app, ["context", "add", "alpha note", "--project"])
    monkeypatch.chdir(beta)
    runner.invoke(app, ["context", "add", "beta note", "--project"])

    switched = runner.invoke(app, ["project", "switch", "alpha"])
    assert switched.exit_code == 0, switched.output
    assert "switched to alpha" in switched.stdout

    # The pin wins over cwd (still in beta): info and context scope to alpha.
    info = runner.invoke(app, ["--json", "project", "info"])
    assert _json(info.stdout)["root"].endswith("alpha")
    listed = runner.invoke(app, ["--json", "context", "list"])
    texts = {entry["text"] for entry in _json(listed.stdout)}
    assert texts == {"alpha note"}


def test_switch_unknown_fails(runner: CliRunner) -> None:
    result = runner.invoke(app, ["project", "switch", "ghost"])
    assert result.exit_code == 1
    assert "no project matches" in result.output


def test_switch_ambiguous_fails(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "x" / "app", tmp_path / "y" / "app"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    for directory in (first, second):
        monkeypatch.chdir(directory)
        runner.invoke(app, ["context", "add", "note", "--project"])
    result = runner.invoke(app, ["project", "switch", "app"])
    assert result.exit_code == 1
    assert "matches multiple projects" in result.output


def test_link_adds_a_repo_to_the_active_project(runner: CliRunner) -> None:
    result = runner.invoke(app, ["project", "link", "git@github.com:acme/app.git"])
    assert result.exit_code == 0, result.output
    info = runner.invoke(app, ["--json", "project", "info"])
    assert _json(info.stdout)["linked_repos"] == ["git@github.com:acme/app.git"]


def test_onboard_seeds_from_ecosystem_markers(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = tmp_path / "pyproj"
    proj.mkdir()
    (proj / "pyproject.toml").touch()
    monkeypatch.chdir(proj)

    seeded = runner.invoke(app, ["--json", "project", "onboard"])
    assert seeded.exit_code == 0, seeded.output
    texts = [entry["text"] for entry in _json(seeded.stdout)["seeded"]]
    assert any("Python project" in text for text in texts)

    # Already onboarded: a second run without --refresh seeds nothing.
    again = runner.invoke(app, ["--json", "project", "onboard"])
    assert _json(again.stdout)["seeded"] == []

    listed = runner.invoke(app, ["--json", "context", "list"])
    assert any("Python project" in e["text"] for e in _json(listed.stdout))


@pytest.mark.parametrize("command", [["project", "onboard"], ["init"]])
@pytest.mark.parametrize(
    ("typed", "why"), [("main.py", "is a file, not a directory"), ("typo", "does not exist")]
)
def test_onboard_or_init_of_a_path_that_is_no_folder_registers_nothing(
    runner: CliRunner, tmp_path: Path, command: list[str], typed: str, why: str
) -> None:
    """Review of #259, round 43, 3/7: ``find_project_root`` takes a start that is not a
    directory for its own root, for a stored root whose folder is gone, and typed paths
    reached it unchecked: ``project onboard repo/src/main.py`` listed a project called
    ``main.py``, and ``repo/src/typo`` one at a path that does not exist. Refused, as
    Welcome and Remote refuse one, and nothing is registered."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    (repo / "src" / "main.py").write_text("", encoding="utf-8")
    path = repo / "src" / typed

    refused = runner.invoke(app, ["--json", *command, str(path)])
    said = runner.invoke(app, [*command, str(path)])
    listed = runner.invoke(app, ["--json", "project", "list"])

    assert refused.exit_code == 1, refused.output
    assert _json(refused.stdout) == {"error": "not_a_directory", "ref": str(path)}
    assert said.exit_code == 1 and f"✗ {path} {why}" in said.output, said.output
    assert _json(listed.stdout) == []


def test_workspace_alias_works(runner: CliRunner, work_dir: Path) -> None:
    result = runner.invoke(app, ["--json", "workspace", "info"])
    assert result.exit_code == 0, result.output
    assert _json(result.stdout)["root"] == str(work_dir.resolve())


def test_onboard_and_refresh_seed_facts_and_start_nothing_but_git(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``project onboard`` seeds one fact per marker; a plain second run seeds nothing; and
    ``--refresh`` reads the markers again and adds only a fact that is new. No run starts a
    process but the ``git rev-parse`` that finds the root, and none packs the codebase: the
    report has no snapshot, and the project's data directory no ``snapshot/``."""
    from aisquare.core import paths
    from aisquare.core.workspace import project_id_for

    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").touch()
    monkeypatch.chdir(proj)
    started: list[str] = []

    class Recorded(subprocess.Popen):  # type: ignore[type-arg]
        def __init__(self, args: Any, *rest: Any, **kwargs: Any) -> None:
            first = args[0] if isinstance(args, (list, tuple)) else str(args).split()[0]
            started.append(Path(str(first)).name)
            super().__init__(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", Recorded)

    first = runner.invoke(app, ["project", "onboard"])
    (proj / "Makefile").touch()
    plain = runner.invoke(app, ["project", "onboard"])
    refreshed = runner.invoke(app, ["--json", "project", "onboard", "--refresh"])
    again = runner.invoke(app, ["project", "onboard", "--refresh"])

    assert first.exit_code == 0, first.output
    assert "seeded 1 project fact(s):\n  - Python project (pyproject.toml).\n" in first.stdout
    # The Makefile is new, and a plain run does not read the markers: it says so rather
    # than "no new facts" (review of #399, m2).
    assert plain.stdout == "✓ already onboarded; --refresh reads the markers again\n", plain.output
    report = _json(refreshed.stdout)
    assert set(report) == {"seeded", "markers_read", "restored", "warning"}
    assert report["markers_read"] is True
    assert [entry["text"] for entry in report["seeded"]] == [
        "Has a Makefile — check its targets for build/test commands."
    ]
    assert again.stdout == "✓ onboarded — no new project facts to seed\n", (
        "a refresh never duplicates a fact, and says it read them"
    )
    assert set(started) <= {"git"}, started
    assert not (paths.project_data_dir(project_id_for(proj.resolve())) / "snapshot").exists()


def test_onboarding_a_forgotten_project_again_says_its_facts_are_back(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forget keeps a project's facts, hidden, so onboarding it again finds them. That is
    no re-run: "already onboarded" would say nothing happened while the project came back
    to the list. It says so, reads the markers as a first run does, and seeds only a
    fact that is new; a plain run after it is a re-run again (review of #399, m12)."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "package.json").touch()
    monkeypatch.chdir(proj)

    first = runner.invoke(app, ["project", "onboard"])
    forgotten = runner.invoke(app, ["project", "forget", str(proj)])
    gone = runner.invoke(app, ["--json", "project", "list"])
    back = runner.invoke(app, ["project", "onboard"])
    listed = runner.invoke(app, ["--json", "project", "list"])
    plain = runner.invoke(app, ["project", "onboard"])
    runner.invoke(app, ["project", "forget", str(proj)])
    (proj / "Makefile").touch()
    report = _json(runner.invoke(app, ["--json", "project", "onboard"]).stdout)
    newer = runner.invoke(app, ["project", "forget", str(proj)])
    (proj / "go.mod").touch()
    seeded = runner.invoke(app, ["project", "onboard"])

    assert first.stdout == "seeded 1 project fact(s):\n  - Node.js project (package.json).\n", (
        "control: a project never seen before is no comeback"
    )
    assert forgotten.exit_code == 0, forgotten.output
    assert _json(gone.stdout) == [], "control: the project really was forgotten"
    assert back.exit_code == 0, back.output
    assert back.stdout == "✓ onboarded again; its earlier facts are back\n", back.output
    assert [project["root"] for project in _json(listed.stdout)] == [str(proj.resolve())]
    assert plain.stdout == "✓ already onboarded; --refresh reads the markers again\n"
    assert report["restored"] is True
    assert report["markers_read"] is True
    assert [entry["text"] for entry in report["seeded"]] == [
        "Has a Makefile — check its targets for build/test commands."
    ], "it read the markers, and seeded only the new one"
    assert newer.exit_code == 0, newer.output
    assert seeded.stdout == (
        "✓ onboarded again; its earlier facts are back\n"
        "seeded 1 project fact(s):\n"
        "  - Go module (go.mod).\n"
    ), seeded.output


def test_a_forgotten_project_a_capture_brought_back_is_onboarded_again(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forget clears ``onboarded_at``, and a capture (a prompt there, with ``[projects]
    capture`` on) revives the row live but unlisted. Onboarding it then lists it again,
    so it is no re-run either: it says its facts are back and reads the markers. A
    captured row never onboarded has nothing back, though it holds a fact of another
    kind (review of #399, m13)."""
    from aisquare.core.entries import new_entry
    from aisquare.core.store import store_session
    from aisquare.core.workspace import project_id_for
    from aisquare.models import ProjectInfo

    def captured(root: Path, note: str | None = None) -> None:
        project = ProjectInfo(id=project_id_for(root.resolve()), root=root.resolve())
        with store_session() as store:
            store.ensure_project(project)
            if note is not None:
                store.add(new_entry(note, "project", project.id, [], "cli"))

    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "package.json").touch()
    monkeypatch.chdir(proj)
    runner.invoke(app, ["project", "onboard"])
    forgotten = runner.invoke(app, ["project", "forget", str(proj)])
    (proj / "Makefile").touch()
    captured(proj)
    gone = runner.invoke(app, ["--json", "project", "list"])
    back = runner.invoke(app, ["project", "onboard"])
    listed = runner.invoke(app, ["--json", "project", "list"])

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    (fresh / "Cargo.toml").touch()
    captured(fresh, note="Deploys from the release branch.")
    monkeypatch.chdir(fresh)
    first = runner.invoke(app, ["project", "onboard"])

    assert forgotten.exit_code == 0, forgotten.output
    assert _json(gone.stdout) == [], "control: the capture revived it unlisted"
    assert back.stdout == (
        "✓ onboarded again; its earlier facts are back\n"
        "seeded 1 project fact(s):\n"
        "  - Has a Makefile — check its targets for build/test commands.\n"
    ), back.output
    assert [project["root"] for project in _json(listed.stdout)] == [str(proj.resolve())]
    assert first.stdout == "seeded 1 project fact(s):\n  - Rust project (Cargo.toml).\n", (
        "a captured row never onboarded is a first run, not a comeback"
    )


def test_switch_says_so_when_the_state_file_is_not_a_json_object(
    runner: CliRunner, work_dir: Path
) -> None:
    """`pin_project` used to raise the JSON error itself, which `switch` reported as
    `ambiguous_project`; a refused pin has its own error now, and the file is left as it is."""
    from aisquare.core import paths

    runner.invoke(app, ["context", "add", "a note", "--project"])  # registers the cwd project
    paths.ensure_home()
    body = '["was", "a", "list"]\n'
    paths.state_path().write_text(body)

    result = runner.invoke(app, ["--json", "project", "switch", "work"])

    assert result.exit_code == 1, result.output
    assert _json(result.stdout)["error"] == "state_unwritable"
    assert paths.state_path().read_text() == body
