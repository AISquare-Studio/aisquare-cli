"""A fleet row's launch, sealed where its sandboxed agent cannot write it.

The boundary (docs/coding-agents.md, "The trust boundary"). A sandboxed agent is trusted
to the extent of the tree it may write, as a Claude Code agent (no OS sandbox) and a human
contributor are: what the user's own tools later run from that tree is inside that trust,
documented, not sealed (git hooks that live in it and git config its repository includes
from it, a project's own venv, a subdirectory's settings, what the user's own ``aisquare
project onboard`` packs it with, ``CLAUDE.md``/``AGENTS.md``), and so is the launch
environment's ``PATH``: the ``git`` Codex itself runs outside its sandbox, and the
``git``, ``gbrain`` and ``tmux`` aisquare's hooks and broker run there, are the first
``PATH`` names, and a launch whose ``PATH`` names a directory its sandbox may write (an
empty or relative entry is the directory a process runs in) is warned of it
(``store_broker.sandbox_environment``), as is a doctor run in a project whose tree ``PATH``
names (review r13 of #113, finding 1; r14, finding 3). So is an unsandboxed agent's own
environment, which its hooks run in as its tools do: a Claude Code agent's hooks read its
``PYTHONPATH``, as every Python it runs does. What aisquare itself runs or reads outside
the sandbox on a sandboxed agent's behalf (its hooks, its broker and the commands it
runs) and for the fleet (a window's ``aisquare launch``, a hand-over) is kept from that
tree by these means, and only these: in the store's profile, which every sandboxed
launch of aisquare's runs under unless the operator picks a native ``--sandbox`` (which
replaces it, and these paths with it), the store and these launch records are read-only,
all of it but the launch's broker inbox
(:func:`~aisquare.core.agent_adapters.types.store_read_only`), and so are the launch
inputs at the tops of its trees, ``.git``, the other agents' worktrees and the tmux
sockets (``services.store_broker.launch_paths``); whatever the sandbox, a launch is
refused, by ``aisquare launch``, by ``fleet spawn`` before any window, and by a restart,
switch or hand-over before it stops the agent, where its sandbox may write an aisquare
that runs outside it (the launcher's own, or what its hooks run: their config's program,
or what the plugin's launcher finds as its own ``_find`` does, where its hooks run at
all: trusted, or under ``--dangerously-bypass-hook-trust``), with that aisquare's
environment (by what holds its code, not its prefix) and the user site an ``-E``
interpreter loads, or a directory inside one of them (a root given under it), as read
from what that sandbox may write (the system's, the user's and the managed config, its
profile, its arguments:
``store_broker.refuse_inside``, ``AgentAdapter.sandbox_writes``); its Python ignores
``PYTHONPATH`` and ``PYTHONHOME`` (``-E``: ``selfcli.isolated_argv_for``, and the hooks
``agents.install_hooks`` writes for an agent that sandboxes its tools, and the plugin's
launcher, which unsets them), while the agent keeps them as the operator set them for its
own tools (r14, finding 1, by the manager's decision); every git aisquare runs runs no hook and no
fsmonitor of the repository's (``core.workspace.git_argv``); gbrain starts in its brain's
home, never in the tree (``core.brain``); and the agent's board calls go through its
launch's broker (``services.store_broker``).

Before the broker every board call opened the store for writing, and a prompt-injected
Codex coder could rewrite any row in it, its own and every other agent's; what follows
was the defence then, and stays as defence in depth. No value read from the store may
choose what runs outside the sandbox: an executable, a native argument, the directory or
the home an unsandboxed process starts in, the conversation it resumes, or the role,
agent, worktree, account or permission a replacement runs with (review r2 of #113,
finding 3; r3, findings 1, 2, 3 and 5; r4, findings 1, 2, 5, 6 and 10).

Sealing the store's columns one by one did not converge: each round left the next one
open. So a fleet row's launch is ONE record (:class:`LaunchRecord`), written in
:func:`seals_dir`, which the store profile keeps read-only
(:func:`~aisquare.core.agent_adapters.types.store_read_only`):

- ``fleet spawn`` writes it before the window starts, from what it resolved itself:
  the project and its directory, the role, the agent, the binary, the working
  directory and whether it is a worktree, the account (its slot and its home) and the
  launch spec (permission mode, arguments, the window's command line).
- The agent's own hook adds its session (:class:`SessionFacts`: the id, the native id,
  the transcript and the home), from the payload the agent handed it, in a process
  outside any sandbox (:func:`observe`, :func:`adopted`): Claude Code has none, and
  Codex runs its hooks outside the one it gives its tools (measured on 0.162.1). A
  tool in the sandbox that runs a hook itself cannot write here.
- Once tmux has made the window, the spawner adds where it is (:class:`WindowPlace`:
  the server's socket, its name and path, and the pane), in a file of its own beside
  the record (:func:`placed`), so it and the agent's hook never write over each other.
- ``fleet restart``, ``fleet switch``, the usage-limit hand-over and ``fleet tell``'s
  queue read that record and nothing else from the store for what they run. A row
  with no record, or one that cannot be read, is refused with the reason, and so is a
  project whose directory in the store is not the record's.
- Every tmux question or act about a row's window — ``fleet tell`` and a spawn's
  prompt, ``fleet stop`` and with it restart, switch, the hand-over and ``shutdown``,
  whether it runs, the listings and ``reap``, its lingering and late windows, the
  sessions ``shutdown`` takes down, ``rename``, ``attach``, the manager's nudge — goes
  through ONE resolver (``services.fleet.resolve_place``) to the pane and the session
  (by tmux's own id, :attr:`WindowPlace.session_id`) its record places, by its
  record's agent's rules, never to the row's ``pane_id`` at its ``socket_path``, by its
  ``agent``, or by the project's codename: a sandbox that rewrote those pointed them at
  any pane of the user's (review r5 of #113, finding 1), skipped the check that keeps
  an Enter out of Codex's dialogs (r6, finding 1), or had another project's session
  taken down (r6, finding 7; r7, finding 2). ``tests/test_fleet_placement.py``
  enumerates every tmux call and holds it to that. A rewritten row is told nothing and
  stopped where its record places it (r6, finding 4), its label and its ``ended_at``
  deciding nothing (r8, findings 2, 5 and 6). The sessions a project's spawns made are
  kept here too (:func:`project_sessions`): what later spawns join and ``shutdown``
  takes down, whatever rows are left (r8, findings 2 and 4).
- A row spawned before records were kept is placed by the census (:func:`take_census`),
  taken once, while no record was written here, so before anything sandboxed could have
  written the store; it is then a row like any other, but that its replay starts fresh.
  A row with no record that the census did not find is told nothing, and only a dead
  window of its is removed (r8, findings 1, 3 and 9).
- What the fleet reads of a session to act on outside the sandbox — a failed turn's
  limit, the hand-over it starts — is read from the transcript the record holds, never
  the board session's (r5, finding 2).

Nothing scans the store to mint a record but the census, once, before any record was
kept (above): a row whose record is lost later is refused, naming the way out. What a
record holds is read back as it was written, never compared with a digest of today's
model, so a release that adds a field reads an older record with that field's default.

Validated, besides: a native session id goes on a command line only in the shape the
agent makes it (:func:`native_id`; ``codex resume`` and ``codex queue`` read one that
starts with ``-`` as an option), even from the record. A record's file is named by the
digest of the row's id, never by the id itself, which the store chose.

Read-only, not unreadable: a path Codex's profile denies reading sends Codex's own reads
of its instructions through its sandbox helper, and where bwrap cannot bring up loopback
(a GitHub runner) the agent did not start at all (CI on ca6d43ea).
"""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from aisquare.core import orchestrator, paths
from aisquare.core.atomic import write_replacing
from aisquare.core.locking import holding
from aisquare.models import LaunchSpec

