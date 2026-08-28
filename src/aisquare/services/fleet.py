"""The fleet: one private tmux server, one session per project, one window per agent.

The TUI is a view; THIS is the model. Everything here works with the UI closed
— ``aisquare fleet …`` is the same code the sidebar calls — and nothing here
knows about Textual. State lives in two places only: tmux (the processes) and
``context.db`` (``fleet_agent`` rows, the project's codename, the board). There
is no daemon.

Layout of this module (docs/plans/fleet-tui.md §5.3, §5.7, §7):

- names: codenames, labels, tmux session names, branch names — pure rules;
- ``spawn`` / ``list_agents`` / ``tell`` / ``stop`` / ``reap`` — the lifecycle;
- ``nudge_manager`` — the wake-up a sub-agent's board write sends the manager;
- ``pause`` / ``resume`` — a named board signal the manager's cycle respects.

Every launch goes through ``aisquare launch <role>`` INSIDE the tmux window, so
model ladders, effort offsets, ``team bind`` profiles and the explainability
wiring apply unchanged (§3.4). The session id is minted here, before launch, so
a row knows its board session before the agent's first hook fires.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from aisquare.core import codenames
from aisquare.core.config import FleetRoleSettings, FleetSettings, load_config
from aisquare.core.store import ContextStore, store_session
from aisquare.core.tmux import TmuxServer
from aisquare.core.workspace import active_project
from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo

FLEET_ROLES: tuple[str, ...] = ("manager", "coder", "tester", "reviewer", "validator")
"""The fleet's own roles (§3.3). Any harness or ``team bind`` role is accepted too."""

MANAGER_LABEL = "manager"
"""The one label the fleet reserves: exactly one manager per project."""

LABEL = re.compile(r"^[a-z][a-z0-9-]{1,23}$")
"""An agent label: ≤ 24 chars, no ``.``, ``:`` or spaces (tmux target separators)."""

SESSION_PREFIX = "asq-"
PAUSE_SIGNAL = "fleet-paused"
TASK_SHORT = 8
"""Characters of a task id (after ``tsk_``) that name it in a branch — the same
width ``services.team.short_id`` uses for sessions."""

_NOT_WIRED = "the fleet service is not wired yet — see docs/plans/fleet-tui.md §9 (Phase 3)"


class FleetError(RuntimeError):
    """Anything the fleet cannot do, with the reason in the message."""


class FleetUnavailable(FleetError):
    """tmux is missing or too old for the fleet (§8.2)."""


class NoSuchProject(FleetError):
    """The project reference matched nothing (or several things — said in the message)."""


class NoSuchAgent(FleetError):
    """No live agent with that label in this project."""


@dataclass(frozen=True)
class SpawnReceipt:
    """What ``fleet spawn`` did — including the label it ACTUALLY used."""

    agent: FleetAgent
    asked_label: str | None
    tmux_session: str
    notes: list[str] = field(default_factory=list)
    """Anything the caller should see: a permission-mode fallback, a suffixed label."""


@dataclass(frozen=True)
class TellResult:
    """How a message reached (or did not reach) an agent."""

    delivered: bool
    how: str


@dataclass(frozen=True)
class ReapReport:
    """What a reap pass found."""

    ended: list[FleetAgent] = field(default_factory=list)
    lost: list[FleetAgent] = field(default_factory=list)
    worktrees_removed: list[Path] = field(default_factory=list)


# --- settings ---------------------------------------------------------------------


def settings() -> FleetSettings:
    """The ``[fleet]`` section; defaults when the config is unreadable (fail-open)."""
    try:
        return load_config().fleet
    except Exception:  # a broken config costs the customisation, never the fleet
        return FleetSettings()


def role_settings(role: str, config: FleetSettings | None = None) -> FleetRoleSettings:
    """The role's launch shape, or the built-in default for a role the config omits."""
    config = config or settings()
    return config.roles.get(role, FleetRoleSettings())


def server(config: FleetSettings | None = None) -> TmuxServer:
    """The private tmux server the fleet runs on."""
    return TmuxServer((config or settings()).tmux_socket)


# --- names (§5.7) -------------------------------------------------------------------


def session_name(codename: str) -> str:
    """``asq-<codename>`` — always targeted exactly (``=asq-…``) by the callers."""
    return SESSION_PREFIX + codename


def is_label(text: str) -> bool:
    return LABEL.match(text) is not None


