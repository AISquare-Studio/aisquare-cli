"""Projects (workspaces): identity, listing, switching, linking, onboarding, forgetting."""

from __future__ import annotations

import errno
import os
import shlex
import shutil
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aisquare.core import paths
from aisquare.core import snapshot as snapshot_core
from aisquare.core.config import ProjectsSettings, SnapshotSettings, load_config
from aisquare.core.entries import new_entry
from aisquare.core.names import name_flaw
from aisquare.core.state_file import StateUnwritableError
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
    PendingRevocation,
    ProjectForgetReport,
    ProjectInfo,
    ProjectPruneReport,
    PruneCandidate,
    PruneReason,
    Snapshot,
)
from aisquare.services import install_route

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


def list_projects(*, all: bool = False) -> list[ProjectInfo]:
    """The projects added on purpose — or, with ``all``, the captured directories too (#139)."""
    with store_session() as store:
        return store.list_projects(all=all)


def capture_on() -> bool:
    """``[projects] capture``: whether a directory a hooked session merely runs in gets a
    row of its own, captured and unlisted (#139). Off by default since 0.8.1; an unreadable
    config costs the setting, never the hook, as every section's does, and is read again on
    the next ask (review of #259, round 18).

    Asked twice a SessionStart, once a prompt, and on asq's UI thread every refresh tick:
    :func:`~aisquare.core.config.load_config` parses the file again only when it changed."""
    try:
        return load_config(paths.config_path()).projects.capture
    except Exception:
        return ProjectsSettings().capture


def tracked(store: ContextStore, project: ProjectInfo) -> bool:
    """Whether an automatic path may write rows tied to ``project``: a hook's prompt, turn
    and board rows, the MCP server's session, a destination, a codename. Asked, never
    written: with capture on any directory may (:func:`capture` then captures it); off, only
    a project added on purpose may, and a directory nobody added, or a row captured before,
    may not.

    It captured the directory first when capture was on, so a caller that must not take
    the store's write lock only to ask (a session start) had to say so, while another in
    the same hook took it anyway (review of #259, round 17). The write is :func:`capture`.
    """
    if capture_on():
        return True
    stored = store.get_project(project.id)
    return stored is not None and stored.onboarded_at is not None


def capture(store: ContextStore, project: ProjectInfo) -> None:
    """CAPTURE ``project`` while ``[projects] capture`` is on, as #139 did
    (:meth:`~aisquare.core.store.SqliteStore.ensure_project`): a live, unlisted row for the
    rows an automatic path is about to write. Off, nothing: the caller asked
    :func:`tracked` first, and only an added project gets that far."""
    if capture_on():
        store.ensure_project(project)


class NotAdded(RuntimeError):
    """A deliberate action needs a project row, capture is off, and nobody added this one."""

    def __init__(self, project: ProjectInfo) -> None:
        super().__init__(
            f"{display_name(project)} is not one of your projects — add it first: "
            f"aisquare project onboard {shlex.quote(str(project.root))}"
        )
        self.project = project


def switch(name: str) -> ProjectInfo:
    """Pin the project matching ``name`` (a name or id prefix) as active.

    Pinning is choosing it on purpose, so a captured directory is onboarded
    too (#139): otherwise the ACTIVE project would be missing from ``project
    list`` — no ``*`` row — and from the sidebar.

    Raises ``KeyError`` if nothing matches and ``ValueError`` if it is ambiguous.
    """
    with store_session() as store:
        found = _one_match(store, name)
    return switch_to(found)


def switch_to(project: ProjectInfo) -> ProjectInfo:
    """:func:`switch` to a project already found: added, and pinned as active. The remote's
    ``project/switch`` finds it among the projects a phone may reach, not by
    :func:`switch`'s lookup, which names any directory the hooks captured."""
    with store_session() as store:
        project = store.onboard_project(project)
    pin_project(project.id)
    return project


