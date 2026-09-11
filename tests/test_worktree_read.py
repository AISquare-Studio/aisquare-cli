"""The public read-only worktree view.

Every test builds a real repository in a tmp_path and runs real git: the module
is a thin, careful wrapper around git's own output, and a fake git would only
prove that the fake matches the parser.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aisquare.core.spawn import EXCLUDED, SEAMS
from aisquare.services import worktree_read as wr


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    (root / "kept.txt").write_text("one\ntwo\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


def test_a_plain_directory_is_not_a_repository(tmp_path: Path) -> None:
    assert wr.is_repository(tmp_path) is False
    assert wr.read_diff(tmp_path) is None


def test_default_branch_falls_back_to_a_local_main(repo: Path) -> None:
    assert wr.default_branch(repo) == "main"


def test_current_branch_is_none_when_detached(repo: Path) -> None:
    assert wr.current_branch(repo) == "main"
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    git(repo, "checkout", "-q", head)
    assert wr.current_branch(repo) is None


def test_a_branch_with_no_changes_reports_nothing(repo: Path) -> None:
    git(repo, "checkout", "-qb", "feature")
    diff = wr.read_diff(repo)
    assert diff is not None
    assert diff.files == ()
    assert diff.patch is None
    assert diff.truncated is False


def test_added_modified_and_deleted_are_named_and_counted(repo: Path) -> None:
    git(repo, "checkout", "-qb", "feature")
    (repo / "new.txt").write_text("a\nb\nc\n")
    (repo / "kept.txt").write_text("one\ntwo\nthree\n")
    (repo / "gone.txt").write_text("x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add gone")
    git(repo, "rm", "-q", "gone.txt")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "work")

    diff = wr.read_diff(repo, base="main")
    assert diff is not None
    by_path = {f.path: f for f in diff.files}
    assert by_path["new.txt"].status == "added"
    assert by_path["new.txt"].additions == 3
    assert by_path["kept.txt"].status == "modified"
    assert by_path["kept.txt"].additions == 1
    assert by_path["kept.txt"].deletions == 0
    assert "gone.txt" not in by_path, "a file added and removed on the branch is not a change"
    assert diff.base == "main"
    assert diff.patch and "new.txt" in diff.patch


def test_the_comparison_is_against_the_fork_point_not_the_moving_tip(repo: Path) -> None:
    """The base branch moving on must not be attributed to this worktree."""
    git(repo, "checkout", "-qb", "feature")
    (repo / "mine.txt").write_text("mine\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "mine")

    git(repo, "checkout", "-q", "main")
    (repo / "theirs.txt").write_text("theirs\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "theirs")
    git(repo, "checkout", "-q", "feature")

    diff = wr.read_diff(repo, base="main")
    assert diff is not None
    paths = {f.path for f in diff.files}
    assert paths == {"mine.txt"}, "somebody else's commit must not read as this agent's deletion"


def test_untracked_files_are_counted_and_never_diffed(repo: Path) -> None:
    (repo / "scratch.log").write_text("noise\n")
    diff = wr.read_diff(repo)
    assert diff is not None
    assert diff.untracked == 1
    assert all(f.path != "scratch.log" for f in diff.files)
    # The index must be untouched: writing to it is what this module refuses to do.
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True
    ).stdout
    assert status.strip() == "?? scratch.log"


def test_an_oversized_patch_is_dropped_whole_and_declared(repo: Path) -> None:
    git(repo, "checkout", "-qb", "feature")
    (repo / "big.txt").write_text("line\n" * 5000)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "big")

    diff = wr.read_diff(repo, base="main", max_patch_bytes=200)
    assert diff is not None
    assert diff.patch is None, "half a unified diff is not a diff"
    assert diff.truncated is True
    assert diff.files, "the file list survives the patch being dropped"


def test_the_file_list_is_capped_and_says_so(repo: Path) -> None:
    git(repo, "checkout", "-qb", "feature")
    for i in range(5):
        (repo / f"f{i}.txt").write_text(f"{i}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "many")

    diff = wr.read_diff(repo, base="main", max_files=2)
    assert diff is not None
    assert len(diff.files) == 2
    assert diff.truncated is True


def test_binary_files_report_zero_rather_than_a_dash(repo: Path) -> None:
    git(repo, "checkout", "-qb", "feature")
    (repo / "blob.bin").write_bytes(bytes(range(256)) * 8)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "binary")

    diff = wr.read_diff(repo, base="main")
    assert diff is not None
    blob = next(f for f in diff.files if f.path == "blob.bin")
    assert blob.binary is True
    assert blob.additions == 0 and blob.deletions == 0


def test_counts_reports_dirty_and_ahead(repo: Path) -> None:
    git(repo, "checkout", "-qb", "feature")
    (repo / "kept.txt").write_text("one\ntwo\nthree\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "ahead by one")
    (repo / "kept.txt").write_text("one\ntwo\nthree\nfour\n")

    dirty, ahead = wr.counts(repo, base="main")
    assert dirty == 1
    assert ahead == 1


def test_worktree_branches_maps_each_linked_tree(repo: Path, tmp_path: Path) -> None:
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "-b", "side", str(linked))
    branches = wr.worktree_branches(repo)
    assert branches[linked.resolve()] == "side" or branches[linked] == "side"


def test_the_spawn_site_is_registered(repo: Path) -> None:
    """A git call this package makes has to be in the registry that names them."""
    seam = SEAMS["aisquare/services/worktree_read.py::_git"]
    assert seam.decision == EXCLUDED
    assert seam.strips_identity is True


def test_the_module_names_no_mutating_git_command() -> None:
    """Structural proof of the read-only claim, not a promise in a docstring."""
    source = Path(wr.__file__).read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]  # past the module docstring
    for forbidden in ('"add"', '"commit"', '"stash"', '"checkout"', '"reset"', '"clean"', '"push"'):
        assert forbidden not in body, f"{forbidden} has no business in a read-only view"
