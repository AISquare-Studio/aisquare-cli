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
                             this directory (multi-repo executions). Inside a
                             fleet window the fleet row's board wins; see
                             ``team_project``.
- ``AISQUARE_TEAM_DELTA=0``— mute the per-prompt teammate delta injection.
- ``AISQUARE_TEAM_LEASE_MIN`` — claim lease in minutes (default 120; long
                             agentic turns only renew on prompt submit).
- ``AISQUARE_FLEET_AGENT``  — the fleet_agent row this session runs in; set by
                             ``fleet spawn`` on the window, read at session start
                             to join the session to its row and brief it on the
                             task it was spawned for. Inherited by the agent's
                             own child processes — see ``team._assignment``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from aisquare.core.workspace import find_project_root, git_common_root, project_id_for
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


def env_persona() -> str | None:
    """The persona this session was launched as, if any (``AISQUARE_PERSONA``).

    What ``launch --persona`` exports and the session-start hook reads — not a
    config input, so a hand-typed ``AISQUARE_PERSONA=skeptic aisquare launch
    coder`` works with no fleet at all (docs/plans/spawn-personas.md §3.8).
    """
    persona = os.environ.get("AISQUARE_PERSONA", "").strip()
    return persona or None


FLEET_AGENT_ENV_VAR = "AISQUARE_FLEET_AGENT"
"""The variable that carries a fleet row's id into its window — an IDENTITY,
which is why ``fleet spawn`` keeps it out of the tmux session environment."""


def env_fleet_agent() -> str | None:
    """The ``fleet_agent`` row this session runs as (``AISQUARE_FLEET_AGENT``).

    ``fleet spawn`` sets it on the tmux window it starts, so the session that
    comes up inside can be joined to the row — and told the task the row was
    spawned for. Nothing read it before: the task was recorded on the row and
    named the label and branch, and the agent itself was never told.
    """
    agent_id = os.environ.get(FLEET_AGENT_ENV_VAR, "").strip()
    return agent_id or None


def env_claude_pid() -> int | None:
    """The pid of the Claude Code process running this hook (``CLAUDE_PID``), if any.

    Claude Code exports it to every process it starts — hooks included — and
    writes ITS OWN pid each time, so a hook always reads the process that fired
    it: a nested ``claude -p`` inherits the parent's value into its environment
    and still hands its hooks its own. Measured on Claude Code 2.1.272: a hook
    under a fleet pane read the pane's ``#{pane_pid}`` on startup, on the
    ``SessionEnd(clear)`` and on the ``SessionStart(clear)`` that follows it,
    while the session id changed underneath. That is the PROCESS identity a
    session id is not — ``/clear`` mints a new id in the same process, a nested
    child is a new process under the same ``AISQUARE_FLEET_AGENT`` — and it is
    what ``services.team`` binds a fleet row to. ``None`` for a binary that does
    not export it, and for a value that is not a number.
    """
    raw = os.environ.get("CLAUDE_PID", "").strip()
    return int(raw) if raw.isdigit() else None


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


TEAM_HUB_ENV_VAR = "AISQUARE_TEAM_HUB"
"""The hub that pins every session to one board (:func:`team_project`). ``fleet
spawn`` sets it on every window it starts, to the fleet's own root
(``services.fleet.spawn``), and inside a fleet window the row's board wins anyway
(:func:`_fleet_board`)."""


def team_hub() -> Path | None:
    """The hub this process honours: ``AISQUARE_TEAM_HUB`` as an absolute path, else ``None``.

    :func:`team_project`'s rule without its warning: a relative value is
    ignored. For a surface that has to say it is under a hub, such as the fleet
    UI's Explainability tab, whose key then belongs to the hub project.
    """
    hub = os.environ.get(TEAM_HUB_ENV_VAR, "").strip()
    if not hub or not Path(hub).expanduser().is_absolute():
        return None
    return Path(hub).expanduser().resolve()


