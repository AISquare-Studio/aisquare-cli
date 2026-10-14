"""Launch specs the fleet replays, kept from a write made inside an agent's sandbox.

A row's ``launch_spec`` (``models.LaunchSpec``) is replayed outside any sandbox: by
``fleet restart``, ``fleet switch`` and the automatic usage-limit hand-over, binary and
arguments both. It lives in the store, which an agent that sandboxes its tools must be
able to write for every board call (``AgentAdapter.store_access``), so the store cannot
keep it from such an agent: a prompt-injected Codex coder could point it at a program of
its own, or hand Codex a ``-c notify=…`` that runs one, and the next hand-over ran it with
the operator's full rights (review r2 of #113, finding 3).

What a sandbox cannot write is :func:`specs_dir`: the store profile names it read-only
(:func:`~aisquare.core.agent_adapters.types.store_read_only`). ``fleet spawn`` records
the digest of each spec there under the row's id, and a replay takes only a spec whose
digest it finds (``services.fleet``). Read-only, not unreadable: a path Codex's profile
denies reading sends Codex's own reads of its instructions through its sandbox helper,
and where bwrap cannot bring up loopback (a GitHub runner) the agent did not start at
all (CI on ca6d43ea).
"""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from pathlib import Path

from aisquare.core import paths
from aisquare.core.atomic import write_replacing
from aisquare.models import LaunchSpec

DIR_NAME = "launch-specs"


def specs_dir() -> Path:
    """Where the digests of the launch specs ``fleet spawn`` recorded are kept."""
    return paths.aisquare_home() / DIR_NAME


def digest(spec: LaunchSpec) -> str:
    """``spec``'s SHA-256, over every field, as JSON with sorted keys."""
    body = json.dumps(spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _digest_path(agent_id: str) -> Path:
    return specs_dir() / f"{agent_id}.sha256"


def ensure_dir() -> bool:
    """Whether :func:`specs_dir` is there, made when it is not; never raises.

    The empty file a Codex sandbox leaves where a path its profile names did not exist
    (measured on 0.162.1) is replaced. The process that makes the directory records the
    specs the store already holds (:func:`_record_recorded`), as every spawn before it
    would have: the agents running across an upgrade keep their launch.
    """
    directory = specs_dir()
    if directory.is_dir():
        return True
    try:
        if directory.is_file() and directory.stat().st_size == 0:
            directory.unlink()
        paths.ensure_home()
        directory.mkdir()
    except FileExistsError:
        return directory.is_dir()  # another process made it, and records what it found
    except OSError:
        return False
    _record_recorded()
    return True


def record(agent_id: str, spec: LaunchSpec) -> bool:
    """Record ``spec`` as the launch of the row ``agent_id``: whether it was written. Only a
    process outside the sandbox can (the directory is read-only in it)."""
    if not ensure_dir():
        return False
    try:
        write_replacing(_digest_path(agent_id), digest(spec) + "\n", keep_mode=False)
    except OSError:
        return False
    return True


def holds(agent_id: str, spec: LaunchSpec) -> bool:
    """Whether ``spec`` is the launch ``fleet spawn`` recorded for the row ``agent_id``: a
    spec changed since, and one no digest was recorded for, do not hold."""
    try:
        recorded = _digest_path(agent_id).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return False
    return recorded == digest(spec)


def _record_recorded() -> None:
    """Record the specs the store held before :func:`specs_dir` was made; a store that
    cannot be read leaves them unrecorded, which a replay then says (never raises)."""
    from aisquare.core.store import store_session

    with suppress(Exception), store_session() as store:
        for agent_id, spec in store.recorded_launch_specs():
            with suppress(OSError):
                write_replacing(_digest_path(agent_id), digest(spec) + "\n", keep_mode=False)
