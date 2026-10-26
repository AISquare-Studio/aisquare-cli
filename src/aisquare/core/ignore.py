"""What the code index (#285) leaves out of a project, and the commit it was read at.

Moved here from ``core/snapshot.py`` when #399 removed the codebase pack: the index
(``core/code_index.py``) read the pack's ignore list, and the list outlives the pack.

The list is :data:`DEFAULT_IGNORE`, then every checkout found below the root
(:func:`nested_repos`), then the operator's ``[code] ignore`` (``[snapshot] ignore``
before 0.9.0, carried over by ``core.config``).
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

from aisquare.core.spawn import git_env
from aisquare.core.workspace import git_argv

#: What no index should read whatever the repo's ``.gitignore`` says: dependency
#: trees, build output, caches, and this tool's own worktree directories. Globs in
#: which a name matches at any depth. The operator's ``[code] ignore`` EXTENDS this
#: list, and :func:`nested_repos` adds any checkout found below the root.
DEFAULT_IGNORE: tuple[str, ...] = (
    "**/node_modules/**",
    "**/.venv/**",
    "**/venv/**",
    "**/.git/**",
    "**/__pycache__/**",
    "**/dist/**",
    "**/build/**",
    "**/coverage/**",
    "**/.aisquare-worktrees/**",
    "**/*.worktrees/**",
)


def head_sha(root: Path) -> str | None:
    """Best-effort current commit of ``root``."""
    try:
        result = subprocess.run(
            git_argv("rev-parse", "HEAD"),
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=True,
            env=git_env(),
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return result.stdout.strip() or None


def nested_repos(root: Path) -> list[str]:
    """Checkouts below ``root`` — repositories or worktrees of their own — as ignore patterns.

    A sibling repo cloned into the project, a worktree directory, a vendored fork: each
    is another project's codebase, and indexing it here would put a second codebase in
    this project's index. Detected by a ``.git`` entry below the root (a directory for a
    repository, a file for a worktree or submodule) — never the root's own. The walk
    prunes :data:`DEFAULT_IGNORE` names so a ``node_modules`` tree is never descended,
    never follows symlinks, does not enter a checkout once found, and skips what it
    cannot read.
    """
    stems = [pattern.removeprefix("**/").removesuffix("/**") for pattern in DEFAULT_IGNORE]
    found: list[str] = []
    for current, dirs, files in os.walk(root, topdown=True, onerror=lambda _exc: None):
        here = Path(current)
        if here != root and (".git" in dirs or ".git" in files):
            found.append(here.relative_to(root).as_posix() + "/**")
            dirs[:] = []
            continue
        dirs[:] = [
            name for name in dirs if not any(fnmatch.fnmatchcase(name, stem) for stem in stems)
        ]
    return sorted(found)


def ignore_patterns(root: Path, extra: Sequence[str] = ()) -> list[str]:
    """What the index of ``root`` leaves out: the defaults, nested checkouts, then the
    operator's."""
    patterns = [*DEFAULT_IGNORE, *nested_repos(root)]
    for pattern in extra:
        cleaned = pattern.strip()
        if cleaned and cleaned not in patterns:
            patterns.append(cleaned)
    return patterns
