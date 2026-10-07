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

- **It never types into an agent.** No ``prompt=`` reaches ``fleet.spawn``
  (:class:`Spawner` has no such parameter, so mypy holds this as well as the
  tests). A folder Claude Code has not seen makes it ask, once, whether to trust
  the folder, and the fleet's prompt typing decides when the agent is ready from
  the pane's foreground process, not from its screen. #240's captain work
  measured that typing into that question picks "No, exit". The user answers it
  in the manager's pane, and the coders start after that, from their own button.
- **It is idempotent.** It never starts a second manager, and it tops the coders
  up to :data:`CODERS` rather than adding two more each time it runs.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from aisquare.core import claude_accounts as accounts_core
from aisquare.core import harness, selfcli
from aisquare.core import tmux as tmux_core
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo
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
    """aisquare's hooks are in the config dir a session from this shell reads."""
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


def _connected_default() -> bool:
    return agents_service.claude_code_connected()


def _signed_in_default() -> bool:
    return accounts_core.signed_in(accounts_core.default_account())


def probe_claude(
    *,
    sign_in: bool = True,
    which: Callable[[str], str | None] = shutil.which,
    connected: Callable[[], bool] | None = None,
    signed_in: Callable[[], bool] | None = None,
) -> ClaudeState:
    """Claude Code as the fleet will meet it. Starts no process; never raises.

    The binary is the one ``fleet.spawn`` would launch for the manager
    (``harness.resolve_binary``), looked up the way spawn looks it up, so an
    ``AISQUARE_AGENT_BIN`` stand-in is what this finds too. ``connected`` is the
    one shared answer to "is Claude Code connected" (``agents.claude_code_connected``),
    which the doctor's row asks as well. ``sign_in`` reads the plain ``claude``'s
    login, which lives in a file that can be tens of megabytes, so the periodic
    re-check leaves it out.
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
    try:
        is_connected = (connected or _connected_default)()
    except Exception as exc:
        problems.append(f"could not read the hooks: {_why(exc)}")
        is_connected = False
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
    """``aisquare --json agents connect claude-code``, exactly as the doctor's button runs it."""
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
    nor is a registered project whose folder has gone.
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
        except OSError:
            continue
        if root in taken or not present or _never_offered(project.root, where):
            continue
        taken.add(root)
        items.append(
            Candidate(
                root=project.root,
                is_git=fleet_service.is_git_project(project.root),
                project=project,
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


LiveAgents = Callable[[ProjectInfo], list[FleetAgent]]


def live_agents(project: ProjectInfo) -> list[FleetAgent]:
    """The project's agents whose rows are not ended — what ``fleet.spawn`` counts as live."""
    return [s.agent for s in fleet_service.list_agents(project) if s.agent.ended_at is None]


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


def start_fleet(
    project: ProjectInfo,
    *,
    manager: bool = True,
    coders: int = CODERS,
    spawn: Spawner | None = None,
    live: LiveAgents | None = None,
    on_step: Callable[[FleetStep], None] | None = None,
) -> FleetStart:
    """Start the manager and top the coders up to ``coders``, one at a time; never raises.

    A live manager is reported as ``running`` and never spawned again; live
    coders count toward ``coders``, so a second call starts only what is
    missing. Coders take the role's worktree default in a git repository and
    ``worktree=False`` elsewhere, with a note. The first refusal (no tmux, the
    agent cap, a worktree git will not make) stops the call, and its reason is
    on its step. ``on_step`` hears each step as it lands, for a caller that
    shows progress. Nothing is typed into any agent: see the module docstring.
    """
    spawner: Spawner = spawn if spawn is not None else fleet_service.spawn
    steps: list[FleetStep] = []

    def done(step: FleetStep) -> bool:
        steps.append(step)
        if on_step is not None:
            on_step(step)
        return step.outcome != "refused"

    try:
        running = (live or live_agents)(project)
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
    if missing:
        git = fleet_service.is_git_project(project.root)
        held = {agent.label for agent in running}
        for label in _free_labels("coder", missing, held):
            step = _spawn(spawner, project, "coder", label=label, worktree=None if git else False)
            if not git and step.outcome == "started":
                step = FleetStep(
                    step.label,
                    step.role,
                    step.outcome,
                    step.detail,
                    step.agent,
                    (NOT_GIT_NOTE, *step.notes),
                )
            if not done(step):
                break
    return FleetStart(tuple(steps))
