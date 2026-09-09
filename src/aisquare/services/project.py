"""Projects (workspaces): identity, listing, switching, linking and onboarding."""

from __future__ import annotations

import subprocess
from pathlib import Path

from aisquare.core import snapshot as snapshot_core
from aisquare.core.config import SnapshotSettings, load_config
from aisquare.core.entries import new_entry
from aisquare.core.store import store_session
from aisquare.core.workspace import (
    active_project,
    find_project_root,
    pin_project,
    project_id_for,
)
from aisquare.models import ContextEntry, OnboardReport, ProjectInfo, Snapshot

# Files at a project root that imply a fact worth seeding during onboarding.
_ECOSYSTEM_MARKERS: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", "Python project (pyproject.toml)."),
    ("setup.py", "Python project (setup.py)."),
    ("package.json", "Node.js project (package.json)."),
    ("Cargo.toml", "Rust project (Cargo.toml)."),
    ("go.mod", "Go module (go.mod)."),
    ("Gemfile", "Ruby project (Gemfile)."),
    ("pom.xml", "Java/Maven project (pom.xml)."),
    ("Makefile", "Has a Makefile — check its targets for build/test commands."),
    ("Dockerfile", "Containerised (Dockerfile present)."),
)


def info() -> ProjectInfo:
    """Describe the active project."""
    with store_session() as store:
        return active_project(store)


def list_projects() -> list[ProjectInfo]:
    """List all registered projects."""
    with store_session() as store:
        return store.list_projects()


def switch(name: str) -> ProjectInfo:
    """Pin the project matching ``name`` (a name or id prefix) as active.

    Raises ``KeyError`` if nothing matches and ``ValueError`` if it is ambiguous.
    """
    with store_session() as store:
        matches = store.find_projects(name)
    if not matches:
        raise KeyError(name)
    if len(matches) > 1:
        candidates = ", ".join(project.root.name for project in matches)
        raise ValueError(f"'{name}' matches multiple projects: {candidates}")
    pin_project(matches[0].id)
    return matches[0]


def link(repo: str) -> ProjectInfo:
    """Link a repository into the active project."""
    with store_session() as store:
        project = active_project(store)
        store.ensure_project(project)
        return store.add_linked_repo(project.id, repo)


def onboard(path: Path | None, *, refresh: bool) -> OnboardReport:
    """Pack the codebase into a snapshot and seed facts from ecosystem markers.

    Generates the Repomix snapshot (full pack + skeleton + index) on first run,
    or when ``refresh`` is set, and seeds one fact per detected marker. Existing
    facts are never duplicated; the snapshot is reused unless ``refresh``.
    """
    root = find_project_root(path or Path.cwd())
    project = ProjectInfo(id=project_id_for(root), root=root, linked_repos=[])
    facts = [fact for marker, fact in _ECOSYSTEM_MARKERS if (root / marker).exists()]
    seeded: list[ContextEntry] = []
    with store_session() as store:
        store.ensure_project(project)
        project_entries = store.entries("project", project_id=project.id)
        already_onboarded = any(entry.source == "onboard" for entry in project_entries)
        if not (already_onboarded and not refresh):
            existing = {entry.text for entry in project_entries}
            for fact in facts:
                if fact in existing:
                    continue
                seeded.append(
                    store.add(new_entry(fact, "project", project.id, ["onboarding"], "onboard"))
                )
    return OnboardReport(seeded=seeded, snapshot=_ensure_snapshot(project, refresh=refresh))


def snapshot_settings() -> SnapshotSettings:
    """The ``[snapshot]`` section; the defaults when the config is unreadable (fail-open)."""
    try:
        return load_config().snapshot
    except Exception:  # a broken config costs the knobs, never the onboarding
        return SnapshotSettings()


def _ensure_snapshot(project: ProjectInfo, *, refresh: bool) -> Snapshot | None:
    """Generate (or reuse) the codebase snapshot; ``None`` if repomix is unavailable."""
    if snapshot_core.exists(project.id) and not refresh:
        return snapshot_core.load(project.id)
    settings = snapshot_settings()
    try:
        return snapshot_core.generate(
            project.id,
            project.root,
            head=snapshot_core.head_sha(project.root),
            max_tokens=settings.max_tokens,
            ignore=settings.ignore,
        )
    except snapshot_core.RepomixUnavailableError:
        return None
    except (subprocess.SubprocessError, OSError):
        return None
