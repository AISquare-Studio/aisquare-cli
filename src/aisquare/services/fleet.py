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

Two process seams live here, both registered in ``core.spawn.SEAMS``: every tmux
command goes through :class:`~aisquare.core.tmux.TmuxServer` (its own seam), and
:func:`_git` is the ONE place git runs for worktrees (§3.5). ``services.team``
and ``cli.launch`` are imported lazily — the CLI imports this module, and the
team service may one day call back into it for nudges.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

from aisquare.core import codenames, harness
from aisquare.core.config import FleetRoleSettings, FleetSettings, load_config
from aisquare.core.ids import new_agent_id
from aisquare.core.store import AmbiguousIdError, ContextStore, store_session
from aisquare.core.tmux import TmuxError, TmuxServer, TmuxUnavailable, WindowInfo
from aisquare.core.workspace import active_project
from aisquare.models import (
    FleetAgent,
    FleetAgentState,
    FleetAgentStatus,
    ProjectInfo,
    TeamSession,
    TeamTask,
)
from aisquare.services import explainability as explainability_service

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

NUDGE_TEXT = "aisquare: board update — read the delta above."
"""The one line a nudge types (§7.3). It carries nothing — the existing delta
injection on ``UserPromptSubmit`` delivers the details — and never starts with
``/`` or ``!``, Claude Code's command prefixes."""

NUDGE_DEBOUNCE = timedelta(seconds=5)
"""Two board writes inside this window wake the manager once."""

PROMPT_TIMEOUT = 20.0
"""How long ``spawn --prompt`` waits for the agent to come up before typing anyway."""

ACTIVITY_WINDOW = timedelta(seconds=5)
"""Output within this window reads as ``working`` for an agent the board cannot
vouch for (no hooks, or a stale row). Measured on tmux 3.7c: the plan's
``window_activity_flag`` is set the instant a detached window is created and never
clears without a client, so it cannot tell working from idle; ``#{window_activity}``
— the epoch second of the last output — can."""

_STATUS_WINDOW = 1.0
"""Seconds to keep polling a DEAD pane for its exit status (see ``stop``)."""