_log = logging.getLogger(__name__)
DIR_NAME = "launch-seals"
VERSION = 1
"""The version of :class:`LaunchRecord` this aisquare writes; a newer one is refused."""

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


class SessionFacts(BaseModel):
    """The agent session a replay resumes, as the agent's own hook was handed it."""

    id: str
    """The board's id for the session (Claude Code's own; another agent's key)."""
    agent: str
    """The adapter id of the agent whose hook it was."""
    native_id: str | None = None
    """The agent's own id for it (``codex resume <id>``)."""
    transcript: str | None = None
    """Its transcript, by the path the agent gave (``claude --resume <path>``)."""
    home: str | None = None
    """The config dir it runs under, where its native queue is (``CODEX_HOME``)."""
    started_at: datetime | None = None
    """When the agent's own hook first said this session started under THIS launch (its
    start, or a prompt): ``None`` while the agent has not come up, at a dialog of its own
    that fires no hook. Written only by the hook (:func:`observe`, :func:`adopted`), never
    carried from a predecessor's record, and never read from the board's session row,
    whose ``last_seen_at`` an ``end_session`` stamps with its own time (review r7 of #113,
    finding 5)."""
    seam_persona: str | None = None
    """The persona the launch that BEGAN this session passed (``""``: none), carried into
    the record of each launch that resumes it (``services.fleet._respawn``): an agent whose
    resume keeps the system prompt its thread began with (Codex's) holds that one in its
    system seam, whatever a later launch's ``--persona``. ``None`` while the record's own
    launch began it, whose ``--persona`` it is."""


