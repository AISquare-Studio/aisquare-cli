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


@pytest.mark.parametrize("layout", ["submodule-worktree", "moved-principal", "windows-gitdir"])
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
    project root (review of #243, round 13, 1/4). Read as git reads it, each is the root a
    working git gives, and a checkout whose ``.git`` names nothing there is itself."""
    from aisquare.core import workspace

    repo, worktree = repo_with_worktree
    _, sub = repo_with_submodule
    if layout == "submodule-worktree":
        start = tmp_path / "dep-wt"
        _git("worktree", "add", "-q", str(start), "-b", "wt", cwd=sub)
    elif layout == "moved-principal":
        repo.rename(tmp_path / "moved")
        start = worktree
    else:
        start = tmp_path / "wsl"
        start.mkdir()
        (start / ".git").write_text("gitdir: C:/Users/me/repo/.git/worktrees/wsl\n")
    assert find_project_root(start) == start.resolve(), "with git"
    monkeypatch.setattr(workspace, "git_common_root", lambda start: None)
    assert find_project_root(start) == start.resolve(), "with git failing"
    assert team_project(start).root == start.resolve()


@pytest.fixture(scope="module")
def layouts(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Each layout a checkout can have, built once with real git."""
    top = tmp_path_factory.mktemp("layouts").resolve()
    origin = top / "origin"
    origin.mkdir()
    _git("init", "-q", cwd=origin)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=origin)
    _git("clone", "-q", str(origin), "normal", cwd=top)
    _git("worktree", "add", "-q", str(top / "normal-wt"), "-b", "wt", cwd=top / "normal")
    (top / "normal" / "vendor" / ".git").mkdir(parents=True)
    (top / "store").mkdir()
    _git("init", "-q", f"--separate-git-dir={top / 'store' / 'proj.git'}", "proj", cwd=top)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=top / "proj")
    coder = top / "proj" / ".aisquare-worktrees" / "coder-1"
    _git("worktree", "add", "-q", str(coder), "-b", "coder", cwd=top / "proj")
    _git("clone", "-q", "--bare", str(origin), "repo.git", cwd=top)
    _git("worktree", "add", "-q", str(top / "main"), cwd=top / "repo.git")
    (top / "x").mkdir()
    _git("clone", "-q", "--bare", str(origin), str(top / "x" / ".git"), cwd=top)
    _git("worktree", "add", "-q", str(top / "x" / "wt"), cwd=top / "x" / ".git")
    return top


@pytest.mark.parametrize(
    ("start", "root"),
    [
        ("normal", "normal"),
        ("normal-wt", "normal"),
        ("normal/vendor", "normal"),  # an empty .git, which git walks past
        ("proj", "store/proj.git"),  # --separate-git-dir
        ("proj/.aisquare-worktrees/coder-1", "store/proj.git"),  # fleet's worktree of it
        ("main", "repo.git"),  # a bare repository's worktree
        ("x/wt", "x"),  # a worktree of a bare repository cloned to x/.git
    ],
)
def test_a_layout_has_main_s_root_whether_git_answers_or_not(
    start: str, root: str, layouts: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The project root of each layout is the one origin/main's git path gives, whether git
    answers or not. Round 14 made a ``--separate-git-dir`` checkout and a bare repository's
    worktree roots of their own where git answered, which moved every project of those
    layouts to a new id, split fleet's coder worktrees from their manager's board, and left
    ``project prune`` offering to drop the new project (review of #243, round 15, 1/5 to
    3/5); the failed-git fallback stopped at an empty ``.git`` that git walks past (5/5).
    The fallback reads exactly what :func:`git_common_root` says, and ``project prune``
    gives a worktree the principal its root is."""
    from aisquare.core import workspace

    expected = layouts / root
    for directory in (layouts / start, layouts / start / "src"):
        directory.mkdir(exist_ok=True)
        answered = workspace.git_common_root(directory)
        assert find_project_root(directory) == expected, f"{directory}, with git"
        with monkeypatch.context() as failing:
            failing.setattr(workspace, "git_common_root", lambda start: None)
            assert workspace._discovered_common_root(directory) == answered, directory
            assert find_project_root(directory) == expected, f"{directory}, with git failing"
    if (layouts / start / ".git").is_file():
        assert workspace.worktree_principal(layouts / start) == expected


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