def _one_match(store: ContextStore, term: str) -> ProjectInfo:
    """The single registered project ``term`` names — a name, codename, id prefix or nickname.

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
    """The registered project ``ref`` names: an id prefix, a name, a codename, a path or
    a nickname (only when nothing else matches: :meth:`ContextStore.find_projects`).

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
        # `reap` is named for rows whose SERVER still answers — it refuses to end a
        # row on a server it cannot reach, and an operator following that advice
        # after a hand-run `tmux kill-server` reaped nothing, twice. The scoped
        # `fleet shutdown` is the command that can, on their word.
        # Named by its codename, else its id, never its nickname: free text that
        # `find_projects` matches last, so `--project web` for a project nicknamed "web"
        # shut down `/work/web`'s fleet when that was registered too (review of #259,
        # round 13). Quoted for this shell all the same (review of #257).
        # The stop and the reap name it too: alone, each acts on the active project, not
        # on the one `forget` was given (review of #259, round 19).
        scope = project.codename or project.id
        shutdown = install_route.command_line(["aisquare", "fleet", "shutdown", "--project", scope])
        stop = install_route.command_line(["aisquare", "fleet", "stop", "--project", scope])
        reap = install_route.command_line(["aisquare", "fleet", "reap", "--project", scope])
        super().__init__(
            f"{display_name(project)} has {len(agents)} live fleet agent(s): {labels} — "
            f"stop them first ({stop} <label>), or run {reap} if they are already gone; if "
            f"their tmux server is gone too, {shutdown} records them"
        )
        self.project = project
        self.agents = agents


def display_name(project: ProjectInfo) -> str:
    """How the project is named to a person: its nickname, else its directory, else the id."""
    return project.nickname or project.root.name or project.id


NICKNAME_MAX = 255
"""The longest nickname :func:`set_nickname` keeps, in characters."""


def set_nickname(project: ProjectInfo, nickname: str | None) -> ProjectInfo:
    """Give ``project`` the name a person sees, or clear it: ``None`` or blank clears.

    Free text, trimmed: spaces, punctuation, any script and any emoji are kept,
    and the codename (which names the tmux session and the branches) is not
    touched. ``ValueError`` for more than :data:`NICKNAME_MAX` characters, or
    for what :func:`core.names.name_flaw` refuses in an account's alias too: a
    control character (a nickname is one line on every surface that shows it),
    a lone surrogate (bytes the terminal's encoding could not decode, which
    reached the store as a codec error), or a character that prints nothing or
    turns the text round — and nothing is written. ``KeyError`` when the
    project is not registered, or is forgotten.
    """
    text = (nickname or "").strip()
    if len(text) > NICKNAME_MAX:
        raise ValueError(
            f"a nickname is at most {NICKNAME_MAX} characters; this one has {len(text)}"
        )
    flaw = name_flaw(text)
    if flaw == "control":
        raise ValueError("a nickname is one line: no newlines, tabs or other control characters")
    if flaw == "undecodable":
        raise ValueError(
            "a nickname is text: part of this one is bytes the terminal's encoding could not decode"
        )
    if flaw == "invisible":
        raise ValueError(
            "a nickname prints as it is typed: no invisible formatting characters "
            "(a zero-width space, a right-to-left override)"
        )
    with store_session() as store:
        return store.set_project_nickname(project.id, text or None)


def forget(ref: str, *, purge: bool = False) -> ProjectForgetReport:
    """Remove the registration ``ref`` names; with ``purge``, everything it owns too.

    Refused (``ProjectBusyError``) while the project has LIVE fleet agents:
    their rows are how ``fleet stop`` and ``fleet reap`` find the panes, and
    a registration with agents on it is by definition not stale.

    Without ``purge`` the registration is tombstoned and the project's
    context entries, prompt history, board rows and ended fleet-agent rows stay
    in the store, hidden — reachable again only by registering the root again.
    With ``purge`` they are deleted, and so is ``~/.aisquare/projects/<id>/``
    (the snapshot and brain), and a key the CLI minted for it is revoked — or,
    when it cannot be yet, kept owed and named in the report
    (:func:`_revoke_owed`).

    If the project was the ACTIVE one — pinned, or the one the working
    directory resolves to — the pin moves to the most recently touched
    remaining project, or is cleared when none remain; the report says which.
    """
    return forget_project(resolve(ref), purge=purge)


