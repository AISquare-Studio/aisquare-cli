"""Git worktrees resolve to their principal repository, for context as well as team.

A linked worktree's ``.git`` is a *file*, so a plain marker walk stopped inside
the worktree and gave it its own project id — the repo's context pool was
invisible from a feature branch checkout, while team traffic (which already
asked git) correctly shared one board. Both paths now agree.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.orchestrator import team_project
from aisquare.core.workspace import find_project_root


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A git repo plus a linked worktree on a feature branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    worktree = tmp_path / "wt-feature"
    _git("worktree", "add", "-q", str(worktree), "-b", "feature", cwd=repo)
    return repo, worktree


def test_worktree_resolves_to_the_principal_repo(repo_with_worktree: tuple[Path, Path]) -> None:
    repo, worktree = repo_with_worktree
    assert find_project_root(worktree) == repo.resolve()


def test_context_written_in_the_repo_is_visible_from_a_worktree(
    runner: CliRunner, repo_with_worktree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, worktree = repo_with_worktree
    monkeypatch.chdir(repo)
    runner.invoke(app, ["context", "add", "MAIN-REPO-FACT", "--project"])

    monkeypatch.chdir(worktree)
    listed = runner.invoke(app, ["--json", "context", "list"])

    texts = [entry["text"] for entry in json.loads(listed.stdout)]
    assert "MAIN-REPO-FACT" in texts, "a worktree must inherit its repo's context pool"


def test_context_added_in_a_worktree_is_visible_from_the_repo(
    runner: CliRunner, repo_with_worktree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, worktree = repo_with_worktree
    monkeypatch.chdir(worktree)
    runner.invoke(app, ["context", "add", "WORKTREE-FACT", "--project"])

    monkeypatch.chdir(repo)
    listed = runner.invoke(app, ["--json", "context", "list"])

    texts = [entry["text"] for entry in json.loads(listed.stdout)]
    assert "WORKTREE-FACT" in texts


def test_context_and_team_agree_on_project_identity(
    repo_with_worktree: tuple[Path, Path],
) -> None:
    # The bug was precisely that these two disagreed inside a worktree.
    _, worktree = repo_with_worktree
    assert find_project_root(worktree) == team_project(worktree).root


@pytest.fixture
def repo_with_submodule(tmp_path: Path) -> tuple[Path, Path]:
    """A superproject with a checked-out submodule."""
    dep = tmp_path / "dep"
    dep.mkdir()
    _git("init", "-q", cwd=dep)
    _git("commit", "-q", "--allow-empty", "-m", "dep init", cwd=dep)
    superproject = tmp_path / "super"
    superproject.mkdir()
    _git("init", "-q", cwd=superproject)
    _git("commit", "-q", "--allow-empty", "-m", "super init", cwd=superproject)
    _git(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(dep),
        "dep",
        cwd=superproject,
    )
    return superproject, superproject / "dep"


def test_a_submodule_is_its_own_project_not_a_git_internal_dir(
    repo_with_submodule: tuple[Path, Path],
) -> None:
    """Inside a submodule, ``--git-common-dir`` names ``<super>/.git/modules/<name>``.

    That is git bookkeeping, not anyone's project root — scoping context there
    would file everything under a directory nobody works in. The submodule is a
    repository in its own right (its ``.git`` is a file, like a worktree's, but
    it is not a worktree of the superproject), so the marker walk must win and
    make the submodule checkout its own project.
    """
    _, sub = repo_with_submodule
    root = find_project_root(sub)
    assert ".git" not in root.parts, "a project root must never sit inside a .git dir"
    assert root == sub.resolve()


def test_team_and_context_agree_inside_a_submodule(
    repo_with_submodule: tuple[Path, Path],
) -> None:
    _, sub = repo_with_submodule
    assert team_project(sub).root == find_project_root(sub)


def test_a_plain_repo_is_still_its_own_root(tmp_path: Path) -> None:
    repo = tmp_path / "plain"
    (repo / "src").mkdir(parents=True)
    _git("init", "-q", cwd=repo)

    assert find_project_root(repo / "src") == repo.resolve()


def test_a_non_git_directory_still_falls_back_to_markers(tmp_path: Path) -> None:
    root = tmp_path / "hg-style"
    (root / ".hg").mkdir(parents=True)
    (root / "nested").mkdir()

    assert find_project_root(root / "nested") == root.resolve()


def test_a_directory_with_no_markers_resolves_to_itself(tmp_path: Path) -> None:
    bare = tmp_path / "nothing"
    bare.mkdir()

    assert find_project_root(bare) == bare.resolve()
