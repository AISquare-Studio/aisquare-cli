"""Nothing a sandboxed agent can write in the store chooses what runs outside its sandbox.

The rule. The store (``context.db``) is writable from inside an agent's sandbox: every
board call opens it for writing (``AgentAdapter.store_access``), so a prompt-injected
Codex coder can rewrite any row in it. No value read from the store may therefore choose
what runs outside the sandbox (an executable, a native argument, the home an unsandboxed
native command reads, or the role, agent, worktree or account a replacement runs with)
unless it is sealed here or validated against something the sandbox cannot write (review
r2 of #113, finding 3; r3, findings 1, 2, 3 and 5):

- Sealed: a fleet row's launch (:func:`sealed`: its launch spec, binary, agent, role,
  worktree, working directory, account slot and project), which ``fleet restart``,
  ``fleet switch`` and the usage-limit hand-over replay, and whose binary ``fleet tell``
  queues with. ``fleet spawn`` records its digest in :func:`seals_dir`, which the store
  profile names read-only (:func:`~aisquare.core.agent_adapters.types.store_read_only`).
  A row whose digest is not there is not replayed and not queued with: the caller
  refuses it and says why (:func:`refusal`), and never falls back to the row's values.
- Validated: a native session id goes on a command line only in the shape the agent
  makes it (:func:`native_id`: Codex's thread ids are UUIDs, and ``codex resume`` and
  ``codex queue`` read one that starts with ``-`` as an option), a transcript only by its
  absolute path, and the home an unsandboxed native command runs under only when it is
  one of that agent's account homes (``services.fleet``). A digest's file is named by
  the digest of the row's id, never by the id itself, which the store chose.

Read-only, not unreadable: a path Codex's profile denies reading sends Codex's own reads
of its instructions through its sandbox helper, and where bwrap cannot bring up loopback
(a GitHub runner) the agent did not start at all (CI on ca6d43ea).

The directory is ready once it holds :data:`READY`, written after every row the store
held when it was first made has been recorded (as every spawn before it would have
been): the agents running across an upgrade keep their launch. Until then each process
that needs it records them again, so a recording that failed, or one still under way in
another process, is never read as a seal that does not hold. A store migration that
changes a sealed column (:func:`sealed`) has to seal the rows it changed again.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from aisquare.core import paths
from aisquare.core.atomic import write_replacing
from aisquare.models import FleetAgent

DIR_NAME = "launch-seals"
READY = "ready"
"""The file that says :func:`seals_dir` is complete (:func:`ensure_dir`)."""

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def seals_dir() -> Path:
    """Where the digests of the launches ``fleet spawn`` recorded are kept."""
    return paths.aisquare_home() / DIR_NAME


def sealed(agent: FleetAgent) -> dict[str, Any]:
    """What of ``agent``'s row the seal covers: everything a replay starts it with."""
    spec = agent.launch_spec
    return {
        "id": agent.id,
        "project_id": agent.project_id,
        "role": agent.role,
        "agent": agent.agent,
        "binary": agent.binary,
        "cwd": str(agent.cwd),
        "worktree": agent.worktree,
        "account_slot": agent.account_slot,
        "launch_spec": spec.model_dump(mode="json") if spec is not None else None,
    }


def digest(agent: FleetAgent) -> str:
    """The SHA-256 of :func:`sealed`, as JSON with sorted keys."""
    body = json.dumps(sealed(agent), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _seal_path(agent_id: str) -> Path:
    """The file of the row ``agent_id``'s seal, named by the id's digest: the id is the
    store's, and joined into the path it could name a file the sandbox wrote (``..``, a
    separator, an absolute path)."""
    name = hashlib.sha256(agent_id.encode("utf-8", "surrogatepass")).hexdigest()
    return seals_dir() / f"{name}.sha256"


def _write(agent: FleetAgent) -> None:
    write_replacing(_seal_path(agent.id), digest(agent) + "\n", keep_mode=False)


def ensure_dir() -> str | None:
    """``None`` once :func:`seals_dir` is ready, made and filled when it is not; otherwise
    why it is not. Never raises.

    The empty file a Codex sandbox leaves where a path its profile names did not exist
    (measured on 0.162.1) is replaced. Every row the store holds is recorded before
    :data:`READY` is written, so another process finds the directory ready only once
    it is complete; a store that cannot be read leaves it unready, for the next process
    to fill.
    """
    directory = seals_dir()
    if (directory / READY).is_file():
        return None
    try:
        if directory.is_file() and directory.stat().st_size == 0:
            directory.unlink()
        paths.ensure_home()
        directory.mkdir(exist_ok=True)
    except OSError as exc:
        return f"{directory} could not be made ({exc.strerror or exc})"
    from aisquare.core.store import store_session

    try:
        with store_session() as store:
            rows = store.every_fleet_agent()
        for row in rows:
            _write(row)
        write_replacing(directory / READY, "", keep_mode=False)
    except Exception as exc:  # a store that cannot be read, a directory that refuses a write
        return (
            f"the launches the store held when {directory} was first made could not be "
            f"recorded ({type(exc).__name__}: {exc})"
        )
    return None


def record(agent: FleetAgent) -> str | None:
    """Seal ``agent``'s row as its launch: ``None`` when sealed, otherwise why it could not
    be. Only a process outside the sandbox can (the directory is read-only in it)."""
    why = ensure_dir()
    if why is not None:
        return why
    try:
        _write(agent)
    except OSError as exc:
        return f"{_seal_path(agent.id)} could not be written ({exc.strerror or exc})"
    return None


def refusal(agent: FleetAgent) -> str | None:
    """``None`` when ``agent`` is the row ``fleet spawn`` sealed; otherwise why a replay
    of it, or a command run with its binary, is refused. Never raises."""
    why = ensure_dir()
    if why is not None:
        return f"no launch seal could be checked: {why}"
    try:
        recorded = _seal_path(agent.id).read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return (
            "no launch seal was recorded for its row (it was spawned by an aisquare that "
            "keeps none, or while seals could not be written, or written into the store by "
            "something other than `fleet spawn`)"
        )
    except (OSError, UnicodeDecodeError) as exc:
        return f"its launch seal could not be read ({exc})"
    if recorded != digest(agent):
        return (
            "its row is not the launch `fleet spawn` sealed: its launch spec, binary, agent, "
            "role, worktree, directory or account changed in the store since"
        )
    return None


def holds(agent: FleetAgent) -> bool:
    """Whether ``agent`` is the row ``fleet spawn`` sealed (:func:`refusal`)."""
    return refusal(agent) is None


def native_id(value: str | None) -> str | None:
    """``value`` when it is a session id in the shape an agent makes one (a UUID, Codex's
    thread id and Claude Code's session id both), ``None`` otherwise: what goes on a
    native command line from the store (``codex resume <id>``, ``codex queue --thread
    <id>``) is never an option or a name the store chose."""
    if value is None or not _UUID.fullmatch(value):
        return None
    return value