def forget_project(project: ProjectInfo, *, purge: bool = False) -> ProjectForgetReport:
    """:func:`forget` of a project its caller resolved: the remote's ``project/remove``
    reads its ``ref`` without the server's working directory, which :func:`resolve` would
    read a path against."""
    with store_session() as store:
        live = store.fleet_agents(project.id, live_only=True)
        if live:
            raise ProjectBusyError(project, live)
        was_active = active_project(store).id == project.id
        removed = store.purge_project(project.id) if purge else {}
        if not purge:
            store.forget_project(project.id)
        active, pin_error = _repin(store) if was_active else (None, None)
    return ProjectForgetReport(
        project=project,
        purged=purge,
        removed=removed,
        data_dir_removed=purge and _remove_data_dir(project.id),
        active=active,
        active_changed=was_active,
        pin_error=pin_error,
        keys_still_live=_revoke_owed([project.id]) if purge else [],
    )


def prune_candidates(
    *, missing: bool, worktrees: bool, captured_older_than: int | None = None
) -> list[PruneCandidate]:
    """The registrations ``project prune`` would drop, and why — nothing is changed.

    ``missing``: the root is no longer a directory on disk. ``worktrees``: the
    root is a linked git worktree whose principal repository is ITSELF a
    registered project — a worktree of an unregistered repo is kept, since it
    is the only handle on that repo's context. ``captured_older_than``: a
    directory a session merely ran in (never added on purpose, #139) that holds
    no context entries and was last touched more than that many days ago —
    the scratch directories that pile up under a hooked Claude Code. Captured
    rows are considered for the other two reasons too: a missing root is
    missing whether or not anyone added it. A live fleet agent count is
    carried so the plan can show what will be kept and why.

    Only what a reason asked for is asked: the live agents of a candidate alone, and a
    root's ``stat`` only for ``missing`` or ``worktrees``. Doctor asks for the captured
    rows alone on every run, and got a live-agent query for every project and a ``stat`` of
    every root, some on network mounts (review of #259, round 18). A root that cannot be
    asked (``EACCES``, a mount's ``EIO``) is not called missing, and fails no plan
    (:func:`_is_dir`).
    """
    with store_session() as store:
        projects = store.list_projects(all=True)
        by_root = {project.root: project for project in projects}
        activity = store.project_activity() if captured_older_than is not None else {}
        cutoff = (
            (datetime.now(tz=UTC) - timedelta(days=captured_older_than)).isoformat()
            if captured_older_than is not None
            else None
        )
        found: list[PruneCandidate] = []

        def candidate(
            project: ProjectInfo, reason: PruneReason, principal: ProjectInfo | None = None
        ) -> None:
            live = len(store.fleet_agents(project.id, live_only=True))
            found.append(
                PruneCandidate(
                    project=project, reason=reason, principal=principal, live_agents=live
                )
            )

        for project in projects:
            if (
                cutoff is not None
                and project.onboarded_at is None
                and activity.get(project.id, "") < cutoff
                and not store.entries("project", project_id=project.id)
            ):
                candidate(project, "captured")
                continue
            if not (missing or worktrees):
                continue
            if not _is_dir(project.root):
                if missing:
                    candidate(project, "missing")
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
                candidate(project, "worktree", principal=principal)
    return found


#: What ``stat`` says of a root that is gone: nothing there, a file where a directory on its
#: path was, or a link that can never reach one.
_GONE = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})


def _is_dir(root: Path) -> bool:
    """Whether ``root`` is a directory; one that cannot be asked is taken to be one.

    ``os.stat``, not ``Path.is_dir``: that raised for ``EACCES`` up to Python 3.13, and from
    3.14 it is ``os.path.isdir``, which says False for any ``OSError``. On 3.14 a root under
    a folder this user may not search, or on a mount that answered ``EIO``, was called
    missing, and ``prune --missing`` dropped it (review of #259, round 19)."""
    try:
        return stat.S_ISDIR(os.stat(root).st_mode)
    except OSError as exc:
        return exc.errno not in _GONE


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
        active, pin_error = _repin(store) if active_changed else (None, None)
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
        pin_error=pin_error,
        keys_still_live=_revoke_owed(dropped) if purge else [],
    )


