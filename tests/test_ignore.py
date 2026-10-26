"""``core.ignore``: what the code index (#285) leaves out, and the commit it reads.

Moved from tests/test_snapshot.py with the helpers, when #399 removed the codebase pack
they were written for (#82).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aisquare.core import ignore


def test_head_sha_decodes_gits_output_as_utf8(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``subprocess`` with ``text=True`` and no explicit encoding decodes with the locale's
    codec, cp1252 on Windows, so the UTF-8 git emits could raise inside the reader thread."""
    seen: dict[str, object] = {}

    def _capture(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="deadbeef\n", stderr="")

    monkeypatch.setattr("aisquare.core.ignore.subprocess.run", _capture)

    assert ignore.head_sha(tmp_path) == "deadbeef"
    assert seen["encoding"] == "utf-8"
    assert seen["errors"] == "replace"


def test_ignore_patterns_start_with_the_defaults_and_extend_with_the_operators(
    tmp_path: Path,
) -> None:
    """The operator's list EXTENDS the built-ins: setting one pattern never loses node_modules.

    Blanks drop, whitespace is trimmed, a default repeated by the operator is not kept
    twice, and the defaults come first.
    """
    patterns = ignore.ignore_patterns(
        tmp_path, ["docs/generated/**", " **/fixtures/** ", "", "**/dist/**"]
    )
    assert patterns[: len(ignore.DEFAULT_IGNORE)] == list(ignore.DEFAULT_IGNORE)
    assert patterns[len(ignore.DEFAULT_IGNORE) :] == ["docs/generated/**", "**/fixtures/**"]
    for name in (
        "node_modules",
        ".venv",
        "venv",
        ".git",
        "__pycache__",
        "dist",
        "build",
        "coverage",
        ".aisquare-worktrees",
        "*.worktrees",
    ):
        assert f"**/{name}/**" in ignore.DEFAULT_IGNORE, name


def test_nested_repos_below_the_root_are_excluded_and_not_walked(tmp_path: Path) -> None:
    """Another project's checkout inside this one is that project's, not ours.

    A repository (``.git`` directory) and a worktree (``.git`` FILE) are both found; the
    root's own ``.git`` is not "nested"; a checkout inside a default-ignored tree is never
    visited; and a found checkout is not descended, so a repo inside a repo yields one
    pattern, not two.
    """
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "vendor" / "lib" / ".git").mkdir(parents=True)
    (root / "vendor" / "lib" / "deeper" / ".git").mkdir(parents=True)
    (root / "checkouts" / "wt").mkdir(parents=True)
    (root / "checkouts" / "wt" / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    (root / "node_modules" / "pkg" / ".git").mkdir(parents=True)
    (root / "proj.worktrees" / "feature" / ".git").mkdir(parents=True)

    assert ignore.nested_repos(root) == ["checkouts/wt/**", "vendor/lib/**"]
    assert ignore.ignore_patterns(root)[len(ignore.DEFAULT_IGNORE) :] == [
        "checkouts/wt/**",
        "vendor/lib/**",
    ]