class WindowPlace(BaseModel):
    """Where a spawn's window is, as tmux answered the spawner: what every keystroke the
    fleet types into the row's pane, and every kill of its window, is aimed at."""

    tmux_socket: str
    """The socket's name (``tmux -L``), as the fleet's config named it."""
    pane_id: str
    socket_path: str | None = None
    """Where the server said it listened (``tmux -S``), when it said."""
    boot_id: str | None = None
    created_at: datetime
    """The row's own: after the window existed, so a server started later holds none of
    its panes (``services.fleet._outlived``)."""
    server_started_at: datetime | None = None
    """When the server the window was made on started, as tmux answered the spawner: the
    identity of the server its pane id names, since every server numbers from ``%0``. A
    server at the socket that started at any other time is another
    (``services.fleet._started_after``); ``None`` in a record written before it was kept,
    judged by :attr:`created_at` alone."""
    session: str | None = None
    """The name of the tmux session the window was made in, as tmux answered (``fleet
    rename`` renames it with the session); for reports and ``fleet attach``'s line."""
    session_id: str | None = None
    """tmux's own id for that session (``$3``), as it answered the spawner: what a spawn
    joins, ``shutdown`` takes down and ``fleet attach`` attaches to, never a session named
    from the project's codename in the store, nor by a name another session can take after
    a rename (review r6 of #113, finding 7; r7, finding 2). Asked only on the server whose
    start is not later than :attr:`created_at`, where the id is still this session's.
    ``None`` in a record written before it was kept: that session is not joined or taken
    down as a whole, its windows only."""


class LaunchRecord(BaseModel):
    """Everything a replay of one fleet row starts outside any sandbox with."""

    version: int = VERSION
    id: str
    project_id: str
    project_root: str
    role: str
    agent: str
    binary: str
    cwd: str
    worktree: bool = False
    account_slot: int | None = None
    account_home: str | None = None
    """The config dir the launch resolved for the account, when it resolved one."""
    spec: LaunchSpec
    session: SessionFacts | None = None
    window: WindowPlace | None = None
    """Where its window is (:func:`placed`); ``None`` until tmux made it, and for a record
    written by an aisquare that kept no such thing."""
    label: str | None = None
    """The row's label as its spawn recorded it (a label re-picked on a collision
    included): who ``fleet stop <label>`` and ``fleet tell <label>`` mean, never the row's
    ``label`` column, which a sandbox can swap with another row's (review r8 of #113,
    finding 5). ``None`` in a record written before it was kept."""
    legacy: bool = False
    """Made by the census (:func:`take_census`) for a row spawned before launch records
    were kept, from the row as it stood then, before any sandboxed agent could write the
    store: its window is that row's own, its agent starts fresh on a replay (no session
    is held for it), and its session is the one tmux says holds its pane."""


