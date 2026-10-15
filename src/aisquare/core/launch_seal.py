"""Nothing a sandboxed agent can write in the store chooses what runs outside its sandbox.

The rule. The store (``context.db``) is writable from inside an agent's sandbox: every
board call opens it for writing (``AgentAdapter.store_access``), so a prompt-injected
Codex coder can rewrite any row in it, its own and every other agent's. No value read
from the store may therefore choose what runs outside the sandbox: an executable, a
native argument, the directory or the home an unsandboxed process starts in, the
conversation it resumes, or the role, agent, worktree, account or permission a
replacement runs with (review r2 of #113, finding 3; r3, findings 1, 2, 3 and 5; r4,
findings 1, 2, 5, 6 and 10).

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
- What is typed into a row's pane or kills its window — ``fleet tell``, ``fleet stop``
  and with it restart, switch, the hand-over and ``shutdown``, the manager's nudge —
  goes to the pane its record places, never to the row's ``pane_id`` at its
  ``socket_path``: a sandbox that rewrote those pointed them at any pane of the user's
  (review r5 of #113, finding 1). A row that names another pane than its record, or
  whose record places none, is not typed into or killed, and says why.
- What the fleet reads of a session to act on outside the sandbox — a failed turn's
  limit, the hand-over it starts — is read from the transcript the record holds, never
  the board session's (r5, finding 2).

Nothing scans the store to mint a record: a row spawned before this version has none,
and its replay is refused, naming the way out (stop it, spawn it again). What a record
holds is read back as it was written, never compared with a digest of today's model,
so a release that adds a field reads an older record with that field's default.

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
import re
import tempfile
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from aisquare.core import orchestrator, paths
from aisquare.core.atomic import write_replacing
from aisquare.models import LaunchSpec

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
    process outside the sandbox can (the directory is read-only in it)."""
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


def read(agent_id: str) -> LaunchRecord | str:
    """The record of the row ``agent_id``, or why there is none to replay. Never raises."""
    path = _path(agent_id)
    try:
        body = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (
            "no launch record was written for it (it was spawned by an aisquare that "
            "keeps none, or its record could not be written)"
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
        id=session_id, agent=agent, native_id=native, transcript=transcript, home=home
    )
    _SEEN[session_id] = facts
    agent_id = orchestrator.env_fleet_agent()
    if agent_id is None:
        return
    record = read(agent_id)
    if not isinstance(record, LaunchRecord) or record.agent != agent:
        return
    kept = record.session
    if (kept is None and own is True) or (kept is not None and kept.id == session_id):
        _with_session(record, facts)


def adopted(agent_id: str, session_id: str) -> None:
    """The row ``agent_id`` moved onto ``session_id``, as its own agent's hook proved
    (``services.team._adopt``): its record takes what this process was handed for that
    session. Nothing when this process was handed nothing for it. Never raises."""
    facts = _SEEN.get(session_id)
    if facts is None:
        return
    record = read(agent_id)
    if isinstance(record, LaunchRecord):
        _with_session(record, facts)


def _with_session(record: LaunchRecord, facts: SessionFacts) -> None:
    kept = record.session
    if kept is not None and kept.id == facts.id:
        # A later hook of the same session may know less (a prompt names no home).
        facts = SessionFacts(
            id=facts.id,
            agent=facts.agent,
            native_id=facts.native_id or kept.native_id,
            transcript=facts.transcript or kept.transcript,
            home=facts.home or kept.home,
        )
    if facts == kept:
        return
    write(record.model_copy(update={"session": facts}))


def native_id(value: str | None) -> str | None:
    """``value`` when it is a session id in the shape an agent makes one (a UUID, Codex's
    thread id and Claude Code's session id both), ``None`` otherwise: what goes on a
    native command line (``codex resume <id>``, ``codex queue --thread <id>``) is never
    an option or a name."""
    if value is None or not _UUID.fullmatch(value):
        return None
    return value
