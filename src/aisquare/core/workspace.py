"""Resolve which project is active.

The active project is the one pinned by ``project switch`` (stored in
``state.json``) if there is one and it is still registered; otherwise it is
derived from the working directory by walking up to the nearest repository root.
A project's id is a stable hash of its resolved root path.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from aisquare.core import paths
from aisquare.core.ids import PROJECT_PREFIX
from aisquare.core.store import ContextStore
from aisquare.models import ProjectInfo

# Directory markers that identify a project root, nearest-first.
_ROOT_MARKERS = (".git", ".hg", ".aisquare")
_PIN_KEY = "active_project_id"


def git_common_root(start: Path) -> Path | None:
    """The principal repository root for ``start``, resolving git worktrees.

    A linked worktree's ``.git`` is a *file* pointing at the principal
    repository, so a plain marker walk stops inside the worktree and treats it
    as its own project. ``--git-common-dir`` names the shared directory
    instead, which is what makes every checkout of one repository resolve to a
    single project.

    Returns ``None`` when git is unavailable or ``start`` is not in a work
    tree, leaving callers on the marker-based fallback.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    common = result.stdout.strip()
    if not common:
        return None
    common_dir = Path(common)
    if not common_dir.is_absolute():
        common_dir = (start / common_dir).resolve()
    # <principal>/.git → <principal>; a bare repo's common dir is the repo itself.
    root = common_dir.parent if common_dir.name == ".git" else common_dir
    return root.resolve()


def find_project_root(start: Path) -> Path:
    """Return the project root for ``start``, resolving git worktrees.

    Git is asked first so that every worktree of a repository resolves to the
    principal checkout — a feature branch in ``../wt-auth`` shares the parent
    repo's context pool instead of starting empty. Falls back to the nearest
    ancestor carrying a project marker, then to ``start`` itself, so every
    directory resolves to *some* project.
    """
    start = start.resolve()
    common = git_common_root(start)
    if common is not None:
        return common
    for directory in (start, *start.parents):
        if any((directory / marker).exists() for marker in _ROOT_MARKERS):
            return directory
    return start


def project_id_for(root: Path) -> str:
    """Derive a stable project id from a resolved root path."""
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
    return PROJECT_PREFIX + digest[:24]


def current_project(cwd: Path | None = None) -> ProjectInfo:
    """Describe the project containing ``cwd`` (default: the process cwd)."""
    root = find_project_root(cwd or Path.cwd())
    return ProjectInfo(id=project_id_for(root), root=root, linked_repos=[])


def pinned_project_id() -> str | None:
    """Return the project id pinned by ``project switch``, or ``None``."""
    path = paths.state_path()
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8")).get(_PIN_KEY)
    return value if isinstance(value, str) else None


def pin_project(project_id: str | None) -> None:
    """Pin (or, with ``None``, unpin) the active project in ``state.json``."""
    paths.ensure_home()
    path = paths.state_path()
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if project_id is None:
        data.pop(_PIN_KEY, None)
    else:
        data[_PIN_KEY] = project_id
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def active_project(store: ContextStore, cwd: Path | None = None) -> ProjectInfo:
    """The pinned project if one is set and still registered, else cwd-resolved.

    When the resolved project is already registered, its stored record (with any
    linked repos) is returned rather than a bare cwd-derived one.
    """
    pinned = pinned_project_id()
    if pinned is not None:
        found = store.get_project(pinned)
        if found is not None:
            return found
    resolved = current_project(cwd)
    stored = store.get_project(resolved.id)
    return stored if stored is not None else resolved
