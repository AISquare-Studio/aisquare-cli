"""Resolve which project the current working directory belongs to.

This is the minimal identity needed to scope ``pool == "project"`` context: it
walks up from the working directory to the nearest repository root and derives a
stable id from that path. The full project registry (``project list/switch/
link``) builds on top of this later; for now a project *is* its root directory.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from aisquare.core.ids import PROJECT_PREFIX
from aisquare.models import ProjectInfo

# Directory markers that identify a project root, nearest-first.
_ROOT_MARKERS = (".git", ".hg", ".aisquare")


def find_project_root(start: Path) -> Path:
    """Return the nearest ancestor of ``start`` that looks like a project root.

    Falls back to ``start`` itself when no marker is found, so every directory
    resolves to *some* project.
    """
    start = start.resolve()
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