_POLL_INTERVAL = 0.25
_PROMPT_SETTLE = 2.0
"""After the agent process appears, its UI needs a moment before it reads input."""
_SHELLS = frozenset({"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "ash"})
_NOT_AN_AGENT = _SHELLS | {"tmux"}
"""Foreground commands that are never the agent: a shell (left behind after an
exit), and ``tmux`` itself — a new pane reads ``tmux`` for the first few hundred
milliseconds, between the fork and the exec (measured on 3.7c)."""
_LABEL_RETRIES = 5
_GIT_TIMEOUT = 120.0
_SEP = "|~|"
_ACTIVITY_FORMAT = f"#{{pane_id}}{_SEP}#{{window_activity}}"

#: Injection points for tests: a fake clock keeps the prompt wait and the stop
#: grace period instant without touching the logic they time.
_sleep: Callable[[float], None] = time.sleep
_monotonic: Callable[[], float] = time.monotonic

#: The board states a fresh ``team_session`` row can hand a fleet row (§5.1).
_BOARD_STATES: dict[str, FleetAgentState] = {
    "working": "working",
    "waiting": "waiting",
    "attention": "attention",
}


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


def _team() -> ModuleType:
    """``services.team``, imported on first use (see the module docstring)."""
    from aisquare.services import team

    return team


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _name(project: ProjectInfo) -> str:
    return project.root.name or project.id


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
    """``fleet/<codename>/<task-short-id>-<slug>``; ``fleet/<codename>/<slug>`` without a task.

    Every segment is built from slug-safe parts (a codename, a base32 id, a
    slug), so the result is a legal ref by construction and needs no
    ``git check-ref-format`` round trip.
    """
    parts = [f"fleet/{codename}"]
    leaf = slugify(title or "")
    if task_id:
        short = task_id.removeprefix("tsk_")[:TASK_SHORT]
        leaf = f"{short}-{leaf}" if leaf else short
    parts.append(leaf or "work")
    return "/".join(parts)


def is_git_project(root: Path) -> bool:
    """Whether the project root is a git checkout — the precondition for worktrees.

    A project root is either a repository root (``find_project_root`` resolves
    worktrees to their principal checkout, whose ``.git`` is a directory) or a
    plain directory holding several repos, which has no ``.git`` at all.
    """
    return (root / ".git").exists()


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
    if wanted == MANAGER_LABEL and role != "manager":
        raise FleetError(f"the label {MANAGER_LABEL!r} is reserved for the manager role")
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


# --- git: the fleet's one process seam besides tmux (§3.5) --------------------------


@dataclass(frozen=True)
class _GitResult:
    returncode: int
    stdout: str
    stderr: str


def _git(cwd: Path, *args: str) -> _GitResult:
    """THE git seam: worktree add/remove/list and the branch queries behind ``reap``.

    Registered in ``core.spawn.SEAMS`` as EXCLUDED and not stripped — git starts
    no model process, exactly like the ``rev-parse`` seams in ``core``. A
    non-zero exit is an answer (callers read ``returncode``); a missing git or
    a hang is not, and becomes a :class:`FleetError` with the reason.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise FleetError("git is not installed (or not on PATH) — worktrees need it") from exc
    except subprocess.TimeoutExpired as exc:
        raise FleetError(f"git {args[0]} did not finish within {_GIT_TIMEOUT:.0f} s") from exc
    return _GitResult(completed.returncode, completed.stdout, completed.stderr)


def _git_ok(cwd: Path, *args: str) -> str:
    """Run git and raise :class:`FleetError` carrying git's own words on failure."""
    result = _git(cwd, *args)
    if result.returncode != 0:
        reason = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise FleetError(f"git {' '.join(args)} failed: {reason}")
    return result.stdout


def _exclude_worktrees(root: Path, worktree_dir: str, notes: list[str]) -> None:
    """Add ``<worktree_dir>/`` to ``.git/info/exclude`` once — never to a tracked file.

    Fail-open: a read-only ``.git`` costs a note and an untracked directory in
    ``git status``, never the spawn.
    """
    line = worktree_dir.rstrip("/") + "/"
    try:
        common = _git_ok(root, "rev-parse", "--git-common-dir").strip()
        git_dir = Path(common) if Path(common).is_absolute() else root / common
        exclude = git_dir / "info" / "exclude"
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if line in existing.splitlines():
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(f"# aisquare fleet worktrees (docs/plans/fleet-tui.md §3.5)\n{line}\n")
    except (OSError, FleetError) as exc:
        notes.append(
            f"could not add {line} to .git/info/exclude ({exc}) — "
            "git status will list the worktrees as untracked"
        )


def _ensure_worktree(
    root: Path, worktree_dir: str, label: str, branch: str, notes: list[str]
) -> Path:
    """``<root>/<worktree_dir>/<label>`` on ``branch``, created or reused.

    Reuse is deliberate: a coder respawned on the same task after review
    findings must land in the tree that holds its branch, not beside it. A
    branch that already exists is checked out rather than recreated, so the
    second spawn continues the first one's work.
    """
    path = root / worktree_dir / label
    if path.exists():
        if (path / ".git").exists():
            notes.append(f"reusing the existing worktree at {path}")
            return path
        raise FleetError(
            f"{path} exists and is not a git worktree — remove it or pick another label"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    # A worktree whose directory was deleted by hand stays registered and blocks
    # `add` at the same path; prune is idempotent and cheap.
    _git(root, "worktree", "prune")
    if _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0:
        _git_ok(root, "worktree", "add", str(path), branch)
        notes.append(f"branch {branch} already existed — checked it out")
    else:
        _git_ok(root, "worktree", "add", str(path), "-b", branch)
    _exclude_worktrees(root, worktree_dir, notes)
    return path


def _worktree_branches(root: Path) -> dict[Path, str]:
    """Resolved worktree path → branch name, from ``git worktree list --porcelain``."""
    listed = _git(root, "worktree", "list", "--porcelain")
    if listed.returncode != 0:
        return {}
    branches: dict[Path, str] = {}
    current: Path | None = None
    for line in listed.stdout.splitlines():
        if line.startswith("worktree "):
            current = Path(line.removeprefix("worktree ")).resolve()
        elif line.startswith("branch ") and current is not None:
            branches[current] = line.removeprefix("branch ").removeprefix("refs/heads/")
        elif not line:
            current = None
    return branches


def _default_branch(root: Path) -> str:
    """The branch fleet work merges into: origin's HEAD, else main/master, else HEAD."""
    head = _git(root, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
    if head.returncode == 0 and head.stdout.strip():
        return head.stdout.strip()
    for name in ("main", "master"):
        if _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}").returncode == 0:
            return name
    return "HEAD"


def _merged_branches(root: Path) -> set[str]:
    """Branch names already merged into the default branch."""
    merged = _git(root, "branch", "--merged", _default_branch(root), "--format=%(refname:short)")
    if merged.returncode != 0:
        return set()
    return {line.strip() for line in merged.stdout.splitlines() if line.strip()}


def _remove_merged_worktrees(store: ContextStore, project: ProjectInfo, report: ReapReport) -> None:
    """Remove the worktrees of ENDED agents whose branch is merged — never unmerged work.

    A dirty worktree makes ``git worktree remove`` refuse, and that refusal is
    honoured: uncommitted changes are unmerged work too. A tree a live agent
    still sits in (a respawn reused it) is never touched.
    """
    root = project.root
    if not is_git_project(root):
        return
    candidates = [
        agent
        for agent in store.fleet_agents(project.id)
        if agent.ended_at is not None and agent.worktree and agent.cwd.exists()
    ]
    if not candidates:
        return
    in_use = {agent.cwd.resolve() for agent in store.fleet_agents(project.id, live_only=True)} | {
        root.resolve()
    }
    try:
        branches = _worktree_branches(root)
        merged = _merged_branches(root)
    except FleetError:
        return  # no git here: nothing can be removed safely, so nothing is
    removed: set[Path] = set()
    for agent in candidates:
        path = agent.cwd.resolve()
        if path in in_use or path in removed:
            continue
        branch = branches.get(path)
        if branch is None or branch not in merged:
            continue
        if _git(root, "worktree", "remove", str(agent.cwd)).returncode == 0:
            removed.add(path)
            report.worktrees_removed.append(agent.cwd)


# --- what tmux knows about a pane -----------------------------------------------------


@dataclass(frozen=True)
class _PaneView:
    """The facts state derivation needs, whichever tmux query produced them."""

    dead: bool
    dead_status: int | None
    current_command: str
    last_output: datetime | None
    """When the window last produced output, from ``#{window_activity}``; ``None``
    when tmux did not say."""


def _agent_running(current_command: str) -> bool:
    """Whether the pane's foreground process is the agent, not a shell or our launcher.

    ``python…`` is ``aisquare launch`` still resolving the role; ``tmux`` is the
    pane before its first exec; a shell is what remains when an agent started
    through one has exited. None of them is a place to type into.
    """
    command = current_command.strip()
    return bool(command) and command not in _NOT_AN_AGENT and not command.startswith("python")


def _activity_times(srv: TmuxServer) -> dict[str, datetime]:
    """Pane id → when its window last produced output, for every pane on the server.

    One ``list-panes -a`` covers every session, so a pane living under a session
    name tmux was never told to rename is found too. Fail-open: an unreadable
    answer costs the ``working`` / ``waiting`` distinction for hookless agents
    (they read as waiting), never the listing.
    """
    try:
        out = srv.run("list-panes", "-a", "-F", _ACTIVITY_FORMAT)
    except TmuxError:
        return {}
    times: dict[str, datetime] = {}
    for line in out.splitlines():
        pane_id, _, epoch = line.partition(_SEP)
        if pane_id and epoch.strip().isdigit():
            times[pane_id] = datetime.fromtimestamp(int(epoch), tz=UTC)
    return times


def _observe(
    srv: TmuxServer, tmux_session: str | None, agents: Sequence[FleetAgent]
) -> dict[str, _PaneView] | None:
    """Pane facts for every live agent, or ``None`` when tmux cannot be asked at all.

    One ``list-panes`` per project answers for every window in the session; a
    pane missing there is asked about individually, because a codename rename
    tmux never heard of leaves the panes alive under the old session name. The
    ``activity`` flag those answers carry is not used (see :data:`ACTIVITY_WINDOW`);
    the last-output time comes from one more ``list-panes`` over the server.
    """
    try:
        windows = (
            {window.pane_id: window for window in srv.list_windows(tmux_session)}
            if tmux_session
            else {}
        )
        live = [agent for agent in agents if agent.ended_at is None]
        output_at = _activity_times(srv) if live else {}
        seen: dict[str, _PaneView] = {}
        for agent in live:
            window = windows.get(agent.pane_id)
            if window is not None:
                seen[agent.pane_id] = _PaneView(
                    window.dead,
                    window.dead_status,
                    window.current_command,
                    output_at.get(agent.pane_id),
                )
                continue
            facts = srv.pane_facts(agent.pane_id)
            if facts is not None:
                seen[agent.pane_id] = _PaneView(
                    facts.dead,
                    facts.dead_status,
                    facts.current_command,
                    output_at.get(agent.pane_id),
                )
        return seen
    except TmuxError:
        return None


def _exit_detail(status: int | None) -> str | None:
    return f"exit {status}" if status is not None else None


def _derive(
    agent: FleetAgent,
    session: TeamSession | None,
    pane: _PaneView | None,
    *,
    observed: bool,
    now: datetime,
) -> tuple[FleetAgentState, str | None]:
    """State from the board first, tmux second (§5.1) — with one honest exception.

    A dead or vanished pane is a hard fact no board row can contradict: a
    process that has exited cannot be waiting, however recently its hooks
    fired — and a killed agent fires no ``SessionEnd``, so its row would say
    ``waiting`` for ``_STALE_AFTER`` otherwise. So death and absence are read
    first; then a fresh ``team_session`` row (not ended, seen within
    ``_STALE_AFTER``) decides between working, waiting and attention; then
    recent output (:data:`ACTIVITY_WINDOW`) means working; and when tmux cannot
    be asked at all the answer is ``unknown``, never a guess.
    """
    if agent.ended_at is not None:
        return "exited", _exit_detail(agent.exit_status)
    if observed:
        if pane is None:
            return "lost", "pane gone"
        if pane.dead:
            return "exited", _exit_detail(pane.dead_status)
    hooks = "no hooks" if agent.session_id is None else None
    if session is not None and session.ended_at is None:
        fresh = now - session.last_seen_at <= _team()._STALE_AFTER
        board_state = _BOARD_STATES.get(session.state)
        if fresh and board_state is not None:
            return board_state, None
    if not observed or pane is None:
        return "unknown", hooks or "tmux unavailable"
    busy = pane.last_output is not None and now - pane.last_output <= ACTIVITY_WINDOW
    return ("working" if busy else "waiting"), hooks


def _status(
    agent: FleetAgent,
    session: TeamSession | None,
    observed: dict[str, _PaneView] | None,
    tmux_session: str | None,
    now: datetime,
) -> FleetAgentStatus:
    pane = observed.get(agent.pane_id) if observed is not None else None
    state, detail = _derive(agent, session, pane, observed=observed is not None, now=now)
    return FleetAgentStatus(
        agent=agent, state=state, detail=detail, session=session, tmux_session=tmux_session
    )


def _require_tmux(srv: TmuxServer) -> None:
    try:
        srv.require()
    except TmuxUnavailable as exc:
        raise FleetUnavailable(str(exc)) from exc


def _role_ok(role: str) -> bool:
    """A fleet role, a harness role, or anything ``aisquare launch`` would accept."""
    if role in FLEET_ROLES or role in harness.ROLE_PROFILES:
        return True
    from aisquare.cli import launch  # lazy: the CLI package imports this service

    return launch._role_ok(role)


def _task_for(store: ContextStore, project: ProjectInfo, task_id: str | None) -> TeamTask | None:
    """The board task an agent is spawned for — on THIS project's board, or refused."""
    if task_id is None:
        return None
    try:
        task = store.get_task(task_id)
    except AmbiguousIdError as exc:
        raise FleetError(f"task {task_id!r} is ambiguous — use more of the id") from exc
    if task is None:
        raise FleetError(f"no task matches {task_id!r}")
    if task.project_id != project.id:
        raise FleetError(f"task {task_id!r} belongs to another project's board")
    return task


def _live_agent(store: ContextStore, project: ProjectInfo, label: str) -> FleetAgent:
    agent = store.fleet_agent_by_label(project.id, label, live_only=True)
    if agent is None:
        raise NoSuchAgent(
            f"no live agent {label!r} in {_name(project)} — "
            "`aisquare fleet ls` shows who is running"
        )
    return agent


# --- lifecycle ----------------------------------------------------------------------


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

    The window runs ``python -m aisquare launch <role> …`` (§3.4): permission
    mode as ``--permission-mode`` (flag > role config > ``auto``; the empty
    string passes no flag), the minted ``--session-id`` unless the caller
    already named or resumed a session, ``--name <label>``, then the role's
    ``extra_args`` and the caller's ``agent_args``. ``AISQUARE_FLEET_AGENT``
    carries the row id into the window; ``CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=0``
    keeps Claude's native teams out of the fleet unless configured otherwise (§7.6).
    """
    config = settings()
    if not _role_ok(role):
        raise FleetError(
            f"unknown role {role!r} — expected one of: {', '.join(FLEET_ROLES)}, a harness "
            "role, or one bound with `aisquare team bind`"
        )
    srv = server(config)
    _require_tmux(srv)
    resolution = harness.resolve_binary(role, override=binary)
    if shutil.which(resolution.binary) is None:
        raise FleetError(
            f"{resolution.binary!r} is not on your PATH (chosen by: {resolution.source}) — "
            "install it, pass --bin, or change the role's binding"
        )
    role_config = role_settings(role, config)
    notes: list[str] = []
    with store_session() as store:
        project = ensure_codename(project, store)
        codename = project.codename or codenames.codename_for(project.id)
        live = store.fleet_agents(project.id, live_only=True)
        if role == "manager":
            existing = next((agent for agent in live if agent.role == "manager"), None)
            if existing is not None:
                raise FleetError(
                    f"{_name(project)} already has a manager ({existing.id}) — one per "
                    "project; `aisquare fleet stop manager` first"
                )
        if len(live) >= config.max_agents_per_project:
            raise FleetError(
                f"{_name(project)} already runs {len(live)} agents "
                f"(max_agents_per_project = {config.max_agents_per_project}) — stop one, "
                "or raise the limit in [fleet]"
            )
        task = _task_for(store, project, task_id)
        resolved_task_id = task.id if task is not None else None
        picked = next_label(project, role, wanted=label, task_id=resolved_task_id, store=store)
    if label is not None and picked != label:
        if role == "manager":
            notes.append(f"the manager is always labelled {MANAGER_LABEL!r} (asked: {label!r})")
        else:
            notes.append(f"label {label!r} is held by a live agent — using {picked!r}")

    use_worktree = role_config.worktree if worktree is None else worktree
    cwd = project.root
    if use_worktree:
        if not is_git_project(project.root):
            raise FleetError(
                "not a git repository — spawn without --worktree or pick a repo inside it"
            )
        branch = branch_name(
            codename,
            task_id=resolved_task_id,
            title=task.title if task is not None else picked,
        )
        cwd = _ensure_worktree(project.root, config.worktree_dir, picked, branch, notes)

    mode = role_config.permission_mode if permission_mode is None else permission_mode
    role_args = list(role_config.extra_args)
    extra = list(agent_args)
    identity = explainability_service.plan_session_identity(resolution.binary, [*role_args, *extra])
    if identity.session_id is None:
        notes.append(
            f"no board join for this agent ({identity.note}) — its state comes from tmux alone"
        )
    agent_id = new_agent_id()
    flags: list[str] = []
    if binary is not None:
        # `launch` re-resolves the binary inside the window; an explicit --bin
        # must reach it, or the row would name one agent and the pane run another.
        flags += ["--command", resolution.binary]
    if mode:
        flags += ["--permission-mode", mode]
    flags += list(identity.inject_args)
    flags += ["--name", picked]
    command = [sys.executable, "-m", "aisquare", "launch", role, *flags, *role_args, *extra]
    env = {"AISQUARE_FLEET_AGENT": agent_id}
    if config.disable_native_agent_teams:
        env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "0"
    tmux_session = session_name(codename)
    try:
        window = srv.spawn_window(tmux_session, name=picked, cwd=cwd, command=command, env=env)
    except TmuxError as exc:
        raise FleetError(f"tmux could not start the window: {exc}") from exc

    agent = FleetAgent(
        id=agent_id,
        project_id=project.id,
        label=picked,
        role=role,
        binary=resolution.binary,
        tmux_socket=config.tmux_socket,
        pane_id=window.pane_id,
        session_id=identity.session_id,
        cwd=cwd,
        worktree=use_worktree,
        task_id=resolved_task_id,
        spawned_by=spawned_by,
        created_at=_now(),
    )
    stored = _record(agent, project, srv, wanted=label, notes=notes)
    if prompt:
        _type_prompt(srv, stored.pane_id, prompt, notes)
    return SpawnReceipt(agent=stored, asked_label=label, tmux_session=tmux_session, notes=notes)


def _record(
    agent: FleetAgent,
    project: ProjectInfo,
    srv: TmuxServer,
    *,
    wanted: str | None,
    notes: list[str],
) -> FleetAgent:
    """Write the row; on a live-label collision re-pick the label and retry.

    The window already exists by now, so the label is the one thing that can
    still be wrong: a parallel spawn may have taken it between ``next_label``
    and this write. The partial unique index says so with ``IntegrityError``;
    the suffixed label is recorded and the receipt says which. If no label can
    be recorded the window is killed rather than left running unrecorded.
    """
    with store_session() as store:
        for _ in range(_LABEL_RETRIES):
            try:
                return store.upsert_fleet_agent(agent)
            except sqlite3.IntegrityError:
                if agent.role == "manager":
                    break
                relabel = next_label(
                    project, agent.role, wanted=wanted, task_id=agent.task_id, store=store
                )
                notes.append(
                    f"label {agent.label!r} was taken while starting — recorded as "
                    f"{relabel!r} (the tmux window keeps its name)"
                )
                agent = agent.model_copy(update={"label": relabel})
    with contextlib.suppress(TmuxError):
        srv.kill_window(agent.pane_id)
    if agent.role == "manager":
        raise FleetError(f"{_name(project)} already has a manager — one per project")
    raise FleetError(f"could not record {agent.label!r}: every label tried was taken")


def _type_prompt(srv: TmuxServer, pane_id: str, prompt: str, notes: list[str]) -> None:
    """Wait (bounded) for the agent to come up, then paste the prompt and press Enter.

    Ready means the pane's foreground process is no longer our launcher (or it
    has produced scrollback). Past :data:`PROMPT_TIMEOUT` the prompt is typed
    anyway and the receipt says so — a slow start must not turn into a hang,
    and the text sits harmlessly in the input buffer until the agent reads it.
    """
    deadline = _monotonic() + PROMPT_TIMEOUT
    ready = False
    while True:
        facts = srv.pane_facts(pane_id)
        if facts is None or facts.dead:
            notes.append("the agent exited before the prompt could be typed")
            return
        if _agent_running(facts.current_command) or facts.history_size > 0:
            ready = True
            break
        if _monotonic() >= deadline:
            break
        _sleep(_POLL_INTERVAL)
    if ready:
        _sleep(_PROMPT_SETTLE)
    else:
        notes.append(
            f"the agent did not come up within {PROMPT_TIMEOUT:.0f} s — prompt typed anyway"
        )
    try:
        srv.paste(pane_id, prompt)
        srv.send_keys(pane_id, "Enter")
    except TmuxError as exc:
        notes.append(f"could not type the prompt: {exc}")


def list_agents(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
    """The project's agents with their DERIVED state (§5.1)."""
    srv = server()
    with store_session() as store:
        current = store.get_project(project.id) or project
        agents = store.fleet_agents(project.id, live_only=live_only)
        sessions = {session.id: session for session in store.team_sessions(project.id)}
    tmux_session = session_name(current.codename) if current.codename else None
    observed = _observe(srv, tmux_session, agents)
    now = _now()
    return [
        _status(
            agent,
            sessions.get(agent.session_id) if agent.session_id else None,
            observed,
            tmux_session,
            now,
        )
        for agent in agents
    ]


def status_of(agent: FleetAgent) -> FleetAgentStatus:
    """One agent's derived state: board session first, tmux facts second."""
    srv = server()
    with store_session() as store:
        project = store.get_project(agent.project_id)
        session = store.get_session(agent.session_id) if agent.session_id else None
    tmux_session = session_name(project.codename) if project and project.codename else None
    observed = _observe(srv, tmux_session, [agent])
    return _status(agent, session, observed, tmux_session, _now())


def manager_of(project: ProjectInfo) -> FleetAgent | None:
    """The project's live manager, if any."""
    with store_session() as store:
        agent = store.fleet_agent_by_label(project.id, MANAGER_LABEL, live_only=True)
    return agent if agent is not None and agent.role == "manager" else None


def tell(project: ProjectInfo, label: str, text: str, *, sender: str | None = None) -> TellResult:
    """Type ``text`` into a WAITING agent; otherwise file it as a board note to it.

    Typing into a working agent would interleave with its turn; into one that
    needs the human, it could answer a permission prompt. So anything but
    ``waiting`` with a live pane goes to the board, addressed to the label,
    where the agent's next delta injection delivers it.
    """
    with store_session() as store:
        agent = _live_agent(store, project, label)
    status = status_of(agent)
    if status.state == "waiting":
        srv = server()
        try:
            srv.paste(agent.pane_id, text)
            srv.send_keys(agent.pane_id, "Enter")
        except TmuxError as exc:
            how = _file_note(project, label, text, sender)
            return TellResult(False, f"tmux could not type it ({exc}) — {how}")
        return TellResult(True, "typed into its pane (it was waiting)")
    how = _file_note(project, label, text, sender)
    return TellResult(False, f"it is {status.state} — {how}")


def _file_note(project: ProjectInfo, label: str, text: str, sender: str | None) -> str:
    team = _team()
    try:
        event = team.add_note(text, session_ref=sender, to_role=label, cwd=project.root)
    except team.TeamDisabledError as exc:
        raise FleetError(f"cannot file the message as a board note: {exc}") from exc
    except KeyError as exc:
        raise FleetError(f"unknown sender session {sender!r}") from exc
    return f"filed as board note #{event.seq} to {label}"


def stop(
    project: ProjectInfo, label: str, *, force: bool = False, grace: float = 5.0
) -> FleetAgent:
    """``/exit`` the agent, wait ``grace`` seconds, then kill its window.

    The agent's own ``SessionEnd`` hook releases its claims when it exits
    cleanly; ``force`` skips the ``/exit`` and goes straight to the kill.
    """
    with store_session() as store:
        agent = _live_agent(store, project, label)
    srv = server()
    session = session_name(ensure_codename(project).codename or "")
    exit_status: int | None = None

    def _window() -> WindowInfo | None:
        """The agent's window as ``list-panes -s`` reports it; ``None`` = really gone.

        Deliberately NOT ``pane_facts`` (display-message): on some tmux
        versions a JUST-DEAD pane answers display-message as the wrong pane,
        which the guard there maps to None — indistinguishable from gone, and
        the exit status is lost. The window list names dead panes reliably on
        every version this repo has measured (3.3a, 3.4, 3.5a, 3.7c).
        """
        for window in srv.list_windows(session):
            if window.pane_id == agent.pane_id:
                return window
        return None

    try:
        window = _window()
        if not force and window is not None and not window.dead:
            srv.send_literal(agent.pane_id, "/exit")
            srv.send_keys(agent.pane_id, "Enter")
            deadline = _monotonic() + grace
            dead_seen: float | None = None
            while True:
                window = _window()
                if window is None:
                    break
                if window.dead:
                    if window.dead_status is not None:
                        break
                    # A dead pane usually gets its exit status a poll later —
                    # but tmux 3.4 sometimes NEVER exposes it (measured in its
                    # own -vv server log: dead=1 with `pane_dead_status` empty
                    # on every poll for a full 15 s grace, about one death in
                    # thirty under one CPU; 3.5a and 3.7c always expose it).
                    # One second is plenty for a status that will ever land.
                    if dead_seen is None:
                        dead_seen = _monotonic()
                    elif _monotonic() - dead_seen >= _STATUS_WINDOW:
                        break
                if _monotonic() >= deadline:
                    break
                _sleep(_POLL_INTERVAL)
        if window is not None and window.dead:
            exit_status = window.dead_status
        with suppress(TmuxError):  # already gone — which is what the kill wanted
            srv.kill_window(agent.pane_id)
    except TmuxError:
        pass  # the pane is gone or tmux is: the row still ends
    with store_session() as store:
        return store.end_fleet_agent(agent.id, exit_status=exit_status)


def reap(project: ProjectInfo | None = None) -> ReapReport:
    """Record dead panes as ended, mark vanished panes lost, remove merged worktrees.

    When tmux cannot be asked nothing is marked: absence of evidence is not a
    dead pane, and an unreachable tmux must not end rows whose processes may
    still be running.
    """
    srv = server()
    report = ReapReport()
    with store_session() as store:
        if project is not None:
            projects = [store.get_project(project.id) or project]
        else:
            projects = store.list_projects()
        for current in projects:
            live = store.fleet_agents(current.id, live_only=True)
            if live:
                tmux_session = session_name(current.codename) if current.codename else None
                observed = _observe(srv, tmux_session, live)
                if observed is not None:
                    for agent in live:
                        pane = observed.get(agent.pane_id)
                        if pane is None:
                            report.lost.append(store.end_fleet_agent(agent.id, exit_status=None))
                        elif pane.dead:
                            ended = store.end_fleet_agent(agent.id, exit_status=pane.dead_status)
                            report.ended.append(ended)
                            _emit_exit(store, ended)
            _remove_merged_worktrees(store, current, report)
    for ended in report.ended:
        nudge_manager(ended.project_id, reason=f"{ended.label} exited")
    return report


def _emit_exit(store: ContextStore, agent: FleetAgent) -> None:
    """The ``agent_exited`` board event the manager's Stop hook wakes on (§7.3)."""
    status = "?" if agent.exit_status is None else str(agent.exit_status)
    # The row is already ended; the event is the courtesy, not the record.
    with contextlib.suppress(Exception):
        _team()._emit(
            store,
            agent.project_id,
            "agent_exited",
            f"{agent.label} exited ({status})",
            session_id=agent.session_id,
        )


def rename(project: ProjectInfo, codename: str) -> ProjectInfo:
    """Set the fleet codename (validated, unique) and rename the tmux session with it.

    The row is renamed even when tmux is unreachable — ``reap`` reconciles from
    the panes themselves, which are addressed by id, not by session name.
    """
    if not codenames.is_codename(codename):
        raise FleetError(
            f"codename {codename!r} must look like adjective-animal: two words of "
            "3 to 7 lowercase letters joined by '-'"
        )
    with store_session() as store:
        store.ensure_project(project)
        current = store.get_project(project.id) or project
        old = current.codename
        if old == codename:
            return current
        try:
            updated = store.set_codename(project.id, codename)
        except sqlite3.IntegrityError as exc:
            raise FleetError(f"codename {codename!r} is already taken by another project") from exc
    if old:
        srv = server()
        try:
            if srv.has_session(session_name(old)):
                srv.rename_session(session_name(old), session_name(codename))
        except TmuxError:
            pass
    return updated


def pause(project: ProjectInfo, *, session_ref: str | None = None) -> None:
    """Set the ``fleet-paused`` board signal — the manager spawns nothing while it is on."""
    _set_pause(project, "on", session_ref)


def resume(project: ProjectInfo, *, session_ref: str | None = None) -> None:
    _set_pause(project, "off", session_ref)


def _set_pause(project: ProjectInfo, value: str, session_ref: str | None) -> None:
    team = _team()
    try:
        team.set_signal(PAUSE_SIGNAL, value, session_ref=session_ref, cwd=project.root)
    except team.TeamDisabledError as exc:
        raise FleetError(f"cannot set the pause signal: {exc}") from exc
    except KeyError as exc:
        raise FleetError(f"unknown session {session_ref!r}") from exc


def is_paused(project: ProjectInfo) -> bool:
    team = _team()
    try:
        state = team.read_signal(PAUSE_SIGNAL, cwd=project.root)
    except team.TeamDisabledError:
        return False  # a disabled board holds no signals: nothing can be paused
    return state is not None and state.value == "on"


def nudge_manager(project_id: str, *, reason: str) -> bool:
    """Wake a WAITING manager with one fixed line + Enter (§7.3); ``False`` if not sent.

    Never raises and never touches a manager that is working or needs the
    human: this runs inside sub-agents' CLI processes, where a failure must
    cost nothing but the nudge. ``reason`` is for the caller's own log — the
    nudge text is fixed and carries nothing (the delta injection does).
    """
    try:
        with store_session() as store:
            manager = store.fleet_agent_by_label(project_id, MANAGER_LABEL, live_only=True)
            if manager is None or manager.session_id is None:
                return False
            session = store.get_session(manager.session_id)
            if session is None or session.ended_at is not None or session.state != "waiting":
                return False
            key = f"nudge:{session.id}"
            now = _now()
            last = store.get_meta(key)
            if last is not None and now - datetime.fromisoformat(last) < NUDGE_DEBOUNCE:
                return False
            srv = server()
            facts = srv.pane_facts(manager.pane_id)
            if facts is None or facts.dead or not _agent_running(facts.current_command):
                return False
            store.set_meta(key, now.isoformat())
            srv.send_literal(manager.pane_id, NUDGE_TEXT)
            srv.send_keys(manager.pane_id, "Enter")
            return True
    except Exception:
        return False


def attach_argv(project: ProjectInfo) -> list[str]:
    """The ``tmux attach`` command for the project's fleet session (the escape hatch).

    Refuses when the session does not exist. ``tmux attach`` would only answer
    with its own "can't find session" — and the CLI ``exec``s this argv, so
    nothing here may hand over an attach that has nothing to attach to.
    """
    srv = server()
    _require_tmux(srv)
    project = ensure_codename(project)
    name = session_name(project.codename or codenames.codename_for(project.id))
    if not srv.has_session(name):
        raise FleetError(
            f"no fleet session {name} for {_name(project)} — nothing to attach to; "
            "`aisquare fleet spawn manager` starts one"
        )
    return srv.attach_argv(name)