class SessionPlace(BaseModel):
    """A tmux session one of a project's spawns made, as tmux answered it (its id, its
    name) and where (the socket's name and path): what later spawns join and ``shutdown``
    takes down, whatever rows are left (:func:`project_sessions`)."""

    tmux_socket: str
    socket_path: str | None = None
    session_id: str
    name: str
    created_at: datetime
    """When it was recorded: a server that started later holds none of it."""
    server_started_at: datetime | None = None
    """When its server started, as tmux answered (:attr:`WindowPlace.server_started_at`)."""


class WindowEntry(BaseModel):
    """A window a spawn of the project made, for which row, as tmux answered the spawner:
    how the fleet tells which of the project's rows run from the panes alone, whatever
    their ``ended_at`` says (review r9 of #113, findings 5 and 6)."""

    row_id: str
    tmux_socket: str
    socket_path: str | None = None
    session_id: str
    pane_id: str
    created_at: datetime
    server_started_at: datetime | None = None
    """When its server started, as tmux answered (:attr:`WindowPlace.server_started_at`)."""


class ProjectSessions(BaseModel):
    """The sessions a project's spawns made, and the windows they made in them, kept beside
    the launch records, read-only to an agent's sandbox: which sessions are the project's
    and which of its rows run, never worked out from its rows' ``ended_at`` or age, store
    columns (review r8 of #113, findings 2 and 4; r9, findings 5 and 6)."""

    version: int = VERSION
    project_id: str
    sessions: list[SessionPlace] = Field(default_factory=list)
    windows: list[WindowEntry] = Field(default_factory=list)


class LegacyCensus(BaseModel):
    """The rows that had no launch record when this aisquare first read the fleet, each
    as a launch record made from the row as it stood then (:attr:`LaunchRecord.legacy`).

    Taken once (:func:`take_census`), by the first fleet read or spawn outside any
    sandbox and before any agent that sandboxes its tools is launched, so before anything
    in a sandbox could have written the store; never rewritten. A row inserted later with
    no record is not in it, and is refused (review r8 of #113, finding 1)."""

    version: int = VERSION
    taken_at: datetime
    records: dict[str, LaunchRecord] = Field(default_factory=dict)


#: The session facts this process's hooks were handed, by the board's session id: what
#: :func:`adopted` writes when the row moves onto that session.
_SEEN: dict[str, SessionFacts] = {}


def seals_dir() -> Path:
    """Where the records of the launches ``fleet spawn`` made are kept."""
    return paths.aisquare_home() / DIR_NAME


def _path(agent_id: str) -> Path:
    """The record of the row ``agent_id``, named by the id's digest: the id is the
    store's, and joined into the path it could name a file the sandbox wrote (``..``, a
    separator, an absolute path)."""
    name = hashlib.sha256(agent_id.encode("utf-8", "surrogatepass")).hexdigest()
    return seals_dir() / f"{name}.json"


def _window_path(agent_id: str) -> Path:
    """Where the row ``agent_id``'s window is recorded (:func:`placed`), beside its record."""
    record = _path(agent_id)
    return record.with_name(f"{record.stem}.window.json")


def ensure_dir() -> str | None:
    """``None`` once :func:`seals_dir` exists, made when it is not; otherwise why it could
    not be. Never raises. The empty file a Codex sandbox leaves where a path its profile
    names did not exist (measured on 0.162.1) is replaced."""
    directory = seals_dir()
    try:
        if directory.is_file() and directory.stat().st_size == 0:
            directory.unlink()
        paths.ensure_home()
        directory.mkdir(exist_ok=True)
    except OSError as exc:
        return f"{directory} could not be made ({exc.strerror or exc})"
    return None


