"""Orchestrator plumbing: worktree-safe project identity and the env knobs.

The orchestrator must put every checkout of one repository on the same board —
including git worktrees, whose ``.git`` *file* would otherwise make them their
own project. Identity therefore resolves through ``git rev-parse
--git-common-dir`` (the principal repository) and deliberately ignores the
``project switch`` pin, which routes *context*, not team traffic.

Behaviour is controlled by environment variables, not config — the feature
branch is the gate:

- ``AISQUARE_TEAM=0``      — master off switch: hooks and commands no-op.
- ``AISQUARE_ROLE``        — role for this session (also activates the orchestrator
                             for the project on session start).
- ``AISQUARE_TEAM_HUB``    — pin every session/command to one board rooted at
                             this directory (multi-repo executions).
- ``AISQUARE_TEAM_DELTA=0``— mute the per-prompt teammate delta injection.
- ``AISQUARE_TEAM_LEASE_MIN`` — claim lease in minutes (default 120; long
                             agentic turns only renew on prompt submit).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from aisquare.core.workspace import find_project_root, project_id_for
from aisquare.models import ProjectInfo

_OFF_VALUES = {"0", "false", "no", "off"}
DEFAULT_LEASE_MINUTES = 120


def _flag_on(name: str) -> bool:
    """An env flag is on unless explicitly set to an off value (default: on)."""
    return os.environ.get(name, "").strip().lower() not in _OFF_VALUES


def team_enabled() -> bool:
    """Whether the orchestrator is enabled at all (``AISQUARE_TEAM=0`` disables)."""
    return _flag_on("AISQUARE_TEAM")


def env_role() -> str | None:
    """The role this session was launched with, if any (``AISQUARE_ROLE``)."""
    role = os.environ.get("AISQUARE_ROLE", "").strip()
    return role or None


def delta_enabled() -> bool:
    """Whether per-prompt teammate deltas are injected (``AISQUARE_TEAM_DELTA=0`` mutes)."""
    return _flag_on("AISQUARE_TEAM_DELTA")


def lease_minutes() -> int:
    """Claim-lease length in minutes (``AISQUARE_TEAM_LEASE_MIN``)."""
    raw = os.environ.get("AISQUARE_TEAM_LEASE_MIN", "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_LEASE_MINUTES
    return value if value > 0 else DEFAULT_LEASE_MINUTES


def _git_common_root(start: Path) -> Path | None:
    """The principal repository root for ``start``, resolving worktrees.

    Returns ``None`` when git is unavailable or ``start`` is not in a work
    tree — callers fall back to the marker-based project root.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    common = result.stdout.strip()
    if not common:
        return None
    common_dir = Path(common)
    if not common_dir.is_absolute():
        common_dir = (start / common_dir).resolve()
    # <principal>/.git → <principal>; a bare repo's common dir is the repo itself.
    root = common_dir.parent if common_dir.name == ".git" else common_dir
    return root.resolve()


def team_project(cwd: Path | None = None) -> ProjectInfo:
    """The project this directory's team traffic belongs to.

    ``AISQUARE_TEAM_HUB`` overrides everything: an execution that spans
    several repositories (planner in one, coders and runner in others) sets
    it to one hub directory so every session shares a single board. Otherwise
    worktrees resolve to their principal checkout, so the team shares one board
    regardless of which worktree a session sits in.
    """
    hub = os.environ.get("AISQUARE_TEAM_HUB", "").strip()
    if hub:
        root = Path(hub).expanduser().resolve()
        return ProjectInfo(id=project_id_for(root), root=root, linked_repos=[])
    start = (cwd or Path.cwd()).resolve()
    root = _git_common_root(start) or find_project_root(start)
    return ProjectInfo(id=project_id_for(root), root=root, linked_repos=[])