def _fleet_board() -> ProjectInfo | None:
    """The project of the fleet row ``AISQUARE_FLEET_AGENT`` names, or ``None``.

    ``None`` outside a fleet window, for an id with no row or no project, and on
    any failure to read the store: a stale or foreign id never strands a command,
    and the resolution below stands as before. Read each time, never cached: a row
    id is unique within ONE store, and a process can read another (a test, or a
    command pointed at another ``AISQUARE_HOME``).
    """
    agent_id = env_fleet_agent()
    if agent_id is None:
        return None
    try:
        from aisquare.core.store import store_session  # lazy: this module is on every hook path

        with store_session() as store:
            row = store.get_fleet_agent(agent_id)
            project = store.get_project(row.project_id) if row is not None else None
    except Exception:  # fail open: the board falls back to the hub or the checkout
        return None
    if project is None:
        return None
    return ProjectInfo(id=project.id, root=project.root, linked_repos=[])


#: Relative hub values already reported, so a command that resolves the board
#: from several call sites says it once rather than three times. Per process:
#: the variable does not change under a running command.
_WARNED_HUBS: set[str] = set()


def team_project(cwd: Path | None = None) -> ProjectInfo:
    """The project this directory's team traffic belongs to.

    Inside a fleet window (``AISQUARE_FLEET_AGENT`` names a row) the board is
    the fleet row's project, ahead of everything below: the window's hub may be
    one its tmux server inherited, and the seat belongs to its own fleet.
    Otherwise ``AISQUARE_TEAM_HUB`` overrides: an execution that spans
    several repositories (planner in one, coders and runner in others) sets
    it to one hub directory so every session shares a single board. Otherwise
    worktrees resolve to their principal checkout, so the team shares one board
    regardless of which worktree a session sits in.
    """
    hub = os.environ.get(TEAM_HUB_ENV_VAR, "").strip()
    fleet = _fleet_board()
    if fleet is not None:
        # Inside a fleet window the seat belongs to its fleet's board, ahead of any
        # hub the window inherited from its tmux server (card
        # tsk_01m3k89b2f96tt7xc6crzvxzjk: a server started from a shell with a hub
        # exported put every seat of two fleets on a third board).
        pinned = Path(hub).expanduser()
        differs = hub and pinned.is_absolute() and pinned.resolve() != fleet.root
        if differs and hub not in _WARNED_HUBS:
            _WARNED_HUBS.add(hub)
            print(
                # The path as typed, in plain quotes: repr doubled every backslash of a
                # Windows path, and the owner could not find their own path in it (#230).
                f"⚠ AISQUARE_TEAM_HUB='{hub}' names another board, but this is a fleet "
                f"window of {fleet.root.name or fleet.id}, so the fleet's own board wins.",
                file=sys.stderr,
            )
        return fleet
    if hub and not Path(hub).expanduser().is_absolute():
        # A RELATIVE hub inverts the feature. `Path('./').resolve()` is the
        # process cwd, so "one board for sessions in several repositories"
        # becomes "a different board per directory" — and it overrides even the
        # `cwd` argument, so this function can be asked about one directory and
        # answer about another. Nobody can mean that: "the board is wherever I
        # am" is what NOT setting the variable does, and the two lines below do
        # it correctly, worktrees included. So a relative value is always a
        # mistake and ignoring it is safe.
        #
        # Loud, per this repo's fail-open doctrine — silence is how this
        # survived thirty hours and cost two board incidents in one shift.
        # stderr, so `--json` stdout stays machine-readable, and the exit code
        # is untouched: a bad hub costs a warning, never a command.
        if hub not in _WARNED_HUBS:
            _WARNED_HUBS.add(hub)
            print(
                f"⚠ AISQUARE_TEAM_HUB={hub!r} is a RELATIVE path, so it resolves to "
                "whatever directory each command runs in — the opposite of one shared "
                "board. Ignoring it and resolving from the checkout instead; set an "
                "absolute path to pin a hub.",
                file=sys.stderr,
            )
        hub = ""
    if hub:
        root = Path(hub).expanduser().resolve()
        return ProjectInfo(id=project_id_for(root), root=root, linked_repos=[])
    start = (cwd or Path.cwd()).resolve()
    root = git_common_root(start) or find_project_root(start)
    return ProjectInfo(id=project_id_for(root), root=root, linked_repos=[])
