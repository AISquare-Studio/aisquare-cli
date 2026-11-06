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
was recorded. No project's purge reaches them (``project fresh``, ``project forget``): only
``uninstall --purge``, which deletes the store, clears them. A repository is touched by a root
it is inside, or that is inside it (a worktree's).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
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


def recorded(store: ContextStore) -> dict[Path, str]:
    """Every recorded root, with the agent recorded there."""
    found: dict[Path, str] = {}
    for key, value in store.list_meta(PREFIX).items():
        try:
            agent = str(json.loads(value).get("agent") or "")
        except (ValueError, AttributeError):
            agent = ""
        found[Path(key.removeprefix(PREFIX))] = agent
    return found


def touching(tree: Path, roots: Mapping[Path, str]) -> tuple[Path, str] | None:
    """The first of ``roots`` that ``tree`` is inside, or that is inside ``tree``, with its
    agent; ``None`` for none."""
    real = _real(tree)
    for root, agent in sorted(roots.items(), key=lambda item: str(item[0])):
        if real == root or real.is_relative_to(root) or root.is_relative_to(real):
            return root, agent
    return None
