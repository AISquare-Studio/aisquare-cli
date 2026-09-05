"""Projects (workspaces): identity, listing, switching, linking, onboarding, forgetting."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from aisquare.core import paths
from aisquare.core import snapshot as snapshot_core
from aisquare.core.entries import new_entry
from aisquare.core.store import ContextStore, store_session
from aisquare.core.workspace import (
    active_project,
    find_project_root,
    pin_project,
    project_id_for,
    worktree_principal,
)
from aisquare.models import (
    ContextEntry,
    FleetAgent,
    OnboardReport,
    ProjectForgetReport,
    ProjectInfo,
    ProjectPruneReport,
    PruneCandidate,
    Snapshot,
)

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
        project = _one_match(store, name)
    pin_project(project.id)
    return project


def _one_match(store: ContextStore, term: str) -> ProjectInfo:
    """The single registered project ``term`` names — a name, codename or id prefix.

    ``KeyError`` when nothing matches, ``ValueError`` (listing the candidates)
    when several do; the two commands that share this share its wording.
    """
    matches = store.find_projects(term)
    if not matches:
        raise KeyError(term)
    if len(matches) > 1:
        candidates = ", ".join(project.root.name for project in matches)
        raise ValueError(f"'{term}' matches multiple projects: {candidates}")
    return matches[0]


def resolve(ref: str) -> ProjectInfo:
    """The registered project ``ref`` names: an id prefix, a name, a codename or a path.

    A path is tried first whenever ``ref`` looks like one — it exists, or it
    is spelled with a separator or a leading dot — and is looked up by the id
    its resolved root would have, which is the derivation registration used;
    then, if the directory exists, by the root git resolves it to, so a
    worktree path finds its principal. A path that names no registration falls
    through to the name lookup: ``aisquare-cli`` is a directory AND a name.
    Raises ``KeyError`` when nothing matches and ``ValueError`` when several do.
    """
    with store_session() as store:
        candidate = Path(ref).expanduser()
        if candidate.exists() or "/" in ref or ref.startswith("."):
            root = candidate.resolve()
            found = store.get_project(project_id_for(root))
            if found is None and root.is_dir():
                found = store.get_project(project_id_for(find_project_root(root)))
            if found is not None:
                return found
        return _one_match(store, ref)


class ProjectBusyError(Exception):
    """The project has live fleet agents, so forgetting it would strand them."""

    def __init__(self, project: ProjectInfo, agents: list[FleetAgent]) -> None:
        labels = ", ".join(agent.label for agent in agents)
        super().__init__(
            f"{display_name(project)} has {len(agents)} live fleet agent(s): {labels} — "
            "stop them first (aisquare fleet stop <label>), or run aisquare fleet reap "
            "if they are already gone"
        )
        self.project = project
        self.agents = agents


def display_name(project: ProjectInfo) -> str:
    """How the project is named to a person: its directory, falling back to the id."""
    return project.root.name or project.id


def forget(ref: str, *, purge: bool = False) -> ProjectForgetReport:
    """Remove the registration ``ref`` names; with ``purge``, everything it owns too.

    Refused (``ProjectBusyError``) while the project has LIVE fleet agents:
    their rows are how ``fleet stop`` and ``fleet reap`` find the panes, and
    a registration with agents on it is by definition not stale.

    Without ``purge`` the registration is tombstoned and the project's
    context entries, prompt history, board rows and ended fleet-agent rows stay
    in the store, hidden — reachable again only by registering the root again.
    With ``purge`` they are deleted, and so is ``~/.aisquare/projects/<id>/``
    (the snapshot and brain).

    If the project was the ACTIVE one — pinned, or the one the working
    directory resolves to — the pin moves to the most recently touched
    remaining project, or is cleared when none remain; the report says which.
    """
    project = resolve(ref)
    with store_session() as store:
        live = store.fleet_agents(project.id, live_only=True)
        if live:
            raise ProjectBusyError(project, live)
        was_active = active_project(store).id == project.id
        removed = store.purge_project(project.id) if purge else {}
        if not purge:
            store.forget_project(project.id)
        active = _repin(store) if was_active else None
    return ProjectForgetReport(
        project=project,
        purged=purge,
        removed=removed,
        data_dir_removed=purge and _remove_data_dir(project.id),
        active=active,
        active_changed=was_active,
    )


def prune_candidates(*, missing: bool, worktrees: bool) -> list[PruneCandidate]:
    """The registrations ``project prune`` would drop, and why — nothing is changed.

    ``missing``: the root is no longer a directory on disk. ``worktrees``: the
    root is a linked git worktree whose principal repository is ITSELF a
    registered project — a worktree of an unregistered repo is kept, since it
    is the only handle on that repo's context. A live fleet agent count is
    carried so the plan can show what will be kept and why.
    """
    with store_session() as store:
        projects = store.list_projects()
        by_root = {project.root: project for project in projects}
        found: list[PruneCandidate] = []
        for project in projects:
            live = len(store.fleet_agents(project.id, live_only=True))
            if not project.root.is_dir():
                if missing:
                    found.append(
                        PruneCandidate(project=project, reason="missing", live_agents=live)
                    )
                continue
            if not worktrees:
                continue
            principal_root = worktree_principal(project.root)
            if principal_root is None:
                continue
            principal = by_root.get(principal_root) or store.get_project(
                project_id_for(principal_root)
            )
            if principal is not None and principal.id != project.id:
                found.append(
                    PruneCandidate(
                        project=project, reason="worktree", principal=principal, live_agents=live
                    )
                )
    return found


def prune_plan(candidates: list[PruneCandidate], *, purge: bool) -> ProjectPruneReport:
    """The dry-run report: what ``prune`` would drop and what it would keep."""
    return ProjectPruneReport(
        candidates=candidates,
        kept=[candidate for candidate in candidates if candidate.live_agents],
        dry_run=True,
        purged=purge,
    )


def prune(candidates: list[PruneCandidate], *, purge: bool) -> ProjectPruneReport:
    """Drop the candidates — the same forget as :func:`forget`, per registration.

    A candidate with live fleet agents is KEPT and reported rather than failing
    the whole sweep: the point of a prune over hundreds of registrations is
    that one busy project does not stop the other three hundred. Liveness is
    re-read here, not trusted from the plan, since a prompt may have sat open.
    """
    dropped: list[str] = []
    kept: list[PruneCandidate] = []
    with store_session() as store:
        active_id = active_project(store).id
        for candidate in candidates:
            project_id = candidate.project.id
            if store.get_project(project_id) is None:
                continue  # already gone since the plan was made
            live = len(store.fleet_agents(project_id, live_only=True))
            if live:
                kept.append(candidate.model_copy(update={"live_agents": live}))
                continue
            if purge:
                store.purge_project(project_id)
            else:
                store.forget_project(project_id)
            dropped.append(project_id)
        active_changed = active_id in dropped
        active = _repin(store) if active_changed else None
    if purge:
        for project_id in dropped:
            _remove_data_dir(project_id)
    return ProjectPruneReport(
        candidates=candidates,
        dropped=dropped,
        kept=kept,
        dry_run=False,
        purged=purge,
        active=active,
        active_changed=active_changed,
    )


def _repin(store: ContextStore) -> ProjectInfo | None:
    """Pin the most recently touched remaining project; clear the pin when none remain."""
    remaining = store.list_projects()
    if not remaining:
        pin_project(None)
        return None
    activity = store.project_activity()
    newest = max(remaining, key=lambda project: activity.get(project.id, ""))
    pin_project(newest.id)
    return newest


def _remove_data_dir(project_id: str) -> bool:
    """Delete ``~/.aisquare/projects/<id>/`` (snapshot, brain); False if there was none."""
    directory = paths.project_data_dir(project_id)
    if not directory.is_dir():
        return False
    try:
        shutil.rmtree(directory)
    except OSError:
        return False
    return True


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


def _ensure_snapshot(project: ProjectInfo, *, refresh: bool) -> Snapshot | None:
    """Generate (or reuse) the codebase snapshot; ``None`` if repomix is unavailable."""
    if snapshot_core.exists(project.id) and not refresh:
        return snapshot_core.load(project.id)
    try:
        return snapshot_core.generate(
            project.id, project.root, head=snapshot_core.head_sha(project.root)
        )
    except snapshot_core.RepomixUnavailableError:
        return None
    except (subprocess.SubprocessError, OSError):
        return None
