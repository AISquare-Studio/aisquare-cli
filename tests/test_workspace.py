"""Project resolution from the working directory."""

from __future__ import annotations

from pathlib import Path

from aisquare.core.ids import PROJECT_PREFIX
from aisquare.core.store import store_session
from aisquare.core.workspace import (
    active_project,
    current_project,
    find_project_root,
    pin_project,
    pinned_project_id,
    project_id_for,
)
from aisquare.models import ProjectInfo


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


def test_pin_round_trips() -> None:
    assert pinned_project_id() is None
    pin_project("prj_abc")
    assert pinned_project_id() == "prj_abc"
    pin_project(None)
    assert pinned_project_id() is None


def test_active_project_prefers_a_registered_pin(tmp_path: Path) -> None:
    pinned = ProjectInfo(id="prj_pinned", root=tmp_path / "pinned", linked_repos=[])
    with store_session() as store:
        store.ensure_project(pinned)
        pin_project(pinned.id)
        # Even resolving from an unrelated cwd, the pin wins.
        assert active_project(store, cwd=tmp_path).id == "prj_pinned"


def test_active_project_falls_back_to_cwd(tmp_path: Path) -> None:
    with store_session() as store:
        assert active_project(store, cwd=tmp_path).id == project_id_for(tmp_path.resolve())


def test_active_project_ignores_a_stale_pin(tmp_path: Path) -> None:
    pin_project("prj_never_registered")
    with store_session() as store:
        assert active_project(store, cwd=tmp_path).id == project_id_for(tmp_path.resolve())


def test_a_handmade_project_marker_still_resolves(tmp_path: Path) -> None:
    """``<project>/.aisquare`` is an opt-in marker and must keep working."""
    (tmp_path / ".aisquare").mkdir()
    nested = tmp_path / "src"
    nested.mkdir()
    assert find_project_root(nested) == tmp_path.resolve()


def test_our_own_home_is_not_a_project_root(tmp_path: Path) -> None:
    """``~/.aisquare`` is state, not a project.

    Without this, every markerless directory under ``$HOME`` resolves to
    ``$HOME`` and shares one context pool. The home is recognised by its
    layout, so a *different* home than the configured one (what the suite
    itself creates, and what a developer's real ``~/.aisquare`` is relative to
    a temp tree) is caught too.
    """
    home = tmp_path / ".aisquare"
    home.mkdir()
    (home / "config.toml").write_text("", encoding="utf-8")
    bare = tmp_path / "scratch"
    bare.mkdir()
    assert find_project_root(bare) == bare.resolve()


def test_a_git_marker_beats_an_aisquare_home(tmp_path: Path) -> None:
    """Skipping our home must not skip a real marker in the same directory."""
    home = tmp_path / ".aisquare"
    home.mkdir()
    (home / "context.db").write_text("", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "src"
    nested.mkdir()
    assert find_project_root(nested) == tmp_path.resolve()