def writable() -> str | None:
    """``None`` when a record can be written in :func:`seals_dir` now, otherwise why not:
    the directory made (:func:`ensure_dir`), then a file created, written and removed in
    it, as :func:`write` writes one. What a spawn asks before it captures the project or
    makes a worktree: a directory that is there but takes no write (read-only, its disk
    full) refused the record only after both were made (review r5 of #113, finding 9;
    SMOKE4). Never raises."""
    why = ensure_dir()
    if why is not None:
        return why
    directory = seals_dir()
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".probe-", suffix=".tmp") as probe:
            probe.write(b"{}\n")
            probe.flush()
    except OSError as exc:
        return f"{directory} cannot be written ({exc.strerror or exc})"
    return None


def write(record: LaunchRecord) -> str | None:
    """Write ``record``: ``None`` when written, otherwise why it could not be. Only a
    process outside the sandbox can (the directory is read-only in it). A legacy record
    is the census's (:func:`take_census`) and is never written out on its own: its window
    is in the census, and a record file without one would place nothing."""
    if record.legacy:
        return "a legacy row's record is the census's, and is not written out"
    why = ensure_dir()
    if why is not None:
        return why
    body = record.model_dump_json(indent=1, exclude={"window"}) + "\n"
    try:
        write_replacing(_path(record.id), body, keep_mode=False)
    except OSError as exc:
        return f"{_path(record.id)} could not be written ({exc.strerror or exc})"
    return None


def placed(agent_id: str, window: WindowPlace) -> str | None:
    """Record where the row ``agent_id``'s window is: ``None`` when written, otherwise why
    it could not be. Written by the spawner alone, once tmux made the window and before
    the row is in the store, in a file of its own: the agent starts in that window and
    its hook adds its session to the record (:func:`observe`), so one file written by
    both could lose either's write to the other's."""
    why = ensure_dir()
    if why is not None:
        return why
    path = _window_path(agent_id)
    try:
        write_replacing(path, window.model_dump_json(indent=1) + "\n", keep_mode=False)
    except OSError as exc:
        return f"{path} could not be written ({exc.strerror or exc})"
    return None


def _window(agent_id: str) -> WindowPlace | None:
    """Where :func:`placed` recorded the row's window; ``None`` when it did not, or what
    it wrote cannot be read (so nothing is typed into or killed on its word)."""
    try:
        return WindowPlace.model_validate_json(_window_path(agent_id).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValidationError):
        return None


def recorded(agent_id: str) -> bool:
    """Whether a spawn wrote a record for the row ``agent_id`` (readable or not)."""
    return _path(agent_id).exists()


def read(agent_id: str) -> LaunchRecord | str:
    """The record of the row ``agent_id``, or why there is none to replay. Never raises.
    A row spawned before records were kept has the one the census made for it
    (:func:`take_census`), when it was in the fleet then."""
    path = _path(agent_id)
    try:
        body = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        legacy = _census().records.get(agent_id) if census_taken() else None
        if legacy is not None and legacy.id == agent_id:
            return legacy
        return (
            "no launch record was written for it (it was spawned by an aisquare that "
            "keeps none, or its record could not be written), and it was not in the "
            "fleet when this aisquare first read it"
        )
    except (OSError, UnicodeDecodeError) as exc:
        return f"its launch record could not be read ({exc})"
    try:
        record = LaunchRecord.model_validate_json(body)
    except ValidationError as exc:
        return f"its launch record could not be read ({exc.error_count()} invalid fields)"
    if record.version > VERSION:
        return (
            f"its launch record was written by a newer aisquare (version {record.version}); "
            "upgrade to replay it"
        )
    if record.id != agent_id:
        return "its launch record names another row"
    return record.model_copy(update={"window": _window(agent_id)})


def _census_path() -> Path:
    return seals_dir() / "legacy-census.json"


