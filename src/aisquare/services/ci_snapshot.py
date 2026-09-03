"""Snapshot identity for a turn: the exact tree the developer was looking at.

Replay (seam doc C7, Slice 13) needs to rebuild the working tree a prompt was
submitted against. That tree is perishable — the developer keeps editing — so
it is captured *now*, per turn, even though nothing replays it yet:
``git stash create`` writes the dirty tree as a commit object without touching
the index, the working tree or the stash list, and ``update-ref`` under
``refs/aisquare/wip/<trace_id>`` keeps that object from being garbage
collected. A clean tree needs no object; ``HEAD`` is the snapshot.

What travels on the wire is the **object id** — 40 hex — never the ref name;
the contract rejects names, and the name is only where the CLI parks the
object. ``refs/aisquare/wip/`` is outside ``refs/heads`` and ``refs/tags`` so
nothing lists it as a branch, pushes it, or shows it in the stash list.

Honesty over completeness: ``stash create`` does not include untracked files,
so a replay from this snapshot lacks them. That is recorded on the row as
``snapshot_untracked_excluded`` rather than pretended away.

Bounded and fail-open. Every git call shares one small time budget and goes
through :func:`_git`, the single spawn site ``core.spawn.SEAMS`` rules on.
Anything that fails — no git, not a repository, a lock held by the developer's
own git — yields ``None`` and the turn goes ahead with ``snapshot_ref: null``.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from aisquare.core.spawn import untraced_env

GIT_BUDGET_SECONDS = 2.0
"""Total wall clock one capture (or one project_ref) may spend in git."""

WIP_REF_PREFIX = "refs/aisquare/wip/"

WIP_REF_TTL_DAYS = 7
"""How long a turn's stash object is kept alive for replay before its ref is
dropped. Without a bound the refs grew with every dirty-tree prompt forever,
``git gc`` could reclaim nothing behind them, and a secret that was in a tracked
file for one turn stayed recoverable behind a ref the developer did not know
existed. Pruning happens when a new snapshot is taken, within the same budget."""

_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$")
_REF_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class Snapshot:
    """What was captured for one turn."""

    object_id: str
    """The commit the working tree corresponds to: a stash object for a dirty
    tree, ``HEAD`` for a clean one."""
    dirty: bool
    untracked_excluded: bool
    """True whenever the mechanism used cannot carry untracked files, which is
    every time. Recorded per row so the ledger says so, not this docstring."""
    ref: str | None
    """The ref keeping a stash object alive, or ``None`` for a clean tree."""


def capture(root: Path, trace_id: str) -> Snapshot | None:
    """Snapshot ``root``'s working tree for ``trace_id``. Never raises.

    ``None`` when the tree cannot be captured within the budget — the caller
    sends ``snapshot_ref: null`` and records the turn anyway.
    """
    budget = _Budget(GIT_BUDGET_SECONDS)
    stashed = _git(
        root,
        "-c",
        "user.name=aisquare",
        "-c",
        "user.email=aisquare@localhost",
        "stash",
        "create",
        timeout=budget.remaining(),
    )
    if stashed is None:
        return None
    if stashed and _OBJECT_ID.match(stashed):
        ref = None
        tail = trace_id.removeprefix("trc_")
        if _REF_SAFE.match(tail):
            ref = WIP_REF_PREFIX + tail
            if _git(root, "update-ref", ref, stashed, timeout=budget.remaining()) is None:
                ref = None  # the object is still valid for this turn; only its lifetime is not
            else:
                _prune(root, budget)
        return Snapshot(object_id=stashed, dirty=True, untracked_excluded=True, ref=ref)
    head = _git(root, "rev-parse", "HEAD", timeout=budget.remaining())
    if head is None or not _OBJECT_ID.match(head):
        return None
    return Snapshot(object_id=head, dirty=False, untracked_excluded=True, ref=None)


def project_ref(root: Path) -> str | None:
    """``<owner/repo>@<branch>`` for ``root`` — a selector, never authority.

    Built from ``origin``'s path component only, so a remote URL carrying
    ``user:token@`` never contributes the credential. Falls back to the
    directory name when there is no remote, and to ``None`` when there is no
    repository at all. Always within the contract's 500 characters.
    """
    budget = _Budget(GIT_BUDGET_SECONDS)
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD", timeout=budget.remaining())
    if branch is None:
        return None
    remote = _git(root, "remote", "get-url", "origin", timeout=budget.remaining())
    slug = repo_slug(remote) if remote else None
    name = slug or root.name or "repo"
    where = branch if branch and branch != "HEAD" else "detached"
    return f"{name}@{where}"[:500]


def repo_slug(remote_url: str) -> str | None:
    """``owner/repo`` from any common remote URL shape, or ``None``.

    Only the path is read: ``https://user:token@host/o/r.git``,
    ``git@host:o/r.git`` and ``ssh://git@host:22/o/r`` all yield ``o/r``.
    """
    url = remote_url.strip()
    if not url:
        return None
    if "://" in url:
        _, _, rest = url.partition("://")
        _, _, path = rest.partition("/")
    elif ":" in url and not url.startswith("/"):
        _, _, path = url.partition(":")
    else:
        path = url
    parts = [part for part in path.strip("/").split("/") if part]
    if not parts:
        return None
    tail = parts[-2:] if len(parts) >= 2 else parts
    slug = "/".join(tail).removesuffix(".git")
    return slug or None


def _prune(root: Path, budget: _Budget, *, now: float | None = None) -> int:
    """Drop ``refs/aisquare/wip/*`` older than :data:`WIP_REF_TTL_DAYS`; returns how many.

    Fail-open like everything here: a listing that does not come back within
    the budget prunes nothing, and the next snapshot tries again.
    """
    listed = _git(
        root,
        "for-each-ref",
        "--format=%(refname)%09%(creatordate:unix)",
        WIP_REF_PREFIX,
        timeout=budget.remaining(),
    )
    if not listed:
        return 0
    cutoff = (now if now is not None else time.time()) - WIP_REF_TTL_DAYS * 86_400
    dropped = 0
    for line in listed.splitlines():
        ref, _, stamp = line.partition("\t")
        if not (ref.startswith(WIP_REF_PREFIX) and stamp.isdigit() and int(stamp) < cutoff):
            continue
        if _git(root, "update-ref", "-d", ref, timeout=budget.remaining()) is not None:
            dropped += 1
    return dropped


class _Budget:
    def __init__(self, seconds: float) -> None:
        self._deadline = time.monotonic() + seconds

    def remaining(self) -> float:
        return max(0.05, self._deadline - time.monotonic())


def _git(root: Path, *args: str, timeout: float) -> str | None:
    """Run one git command in ``root``; its stdout stripped, or ``None`` on any failure.

    The one spawn site in this module. ``GIT_OPTIONAL_LOCKS=0`` keeps a hook's
    read from contending with the developer's own ``git`` for the index lock;
    the traced-session identity is stripped because a child of a hook is not
    the agent.
    """
    env = untraced_env()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()
