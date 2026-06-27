"""Project resolution from the working directory."""

from __future__ import annotations

from pathlib import Path

from aisquare.core.ids import PROJECT_PREFIX
from aisquare.core.workspace import current_project, find_project_root, project_id_for


def test_root_is_nearest_marker_ancestor(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    assert find_project_root(nested) == tmp_path.resolve()


def test_root_falls_back_to_start_without_marker(tmp_path: Path) -> None:
    assert find_project_root(tmp_path) == tmp_path.resolve()


def test_project_id_is_stable_and_path_specific(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    assert project_id_for(tmp_path) == project_id_for(tmp_path)
    assert project_id_for(tmp_path) != project_id_for(other)
    assert project_id_for(tmp_path).startswith(PROJECT_PREFIX)


def test_current_project_describes_the_resolved_root(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    project = current_project(cwd=tmp_path)
    assert project.root == tmp_path.resolve()
    assert project.id == project_id_for(tmp_path.resolve())
    assert project.linked_repos == []
