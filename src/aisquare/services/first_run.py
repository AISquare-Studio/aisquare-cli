"""The guided first run behind the Welcome page: what this machine has, and the fleet it starts.

Roadmap 9.2. The Welcome view walks a new user from an empty home to a manager
and two coders in three steps: a project, Claude Code installed and connected,
then the fleet. What the view needs to ASK of the machine is here, as data, and
so is the one thing it DOES that no other service did for it: start the fleet.
Adding a project is ``services.onboarding`` (``init`` then ``doctor``, as the
Onboard view runs them), and connecting Claude Code is the doctor's own
one-click fix (``onboarding.KNOWN_FIXES``), so neither has a second
implementation here.

Like ``services.onboarding``, this module never raises: every probe takes its
seams as parameters, so a test hands in fakes, and one that fails answers with
a state that says why. Its callers are background workers whose only way to
report is what they are handed back.

Two rules shape :func:`start_fleet`:

- **It never types into an agent it starts.** No ``prompt=`` reaches
  ``fleet.spawn`` (:class:`Spawner` has no such parameter, so mypy holds this as
  well as the tests). A folder Claude Code has not seen makes it ask, once,
  whether to trust the folder, and the fleet's prompt typing decides when the
  agent is ready from the pane's foreground process, not from its screen. #240's
  captain work measured that typing into that question picks "No, exit". The user
  answers it in the manager's pane, and the coders start after that, from their
  own button. A coder whose window is gone is not started but restarted
  (:class:`Restarter`), as the agent view's Restart does it, and restart types its
  own line: that coder has run in its folder before.
- **It is idempotent.** It never starts a second manager, and it tops the coders
  up to :data:`CODERS` rather than adding two more each time it runs.
"""

from __future__ import annotations

import dataclasses
import shutil
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from aisquare.core import agents as agent_core
from aisquare.core import claude_accounts as accounts_core
from aisquare.core import harness, selfcli
from aisquare.core import tmux as tmux_core
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo
from aisquare.services import agents as agents_service
from aisquare.services import diagnostics, onboarding
from aisquare.services import fleet as fleet_service
from aisquare.services.onboarding import FixCommand, FixResult, PathVerdict, Runner

CODERS = 2
"""How many coders the Welcome page starts beside the manager."""

FLEET_ROLE = "manager"
"""The role whose binary the Claude Code step looks for: the first agent Welcome starts."""

CANDIDATE_LIMIT = 4
"""How many registered projects step 1 offers beside the directory ``asq`` started in."""

BREW_COMMAND = "brew install --cask claude-code"
"""Claude Code's Homebrew cask, offered on macOS after the native installer."""

CONNECT_ARGV: tuple[str, ...] = ("agents", "connect", "claude-code")
"""The doctor's one-click Connect, as ``onboarding.KNOWN_FIXES`` lists it."""


