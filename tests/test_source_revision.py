"""The per-file source identity is about file CONTENTS, of the checkout the check ran in.

One test per fingerprint row of docs/plans/evidence-gate.md's traps table: the home tree
is refused, nested-repository and submodule content counts, the generated-directory
filter applies to untracked files only, and new files are told apart from changed ones.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from aisquare.core.source_revision import (
    GENERATED_DIRECTORIES,
    changed_paths,
    source_file_hashes,
    source_fingerprint,
    source_unchanged,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t.invalid",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(root),
            *args,
        ],
        check=True,
        capture_output=True,
    )


def _repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "one")
    return root


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    return _repo(tmp_path / "checkout", {"app.py": "answer = 1\n"})


def test_an_empty_commit_and_a_branch_switch_keep_the_identity(checkout: Path) -> None:
    before = source_fingerprint(checkout)
    _git(checkout, "commit", "-q", "--allow-empty", "-m", "nothing")
    assert source_fingerprint(checkout) == before, "HEAD moved, no byte changed"
    _git(checkout, "checkout", "-q", "-b", "other")
    assert source_fingerprint(checkout) == before, "same bytes on another branch"
    (checkout / "app.py").write_text("answer = 2\n")
    assert source_fingerprint(checkout) != before, "an uncommitted edit counts"


def test_untracked_nonignored_files_count_and_ignored_ones_do_not(checkout: Path) -> None:
    before = source_fingerprint(checkout)
    (checkout / ".gitignore").write_text("build/\n")
    _git(checkout, "add", ".gitignore")
    _git(checkout, "commit", "-q", "-m", "ignore")
    with_ignore = source_fingerprint(checkout)
    assert with_ignore != before
    (checkout / "build").mkdir()
    (checkout / "build" / "out.o").write_bytes(b"\x00")
    assert source_fingerprint(checkout) == with_ignore, "an ignored file is not source"
    (checkout / "new.py").write_text("x = 1\n")
    assert source_fingerprint(checkout) != with_ignore, "an untracked nonignored file is"


def test_the_generated_directory_filter_applies_to_untracked_files_only(tmp_path: Path) -> None:
    """Trap: the filter dropped tracked source under any path named ``venv`` and the like."""
    root = _repo(tmp_path / "repo", {"app.py": "a\n", "venv/tool.py": "tracked = 1\n"})
    before = source_fingerprint(root)
    for name in ("__pycache__", ".pytest_cache", "node_modules"):
        assert name in GENERATED_DIRECTORIES
        (root / name).mkdir()
        (root / name / "cache.bin").write_bytes(b"\x00\x01")
    (root / "venv" / "untracked.py").write_text("generated\n")
    assert source_fingerprint(root) == before, "untracked build output is not source"
    (root / "venv" / "tool.py").write_text("tracked = 2\n")
    assert source_fingerprint(root) != before, "tracked source under venv/ still counts"


def test_a_submodule_contributes_its_content_not_only_its_commit(
    checkout: Path, tmp_path: Path
) -> None:
    """Trap: edits, new files and commits inside a submodule never changed the identity."""
    library = _repo(tmp_path / "library", {"lib.py": "x = 1\n"})
    _git(
        checkout,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(library),
        "vendor",
    )
    _git(checkout, "commit", "-q", "-m", "vendor")
    populated = source_fingerprint(checkout)
    (checkout / "vendor" / "lib.py").write_text("x = 2\n")
    edited = source_fingerprint(checkout)
    assert edited != populated, "an edit inside the submodule counts"
    (checkout / "vendor" / "new.py").write_text("y = 1\n")
    assert source_fingerprint(checkout) != edited, "a new file inside the submodule counts"
    # A fresh clone has the gitlink but no checked-out tree: no crash, and a different
    # identity from the populated one.
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(checkout), str(clone)], check=True, capture_output=True
    )
    assert source_fingerprint(clone) != populated


def test_an_untracked_nested_repository_contributes_its_content(checkout: Path) -> None:
    nested = _repo(checkout / "nested", {"inner.py": "z = 1\n"})
    before = source_fingerprint(checkout)
    (nested / "inner.py").write_text("z = 2\n")
    edited = source_fingerprint(checkout)
    assert edited != before, "an edit inside the nested repository counts"
    _git(nested, "commit", "-q", "-am", "inner")
    assert source_fingerprint(checkout) == edited, "its commit alone moved no byte"
    (nested / "more.py").write_text("w = 1\n")
    assert source_fingerprint(checkout) != edited


def test_a_new_changed_or_vanished_file_is_each_a_change(checkout: Path) -> None:
    """Owner's decision on M6: no path is exempt as a check's output. A new file counts
    like an edit; one git ignores is not source at all."""
    (checkout / "other.py").write_text("b = 1\n")
    recorded = source_file_hashes(checkout)
    (checkout / "report.xml").write_text("<testsuite/>")
    assert changed_paths(recorded, source_file_hashes(checkout)) == ["report.xml"]
    (checkout / "report.xml").unlink()
    assert source_unchanged(recorded, source_file_hashes(checkout))
    (checkout / "app.py").write_text("answer = 3\n")
    assert not source_unchanged(recorded, source_file_hashes(checkout)), "an edit counts"
    (checkout / "app.py").write_text("answer = 1\n")
    assert source_unchanged(recorded, source_file_hashes(checkout))
    (checkout / "other.py").unlink()
    assert changed_paths(recorded, source_file_hashes(checkout)) == ["other.py"]


def test_a_non_git_directory_is_walked_without_its_generated_directories(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "main.c").write_text("int main;\n")
    before = source_fingerprint(root)
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    assert source_fingerprint(root) == before
    (root / "main.c").write_text("int main(void);\n")
    assert source_fingerprint(root) != before


def test_a_root_that_holds_aisquare_home_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Trap: a non-git directory under ``~`` resolved up to ``~`` and hashed all of it."""
    from aisquare.core.paths import HOME_ENV_VAR

    home = tmp_path / "home"
    (home / ".aisquare").mkdir(parents=True)
    (home / "notes.txt").write_text("a file in the home directory")
    monkeypatch.setenv(HOME_ENV_VAR, str(home / ".aisquare"))
    with pytest.raises(ValueError, match="AI Square's home"):
        source_fingerprint(home)
    with pytest.raises(ValueError, match="AI Square's home"):
        source_fingerprint(home / ".aisquare")


def test_the_user_home_directory_is_refused_wherever_aisquare_home_is(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "user"
    home.mkdir()
    (home / "notes.txt").write_text("x")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    with pytest.raises(ValueError, match="home directory"):
        source_fingerprint(home)
    project = home / "code"
    project.mkdir()
    (project / "a.py").write_text("x = 1\n")
    assert source_fingerprint(project).startswith("sha256:"), "a directory under it is fine"


def test_a_git_failure_is_a_readable_error(checkout: Path) -> None:
    (checkout / ".git" / "index").write_bytes(b"not an index")
    with pytest.raises(ValueError, match="git could not list"):
        source_fingerprint(checkout)


@pytest.mark.skipif(sys.platform == "win32", reason="chmod 0 does not deny reads on Windows")
def test_an_unreadable_source_file_is_unknown(checkout: Path) -> None:
    secret = checkout / "secret.py"
    secret.write_text("x = 1\n")
    secret.chmod(0)
    try:
        with pytest.raises(ValueError, match="cannot read source file"):
            source_fingerprint(checkout)
    finally:
        secret.chmod(0o644)