def slugify(text: str, *, limit: int = 32) -> str:
    """``[^a-z0-9]+`` → ``-``, lowercased, clipped, no leading or trailing ``-``."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-")


def branch_name(codename: str, *, task_id: str | None, title: str | None) -> str:
    """``fleet/<codename>/<task-short-id>-<slug>``; ``fleet/<codename>/<slug>`` without a task."""
    parts = [f"fleet/{codename}"]
    leaf = slugify(title or "")
    if task_id:
        short = task_id.removeprefix("tsk_")[:TASK_SHORT]
        leaf = f"{short}-{leaf}" if leaf else short
    parts.append(leaf or "work")
    return "/".join(parts)


def resolve_project(ref: str | None = None, *, cwd: Path | None = None) -> ProjectInfo:
    """A project by id prefix, directory name or codename — or the active one for ``cwd``."""
    with store_session() as store:
        if ref is None:
            return active_project(store, cwd)
        matches = store.find_projects(ref)
        if not matches:
            raise NoSuchProject(f"no project matches {ref!r} (id prefix, name or codename)")
        if len(matches) > 1:
            names = ", ".join(
                f"{p.root.name or p.id}" + (f" · {p.codename}" if p.codename else "")
                for p in matches
            )
            raise NoSuchProject(f"{ref!r} matches several projects: {names} — use the codename")
        return matches[0]


def ensure_codename(project: ProjectInfo, store: ContextStore | None = None) -> ProjectInfo:
    """Give the project its codename if it has none (lazily, on first fleet contact)."""
    if project.codename:
        return project

    def assign(store: ContextStore) -> ProjectInfo:
        store.ensure_project(project)
        current = store.get_project(project.id)
        if current is not None and current.codename:
            return current
        name = codenames.codename_for(project.id, taken=store.codenames_in_use())
        return store.set_codename(project.id, name)

    if store is not None:
        return assign(store)
    with store_session() as opened:
        return assign(opened)


def next_label(
    project: ProjectInfo,
    role: str,
    *,
    wanted: str | None = None,
    task_id: str | None = None,
    store: ContextStore | None = None,
) -> str:
    """The label a new agent gets: the asked one, or ``<role>-<task>`` / ``<role>-<n>``,
    suffixed ``-2``, ``-3`` while a LIVE agent already holds it (§5.7)."""
    if wanted is not None and not is_label(wanted):
        raise FleetError(
            f"label {wanted!r} is not valid — lowercase letters, digits and '-', "
            "2 to 24 characters, no '.', ':' or spaces"
        )
    if role == "manager":
        return MANAGER_LABEL

    def pick(store: ContextStore) -> str:
        live = {agent.label for agent in store.fleet_agents(project.id, live_only=True)}
        if wanted is not None:
            base = wanted
        elif task_id:
            base = f"{role}-{task_id.removeprefix('tsk_')[:TASK_SHORT]}"
        else:
            base = f"{role}-1"
        if base not in live:
            return base
        stem = wanted if wanted is not None else role
        for n in range(2, 1000):
            candidate = f"{stem}-{n}"
            if candidate not in live:
                return candidate
        raise FleetError(f"no free label for {stem!r}")

    if store is not None:
        return pick(store)
    with store_session() as opened:
        return pick(opened)


# --- lifecycle (Phase 3 wires these) ------------------------------------------------


def spawn(
    project: ProjectInfo,
    role: str,
    *,
    label: str | None = None,
    task_id: str | None = None,
    worktree: bool | None = None,
    permission_mode: str | None = None,
    binary: str | None = None,
    prompt: str | None = None,
    agent_args: Sequence[str] = (),
    spawned_by: str = "user",
) -> SpawnReceipt:
    """Start an agent for ``project`` in the fleet's tmux server and record it.

    Every ``None`` means "the role's default" (config, then built-in). Refuses
    past ``max_agents_per_project``, a second manager, a worktree in a non-git
    project, and an unknown role — each with the reason in the message.
    """
    raise FleetError(_NOT_WIRED)


def list_agents(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
    """The project's agents with their DERIVED state (§5.1)."""
    raise FleetError(_NOT_WIRED)


def status_of(agent: FleetAgent) -> FleetAgentStatus:
    """One agent's derived state: board session first, tmux facts second."""
    raise FleetError(_NOT_WIRED)


def manager_of(project: ProjectInfo) -> FleetAgent | None:
    """The project's live manager, if any."""
    raise FleetError(_NOT_WIRED)


def tell(project: ProjectInfo, label: str, text: str, *, sender: str | None = None) -> TellResult:
    """Type ``text`` into a WAITING agent; otherwise file it as a board note to it."""
    raise FleetError(_NOT_WIRED)


def stop(
    project: ProjectInfo, label: str, *, force: bool = False, grace: float = 5.0
) -> FleetAgent:
    """``/exit`` the agent, wait ``grace`` seconds, then kill its window."""
    raise FleetError(_NOT_WIRED)


def reap(project: ProjectInfo | None = None) -> ReapReport:
    """Record dead panes as ended, mark vanished panes lost, remove merged worktrees."""
    raise FleetError(_NOT_WIRED)


def rename(project: ProjectInfo, codename: str) -> ProjectInfo:
    """Set the fleet codename (validated, unique) and rename the tmux session with it."""
    raise FleetError(_NOT_WIRED)


def pause(project: ProjectInfo, *, session_ref: str | None = None) -> None:
    """Set the ``fleet-paused`` board signal — the manager spawns nothing while it is on."""
    raise FleetError(_NOT_WIRED)


def resume(project: ProjectInfo, *, session_ref: str | None = None) -> None:
    raise FleetError(_NOT_WIRED)


def is_paused(project: ProjectInfo) -> bool:
    raise FleetError(_NOT_WIRED)


def nudge_manager(project_id: str, *, reason: str) -> bool:
    """Wake a WAITING manager with one fixed line + Enter (§7.3); ``False`` if not sent.

    Never raises and never touches a manager that is working or needs the
    human: this runs inside sub-agents' CLI processes, where a failure must
    cost nothing but the nudge.
    """
    return False


def attach_argv(project: ProjectInfo) -> list[str]:
    """The ``tmux attach`` command for the project's fleet session (the escape hatch)."""
    raise FleetError(_NOT_WIRED)
