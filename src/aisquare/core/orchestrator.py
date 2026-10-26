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

from aisquare.core.workspace import _resolved_or_spelled, find_project_root, project_id_for
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


PERSONA_ENV_VAR = "AISQUARE_PERSONA"
"""The persona a session was launched as (``launch --persona``); blank is none."""


def env_persona() -> str | None:
    """The persona this session was launched as, if any (``AISQUARE_PERSONA``).

    What ``launch --persona`` exports and the session-start hook reads — not a
    config input, so a hand-typed ``AISQUARE_PERSONA=skeptic aisquare launch
    coder`` works with no fleet at all (docs/plans/spawn-personas.md §3.8).
    """
    persona = os.environ.get(PERSONA_ENV_VAR, "").strip()
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


BOARD_CONTEXT_ENV_VAR = "AISQUARE_BOARD_CONTEXT"
"""What of the board an agent reads (P1 WS-2, #279): ``all``, ``relevant`` or ``none``.
``fleet spawn`` sets it on the window from ``[fleet.roles.<role>] board_context`` or
``--board-context``; ``launch`` and ``team spawn`` honour it from the shell."""

BOARD_CONTEXTS: tuple[str, ...] = ("all", "relevant", "none")


def board_context() -> str:
    """:data:`BOARD_CONTEXT_ENV_VAR`'s value, ``all`` when unset or not one of
    :data:`BOARD_CONTEXTS` (the board as before #279)."""
    raw = os.environ.get(BOARD_CONTEXT_ENV_VAR, "").strip().lower()
    return raw if raw in BOARD_CONTEXTS else "all"


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
spawn`` sets it on every window it starts, as :func:`window_team_hub` says."""


def window_team_hub() -> str:
    """``AISQUARE_TEAM_HUB`` for a window this process spawns: the hub it honours, else blank.

    A window inherits the tmux SERVER's environment, which was frozen when the
    private server first started, and ``launch`` inside the window joins
    ``team_project`` from there. The fleet UI's Explainability tab resolves the
    same question in THIS process (``key_project``). With the hub exported in
    one and not the other, the tab showed and attached one project's key while
    the window's launches took another's: a 409 ``agent_not_registered`` on
    every span, or traces in the other project's workspace (review of #170,
    D1b round 2, B1). Set on the window, the window resolves as its spawner
    does. The value is the hub's absolute path when this process honours one,
    else blank. ``team_project`` reads blank as unset, and a relative value,
    which this process ignores, becomes blank too. Blank, not absent, because
    ``tmux -e`` can only set, and the server's retained value must not leak in
    as ours.
    """
    hub = team_hub()
    return str(hub) if hub is not None else ""


def team_hub() -> Path | None:
    """The hub this process honours: ``AISQUARE_TEAM_HUB`` as an absolute path, else ``None``.

    :func:`team_project`'s rule without its warning: a relative value is
    ignored. For a surface that has to say it is under a hub, such as the fleet
    UI's Explainability tab, whose key then belongs to the hub project.
    """
    hub = os.environ.get(TEAM_HUB_ENV_VAR, "").strip()
    if not hub or not Path(hub).expanduser().is_absolute():
        return None
    return _hub_root(hub)


def _hub_root(hub: str) -> Path:
    """The board root an absolute ``hub`` names, resolved as every project root is, and
    through a symlink loop as it is spelled, made absolute, as ``project.resolve`` reads a
    root that became one (``workspace._resolved_or_spelled``): resolved raw, the loop raised
    ``RuntimeError`` on 3.11 and 3.12, which the readers that skip a root they cannot ask
    (``suppress(OSError)``) let through, and every phone board write that works out its
    reach was a 500; from 3.13 ``resolve`` handed back the path as far as it got. A hub is a
    board's name, and one that does not exist names it too: like a relative one, a bad hub
    costs no command (review of #259, round 42, 6/11)."""
    return _resolved_or_spelled(Path(hub).expanduser())


#: Relative hub values already reported, so a command that resolves the board
#: from several call sites says it once rather than three times. Per process:
#: the variable does not change under a running command.
_WARNED_HUBS: set[str] = set()


def team_project(cwd: Path | None = None) -> ProjectInfo:
    """The project this directory's team traffic belongs to.

    ``AISQUARE_TEAM_HUB`` overrides everything: an execution that spans
    several repositories (planner in one, coders and runner in others) sets
    it to one hub directory so every session shares a single board. Otherwise
    worktrees resolve to their principal checkout, so the team shares one board
    regardless of which worktree a session sits in.
    """
    hub = os.environ.get(TEAM_HUB_ENV_VAR, "").strip()
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
        return hub_project(hub)
    return directory_project(cwd)


def hub_project(hub: str) -> ProjectInfo:
    """The board an absolute ``hub`` names: :func:`team_project`'s answer under it, for a
    caller that gives a window a hub this process does not honour (``fleet.spawn``'s
    ``hub``)."""
    root = _hub_root(hub)
    return ProjectInfo(id=project_id_for(root), root=root, linked_repos=[])


def directory_project(cwd: Path | None = None) -> ProjectInfo:
    """The project of the directory itself, whatever ``AISQUARE_TEAM_HUB`` says: its
    principal checkout's, so every worktree of one repository answers alike.

    :func:`team_project`'s answer when no hub is set. Under one, it is still where a
    session's own work is: a session the human launched in ``/work/app`` is on the hub's
    board, and its fleet commands are about ``app`` (review of #259, round 23).

    One ``git`` call: ``find_project_root`` asks ``git_common_root`` first. Asked here
    first too, it ran twice for a directory that is not a git checkout (review of #259,
    round 30, 7/8). A symlink loop on the way to ``cwd`` raises ``OSError``, as
    ``find_project_root`` resolves it (round 40, 3/6).
    """
    root = find_project_root(cwd or Path.cwd())
    return ProjectInfo(id=project_id_for(root), root=root, linked_repos=[])