def _why(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


# --------------------------------------------------------------------------- Claude Code


@dataclass(frozen=True)
class InstallRoute:
    """One way to install Claude Code: how it is installed, and the command a terminal runs."""

    how: str
    command: str


def install_routes(platform: str | None = None) -> tuple[InstallRoute, ...]:
    """Claude Code's install commands for ``platform`` (default: this one), best first.

    The native installer comes first: it is what ``install.sh`` runs and the layout
    ``diagnostics.claude_code_version`` reads first. npm comes last everywhere,
    because it needs a Node.js the machine may not have. On Windows the fleet
    runs inside WSL2, where tmux is, so the one route is the installer run there.
    """
    where = platform if platform is not None else sys.platform
    native = InstallRoute("installer", accounts_core.INSTALL_COMMAND)
    npm = InstallRoute("npm", accounts_core.INSTALL_ALTERNATIVE)
    if where == "win32":
        return (InstallRoute("inside WSL2", accounts_core.INSTALL_COMMAND),)
    if where == "darwin":
        return (native, InstallRoute("Homebrew", BREW_COMMAND), npm)
    return (native, npm)


@dataclass(frozen=True)
class ClaudeState:
    """What step 2 shows: the agent the fleet launches, where it is, and whether it is wired."""

    wanted: str
    """What a fleet launch starts: ``claude``, or the override the user set."""
    source: str
    """Which rung chose it (``harness.BinaryResolution.source``): ``default``, ``env``…"""
    binary: str | None = None
    """Where it was found on ``PATH``; ``None`` when it is not there."""
    version: str | None = None
    connected: bool = False
    """aisquare's hooks are in the config dir a session from this shell reads, and run
    where the fleet's sessions start: the manager's folder, and the coders'."""
    manager_only: bool = False
    """They run where the manager starts but not where the coders do: a plugin installed
    for the project's folder alone, which the coders' worktrees do not load."""
    hooks_off: Path | None = None
    """The settings file that switches every hook off (``"disableAllHooks": true``),
    when one does: Connect cannot change it, so step 2 says so instead of offering it."""
    refused: str | None = None
    """Why `agents connect` would refuse the settings file (not a JSON object, or one
    this user may not write), when it would: Connect could only fail, so step 2 says
    why instead of offering it (review of #257)."""
    signed_in: bool | None = None
    """``None`` when this probe did not look (the periodic one skips it)."""
    problem: str | None = None
    """A probe that failed, in words; whatever it could not read is left at its default."""

    @property
    def found(self) -> bool:
        return self.binary is not None

    @property
    def is_claude(self) -> bool:
        """Whether the fleet launches Claude Code itself rather than a binary the user chose."""
        return harness.is_default_agent(self.wanted)

    @property
    def ready(self) -> bool:
        """Installed and connected: what step 3 waits for. Signing in is not required here,
        because Claude Code asks for it the first time it starts."""
        return self.found and self.connected


def _connected_default(cwd: Path | None = None) -> bool:
    return agents_service.claude_code_connected(cwd=cwd)


def coder_folder(root: Path) -> Path:
    """The folder the shared "connected?" check is asked about for the coders started in ``root``.

    ``root`` itself, unless it is a git repository whose coder role takes worktrees (the
    default): each coder then works in a worktree made in ``<root>/<worktree_dir>``, and
    that folder is asked about. The check reads a session there as it reads a coder's: a
    plugin installed at project scope for ``root`` counts only in ``root``, and ``root``'s
    local settings are found from inside it. Asked about a coder's worktree itself, it
    stops at the worktree's ``.git`` file, short of the local settings Claude Code
    follows a worktree to.
    """
    config = fleet_service.settings()
    try:
        worktrees = fleet_service.role_settings("coder", config).worktree
        git = fleet_service.is_git_project(root)
    except OSError:  # a root this user cannot look into: no worktree is made there either
        return root
    return root / config.worktree_dir if worktrees and git else root


def _hooks_off_default() -> Path | None:
    """The settings file that switches Claude Code's hooks off here, if one does."""
    if not agent_core.hooks_disabled("claude-code"):
        return None
    where = agent_core.ambient_hook_dir("claude-code")
    return where / "settings.json" if where is not None else None


def _refusal_default() -> str | None:
    return agents_service.connect_refusal("claude-code")


def _signed_in_default() -> bool:
    return accounts_core.signed_in(accounts_core.default_account())


def probe_claude(
    *,
    sign_in: bool = True,
    which: Callable[[str], str | None] = shutil.which,
    connected: Callable[[], bool] | None = None,
    signed_in: Callable[[], bool] | None = None,
    hooks_off: Callable[[], Path | None] | None = None,
    refusal: Callable[[], str | None] | None = None,
    cwd: Path | None = None,
) -> ClaudeState:
    """Claude Code as the fleet will meet it. Starts no process; never raises.

    The binary is the one ``fleet.spawn`` would launch for the manager
    (``harness.resolve_binary``), looked up the way spawn looks it up, so an
    ``AISQUARE_AGENT_BIN`` stand-in is what this finds too. ``connected`` is the
    one shared answer to "is Claude Code connected" (``agents.claude_code_connected``),
    which the doctor's row asks as well. ``sign_in`` reads the plain ``claude``'s
    login, which lives in a file that can be tens of megabytes, so the periodic
    re-check leaves it out. Not connected, it asks ``core.agents.hooks_disabled``
    first, as the shared check's contract says: ``"disableAllHooks": true`` reads as
    not connected, and no Connect can change it. Nor can it write a settings file
    ``agents connect`` refuses (``refusal``). ``cwd`` is the folder the fleet starts
    in, which decides a project- or local-scope plugin; asq's own unless given. Given,
    the coders' folder (:func:`coder_folder`) is asked about too: connected for the
    manager alone is ``manager_only``, and not connected.
    """
    problems: list[str] = []
    try:
        resolution = harness.resolve_binary(FLEET_ROLE)
    except Exception as exc:  # a config that will not parse costs the override, not the step
        problems.append(f"could not read the agent binding: {_why(exc)}")
        resolution = harness.BinaryResolution(binary=harness.DEFAULT_AGENT_BINARY, source="default")
    try:
        binary = which(resolution.binary)
    except Exception as exc:
        problems.append(f"could not look for {resolution.binary}: {_why(exc)}")
        binary = None
    version = None
    if binary is not None and harness.is_default_agent(resolution.binary):
        version = diagnostics.claude_code_version(binary)
    manager_only = False
    try:
        # Asked about the folder the fleet starts in (``cwd``), where a project- or
        # local-scope plugin may be the route (review of #257), and where its coders
        # start, which a plugin installed for that folder alone does not reach: the
        # coders ran without aisquare under "Your fleet is up." (sweep 2 of #257).
        is_connected = connected() if connected is not None else _connected_default(cwd)
        if is_connected and connected is None and cwd is not None:
            coders = coder_folder(cwd)
            manager_only = coders != cwd and not _connected_default(coders)
            is_connected = not manager_only
    except Exception as exc:
        problems.append(f"could not read the hooks: {_why(exc)}")
        is_connected, manager_only = False, False
    switched_off: Path | None = None
    if not is_connected:
        try:
            switched_off = (hooks_off or _hooks_off_default)()
        except Exception as exc:
            problems.append(f"could not read the hook settings: {_why(exc)}")
    refused: str | None = None
    if not is_connected and switched_off is None:
        try:
            refused = (refusal or _refusal_default)()
        except Exception as exc:
            problems.append(f"could not read the hook settings: {_why(exc)}")
    signed: bool | None = None
    if sign_in and binary is not None:
        try:
            signed = (signed_in or _signed_in_default)()
        except Exception as exc:
            problems.append(f"could not read the sign-in: {_why(exc)}")
    return ClaudeState(
        wanted=resolution.binary,
        source=resolution.source,
        binary=binary,
        version=version,
        connected=is_connected,
        manager_only=manager_only,
        hooks_off=switched_off,
        refused=refused,
        signed_in=signed,
        problem="; ".join(problems) or None,
    )


def connect_fix() -> FixCommand:
    """The doctor's Connect, taken from ``onboarding.KNOWN_FIXES`` — what one click may run."""
    known = next(k for k in onboarding.KNOWN_FIXES if k.argv == CONNECT_ARGV)
    return FixCommand(
        check="claude-code",
        argv=known.argv,
        scope=known.scope,
        source=f"aisquare {' '.join(known.argv)}",
    )


def connect(*, run: Runner = selfcli.run) -> FixResult:
    """``aisquare --json agents connect claude-code``, exactly as the doctor's button runs it.

    An npm or Homebrew Claude Code that has never started has no ``~/.claude`` yet;
    ``agents connect`` makes it when ``claude`` is on PATH, for every caller
    (``services.agents``), so this is the doctor's fix and nothing else.
    """
    return onboarding.apply_fix(connect_fix(), None, run=run)


# --------------------------------------------------------------------------- tmux and gh


@dataclass(frozen=True)
class TmuxState:
    """Whether the fleet's substrate is here: tmux, new enough."""

    found: bool
    version: tuple[int, int] | None = None
    problem: str | None = None
    """Why the fleet cannot use it; ``None`` when it can."""
    hint: str | None = None
    """The install or upgrade command for this machine."""

    @property
    def ok(self) -> bool:
        return self.found and self.problem is None


def probe_tmux(
    server: tmux_core.TmuxServer | None = None, *, platform: str | None = None
) -> TmuxState:
    """tmux present and at least ``core.tmux.MIN_VERSION``, as ``fleet.spawn`` requires.

    ``tmux -V`` goes through the one tmux seam; a version that cannot be read
    passes, as it does for ``TmuxServer.require``. Never raises.
    """
    hint = diagnostics.install_hint("tmux", platform=platform if platform else sys.platform)
    try:
        srv = server if server is not None else tmux_core.TmuxServer()
        if not srv.available():
            return TmuxState(found=False, problem="tmux is not installed", hint=hint)
    except Exception as exc:
        return TmuxState(found=False, problem=f"tmux could not be checked: {_why(exc)}", hint=hint)
    try:
        version = srv.version()
    except Exception:
        version = None
    if version is not None and version < tmux_core.MIN_VERSION:
        minimum = f"{tmux_core.MIN_VERSION[0]}.{tmux_core.MIN_VERSION[1]}"
        return TmuxState(
            found=True,
            version=version,
            problem=f"tmux {version[0]}.{version[1]} is too old — the fleet needs {minimum}",
            hint=hint,
        )
    return TmuxState(found=True, version=version)


def gh_found(*, which: Callable[[str], str | None] = shutil.which) -> bool:
    """Whether the GitHub CLI is here: the coders open their pull requests with it."""
    try:
        return which("gh") is not None
    except Exception:
        return False


# --------------------------------------------------------------------------- the project


@dataclass(frozen=True)
class Candidate:
    """A folder step 1 offers: the directory ``asq`` started in, or a registered project."""

    root: Path
    is_git: bool
    project: ProjectInfo | None = None
    """The registered project at ``root``, when it is listed already; ``None`` means
    choosing it runs onboarding first."""
    here: bool = False
    """The directory ``asq`` was started in (or the repository containing it)."""

    @property
    def name(self) -> str:
        return self.root.name or str(self.root)


@dataclass(frozen=True)
class Candidates:
    items: tuple[Candidate, ...]
    store_error: str | None = None
    """Why the registered projects are missing from ``items``."""


def _listed_projects() -> list[ProjectInfo]:
    with store_session() as store:
        return store.list_projects()


def _never_offered(root: Path, home: Path) -> bool:
    """The home directory and a filesystem root are never a project to offer."""
    try:
        resolved = root.resolve()
        return resolved == home.resolve() or resolved == Path(resolved.anchor)
    except OSError:
        return True


def _git_inside(root: Path) -> bool | None:
    """``fleet.is_git_project``'s answer for ``root``, or ``None`` when this user cannot look in.

    One ``stat``, with "missing" split out, so every Python answers alike. For a
    folder this user can see but not enter, ``Path.exists`` raises PermissionError
    on 3.11 to 3.13 and answers False on 3.14, where step 1 offered the folder as
    "not a git repository".
    """
    try:
        (root / ".git").stat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return None
    return True


def candidates(
    cwd: Path | None = None,
    *,
    home: Path | None = None,
    projects: Callable[[], list[ProjectInfo]] | None = None,
    validate: Callable[[str], PathVerdict] = onboarding.validate_path,
    limit: int = CANDIDATE_LIMIT,
) -> Candidates:
    """The directory ``asq`` started in, then up to ``limit`` registered projects. Never raises.

    No directory is scanned for repositories: the user's current directory and
    the projects already registered are the only guesses, and the path input
    beside them takes anything else. ``$HOME`` itself is never offered, and
    nor is a registered project whose folder has gone, or one this user cannot
    enter (another user's, a folder at mode 600): agents could not work there.
    """
    where = home if home is not None else Path.home()
    items: list[Candidate] = []
    try:
        start = cwd if cwd is not None else Path.cwd()
    except OSError:
        start = None
    if start is not None:
        try:
            verdict = validate(str(start))
        except Exception:
            verdict = None
        root = verdict.root if verdict is not None and verdict.ok else None
        if verdict is not None and root is not None and not _never_offered(root, where):
            listed = verdict.registered
            items.append(
                Candidate(
                    root=root,
                    is_git=verdict.is_git,
                    project=listed if listed is not None and listed.onboarded_at else None,
                    here=True,
                )
            )
    store_error: str | None = None
    try:
        listed_projects = (projects or _listed_projects)()
    except Exception as exc:  # a locked or damaged store costs the list, never the page
        listed_projects = []
        store_error = _why(exc)
    taken = {item.root.resolve() for item in items}
    extra = 0
    for project in listed_projects:
        if extra >= limit:
            break
        try:
            root = project.root.resolve()
            present = project.root.is_dir()
            is_git = _git_inside(project.root) if present else False
        except (OSError, RuntimeError):  # RuntimeError: a symlink loop, on 3.11/3.12
            continue
        if root in taken or not present or is_git is None or _never_offered(project.root, where):
            continue
        taken.add(root)
        items.append(
            Candidate(
                root=project.root,
                is_git=is_git,
                # A captured row (the shell lists them while `a` is on) was never
                # added on purpose: choosing it onboards it, as for any folder.
                project=project if project.onboarded_at else None,
            )
        )
        extra += 1
    return Candidates(items=tuple(items), store_error=store_error)


# --------------------------------------------------------------------------- the fleet


StepOutcome = Literal["started", "running", "refused"]


@dataclass(frozen=True)
class FleetStep:
    """One agent of the fleet Welcome starts, and what happened to it."""

    label: str
    role: str
    outcome: StepOutcome
    detail: str = ""
    """The agent's id when it started; the reason when it was refused."""
    agent: FleetAgent | None = None
    notes: tuple[str, ...] = ()
    """What the spawn's receipt said beside the agent (a reused worktree, a size note)."""


@dataclass(frozen=True)
class FleetStart:
    """Everything one :func:`start_fleet` call did, in order; it stops at the first refusal."""

    steps: tuple[FleetStep, ...]

    @property
    def refused(self) -> FleetStep | None:
        return next((step for step in self.steps if step.outcome == "refused"), None)

    @property
    def manager(self) -> FleetAgent | None:
        return next((s.agent for s in self.steps if s.role == "manager" and s.agent), None)


class Spawner(Protocol):
    """The part of ``fleet.spawn`` Welcome uses — no ``prompt`` (see the module docstring)."""

    def __call__(
        self,
        project: ProjectInfo,
        role: str,
        *,
        label: str | None = None,
        worktree: bool | None = None,
    ) -> fleet_service.SpawnReceipt: ...


class Restarter(Protocol):
    """The part of ``fleet.restart`` Welcome uses: the row it means, by label and id."""

    def __call__(
        self, project: ProjectInfo, label: str, *, agent_id: str | None = None
    ) -> fleet_service.RestartReceipt: ...


RUNNING: frozenset[str] = frozenset({"working", "waiting", "attention", "limited"})
"""The states in which the fleet's listing sees an agent there: all the Welcome page
calls running. ``unknown`` is no verdict (tmux could not be asked: after a reboot every
row reads so), and ``lost`` (its window is gone) and ``exited`` agents are not there."""

LiveAgents = Callable[[ProjectInfo], list[FleetAgentStatus]]


def live_agents(project: ProjectInfo) -> list[FleetAgentStatus]:
    """The project's agents whose rows are not ended, with the state the listing derives.

    Each holds its label and a place under the cap, as ``fleet.spawn`` counts them,
    whether it is running or not.
    """
    return [s for s in fleet_service.list_agents(project) if s.agent.ended_at is None]


def _free_labels(role: str, count: int, held: set[str]) -> Iterator[str]:
    """``<role>-1``, ``<role>-2``… skipping the labels a live agent holds; ``count`` of them."""
    n = 0
    given = 0
    while given < count:
        n += 1
        label = f"{role}-{n}"
        if label not in held:
            given += 1
            yield label


NOT_GIT_NOTE = "not a git repository, so the coders work in the project folder (no worktrees)"


def _spawn(
    spawn: Spawner, project: ProjectInfo, role: str, *, label: str | None, worktree: bool | None
) -> FleetStep:
    try:
        receipt = spawn(project, role, label=label, worktree=worktree)
    except fleet_service.FleetError as exc:
        return FleetStep(label=label or role, role=role, outcome="refused", detail=_why(exc))
    except Exception as exc:  # a bug in the fleet path is a refusal to show, not a crash
        return FleetStep(
            label=label or role,
            role=role,
            outcome="refused",
            detail=f"{type(exc).__name__}: {_why(exc)}",
        )
    agent = receipt.agent
    return FleetStep(
        label=agent.label,
        role=role,
        outcome="started",
        detail=agent.id,
        agent=agent,
        notes=tuple(receipt.notes),
    )


def _restart(restart: Restarter, project: ProjectInfo, agent: FleetAgent) -> FleetStep:
    try:
        # Pinned to the row the listing read, as the agent view's Restart pins it: a label
        # restarted elsewhere since (by the manager) is that agent's, and is left running.
        receipt = restart(project, agent.label, agent_id=agent.id)
    except fleet_service.FleetError as exc:
        return FleetStep(label=agent.label, role=agent.role, outcome="refused", detail=_why(exc))
    except Exception as exc:  # a bug in the fleet path is a refusal to show, not a crash
        return FleetStep(
            label=agent.label,
            role=agent.role,
            outcome="refused",
            detail=f"{type(exc).__name__}: {_why(exc)}",
        )
    started = receipt.started
    return FleetStep(
        label=started.label,
        role=agent.role,
        outcome="started",
        detail=started.id,
        agent=started,
        notes=(f"restarted — {receipt.how}", *receipt.notes),
    )


def start_fleet(
    project: ProjectInfo,
    *,
    manager: bool = True,
    coders: int = CODERS,
    spawn: Spawner | None = None,
    live: LiveAgents | None = None,
    on_step: Callable[[FleetStep], None] | None = None,
    restart: Restarter | None = None,
) -> FleetStart:
    """Start the manager and top the coders up to ``coders``, one at a time; never raises.

    A running manager (:data:`RUNNING`) is reported as ``running`` and never
    spawned again; running coders count toward ``coders``, so a second call
    starts only what is missing. A coder that is not running is not counted,
    and its row keeps its label and a place under the cap until it is
    restarted or reaped. One whose window is gone (``lost``: after a reboot, or
    closed by hand) is restarted under its own label (``fleet.restart``), with
    its worktree, its account and its session when that can be resumed; one
    tmux cannot answer for (``unknown``) may still run, so the one started in
    its place takes the next free label. A manager in either state is
    ``fleet.spawn``'s to refuse, with the way to clear it. Coders take the
    role's worktree default in a git repository and ``worktree=False``
    elsewhere, with a note. The first refusal (no tmux, the agent cap, a
    worktree git will not make) stops the call, and its reason is on its step.
    ``on_step`` hears each step as it lands, for a caller that shows progress.
    Nothing is typed into an agent it starts: see the module docstring.
    """
    spawner: Spawner = spawn if spawn is not None else fleet_service.spawn
    restarter: Restarter = restart if restart is not None else fleet_service.restart
    steps: list[FleetStep] = []

    def done(step: FleetStep) -> bool:
        steps.append(step)
        if on_step is not None:
            on_step(step)
        return step.outcome != "refused"

    try:
        listed = (live or live_agents)(project)
    except Exception as exc:
        done(
            FleetStep(
                label="fleet",
                role="",
                outcome="refused",
                detail=f"could not read the fleet: {_why(exc)}",
            )
        )
        return FleetStart(tuple(steps))
    running = [status.agent for status in listed if status.state in RUNNING]
    if manager:
        existing = next((agent for agent in running if agent.role == "manager"), None)
        if existing is not None:
            done(FleetStep(existing.label, "manager", "running", existing.id, existing))
        elif not done(_spawn(spawner, project, "manager", label=None, worktree=None)):
            return FleetStart(tuple(steps))
    live_coders = [agent for agent in running if agent.role == "coder"]
    for agent in live_coders[:coders]:
        done(FleetStep(agent.label, "coder", "running", agent.id, agent))
    missing = max(0, coders - len(live_coders))
    # Restarted, not started beside: each coder started in a lost one's place took a
    # place of its own under the cap, and two lost coders (every coder, after a reboot)
    # left none under the default 4, so Start the coders was refused for ever.
    lost = [s.agent for s in listed if s.agent.role == "coder" and s.state == "lost"]
    for agent in lost[:missing]:
        if not done(_restart(restarter, project, agent)):
            return FleetStart(tuple(steps))
        missing -= 1
    if missing:
        git = fleet_service.is_git_project(project.root)
        held = {status.agent.label for status in listed}  # running or not, as spawn holds them
        for label in _free_labels("coder", missing, held):
            step = _spawn(spawner, project, "coder", label=label, worktree=None if git else False)
            if not git and step.outcome == "started":
                step = dataclasses.replace(step, notes=(NOT_GIT_NOTE, *step.notes))
            if not done(step):
                break
    return FleetStart(tuple(steps))
