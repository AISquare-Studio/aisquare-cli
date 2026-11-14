"""Where an agent that sandboxes its tools could write, remembered for good (#99, the owner's
ruling on #97's verification).

A worktree a ``fleet spawn`` makes runs none of the repository's hooks where such an agent
could have changed them (``services.fleet._repository_hooks_run``): aisquare's git runs them
outside its sandbox. "Has run in this project" was read off the project's sessions and fleet
rows, which ``project fresh`` deletes while the checkout keeps the hooks the agent edited, and
which never name a repository inside the folder a session ran in (a Codex started in a parent
folder of several repositories). So every launch of such an agent and every session it starts
records where it may write: its sandbox's writable roots (``AgentAdapter.sandbox_writes``:
its workspace, each directory added, each configured writable root; never the temporary
directories every sandbox writes, nor aisquare's own home), at the least the folder it ran in.

The record is ``team_meta``'s ``sandbox-root:<path>`` keys, valued with the agent and when it
was recorded. It stays on this machine: ``team_meta`` is neither exported nor synced. No
project's purge reaches them (``project fresh``, ``project forget``); ``aisquare agents status
--roots`` lists them, ``aisquare agents disconnect <agent> --forget-root <path>`` forgets one
(#101), and ``uninstall --purge``, which deletes the store, clears them all. A repository is
touched by a root it is inside (a session in a parent folder guards every repository under
it), or that is inside it (a worktree's).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from aisquare.core import paths
from aisquare.core.store import ContextStore

PREFIX = "sandbox-root:"


def _real(path: Path) -> Path:
    return Path(os.path.realpath(path))


def record(store: ContextStore, agent: str, roots: Iterable[Path]) -> list[Path]:
    """Record ``roots`` as places ``agent`` could write (each made absolute and real); the
    temporary directories and aisquare's own home are left out, as no repository is kept
    there. The roots recorded."""
    home = _real(paths.aisquare_home())
    temporary = {_real(Path(p)) for p in ("/tmp", os.environ.get("TMPDIR", "")) if p}
    recorded: list[Path] = []
    for root in roots:
        real = _real(root)
        if real in temporary or real == home or real.is_relative_to(home):
            continue
        if real.parent == real:
            continue  # a filesystem's root holds every repository: it says nothing
        stamp = json.dumps({"agent": agent, "at": datetime.now(UTC).isoformat()})
        store.set_meta(f"{PREFIX}{real}", stamp)
        recorded.append(real)
    return recorded


@dataclass(frozen=True)
class Root:
    """A recorded root: where ``agent`` could write, recorded ``at`` (``None`` when its value
    cannot be read: still a root, as only such an agent records one)."""

    path: Path
    agent: str
    at: datetime | None


def entries(store: ContextStore) -> list[Root]:
    """Every recorded root, by its path."""
    found: list[Root] = []
    for key, value in store.list_meta(PREFIX).items():
        agent, at = "", None
        try:
            data = json.loads(value)
            agent = str(data.get("agent") or "")
            at = datetime.fromisoformat(str(data.get("at")))
        except (ValueError, AttributeError, TypeError):
            pass
        found.append(Root(Path(key.removeprefix(PREFIX)), agent, at))
    return sorted(found, key=lambda root: str(root.path))


def recorded(store: ContextStore) -> dict[Path, str]:
    """Every recorded root, with the agent recorded there."""
    return {root.path: root.agent for root in entries(store)}


def touching(tree: Path, roots: Sequence[Root]) -> Root | None:
    """The first of ``roots`` that ``tree`` is inside, or that is inside ``tree``; ``None``
    for none."""
    real = _real(tree)
    for root in roots:
        if real == root.path or real.is_relative_to(root.path) or root.path.is_relative_to(real):
            return root
    return None


def owner(root: Root) -> str | None:
    """The agent a forget of ``root`` names (``agents disconnect <agent> --forget-root``): the
    agent recorded there where this release knows it; else this release's first agent that
    sandboxes its tools (``capabilities.sandboxes_tools``), as only such an agent records a
    root, for a value that cannot be read or a later release's agent after a rollback. The
    line, the list and the forget all name it, so the command they print works pasted
    (#101's reopen 1). ``None`` only for a release with no such agent."""
    from aisquare.core.agent_adapters import adapters

    known = {adapter.id for adapter in adapters()}
    if root.agent in known:
        return root.agent
    return next(
        (adapter.id for adapter in adapters() if adapter.capabilities.sandboxes_tools), None
    )


def forget_argv(root: Root) -> list[str]:
    """The command that forgets ``root``: ``aisquare agents disconnect <owner> --forget-root
    <path>``, its agent :func:`owner`'s."""
    return [
        "aisquare",
        "agents",
        "disconnect",
        owner(root) or root.agent,
        "--forget-root",
        str(root.path),
    ]


def forget(store: ContextStore, path: Path) -> Root | None:
    """Forget the record of ``path`` (made absolute and real); the root forgotten, or ``None``
    where none was recorded."""
    real = _real(path)
    found = next((root for root in entries(store) if root.path == real), None)
    if found is not None:
        store.delete_meta(f"{PREFIX}{real}")
    return found
