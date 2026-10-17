"""Projects (workspaces): identity, listing, switching, linking, onboarding, forgetting."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import subprocess
from collections.abc import Collection, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

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
        return load_config(paths.config_path(), copy=False).projects.capture
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
    :func:`tracked` first, and only an added project gets that far.

    A live row there already is left unwritten: ``ensure_project`` is an upsert and a commit
    whatever it finds, and a session start with a board and CI took the store's write lock
    for it twice, the team hook's and CI's, as a fleet started its agents (review of #259,
    round 20). A forgotten row reads as none, and is revived."""
    if capture_on() and store.get_project(project.id) is None:
        store.ensure_project(project)


def onboard_command(root: Path) -> str:
    """``aisquare project onboard <root>``, quoted for this shell (``install_route.command_line``):
    ``shlex.quote`` wrapped a Windows path in single quotes, which cmd.exe passes on as part
    of the path. Every surface that says how to add a directory back names it so: the
    refusals, the Remove dialog and its toast (review of #259, round 20)."""
    return install_route.command_line(["aisquare", "project", "onboard", str(root)])


class NotAdded(RuntimeError):
    """A deliberate action needs a project row, capture is off, and nobody added this one."""

    def __init__(self, project: ProjectInfo) -> None:
        super().__init__(
            f"{display_name(project)} is not one of your projects — add it first: "
            f"{onboard_command(project.root)}"
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


def fleet_command(verb: str, project: ProjectInfo, *words: str) -> str:
    """``aisquare fleet <verb> --project <ref> [words…]`` for ``project``, quoted for this shell
    (review of #257). Always with the project: alone, a fleet command acts on the active
    project, not on the one a message is about (review of #259, round 19). ``words`` (a
    label, a flag) come after it, quoted too; a placeholder such as ``<label>`` is the
    caller's to append, unquoted.

    Named by its codename, else its id, never its nickname: free text that ``find_projects``
    matches last, so ``--project web`` for a project nicknamed "web" shut down ``/work/web``'s
    fleet when that was registered too (round 13). Nor its directory's name, which two
    captured rows share. One place for the rule: :class:`ProjectBusyError` and doctor's
    capture-off line each built it (round 20)."""
    ref = project.codename or project.id
    return install_route.command_line(["aisquare", "fleet", verb, "--project", ref, *words])


class BoardReader(NamedTuple):
    """A project whose fleet rows hold another's board, as a hub's (:func:`_board_readers`)."""

    project: ProjectInfo
    rows: list[FleetAgent]
    """Its rows that do: live, or a stopped one a ``restart`` acts on."""
    forgotten: bool
    """Registered no more (or never): no ``fleet`` command finds it, nor ``project forget``,
    until it is onboarded again. Only its live rows hold the board then."""

    @property
    def has_live(self) -> bool:
        """Whether any of :attr:`rows` is live, so a ``fleet stop`` frees it."""
        return any(row.ended_at is None for row in self.rows)


class ProjectBusyError(Exception):
    """The project has live fleet agents, or other projects' (``readers``') fleet agents
    read its board, as a hub's, live or a restart away, so forgetting it would strand them.

    Every reader, each with what frees it: named by the first one alone, the refusal of a
    hub two projects read sent the user through the first one's forget into a second
    refusal, naming the other (review of #259, round 35, 2/8)."""

    def __init__(
        self,
        project: ProjectInfo,
        agents: Sequence[FleetAgent] = (),
        *,
        readers: Sequence[BoardReader] = (),
    ) -> None:
        if readers:
            message = f"{display_name(project)} is the board " + ". It is also the board ".join(
                _read_by(reader) for reader in readers
            )
        else:
            message = f"{display_name(project)} has {len(agents)} live fleet agent(s): " + (
                _of(project, agents)
            )
        super().__init__(message)
        self.project = project
        self.agents = list(agents) or [row for reader in readers for row in reader.rows]
        self.readers = list(readers)


def _read_by(reader: BoardReader) -> str:
    """``<reader>'s <n> [live ]fleet agent(s) read: <labels> — <ways>``, of
    :class:`ProjectBusyError`."""
    stopped = any(row.ended_at is not None for row in reader.rows)
    return (
        f"{display_name(reader.project)}'s {len(reader.rows)} {'' if stopped else 'live '}"
        f"fleet agent(s) read: {_of(reader.project, reader.rows, reader=reader)}"
    )


def _of(
    owner: ProjectInfo, agents: Sequence[FleetAgent], *, reader: BoardReader | None = None
) -> str:
    """``<labels> — <ways>``: ``agents``, rows of ``owner``, and what frees the board they hold
    (``reader``, or ``owner``'s own when ``None``)."""
    stopped = sum(agent.ended_at is not None for agent in agents)
    labels = ", ".join(
        agent.label + (" (stopped)" if agent.ended_at is not None else "") for agent in agents
    )
    ways: list[str] = []
    if reader is not None and reader.forgotten:
        # No fleet command finds a forgotten project (`find_projects`), so its stop would say
        # "no project matches" (review of #259, round 35, 2/8).
        ways.append(
            f"{display_name(owner)} is forgotten: {onboard_command(owner.root)} adds it back first"
        )
    if not stopped or stopped < len(agents):
        # `reap` is named for rows whose SERVER still answers — it refuses to end a
        # row on a server it cannot reach, and an operator following that advice
        # after a hand-run `tmux kill-server` reaped nothing, twice. The scoped
        # `fleet shutdown` is the command that can, on their word.
        shutdown = fleet_command("shutdown", owner)
        stop = fleet_command("stop", owner)
        reap = fleet_command("reap", owner)
        ways.append(
            f"stop {'the live ones' if stopped else 'them'} first ({stop} <label>), or run "
            f"{reap} if they are already gone; if their tmux server is gone too, "
            f"{shutdown} records them"
        )
    if stopped and reader is not None:
        forget = install_route.command_line(["aisquare", "project", "forget", str(owner.root)])
        ways.append(
            "a restart of a stopped one starts it on this board again: forget "
            f"{display_name(owner)} first if you are done with its agents ({forget})"
        )
    return f"{labels} — " + "; ".join(ways)


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
    read a path against.

    Refused too while another project's fleet agents read its board, as a hub's
    (:func:`_board_readers`), live or stopped (a restart would start them under it again:
    round 33, 3/10): a restart or a switch of one gives its replacement that hub, whose
    ``launch`` adds the board it joins (``team on``), and a hub forgotten to tidy the list
    came back on it with nothing to say why (review of #259, round 32, 3/9). Not added,
    with ``[projects] capture`` off, the board refuses every write of theirs. Both
    refusals are :func:`_busy`'s, which :func:`prune` asks too."""
    with store_session() as store:
        busy = _busy(store, project)
        if busy is not None:
            raise busy
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


def _busy(
    store: ContextStore,
    project: ProjectInfo,
    readers: Sequence[FleetAgent] | None = None,
    *,
    live: Sequence[FleetAgent] | None = None,
    dropping: Collection[str] = (),
) -> ProjectBusyError | None:
    """Why ``project`` may not be forgotten now, or ``None``: its own live fleet rows
    (``live`` when the caller read them), else another project's that read its board
    (:func:`_board_readers`, of ``readers`` when the caller read them once for many
    projects, :func:`_reading_rows`; a stopped one of a project in ``dropping``, forgotten
    with it, is none).

    The one guard :func:`forget_project` (``project forget``, Remote's ``project/remove``)
    and :func:`prune` ask (:func:`_holds`, for a prune's every candidate at once). Asked
    only in ``forget``, ``prune --missing --purge`` of a hub whose folder held no code
    purged the board another project's live agents were claiming tasks on, and a prune
    without ``--purge`` tombstoned it, which ``forget`` refuses (review of #259, round 33,
    2/10)."""
    own = store.fleet_agents(project.id, live_only=True) if live is None else live
    if own:
        return ProjectBusyError(project, own)
    reading = _board_readers(store, project, readers, dropping)
    if reading:
        return ProjectBusyError(project, readers=reading)
    return None


def _holds(
    store: ContextStore, projects: Sequence[ProjectInfo], readers: Sequence[FleetAgent]
) -> dict[str, ProjectBusyError | None]:
    """What holds each of ``projects`` (:func:`_busy`), by id, when one prune forgets every
    one nothing holds: decided over them all, against the store as it is before any goes.

    A stopped row holds the board it read only while its project is listed to restart it,
    and a prune forgets as it goes. Asked one candidate at a time, a hub and the only
    project that read it, both ``--missing``, came out by their order: the hub first, it
    was kept, its reader dropped, and nothing held it for the next prune; the reader first,
    both went, though the plan the user confirmed kept the hub (review of #259, round 34,
    4/10). So a stopped reader holds nothing when its project goes in this prune too, and
    which go is settled first: every candidate without live agents of its own, less each
    held by a project that stays, until none changes. :func:`prune_candidates` asks this,
    and :func:`prune` again, so the plan and the run agree.
    """
    live = {project.id: store.fleet_agents(project.id, live_only=True) for project in projects}
    dropping = {project.id for project in projects if not live[project.id]}
    while True:
        holds = {
            project.id: _busy(store, project, readers, live=live[project.id], dropping=dropping)
            for project in projects
        }
        free = {project_id for project_id in dropping if holds[project_id] is None}
        if free == dropping:
            return holds
        dropping = free


def forget_refusals(
    store: ContextStore, projects: Sequence[ProjectInfo], *, dropping: Collection[str] = ()
) -> dict[str, ProjectBusyError]:
    """Why ``project forget`` of each of ``projects`` would be refused (:func:`_busy`), by id,
    for each one it would, with the projects of ``dropping`` forgotten first (as a prune
    drops them: :func:`_holds`); the rows that hold a board read once for them all. For
    doctor's capture-off line, which names what frees each captured row prune keeps."""
    if not projects:
        return {}
    readers = _reading_rows(store)
    refused = {
        project.id: _busy(store, project, readers, dropping=dropping) for project in projects
    }
    return {project_id: busy for project_id, busy in refused.items() if busy is not None}


def _reading_rows(store: ContextStore) -> list[FleetAgent]:
    """The fleet rows, in every project, whose recorded board holds a project
    (:func:`_board_readers`): the live ones, and each stopped one a ``restart`` acts on, the
    newest of its label (``ContextStore.fleet_label_heads``).

    A restart of a stopped row starts its replacement under the hub its launch recorded
    (``fleet._board_and_hub``), whose ``launch`` adds the board it joins (``team on``). Asked
    of live rows alone, a hub whose readers' agents had all stopped was forgotten, and a
    Restart of one brought it back on the list with nothing to say why (review of #259,
    round 33, 3/10)."""
    return store.fleet_label_heads()


def _board_readers(
    store: ContextStore,
    project: ProjectInfo,
    readers: Sequence[FleetAgent] | None = None,
    dropping: Collection[str] = (),
) -> list[BoardReader]:
    """Every other project whose fleet rows that hold a board (:func:`_reading_rows`, or
    ``readers``) recorded ``project``'s, as a hub's (``FleetAgent.board``), with those rows,
    in the order of its first; ``[]`` for none. A stopped row of a project forgotten since,
    or forgotten with it (``dropping``), is none: nothing lists it to restart.

    Every one: the first alone, ``forget`` of a hub two projects read named one of them, and
    the forget it advised ended in a refusal naming the other (review of #259, round 35,
    2/8)."""
    rows = _reading_rows(store) if readers is None else readers
    registered: dict[str, ProjectInfo | None] = {}

    def known(project_id: str) -> ProjectInfo | None:
        if project_id not in registered:
            registered[project_id] = store.get_project(project_id)
        return registered[project_id]

    reading: dict[str, list[FleetAgent]] = {}
    for row in rows:
        if (
            row.project_id != project.id
            and (row.board or row.project_id) == project.id
            and (
                row.ended_at is None
                or (row.project_id not in dropping and known(row.project_id) is not None)
            )
        ):
            reading.setdefault(row.project_id, []).append(row)
    found: list[BoardReader] = []
    for project_id, theirs in reading.items():
        info = known(project_id)
        if info is None:
            info = store.get_project(project_id, include_forgotten=True) or ProjectInfo(
                id=project_id, root=theirs[0].cwd
            )
        found.append(BoardReader(info, theirs, forgotten=known(project_id) is None))
    return found


def prune_candidates(
    *,
    missing: bool,
    worktrees: bool,
    captured_older_than: int | None = None,
    store: ContextStore | None = None,
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
    (:func:`_is_dir`). In ``store`` when the caller has one open (doctor, round 19).
    """

    def plan(store: ContextStore) -> list[PruneCandidate]:
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
            found.append(PruneCandidate(project=project, reason=reason, principal=principal))

        for project in projects:
            if (
                cutoff is not None
                and project.onboarded_at is None
                and activity.get(project.id, "") < cutoff
                and not store.has_entries(project.id)
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
        if not found:
            return found  # the rows that read a board are read for a candidate only
        holds = _holds(store, [c.project for c in found], _reading_rows(store))
        return [_held(c, holds[c.project.id]) for c in found]

    if store is not None:
        return plan(store)
    with store_session() as opened:
        return plan(opened)


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


def _held(candidate: PruneCandidate, busy: ProjectBusyError | None) -> PruneCandidate:
    """``candidate`` with what holds it (:func:`_busy`): its live agents, or the projects whose
    agents read its board; as it was when nothing does."""
    if busy is None:
        return candidate.model_copy(update={"live_agents": 0, "read_by": []})
    if busy.readers:
        read_by = [reader.project for reader in busy.readers]
        return candidate.model_copy(update={"live_agents": 0, "read_by": read_by})
    return candidate.model_copy(update={"live_agents": len(busy.agents), "read_by": []})


def prune_plan(candidates: list[PruneCandidate], *, purge: bool) -> ProjectPruneReport:
    """The dry-run report: what ``prune`` would drop and what it would keep."""
    return ProjectPruneReport(
        candidates=candidates,
        kept=[candidate for candidate in candidates if candidate.held],
        dry_run=True,
        purged=purge,
    )


def prune(candidates: list[PruneCandidate], *, purge: bool) -> ProjectPruneReport:
    """Drop the candidates — the same forget as :func:`forget`, per registration.

    A candidate :func:`forget` refuses (:func:`_busy`: its live fleet agents, or another
    project's that read its board, as a hub's) is KEPT and reported rather than failing
    the whole sweep: the point of a prune over hundreds of registrations is
    that one busy project does not stop the other three hundred. What holds it is
    re-read here, not trusted from the plan, since a prompt may have sat open, and decided
    over every candidate before any goes (:func:`_holds`), as the plan was.
    """
    dropped: list[str] = []
    kept: list[PruneCandidate] = []
    with store_session() as store:
        active_id = active_project(store).id
        # Gone since the plan was made: nothing to drop, and no hold to decide.
        here = [c for c in candidates if store.get_project(c.project.id) is not None]
        holds = _holds(store, [c.project for c in here], _reading_rows(store)) if here else {}
        for candidate in here:
            project_id = candidate.project.id
            held = _held(candidate, holds[project_id])
            if held.held:
                kept.append(held)
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