def _revoke_owed(project_ids: list[str]) -> list[PendingRevocation]:
    """Revoke the keys the CLI minted for purged projects; the ones still live.

    A purge deletes the destination row (#142) that names a minted key, and
    ``store.purge_project`` owes the key's revocation in that same transaction.
    The revokes happen here, after the store session has closed and after
    every purge of a sweep — together, under one time budget
    (``destinations.revoke_owed``): made one per project inside the loop, a
    ``prune --purge`` held the store through a 10 s timeout per keyed project
    (review of #172). Signed out, offline or signed in to another host, the
    purge is still done; what is still live is reported, and stays owed for
    the next ``use``, ``doctor --live`` or ``logout``.
    """
    if not project_ids:
        return []
    from aisquare.services import destinations, iam  # lazy: the explainability modules

    return destinations.revoke_owed(iam.signed_in_quietly(), project_ids=set(project_ids)).owed


def _repin(store: ContextStore) -> tuple[ProjectInfo | None, str | None]:
    """Pin the most recently touched remaining project (clear the pin when none remain).

    Returns the project now pinned and, when the pin could not be written, why.
    The pin is the least important thing in a forget that has already
    committed — with ``--purge`` the rows are gone and the data directory is
    about to be — so a ``state.json`` that refuses it is reported in the result
    and never raised over a purge that has already happened: raised, it told
    the operator the command failed, left the data directory orphaned (its
    registration gone, nothing left to name it), and a re-run said "no project
    matches".
    """
    remaining = store.list_projects()
    newest: ProjectInfo | None = None
    if remaining:
        activity = store.project_activity()
        newest = max(remaining, key=lambda project: activity.get(project.id, ""))
    try:
        pin_project(None if newest is None else newest.id)
    except StateUnwritableError as exc:
        return None, str(exc)
    return newest, None


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
        store.onboard_project(project)  # linking a repo is a deliberate add (#139)
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
        store.onboard_project(project)  # the deliberate add (#139)
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
    snapshot, note = _ensure_snapshot(project, refresh=refresh)
    return OnboardReport(seeded=seeded, snapshot=snapshot, snapshot_note=note)


def snapshot_settings() -> SnapshotSettings:
    """The ``[snapshot]`` section; the defaults when the config is unreadable (fail-open)."""
    try:
        return load_config().snapshot
    except Exception:  # a broken config costs the knobs, never the onboarding
        return SnapshotSettings()


def _ensure_snapshot(project: ProjectInfo, *, refresh: bool) -> tuple[Snapshot | None, str | None]:
    """Generate (or reuse) the codebase snapshot: ``(snapshot, why nothing was packed)``.

    The second half is ``None`` when a pack was made or reused. Otherwise it is the
    sentence :mod:`snapshot_core` words: the optional feature being off (no Node, or
    a Node with no packer) is not a pack that failed, and a pack that failed says
    why in the packer's own words. A failed ``--refresh`` rewrites nothing, so a
    last pack agents still get is returned, with a sentence that says so.
    """
    if snapshot_core.exists(project.id) and not refresh:
        return snapshot_core.load(project.id), None
    settings = snapshot_settings()
    failure: str | None = None
    try:
        packed = snapshot_core.generate(
            project.id,
            project.root,
            head=snapshot_core.head_sha(project.root),
            max_tokens=settings.max_tokens,
            ignore=settings.ignore,
        )
    except snapshot_core.RepomixUnavailableError:
        pass
    except (subprocess.SubprocessError, OSError) as exc:
        failure = snapshot_core.failure_reason(exc)
    else:
        return packed, None
    try:
        kept = snapshot_core.load(project.id)
    except (OSError, ValueError):  # an unreadable snapshot.json serves no agent either
        kept = None
    if kept is not None and kept.status in ("ready", "skeleton_only"):
        return kept, snapshot_core.not_refreshed_detail(kept, failure)
    return None, snapshot_core.skipped_detail(failure)
