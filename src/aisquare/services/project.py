"""Projects (workspaces): identity, listing, switching, linking, onboarding, forgetting."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from aisquare.core import paths
from aisquare.core import snapshot as snapshot_core
from aisquare.core.config import ProjectsSettings, SnapshotSettings, load_config
from aisquare.core.entries import new_entry
from aisquare.core.names import name_flaw
from aisquare.core.state_file import StateUnwritableError
from aisquare.core.store import ContextStore, is_the_home, store_session
from aisquare.core.workspace import (
    _is_dir,
    _resolved_or_spelled,
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
    the next ask (review of #259, round 18). The default it falls back to is off, so an
    unreadable config captures nothing more: the same direction as ``capture.enabled``,
    which fails closed on purpose (``hooks.capture_prompt``, #268): both record less, never
    more.

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

    While the experimental captain is on, the aisquare home's row is the one captured row
    that may, whatever capture says: it is the captain's home board
    (``services.captain.state.home_project``), captured on purpose and never onboarded, so
    that it never joins the owner's projects. Asked like any other, the captain's session
    start registered nothing there and briefed it nothing, and its audit and its row's
    events went nowhere, with capture off (0.8.1's default). Off, it is any other captured
    row, as on main: a session started in the home was briefed on the home board once a
    row was left there (review of #240, F7).
    """
    if capture_on():
        return True
    stored = store.get_project(project.id)
    if stored is None:
        return False
    if stored.onboarded_at is not None:
        return True
    if not is_the_home(stored.root):
        return False
    from aisquare.core import experimental  # lazy: only the home's row asks the switch

    return experimental.captain_enabled()


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