_CENSUS_CACHE: dict[str, tuple[tuple[int, int], LegacyCensus]] = {}


def census_taken() -> bool:
    """Whether the census of rows spawned before records were kept was taken here."""
    return _census_path().exists()


def _census() -> LegacyCensus:
    """The census (:func:`take_census`), read once per change of its file; an empty one
    when it cannot be read, so nothing is placed on a guess."""
    path = _census_path()
    try:
        stat = path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        cached = _CENSUS_CACHE.get(str(path))
        if cached is not None and cached[0] == key:
            return cached[1]
        census = LegacyCensus.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValidationError):
        return LegacyCensus(taken_at=datetime.now(tz=UTC))
    _CENSUS_CACHE[str(path)] = (key, census)
    return census


_RECORD_FILE = re.compile(r"[0-9a-f]{64}\.json")


def records_kept() -> bool:
    """Whether any launch record was ever written here: once one was, an agent that
    sandboxes its tools may have run, and the store is no longer the fleet as it stood
    before records were kept, so no census is taken from it (:func:`take_census`)."""
    try:
        return any(_RECORD_FILE.fullmatch(entry.name) for entry in seals_dir().iterdir())
    except OSError:
        return False


def take_census(records: list[LaunchRecord]) -> str | None:
    """Write the census of the rows spawned before records were kept: ``None`` when it was
    written now, or was already (it is never rewritten), otherwise why it could not be.
    Created exclusively, so two processes taking it at once keep the first; only a
    process outside the sandbox can (the directory is read-only in it)."""
    why = ensure_dir()
    if why is not None:
        return why
    census = LegacyCensus(
        taken_at=datetime.now(tz=UTC),
        records={record.id: record.model_copy(update={"legacy": True}) for record in records},
    )
    try:
        with _census_path().open("x", encoding="utf-8") as handle:
            handle.write(census.model_dump_json(indent=1) + "\n")
    except FileExistsError:
        return None
    except OSError as exc:
        return f"{_census_path()} could not be written ({exc.strerror or exc})"
    return None


def legacy_records(project_id: str) -> list[LaunchRecord]:
    """The census's records of ``project_id``'s rows (:func:`take_census`)."""
    if not census_taken():
        return []
    return [record for record in _census().records.values() if record.project_id == project_id]


def _project_path(project_id: str) -> Path:
    """The sessions of ``project_id``, by the id's digest (the id is the store's)."""
    name = hashlib.sha256(project_id.encode("utf-8", "surrogatepass")).hexdigest()
    return seals_dir() / f"project-{name}.json"


def _index(project_id: str) -> ProjectSessions:
    """The project's sessions and windows as written; empty when none were or what was
    cannot be read. Never raises."""
    try:
        kept = ProjectSessions.model_validate_json(
            _project_path(project_id).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, ValidationError):
        return ProjectSessions(project_id=project_id)
    return kept if kept.project_id == project_id else ProjectSessions(project_id=project_id)


def project_sessions(project_id: str) -> list[SessionPlace]:
    """The sessions the project's spawns made (:class:`ProjectSessions`)."""
    return _index(project_id).sessions


def project_windows(project_id: str) -> list[WindowEntry]:
    """The windows the project's spawns made (:class:`WindowEntry`)."""
    return _index(project_id).windows


def _update_index(
    project_id: str, change: Callable[[ProjectSessions], ProjectSessions]
) -> str | None:
    """``change`` the project's index as it stands, under its lock (:func:`_locked`), and
    write it back: ``None`` when done, otherwise why not. Read and replaced unlocked, two
    spawns at once (or a spawn during ``shutdown``'s pruning) lost one's session (review
    r9 of #113, finding 12)."""
    why = ensure_dir()
    if why is not None:
        return why
    path = _project_path(project_id)
    try:
        with _locked(path):
            kept = _index(project_id)
            changed = change(kept)
            if changed != kept:
                write_replacing(path, changed.model_dump_json(indent=1) + "\n", keep_mode=False)
    except OSError as exc:
        return f"{path} could not be written ({exc.strerror or exc})"
    return None


