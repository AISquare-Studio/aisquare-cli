"""The source fingerprint is about file CONTENTS, and it never crashes on a real checkout.

Three things the first version got wrong, each of which turned real evidence stale or
unrecordable: an un-gitignored ``__pycache__`` written by the very test run being
recorded; an empty commit that changed HEAD and nothing else; a submodule entry that
was descended into as if it were a directory of this repository.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aisquare.core.source_revision import GENERATED_DIRECTORIES, source_fingerprint


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(root), *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "app.py").write_text("answer = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "one")
    return root


def test_generated_directories_never_count_even_when_not_gitignored(checkout: Path) -> None:
    before = source_fingerprint(checkout)
    for name in ("__pycache__", ".pytest_cache", ".mypy_cache"):
        assert name in GENERATED_DIRECTORIES
        (checkout / name).mkdir()
        (checkout / name / "cache.bin").write_bytes(b"\x00\x01")
    assert source_fingerprint(checkout) == before, "a test run's caches are not source"
    (checkout / "app.py").write_text("answer = 2\n")
    assert source_fingerprint(checkout) != before, "a real edit still counts"


def test_an_empty_commit_keeps_the_fingerprint_because_no_byte_changed(checkout: Path) -> None:
    before = source_fingerprint(checkout)
    _git(checkout, "commit", "-q", "--allow-empty", "-m", "nothing")
    assert source_fingerprint(checkout) == before
    _git(checkout, "checkout", "-q", "-b", "other")
    assert source_fingerprint(checkout) == before, "same bytes on another branch"


def test_a_submodule_contributes_its_recorded_commit_not_a_crash(
    checkout: Path, tmp_path: Path
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    _git(library, "init", "-q")
    (library / "lib.py").write_text("x = 1\n")
    _git(library, "add", ".")
    _git(library, "commit", "-q", "-m", "lib")
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
    # A fresh clone has the gitlink but no checked-out tree: the same identity.
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(checkout), str(clone)], check=True, capture_output=True
    )
    assert (clone / "vendor").is_dir() and not any((clone / "vendor").iterdir())
    assert source_fingerprint(clone) == populated


def test_a_git_failure_is_a_readable_error_not_a_traceback(checkout: Path) -> None:
    (checkout / ".git" / "index").write_bytes(b"not an index")
    with pytest.raises(ValueError, match="git could not list"):
        source_fingerprint(checkout)


def test_an_unreadable_source_file_is_unknown_not_certified(checkout: Path) -> None:
    secret = checkout / "secret.py"
    secret.write_text("x = 1\n")
    secret.chmod(0)
    try:
        with pytest.raises(ValueError, match="cannot read source file"):
            source_fingerprint(checkout)
    finally:
        secret.chmod(0o644)