def forget_command(root: Path, *, purge: bool = False) -> str:
    """``aisquare project forget [--purge] <root>``, quoted for this shell, as
    :func:`onboard_command` names an add: a prune's root-gone hint, :meth:`BoardReader.frees`
    and doctor's capture-off line each built it by hand (review of #259, round 42, 8/11)."""
    purged = ["--purge"] if purge else []
    return install_route.command_line(["aisquare", "project", "forget", *purged, str(root)])


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
    is spelled with a separator (``/`` or this platform's: a gone root that a prune's
    ``project forget`` names on Windows is ``C:\\...``) or a leading dot — and is looked
    up by the id its resolved root would have, which is the derivation registration used;
    then, if the directory exists, by the root git resolves it to, so a
    worktree path finds its principal. A path that names no registration falls
    through to the name lookup: ``aisquare-cli`` is a directory AND a name.
    Raises ``KeyError`` when nothing matches and ``ValueError`` when several do.

    A path through a symlink loop is looked up as it is spelled, made absolute: a root that
    became one is the root registration resolved, which doctor's capture-off line and
    :class:`ProjectBusyError` print for ``project forget``. Resolved raw, the loop raised
    ``RuntimeError`` on 3.11 and 3.12, and that ``forget`` ended in a traceback (review of
    #259, round 41, 2/6).

    So is a path that cannot be asked (``EACCES``, ``EIO``): ``os.path``, which says False
    for one, where ``Path.exists`` and ``Path.is_dir`` raised up to 3.13, and a checkout git
    and the markers cannot be asked of is none. Doctor and :class:`ProjectBusyError` print
    the ``forget`` of a root under a folder this user may not search, which ``prune
    --missing`` keeps, and it ended in a traceback (round 42, 4/11).

    A ``~word`` that names no home directory here is no path, and is looked up by name:
    nicknames are free text, and ``Path.expanduser`` raised ``RuntimeError`` for ``~wip``,
    which ``project forget`` and every ``--project`` let through as a traceback (round 43,
    5/7).
    """
    with store_session() as store:
        try:
            candidate = Path(ref).expanduser()
        except RuntimeError:  # "Could not determine home directory."
            return _one_match(store, ref)
        if (
            os.path.exists(candidate)
            or any(sep in ref for sep in {"/", os.sep})
            or ref.startswith(".")
        ):
            root = _resolved_or_spelled(candidate)
            found = store.get_project(project_id_for(root))
            if found is None and os.path.isdir(root):
                with suppress(OSError):  # a checkout that cannot be asked
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


class Frees(NamedTuple):
    """The commands that free a hub of one reader (:meth:`BoardReader.frees`), in order."""

    onboard: str | None
    """Its onboard, when it is forgotten: no ``fleet`` command finds it, nor ``forget``."""
    stop: str | None
    """Its ``fleet stop``, a ``<label>`` to fill in, when its agents run: ``forget`` refuses
    a project with live agents."""
    forget: str
    """Its ``project forget``: a stopped row starts on the hub again at a restart."""


BoardRows = Mapping[str, Sequence[FleetAgent]]
"""The fleet rows that hold a board (:func:`reading_rows`), by the board a restart of each
starts it on."""


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

    def frees(self) -> Frees:
        """What frees the board its rows hold, step by step, which :class:`ProjectBusyError`
        and doctor's capture-off line each word: written out in each, their rules differed,
        and a wording fix had to be made twice (review of #259, round 36, 7/9)."""
        return Frees(
            onboard=onboard_command(self.project.root) if self.forgotten else None,
            stop=fleet_command("stop", self.project) + " <label>" if self.has_live else None,
            forget=forget_command(self.project.root),
        )


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
    (``reader``'s :meth:`~BoardReader.frees`, or ``owner``'s own stop when ``None``)."""
    stopped = sum(agent.ended_at is not None for agent in agents)
    labels = ", ".join(
        agent.label + (" (stopped)" if agent.ended_at is not None else "") for agent in agents
    )
    frees = None if reader is None else reader.frees()
    ways: list[str] = []
    if frees is not None and frees.onboard is not None:
        # No fleet command finds a forgotten project (`find_projects`), so its stop would say
        # "no project matches" (review of #259, round 35, 2/8).
        ways.append(f"{display_name(owner)} is forgotten: {frees.onboard} adds it back first")
    if frees is None or frees.stop is not None:
        # `reap` is named for rows whose SERVER still answers — it refuses to end a
        # row on a server it cannot reach, and an operator following that advice
        # after a hand-run `tmux kill-server` reaped nothing, twice. The scoped
        # `fleet shutdown` is the command that can, on their word.
        shutdown = fleet_command("shutdown", owner)
        stop = fleet_command("stop", owner) + " <label>" if frees is None else frees.stop
        reap = fleet_command("reap", owner)
        ways.append(
            f"stop {'the live ones' if stopped else 'them'} first ({stop}), or run "
            f"{reap} if they are already gone; if their tmux server is gone too, "
            f"{shutdown} records them"
        )
    if frees is not None:
        # Stopped, a live one is the newest of its label still, a restart away from this
        # board: with its stop alone, the forget it advised was refused again, naming the
        # stopped row (review of #259, round 36, 1/9).
        then = "" if frees.stop is None else "then "
        ways.append(
            f"{then}forget {display_name(owner)} {'' if then else 'first '}if you are done "
            f"with its agents ({frees.forget}): a stopped one starts on this board again at a "
            "restart"
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
    readers: BoardRows | None = None,
    *,
    live: Sequence[FleetAgent] | None = None,
    dropping: Collection[str] = (),
) -> ProjectBusyError | None:
    """Why ``project`` may not be forgotten now, or ``None``: its own live fleet rows
    (``live`` when the caller read them), else another project's that read its board
    (:func:`_board_readers`, of ``readers`` when the caller read them once for many
    projects, :func:`reading_rows`; a stopped one of a project in ``dropping``, forgotten
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
    store: ContextStore, projects: Sequence[ProjectInfo], readers: BoardRows
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
    store: ContextStore,
    projects: Sequence[ProjectInfo],
    *,
    dropping: Collection[str] = (),
    readers: BoardRows | None = None,
) -> dict[str, ProjectBusyError]:
    """Why ``project forget`` of each of ``projects`` would be refused (:func:`_busy`), by id,
    for each one it would, with the projects of ``dropping`` forgotten first (as a prune
    drops them: :func:`_holds`); the rows that hold a board read once for them all
    (:func:`reading_rows`), or ``readers`` when the caller read them. For doctor's
    capture-off line, which names what frees each captured row prune keeps."""
    if not projects:
        return {}
    if readers is None:
        readers = reading_rows(store)
    refused = {
        project.id: _busy(store, project, readers, dropping=dropping) for project in projects
    }
    return {project_id: busy for project_id, busy in refused.items() if busy is not None}


def reading_rows(store: ContextStore) -> dict[str, list[FleetAgent]]:
    """The fleet rows, in every project, whose board holds a project (:func:`_board_readers`):
    the live ones, and each stopped one a ``restart`` acts on, the newest of its label
    (``ContextStore.fleet_label_heads``). By the board a restart of each starts it on
    (``fleet.restart_board``): the one it recorded (``FleetAgent.board``), but for a row from
    before its launch kept the hub that reads as its own project's and does not show it read
    it, whose replacement reads its checkout's (round 38, 3/4). Oldest first on each.

    A restart of a stopped row starts its replacement under the hub its launch recorded
    (``fleet._board_and_hub``), whose ``launch`` adds the board it joins (``team on``). Asked
    of live rows alone, a hub whose readers' agents had all stopped was forgotten, and a
    Restart of one brought it back on the list with nothing to say why (review of #259,
    round 33, 3/10).

    Grouped once here: a flat list, each lookup walked every row of every project, for each
    candidate of a prune on each pass of its holds (:func:`_holds`) and for each captured
    row doctor asks of, 1.5 M row checks a pass for 300 captured rows over 5 000 heads, at
    each ``asq`` start (round 37, 6/7)."""
    from aisquare.services import fleet as fleet_service  # lazy: fleet imports this module

    by_board: dict[str, list[FleetAgent]] = {}
    checkouts: dict[str, str] = {}
    for row in store.fleet_label_heads():
        by_board.setdefault(fleet_service.restart_board(store, row, checkouts), []).append(row)
    return by_board


def _board_readers(
    store: ContextStore,
    project: ProjectInfo,
    readers: BoardRows | None = None,
    dropping: Collection[str] = (),
) -> list[BoardReader]:
    """Every other project whose fleet rows that hold a board (:func:`reading_rows`, or
    ``readers``) read ``project``'s, as a hub's, or a restart of them would, with those rows,
    in the order of its first; ``[]`` for none. A stopped row of a project forgotten since,
    or forgotten with it (``dropping``), is none: nothing lists it to restart.

    Every one: the first alone, ``forget`` of a hub two projects read named one of them, and
    the forget it advised ended in a refusal naming the other (review of #259, round 35,
    2/8)."""
    rows = (reading_rows(store) if readers is None else readers).get(project.id, ())
    registered: dict[str, ProjectInfo | None] = {}

    def known(project_id: str) -> ProjectInfo | None:
        if project_id not in registered:
            registered[project_id] = store.get_project(project_id)
        return registered[project_id]

    reading: dict[str, list[FleetAgent]] = {}
    for row in rows:
        if row.project_id != project.id and (
            row.ended_at is None
            or (row.project_id not in dropping and known(row.project_id) is not None)
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
    readers: BoardRows | None = None,
    refused: dict[str, ProjectBusyError] | None = None,
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
    (:func:`_is_dir`). In ``store`` when the caller has one open (doctor, round 19), and with
    the rows that hold a board when it read them (``readers``, :func:`reading_rows`): doctor
    asks :func:`forget_refusals` with them too, and read them once for each (round 35, 6/8).
    With ``refused``, what holds each candidate prune keeps is put in it, by id, as
    :func:`forget_refusals` gives it: doctor asked that of the plan's held rows again, the
    hold of each worked out twice on every run (round 36, 8/9).
    """

    def plan(store: ContextStore) -> list[PruneCandidate]:
        # Never the aisquare home's row: the captain's home board, captured on purpose and
        # never onboarded, so ``--captured-only`` took it for a scratch directory once the
        # captain sat idle, and ``--purge`` deleted the captain's board and its audit
        # (review of #240, "also confirmed": prune can purge the home board).
        projects = [p for p in store.list_projects(all=True) if not is_the_home(p.root)]
        why = _why_stale(
            store,
            missing=missing,
            worktrees=worktrees,
            captured_older_than=captured_older_than,
            projects=projects,
        )
        found = [candidate for candidate in map(why, projects) if candidate is not None]
        if not found:
            return found  # the rows that read a board are read for a candidate only
        rows = reading_rows(store) if readers is None else readers
        holds = _holds(store, [c.project for c in found], rows)
        if refused is not None:
            refused.update((key, busy) for key, busy in holds.items() if busy is not None)
        return [_held(c, holds[c.project.id]) for c in found]

    if store is not None:
        return plan(store)
    with store_session() as opened:
        return plan(opened)


def _captured_cutoff(days: int | None) -> str | None:
    """The ISO stamp a ``captured`` candidate's last activity is older than, ``days`` ago;
    ``None`` when no prune asked for captured rows."""
    if days is None:
        return None
    return (datetime.now(tz=UTC) - timedelta(days=days)).isoformat()


def _why_stale(
    store: ContextStore,
    *,
    missing: bool,
    worktrees: bool,
    captured_older_than: int | None,
    projects: Sequence[ProjectInfo] | None = None,
    gone: set[str] | None = None,
) -> Callable[[ProjectInfo], PruneCandidate | None]:
    """Why a project is one ``project prune`` drops, as :func:`prune_candidates` asks it: its
    candidate, with the reason and a worktree's principal, or ``None``. Captured first
    (``captured_older_than``), then a root no longer on disk (``missing``), then a worktree
    of another registered project (``worktrees``); what a reason was not asked for is not
    read. What the rules read of the store as a whole, each project's last activity and the
    registered roots (of ``projects`` when the caller listed them), is read once: the roots on
    the first worktree a principal is looked up for. Listed up front, every :func:`prune`
    run under ``--worktrees``, which a plain prune asks, read every registration to re-read
    the few candidates it was handed (review of #259, round 39, 6/9). With ``gone``, the id of
    each project whose root it found no directory is put in it: :func:`prune` tells a
    worktree kept for its root by it, and ``stat``-ed each such root, some on network mounts,
    a second time (round 40, 6/6).

    The one place the rules are written: :func:`prune` asks them again of each candidate
    before any goes. Written out a second time there, inverted, the copies drifted, and a
    worktree whose tree went while the prompt sat open was kept as no longer stale, though
    the plain prune asks for missing roots too (review of #259, round 38, 2/4 and 4/4)."""
    activity = store.project_activity() if captured_older_than is not None else {}
    cutoff = _captured_cutoff(captured_older_than)
    by_root: dict[Path, ProjectInfo] | None = None

    def principal_at(root: Path) -> ProjectInfo | None:
        nonlocal by_root
        if by_root is None:
            listed = store.list_projects(all=True) if projects is None else projects
            by_root = {project.root: project for project in listed}
        return by_root.get(root) or store.get_project(project_id_for(root))

    def why(project: ProjectInfo) -> PruneCandidate | None:
        if (
            cutoff is not None
            and project.onboarded_at is None
            and activity.get(project.id, "") < cutoff
            and not store.has_entries(project.id)
        ):
            return PruneCandidate(project=project, reason="captured")
        if not (missing or worktrees):
            return None
        if not _is_dir(project.root):
            if gone is not None:
                gone.add(project.id)
            return PruneCandidate(project=project, reason="missing") if missing else None
        if not worktrees:
            return None
        principal_root = worktree_principal(project.root)
        if principal_root is None:
            return None
        principal = principal_at(principal_root)
        if principal is None or principal.id == project.id:
            return None
        return PruneCandidate(project=project, reason="worktree", principal=principal)

    return why


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


def prune(
    candidates: list[PruneCandidate],
    *,
    purge: bool,
    missing: bool,
    worktrees: bool,
    captured_older_than: int | None = None,
) -> ProjectPruneReport:
    """Drop the candidates — the same forget as :func:`forget`, per registration.

    A candidate :func:`forget` refuses (:func:`_busy`: its live fleet agents, or another
    project's that read its board, as a hub's) is KEPT and reported rather than failing
    the whole sweep: the point of a prune over hundreds of registrations is
    that one busy project does not stop the other three hundred. What holds it is
    re-read here, not trusted from the plan, since a prompt may have sat open, and decided
    over every candidate before any goes (:func:`_holds`), as the plan was.

    A purge purges a candidate forgotten elsewhere while the prompt sat open too: passed
    over as gone, its rows and data directory stayed, beside "✓ purged", and no later prune
    lists a forgotten row to purge it (review of #259, round 36, 5/9).

    Why each is a candidate is re-read too, before any goes, by the plan's own rules
    (:func:`_why_stale`, asked what the plan was made with): one no longer a candidate, a
    capture added on purpose or given an entry while the prompt sat open, a missing root
    back on disk, is kept, as changed since the plan (``changed``). Only its holds re-read,
    it was dropped anyway, and under ``--purge`` the entries just saved went with it, and
    its data directory (review of #259, round 37, 2/7). One that is a candidate still, for
    whichever reason the run asks for, goes, and the report gives it as the run read it: a
    worktree whose tree went meanwhile is missing now (round 38, 2/4 and 4/4). One a
    ``--purge`` finds forgotten elsewhere and no longer a candidate is left forgotten, and
    reported so (``forgotten``): counted as changed, it was said kept, though no list shows
    it (round 39, 4/9). One kept because its root went, a worktree a prune not asked for
    missing roots no longer reads as one, is reported so (``root_gone``): said "no longer
    stale", nothing told its user ``prune --missing`` drops it (round 39, 5/9). A plain run
    passes over each one forgotten elsewhere, and reports it so too (``forgotten``), apart
    from one purged: told by this run's ``--purge``, a purged row was said forgotten
    (round 41, 6/6).
    """
    dropped: list[str] = []
    kept: list[PruneCandidate] = []
    ran: dict[str, PruneCandidate] = {}
    with store_session() as store:
        active_id = active_project(store).id
        rootless: set[str] = set()
        why = _why_stale(
            store,
            missing=missing,
            worktrees=worktrees,
            captured_older_than=captured_older_than,
            gone=rootless,
        )
        # Gone since the plan was made: nothing to drop, and no hold to decide.
        here = {
            row.id: row
            for c in candidates
            if (row := store.get_project(c.project.id, include_forgotten=purge)) is not None
        }
        reread = {project_id: why(row) for project_id, row in here.items()}
        lapsed = [project_id for project_id, now in reread.items() if now is None]
        # Under --purge, one forgotten elsewhere meanwhile is left as it was found: not kept.
        # Without it, one gone from a read that leaves tombstones out is forgotten or purged,
        # and the tombstones tell which: said forgotten, a purged row read as one `project
        # list --all` shows and an onboard brings back (round 41, 6/6).
        asked = lapsed if purge else [c.project.id for c in candidates if c.project.id not in here]
        tombstoned = store.forgotten_among(asked) if asked else set()
        forgotten = [project_id for project_id in asked if project_id in tombstoned]
        changed = [project_id for project_id in lapsed if project_id not in tombstoned]
        # A worktree whose tree went is none now, and a run not asked for missing roots keeps it.
        planned = {c.project.id: c.reason for c in candidates}
        root_gone = [
            project_id
            for project_id in changed
            if planned[project_id] == "worktree" and project_id in rootless
        ]
        still = [now for now in reread.values() if now is not None]
        holds = _holds(store, [c.project for c in still], reading_rows(store)) if still else {}
        for candidate in still:
            project_id = candidate.project.id
            held = ran[project_id] = _held(candidate, holds[project_id])
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
        # One the run did not re-read as a candidate was not kept for a hold, nor its hold read
        # again: the plan's, it read `held` in --json, kept for another reason (round 40, 5/6).
        candidates=[ran.get(c.project.id) or _held(c, None) for c in candidates],
        dropped=dropped,
        kept=kept,
        changed=changed,
        forgotten=forgotten,
        root_gone=root_gone,
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


class NotAFolder(Exception):
    """A path typed for ``project onboard`` or ``init`` that names no folder to register
    (:func:`typed_root`): the message says the path and why."""


def typed_root(path: Path | None) -> Path:
    """The project root of the folder a user typed (``project onboard <path>``, ``init
    <path>``), the working directory for none; :class:`NotAFolder` unless it is a folder
    that can be looked inside, as Welcome's ``validate_path`` and Remote's
    ``check_project_add_root`` refuse one.

    ``find_project_root`` takes a start that is not a directory for its own root, a stored
    root whose folder is gone (review of #259, round 42, 3/11), and typed paths reached it
    unchecked: ``project onboard repo/src/main.py`` registered the file as a project of its
    own, whose snapshot then failed, and ``project onboard repo/src/typo`` a path that does
    not exist (round 43, 3/7). A symlink loop, or a folder this user may not look inside,
    is refused with the system's words, where the lookup's ``OSError`` was a traceback.
    """
    start = path or Path.cwd()
    try:
        if not stat.S_ISDIR(os.stat(start).st_mode):
            raise NotAFolder(f"{start} is a file, not a directory")
        return find_project_root(start)
    except (FileNotFoundError, NotADirectoryError):
        raise NotAFolder(f"{start} does not exist") from None
    except OSError as exc:
        raise NotAFolder(f"{start} cannot be looked at ({exc.strerror or exc})") from exc


HOME_WARNING = (
    "this folder is aisquare's own home: it holds aisquare's settings, the fleet database "
    "and every Claude account's login data, so agents working here can read and change them"
)
"""What adding the aisquare home as a project says, once (the owner's decision D5)."""


def onboard_on_purpose(store: ContextStore, project: ProjectInfo) -> tuple[ProjectInfo, str | None]:
    """The owner's own "add this folder" (``init``, ``project onboard``): the project, and
    :data:`HOME_WARNING` when this made the aisquare home a project, else ``None``.

    The home stays hidden by default; only this adds it (D5), and it warns rather than
    refuses. Said when the home BECOMES a project, so ``init``, which onboards and then
    runs ``project onboard``'s steps, says it once.
    """
    before = store.get_project(project.id)
    added = store.onboard_project(project, explicit=True)
    became = is_the_home(project.root) and (before is None or before.onboarded_at is None)
    return added, HOME_WARNING if became else None


def onboard(path: Path | None, *, refresh: bool, pack: bool = True) -> OnboardReport:
    """Pack the codebase into a snapshot and seed facts from ecosystem markers.

    Generates the Repomix snapshot (full pack + skeleton + index) on first run,
    or when ``refresh`` is set, and seeds one fact per detected marker. Existing
    facts are never duplicated; the snapshot is reused unless ``refresh``. ``path`` is
    the user's, refused unless it is a folder (:func:`typed_root`).

    ``pack=False`` (``init``) packs nothing and reports the pack already there, if
    any: no agent is pointed at a pack any more, so it is ``project onboard``'s, on
    demand, and not a minute of every ``init``. The aisquare home is the same either
    way: never packed, a pack left from before deleted, and ``snapshot_note`` says why.
    """
    root = typed_root(path)
    project = ProjectInfo(id=project_id_for(root), root=root, linked_repos=[])
    facts = [fact for marker, fact in _ECOSYSTEM_MARKERS if (root / marker).exists()]
    seeded: list[ContextEntry] = []
    with store_session() as store:
        _, warning = onboard_on_purpose(store, project)  # the deliberate add (#139)
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
    if not pack and not snapshot_core.is_the_homes(project.id):
        return OnboardReport(
            seeded=seeded,
            snapshot=snapshot_core.load(project.id),
            warning=warning,
            code_index=_ensure_code_index(project, refresh=refresh),
        )
    # The home takes this path asked or not: _ensure_snapshot packs nothing there, deletes
    # a pack left from before, and says why, where `init`'s opt-in line would point at a
    # `project onboard` that never packs it (D5, #42).
    snapshot, note = _ensure_snapshot(project, refresh=refresh)
    return OnboardReport(
        seeded=seeded,
        snapshot=snapshot,
        snapshot_note=note,
        warning=warning,
        code_index=_ensure_code_index(project, refresh=refresh),
    )


def _ensure_code_index(project: ProjectInfo, *, refresh: bool) -> str | None:
    """Start building or refreshing the symbol index (#285) detached, as one line for the
    report; ``None`` when ``[code] enabled`` is off. Never inline: a first index of a large
    checkout takes minutes, and the onboard (and ``init``) returns at once. Incremental
    unless ``refresh``; never fails the onboard."""
    from aisquare.services import code as code_service  # lazy: onboard imports this module
    from aisquare.services import hooks as hooks_service  # lazy: hooks imports this module

    if not code_service.settings().enabled:
        return None
    try:
        hooks_service.start_code_index(project, full=refresh)
    except Exception as exc:  # an index is optional; the onboard is not
        return f"not started ({exc}) — build it with `aisquare code index`"
    return "building in the background — `aisquare code index --status` says when it is ready"


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
    last pack is returned, with a sentence that says so.
    """
    if snapshot_core.is_the_homes(project.id):
        # Never packed, refresh or not, and a pack left from before is deleted: its
        # login data must not sit in a file agents are pointed at (owner, 2026-10-23).
        shutil.rmtree(snapshot_core.snapshot_dir(project.id), ignore_errors=True)
        return None, snapshot_core.HOME_SKIPPED
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