def add_project_session(
    project_id: str, place: SessionPlace, window: WindowEntry | None = None
) -> str | None:
    """Record that a spawn of ``project_id`` made (or joined) the session ``place``, and the
    ``window`` it made there: ``None`` when recorded, otherwise why not. An entry for the
    same id on the same socket is replaced (a newer server numbers its sessions from
    ``$0`` again)."""
    key = (place.tmux_socket, place.socket_path, place.session_id)

    def add(kept: ProjectSessions) -> ProjectSessions:
        sessions = [
            session
            for session in kept.sessions
            if (session.tmux_socket, session.socket_path, session.session_id) != key
        ]
        windows = [
            entry for entry in kept.windows if window is None or entry.row_id != window.row_id
        ]
        return kept.model_copy(
            update={
                "sessions": [*sessions, place],
                "windows": [*windows, *([window] if window is not None else [])],
            }
        )

    return _update_index(project_id, add)


def forget_project_sessions(
    project_id: str, sessions: set[tuple[str, str]], windows: set[tuple[str, str]]
) -> None:
    """Drop the project's ``sessions`` and ``windows`` (each by its socket's name and tmux's
    id for it) that tmux no longer holds, from the index as it stands now, so neither list
    grows with every spawn. Never raises."""

    def forget(kept: ProjectSessions) -> ProjectSessions:
        return kept.model_copy(
            update={
                "sessions": [
                    session
                    for session in kept.sessions
                    if (session.tmux_socket, session.session_id) not in sessions
                ],
                "windows": [
                    entry
                    for entry in kept.windows
                    if (entry.tmux_socket, entry.session_id) not in sessions
                    and (entry.tmux_socket, entry.pane_id) not in windows
                ],
            }
        )

    _update_index(project_id, forget)


def forget_row_windows(project_id: str, row_ids: Collection[str]) -> None:
    """Drop the windows of the rows ``row_ids`` (ended and gone: ``reap``'s) from the
    project's index, so the windows the listing asks about do not grow with every spawn
    between two shutdowns (review r10 of #113, finding 11). Never raises."""
    gone = set(row_ids)
    if not gone:
        return

    def forget(kept: ProjectSessions) -> ProjectSessions:
        return kept.model_copy(
            update={"windows": [entry for entry in kept.windows if entry.row_id not in gone]}
        )

    _update_index(project_id, forget)


def rename_project_session(project_id: str, session_id: str, socket: str, name: str) -> None:
    """The project's session ``session_id`` on ``socket`` is called ``name`` now. Never
    raises; a write that fails keeps the old name, which reports alone use."""

    def rename(kept: ProjectSessions) -> ProjectSessions:
        return kept.model_copy(
            update={
                "sessions": [
                    session.model_copy(update={"name": name})
                    if (session.session_id, session.tmux_socket) == (session_id, socket)
                    else session
                    for session in kept.sessions
                ]
            }
        )

    _update_index(project_id, rename)


