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


def test_a_failed_git_gives_the_project_a_working_git_does(
    repo_with_worktree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where git gave no answer, the marker walk stopped at the first marker: a linked
    worktree's own root, or a subdirectory's ``.aisquare`` inside a checkout. One directory
    was then one project while git answered and another while it timed out, and Remote kept
    whichever its first lookup said (review of #243, round 12, 2/10 and 4/10). The nearest
    ``.git`` gives git's answer, and ``team_project`` asks git once, not again for the
    fallback."""
    from aisquare.core import workspace

    repo, worktree = repo_with_worktree
    starts = [worktree, worktree / "sub", worktree / "marked", repo / "marked" / "deep"]
    for start in starts:
        start.mkdir(parents=True, exist_ok=True)
    (worktree / "marked" / ".aisquare").mkdir()
    (repo / "marked" / ".aisquare").mkdir()
    answered = [team_project(start).root for start in starts]
    assert answered == [repo.resolve()] * len(starts), "the premise: git's answer"
    asked: list[Path] = []

    def failed(start: Path) -> Path | None:
        asked.append(start)
        return None

    monkeypatch.setattr(workspace, "git_common_root", failed)
    assert [team_project(start).root for start in starts] == answered
    assert [find_project_root(start) for start in starts] == answered
    assert len(asked) == 2 * len(starts), "one git call for each lookup"


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


def _checkout_of_a_store(layout: str, tmp_path: Path) -> Path:
    """A checkout whose common directory no checkout holds: a ``--separate-git-dir`` one,
    or a linked worktree of a bare repository."""
    if layout == "separate-git-dir":
        checkout = tmp_path / "proj"
        (tmp_path / "store").mkdir()
        _git(
            "init",
            "-q",
            f"--separate-git-dir={tmp_path / 'store' / 'proj.git'}",
            str(checkout),
            cwd=tmp_path,
        )
        _git("commit", "-q", "--allow-empty", "-m", "init", cwd=checkout)
        return checkout
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", cwd=origin)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=origin)
    _git("clone", "-q", "--bare", str(origin), str(tmp_path / "repo.git"), cwd=tmp_path)
    checkout = tmp_path / "main"
    _git("worktree", "add", "-q", str(checkout), cwd=tmp_path / "repo.git")
    return checkout


@pytest.mark.parametrize(
    "layout",
    [
        "submodule-worktree",
        "moved-principal",
        "separate-git-dir",
        "bare-worktree",
        "windows-gitdir",
    ],
)
def test_a_failed_git_reads_the_layout_git_reads(
    layout: str,
    repo_with_worktree: tuple[Path, Path],
    repo_with_submodule: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read from a linked worktree's ``gitdir:`` line alone, a failed git's answer was
    ``<super>/.git/modules/<sub>`` for a submodule's worktree, whether git answered or not,
    and a directory that is not there for a worktree whose principal moved or whose
    ``gitdir:`` is a Windows path read from WSL: ``serve --stdio`` refused each as no
    project root (review of #243, round 13, 1/4). A ``--separate-git-dir`` checkout and a
    bare repository's worktree were git's store, which is no checkout (round 14, 1/3).
    Read as git reads it, each is the root a working git gives: the checkout itself."""
    from aisquare.core import workspace

    repo, worktree = repo_with_worktree
    _, sub = repo_with_submodule
    if layout == "submodule-worktree":
        start = tmp_path / "dep-wt"
        _git("worktree", "add", "-q", str(start), "-b", "wt", cwd=sub)
    elif layout == "moved-principal":
        repo.rename(tmp_path / "moved")
        start = worktree
    elif layout in ("separate-git-dir", "bare-worktree"):
        start = _checkout_of_a_store(layout, tmp_path)
    else:
        start = tmp_path / "wsl"
        start.mkdir()
        (start / ".git").write_text("gitdir: C:/Users/me/repo/.git/worktrees/wsl\n")
    assert find_project_root(start) == start.resolve(), "with git"
    monkeypatch.setattr(workspace, "git_common_root", lambda start: None)
    assert find_project_root(start) == start.resolve(), "with git failing"
    assert team_project(start).root == start.resolve()


@pytest.mark.parametrize("git", ["answers", "fails"])
@pytest.mark.parametrize("layout", ["separate-git-dir", "bare-worktree"])
def test_a_checkout_of_a_store_is_added_and_served_as_itself(
    layout: str, git: str, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given git's store for its root, ``project/add`` refused a ``--separate-git-dir``
    checkout and a bare repository's worktree as "neither a git checkout nor a directory of
    repositories", naming a directory nobody gave, and ``serve --stdio`` refused to
    activate either as no project root (review of #243, round 14, 1/3)."""
    from aisquare.core import workspace
    from aisquare.services import mcp_server
    from aisquare.services.remote_server import check_project_add_root

    checkout = _checkout_of_a_store(layout, tmp_path).resolve()
    (checkout / "src").mkdir()
    if git == "fails":
        monkeypatch.setattr(workspace, "git_common_root", lambda start: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert check_project_add_root(str(checkout / "src")) == checkout
    served: list[Path] = []
    monkeypatch.setattr(mcp_server, "run_stdio", lambda **_: served.append(Path.cwd()))
    monkeypatch.delenv("AISQUARE_TEAM_HUB", raising=False)
    monkeypatch.chdir(checkout / "src")
    result = runner.invoke(app, ["--json", "serve", "--stdio"])
    assert result.exit_code == 0, result.output
    assert served == [checkout / "src"] and team_project(None).root == checkout


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