_LOCK_WAIT_S = 5.0


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold ``<path>.lock`` exclusively for the block, waiting at most :data:`_LOCK_WAIT_S`
    (``core.locking.holding``: non-blocking, polled, dropped by the OS if the holder dies)
    while another holds it; raises ``OSError`` when it cannot be had (``TimeoutError`` when
    held past the wait). Only a held lock is waited on (``core.locking._HELD``): a home whose
    filesystem refuses ``flock`` (``ENOLCK``, ``EOPNOTSUPP`` on NFS or SMB) will not grant it
    on a retry, and waited 5 s in every hook for nothing (review r10 of #113, finding 12).
    Only processes outside the sandbox take it (the directory is read-only in it)."""
    with holding(path.with_name(f"{path.name}.lock"), wait_s=_LOCK_WAIT_S, mode=0o600):
        yield


def _rewrite(agent_id: str, change: Callable[[LaunchRecord], LaunchRecord | None]) -> None:
    """``change`` the row ``agent_id``'s record as it stands, under its lock, and write it
    back when it changed. The spawner (a re-picked label) and the agent's own hook (its
    session) both rewrite the whole record from two processes, and each lost the other's
    field (review r9 of #113, finding 13). A legacy record is the census's and is left
    alone. Never raises: a rewrite that cannot be made is logged, naming the record and
    why (a home whose filesystem refuses locks among them: r10, finding 12), never
    dropped in silence."""
    try:
        with _locked(_path(agent_id)):
            record = read(agent_id)
            if not isinstance(record, LaunchRecord) or record.legacy:
                return
            changed = change(record)
            if changed is not None and changed != record:
                write(changed)
    except OSError as exc:
        _log.warning("the launch record of %s was not rewritten: %s", agent_id, exc)


def relabel(agent_id: str, label: str) -> None:
    """The row ``agent_id`` was stored under ``label`` (re-picked on a collision): its
    record says so, with whatever its hook has added since. Never raises."""
    _rewrite(agent_id, lambda record: record.model_copy(update={"label": label}))


def observe(
    session_id: str,
    *,
    agent: str,
    native: str | None,
    transcript: str | None,
    home: str | None,
    own: bool | None,
) -> None:
    """Note what this hook process was handed for ``session_id``, for :func:`adopted`,
    and add it to the record of this window's row (``AISQUARE_FLEET_AGENT``) when it is
    that row's own session: the one its record already names (a Claude Code agent's id,
    minted at the spawn, which its row never moves onto, so its transcript is known only
    now), or, for a record that names none yet, the first one the agent's own process
    (``own``, ``core.agent_sessions.own_process``) starts, as the launch's own binding
    takes it (a Codex thread's id binds at its start). Never raises; written only from
    outside any sandbox."""
    facts = SessionFacts(
        id=session_id,
        agent=agent,
        native_id=native,
        transcript=transcript,
        home=home,
        started_at=datetime.now(tz=UTC),
    )
    _SEEN[session_id] = facts
    agent_id = orchestrator.env_fleet_agent()
    if agent_id is None:
        return

    def add(record: LaunchRecord) -> LaunchRecord | None:
        if record.agent != agent:
            return None
        kept = record.session
        if (kept is None and own is True) or (kept is not None and kept.id == session_id):
            return _with_session(record, facts)
        return None

    # A legacy row's record is the census's, never written out: its replay is fresh.
    _rewrite(agent_id, add)


def adopted(agent_id: str, session_id: str) -> None:
    """The row ``agent_id`` moved onto ``session_id``, as its own agent's hook proved
    (``services.team._adopt``): its record takes what this process was handed for that
    session. Nothing when this process was handed nothing for it. Never raises."""
    facts = _SEEN.get(session_id)
    if facts is None:
        return
    _rewrite(agent_id, lambda record: _with_session(record, facts))


def _with_session(record: LaunchRecord, facts: SessionFacts) -> LaunchRecord:
    """``record`` holding ``facts``, merged with what it held of the same session."""
    kept = record.session
    if kept is not None and kept.id == facts.id:
        # A later hook of the same session may know less (a prompt names no home).
        facts = SessionFacts(
            id=facts.id,
            agent=facts.agent,
            native_id=facts.native_id or kept.native_id,
            transcript=facts.transcript or kept.transcript,
            home=facts.home or kept.home,
            # The first hook of this launch's session says when it started; a later one
            # does not move it.
            started_at=kept.started_at or facts.started_at,
            seam_persona=kept.seam_persona,
        )
    return record.model_copy(update={"session": facts})


def native_id(value: str | None) -> str | None:
    """``value`` when it is a session id in the shape an agent makes one (a UUID, Codex's
    thread id and Claude Code's session id both), ``None`` otherwise: what goes on a
    native command line (``codex resume <id>``, ``codex queue --thread <id>``) is never
    an option or a name."""
    if value is None or not _UUID.fullmatch(value):
        return None
    return value
