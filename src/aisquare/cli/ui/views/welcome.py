"""The Welcome view: the guided first run — a project, Claude Code, then a manager and two coders.

Roadmap 9.2 (docs/plans/fleet-tui.md §4.2 for where the page sits). Three step
cards under the content id ``welcome``, which stays the shell's first view and
the one it goes back to:

1. **Project** — the directory ``asq`` started in and the registered projects
   (``services.first_run.candidates``), plus a path input judged the way the
   Onboard view judges it. A folder that is not listed yet is onboarded first
   (``services.onboarding.onboard``: ``init``, then ``doctor``), as ``+`` does.
2. **Claude Code** — the binary the fleet launches. When it is missing the card
   names this platform's install command, and the page looks again on every
   refresh tick while it is shown, and on *Check again*. *Sign in* opens the
   Accounts page; *Connect* is the doctor's own one-click fix, and "connected"
   is the one shared answer (``services.agents.claude_code_connected``).
3. **Fleet** — *Start manager*, then *Start the coders* (coder-1 and coder-2),
   through ``services.first_run.start_fleet``, which never types into an agent:
   Claude Code first asks whether to trust the folder, and the user answers that
   in the manager's pane. The card waits for step 2's hooks, because they are
   how the manager receives its instructions. Each agent reads as the shell's
   frame sees it: one whose window is gone, or that tmux cannot answer for (as
   after a reboot), is never called running.

**The page never takes the keyboard on its own.** The sidebar has it at mount,
and the app's keys depend on that. A finished card's buttons leave the Tab
order (they stay clickable), so Tab from the sidebar lands on the next thing to
do; and when the button the keyboard was on goes away, focus moves to the next
step's button rather than to nowhere.

Every string a project or a path puts on screen is ``Text`` or ``Content``,
never markup: a folder called ``[archive]`` reaches the screen as ``[archive]``.
"""

from __future__ import annotations

import dataclasses
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Input, Static
from textual.worker import Worker, WorkerState

from aisquare.cli.ui.sidebar import AccountsSelected, AgentSelected, short_path
from aisquare.cli.ui.views.onboard import render_verdict
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo
from aisquare.services import first_run, onboarding
from aisquare.services.first_run import (
    Candidate,
    Candidates,
    ClaudeState,
    FleetStart,
    FleetStep,
    TmuxState,
)
from aisquare.services.onboarding import FixResult, OnboardOutcome, PathVerdict

WORKER_GROUP = "welcome"
"""Every worker the page starts; one of each name runs at a time."""

FRAME_WAITS = 50
FRAME_WAIT_SECONDS = 0.1
"""How long step 1 waits for the shell's first frame (5 s) before reading the store itself."""

FLEET_WORK = frozenset({"manager", "coders"})
"""Step 3's workers: one start at a time."""

FLEET_UP = "Your fleet is up."
"""The sentence step 3 ends on — stable, so a recording can wait for it."""

_NO_START = object()
"""``WelcomeView._frame_at_start`` before step 3 has started anything."""

_EMPTY_VERDICT = PathVerdict(
    text="", path=None, exists=False, is_dir=False, root=None, registered=None, is_git=False
)


# --------------------------------------------------------------------------- seams


def _probe_tmux() -> TmuxState:
    return first_run.probe_tmux()


def _probe_claude(sign_in: bool, root: Path | None) -> ClaudeState:
    return first_run.probe_claude(sign_in=sign_in, cwd=root)


def _probe_gh() -> bool:
    return first_run.gh_found()


def _find_candidates(listed: list[ProjectInfo] | None) -> Candidates:
    """Step 1's folders, from the shell's frame when there is one — never from a racing store open.

    On a machine that has never run aisquare, the shell's first refresh is what
    creates the store. A worker opening it at that same moment found the file
    still empty and reported it truncated (measured: 5 runs in 6 on a fresh
    home), so the page asks the frame the shell already read. The frame is also
    the shell's idea of "listed", which #240 narrows (it leaves the captain's
    home out). With no frame (a page hosted alone), the store is read.
    """
    if listed is None:
        return first_run.candidates()
    known = {project.id: project for project in listed}
    return first_run.candidates(
        projects=lambda: list(listed),
        validate=lambda text: onboarding.validate_path(text, lookup=known.get),
    )


def _validate(text: str) -> PathVerdict:
    return onboarding.validate_path(text)


def _onboard(path: Path, on_line: Callable[[str], None]) -> OnboardOutcome:
    return onboarding.onboard(path, on_line=on_line)


def _stored_project(project_id: str) -> ProjectInfo | None:
    with store_session() as store:
        return store.get_project(project_id)


def _connect() -> FixResult:
    return first_run.connect()


def _start(project: ProjectInfo, manager: bool, coders: int) -> FleetStart:
    return first_run.start_fleet(project, manager=manager, coders=coders)


@dataclass(frozen=True)
class Seams:
    """What the page asks of the machine and does to it — the services, unless a test says not."""

    tmux: Callable[[], TmuxState] = _probe_tmux
    claude: Callable[[bool, Path | None], ClaudeState] = _probe_claude
    """Called with ``sign_in`` (whether to read the login too; the periodic look does not)
    and the folder step 3 starts in: the chosen project's root, ``None`` before one is
    chosen, which asks about the folder asq started in."""
    gh: Callable[[], bool] = _probe_gh
    candidates: Callable[[list[ProjectInfo] | None], Candidates] = _find_candidates
    """Called with the shell frame's projects, or ``None`` when the page has no shell."""
    validate: Callable[[str], PathVerdict] = _validate
    onboard: Callable[[Path, Callable[[str], None]], OnboardOutcome] = _onboard
    project: Callable[[str], ProjectInfo | None] = _stored_project
    connect: Callable[[], FixResult] = _connect
    start: Callable[[ProjectInfo, bool, int], FleetStart] = _start
    platform: str = sys.platform


DEFAULT_SEAMS = Seams()
"""What a ``WelcomeView`` built without ``seams`` uses — the shell builds it that way."""


# --------------------------------------------------------------------------- the words


def candidate_detail(candidate: Candidate) -> Text:
    """The dim line beside a candidate's button: where it is and what it is."""
    text = Text(short_path(candidate.root), style="dim")
    if candidate.here:
        text.append(" · this folder", style="dim")
    text.append(" · git" if candidate.is_git else " · not a git repository", style="dim")
    if candidate.project is not None:
        text.append(" · listed", style="dim")
    return text


def claude_text(claude: ClaudeState | None, *, platform: str) -> Text:
    """Step 2's body: the agent, its sign-in and its hooks — or how to install it."""
    text = Text()
    if claude is None:
        text.append("Looking for Claude Code…", style="dim")
        return text
    if not claude.found:
        text.append("✗ ", style="red")
        if not claude.is_claude:
            text.append(f"The fleet starts {claude.wanted} (set by: {claude.source}), ")
            text.append("and it is not on your PATH.")
            return text
        if platform == "win32":
            text.append(
                "Claude Code is not installed. The fleet runs inside WSL2; install it there:"
            )
        else:
            text.append("Claude Code is not installed. Install it in a terminal:")
        for index, route in enumerate(first_run.install_routes(platform)):
            text.append("\n    ")
            if index:
                text.append(f"or ({route.how}): ", style="dim")
            text.append(route.command, style="bold")
        text.append("\nThis page notices it within a few seconds.", style="dim")
        return text
    text.append("✓ ", style="green")
    name = "Claude Code" if claude.is_claude else claude.wanted
    text.append(f"{name} {claude.version}" if claude.version else name)
    text.append(f" · {claude.binary}", style="dim")
    if claude.signed_in is True:
        text.append("\n✓ ", style="green")
        text.append("signed in")
    elif claude.signed_in is False:
        text.append("\n· ", style="yellow")
        text.append("not signed in — ")
        text.append(
            "Sign in opens Accounts; Claude Code also asks when it first starts", style="dim"
        )
    if claude.connected:
        text.append("\n✓ ", style="green")
        text.append("connected — the manager gets its instructions through aisquare's hooks")
    elif claude.hooks_off is not None:
        text.append("\n✗ ", style="red")
        text.append(f'hooks are switched off ("disableAllHooks": true in {claude.hooks_off}) — ')
        text.append(
            "Connect cannot change that. Remove it; this page notices within a few seconds."
        )
    elif claude.refused is not None:
        text.append("\n✗ ", style="red")
        text.append(f"aisquare's hooks cannot be written: {claude.refused} — ")
        text.append("Connect cannot change that. Fix it; this page notices within a few seconds.")
    else:
        text.append("\n✗ ", style="red")
        text.append("not connected — Connect installs aisquare's hooks, which bring the manager ")
        text.append("its instructions")
    if claude.problem:
        text.append(f"\n{claude.problem}", style="dim")
    return text


def step_line(step: FleetStep) -> Text:
    """One agent of step 3: ✓ started or running, ✗ refused with the reason."""
    text = Text()
    if step.outcome == "refused":
        text.append("✗ ", style="red")
        text.append(f"{step.label}: {step.detail}")
    else:
        text.append("✓ ", style="green")
        text.append(f"{step.label} — {'started' if step.outcome == 'started' else 'running'}")
    for note in step.notes:
        text.append(f"\n    note: {note}", style="dim")
    return text


def state_line(label: str, state: str, detail: str | None) -> Text:
    """One agent of step 3 that the shell's frame does not see running, as the frame says.

    ``unknown`` is no verdict (tmux could not be asked, as after a reboot), so it gets
    the sidebar's dim ``·``; a ``lost`` agent (its window is gone) or an ``exited`` one gets ``✗``.
    """
    text = Text()
    if state == "unknown":
        text.append("· ", style="dim")
        text.append(f"{label} — state unknown")
    else:
        text.append("✗ ", style="red")
        text.append(f"{label} — {state}")
    if detail:
        text.append(f" ({detail})", style="dim")
    return text


@dataclass(frozen=True)
class _Live:
    """An agent of the chosen project whose row has not ended, and what the frame says of it."""

    agent: FleetAgent
    state: str | None = None
    """The shell frame's state (``models.FleetAgentState``); ``None`` for an agent this page
    started that no frame has read yet, which its start stands in for."""
    detail: str | None = None

    @property
    def running(self) -> bool:
        """Seen there by the listing (``first_run.RUNNING``), or just started by this page."""
        return self.state is None or self.state in first_run.RUNNING


# --------------------------------------------------------------------------- the page


class WelcomeView(VerticalScroll):
    """Three step cards from an empty home to a manager and two coders."""

    can_focus = False
    """The page scrolls to whichever button has focus; it never holds the keyboard itself."""

    DEFAULT_CSS = """
    WelcomeView { padding: 1 2; height: 1fr; }
    WelcomeView .card { height: auto; border: round $primary; padding: 0 1; margin: 1 0 0 0; }
    WelcomeView .card.-done { border: round $success; }
    WelcomeView .card.-waiting { border: round $panel-lighten-2; }
    WelcomeView .card-title { text-style: bold; }
    WelcomeView #project-candidates { height: auto; }
    WelcomeView .row { height: auto; }
    WelcomeView .candidate { height: auto; }
    WelcomeView .candidate Static { width: 1fr; padding: 1 0 0 1; }
    WelcomeView Button { margin: 0 1 0 0; }
    WelcomeView #welcome-path { width: 1fr; }
    WelcomeView #welcome-keys { margin: 1 0 0 0; }
    """

    class Progress(Message):
        """The page added a project or started an agent: the shell re-reads its frame now."""

        def __init__(self, project_id: str | None) -> None:
            self.project_id = project_id
            super().__init__()

    class Connected(Message):
        """Connect, the doctor's own fix, installed the hooks: the shell's report on them is old."""

    BUTTONS: ClassVar[frozenset[str]] = frozenset(
        {
            "welcome-change",
            "welcome-path-use",
            "claude-check",
            "claude-sign-in",
            "claude-connect",
            "fleet-manager",
            "fleet-open",
            "fleet-coders",
        }
    )

    def __init__(
        self,
        *,
        escape_key: str = "f12",
        seams: Seams | None = None,
        recheck_seconds: float | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.escape_key = escape_key
        self.seams = seams if seams is not None else DEFAULT_SEAMS
        self.recheck_seconds = recheck_seconds
        """How often the page looks again while it is shown; ``None`` = the app's refresh."""
        self.tmux: TmuxState | None = None
        self.claude: ClaudeState | None = None
        self._claude_root: Path | None = None
        """The folder :attr:`claude` was asked about: a chosen project's root, or ``None``
        for the folder asq started in."""
        self.gh = True
        self.candidates: Candidates | None = None
        self.verdict: PathVerdict = _EMPTY_VERDICT
        self._typed = ""
        """What the path box holds now; :attr:`verdict` is for it once its judgement lands."""
        self._use_owed = False
        """Enter was pressed while the box's text was still being judged."""
        self.project: ProjectInfo | None = None
        """The project step 1 settled on; step 3's agents start in it."""
        self._picked = False
        """The user acted in step 1 (*Choose another*, or a folder), so the folder asq
        started in is never picked for them again."""
        self.busy: set[str] = set()
        """The workers in flight, by step: ``onboard``, ``connect``, ``manager``, ``coders``.
        Steps 1 and 2 work side by side; a press waits only on its own step's work."""
        self.project_note: Text | None = None
        """Step 1's progress or refusal line."""
        self.connect_error: str | None = None
        self.steps: dict[str, FleetStep] = {}
        """Per label, what the last start said about the chosen project's agents."""
        self._frame_at_start: object = _NO_START
        """The shell's frame when ``steps`` was last written. The shell reads its store and
        sets a new frame object on the UI thread, so any other frame was read after the
        start and is the whole answer. Identity, not wall-clock time: a clock that stepped
        back (DST, NTP) kept every frame "older" than the start for up to an hour, and a
        coder stopped elsewhere read as running (review of #257)."""
        self.fleet_error: str | None = None
        self.opened: str | None = None
        """The manager whose pane *Open the manager* last opened: the trust question's turn
        is over, and the coders are next."""
        self._shown = False
        self._full_owed = False
        """A full look was asked for while another look ran: it starts when that one lands."""
        self._found_ids: frozenset[str] | None = None
        """The shell frame's project ids when step 1 was last listed."""
        self._keyboard = False
        """Whether the keyboard was on this page at the last paint (see :meth:`paint`)."""

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield Static(self._intro(), id="welcome-intro")
        with Vertical(id="step-project", classes="card"):
            yield Static(id="project-title", classes="card-title")
            yield Static(id="project-status")
            yield Vertical(id="project-candidates")
            with Horizontal(id="project-other", classes="row"):
                yield Input(placeholder="or type a folder: ~/Code/your-repo", id="welcome-path")
                yield Button("Use this folder", id="welcome-path-use", disabled=True)
            yield Static(id="welcome-path-verdict")
            yield Button("Choose another", id="welcome-change")
        with Vertical(id="step-claude", classes="card"):
            yield Static(id="claude-title", classes="card-title")
            yield Static(id="claude-status")
            with Horizontal(id="claude-actions", classes="row"):
                yield Button("Connect", id="claude-connect", variant="primary")
                yield Button("Sign in", id="claude-sign-in")
                yield Button("Check again", id="claude-check")
        with Vertical(id="step-fleet", classes="card"):
            yield Static(id="fleet-title", classes="card-title")
            yield Static(id="fleet-status")
            with Horizontal(id="fleet-actions", classes="row"):
                yield Button("Start manager", id="fleet-manager", variant="primary")
                yield Button("Open the manager", id="fleet-open", variant="primary")
                yield Button("Start the coders", id="fleet-coders", variant="primary")
        yield Static(self._keys(), id="welcome-keys")

    def _intro(self) -> Text:
        text = Text()
        text.append("aisquare fleet\n", style="bold")
        text.append(
            "Every project, its manager and the agents it spawns — each a real session, "
            "surfaced here. Three steps to your first fleet:"
        )
        return text

    def _keys(self) -> Text:
        return Text(
            f"{self.escape_key.upper()} hands focus back to the sidebar from an agent pane"
            " · + adds a project · w comes back here · t themes · r refresh · ? help · q quits",
            style="dim",
        )

    def on_mount(self) -> None:
        self.paint()
        self.look(full=True)
        self.find_candidates()
        seconds = self.recheck_seconds
        if seconds is None:
            seconds = float(getattr(self.app, "refresh_seconds", 2.0))
        if seconds > 0:
            self.set_interval(seconds, self._tick)

    def on_show(self) -> None:
        """Back on screen (``w``, or a stop that returns here): the machine may have changed.

        The first Show is the page's first display, whose look ``on_mount`` started.
        """
        if self._shown:
            self.look(full=True)
            self.find_candidates()
        self._shown = True
        self.paint()

    # ------------------------------------------------------------------ workers

    def _run(self, name: str, work: Callable[[], object]) -> None:
        self.run_worker(
            work, name=f"welcome-{name}", group=WORKER_GROUP, thread=True, exit_on_error=False
        )

    def _in_flight(self, name: str) -> bool:
        return any(
            worker.name == f"welcome-{name}" and not worker.is_finished for worker in self.workers
        )

    def look(self, *, full: bool) -> None:
        """Probe the machine off the UI thread.

        ``full`` reads everything, the sign-in included. The periodic look reads
        only what is not ready yet: Claude Code, tmux, gh. A full look asked for
        while another look runs is owed, and starts when that one lands, so
        *Check again*, a finished Connect and a return to the page are never lost.
        """
        if self._in_flight("look"):
            self._full_owed = self._full_owed or full
            return
        seams = self.seams
        root = self.project.root if self.project is not None else None
        claude_due = (
            full or self.claude is None or not self.claude.ready or root != self._claude_root
        )
        tmux_due = full or self.tmux is None or not self.tmux.ok
        gh_due = full or not self.gh

        def probe() -> tuple[TmuxState | None, ClaudeState | None, bool | None, Path | None]:
            return (
                seams.tmux() if tmux_due else None,
                seams.claude(full, root) if claude_due else None,
                seams.gh() if gh_due else None,
                root,
            )

        self._run("look", probe)

    def _not_ready(self) -> bool:
        claude, tmux, project = self.claude, self.tmux, self.project
        return (
            claude is None
            or not claude.ready
            or (project is not None and project.root != self._claude_root)
            or (tmux is not None and not tmux.ok)
            or not self.gh
        )

    def _tick(self) -> None:
        """While shown: look again at what is not ready, list step 1 again when the shell's
        frame lists other projects, and repaint step 3 from the frame."""
        if not self.display:
            return
        if self._not_ready():
            self.look(full=False)
        stale = self.project is None and self._frame_ids() != self._found_ids
        if stale and not self._in_flight("candidates"):
            self.find_candidates()
        self.paint()

    def _frame_ids(self) -> frozenset[str] | None:
        listed = _frame_projects(self.app)
        return frozenset(project.id for project in listed) if listed is not None else None

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        worker = event.worker
        if worker.group != WORKER_GROUP:
            return
        event.stop()
        if event.state not in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            return
        name = (worker.name or "").removeprefix("welcome-")
        self.busy.discard(name)
        if event.state is WorkerState.CANCELLED:
            return
        result: Any = worker.result if event.state is WorkerState.SUCCESS else None
        error = worker.error if event.state is WorkerState.ERROR else None
        if name == "look":
            self._looked(result)
        elif name == "candidates":
            self._found(result, error)
        elif name == "validate":
            self._judged(result, error)
        elif name == "onboard":
            self._onboarded(result, error)
        elif name == "connect":
            self._connected(result, error)
        elif name in ("manager", "coders"):
            self._started(result, error)
        self.paint()

    def _looked(self, result: Any) -> None:
        # Not a tuple: a probe crashed (they never raise), and the card keeps what it had.
        if isinstance(result, tuple):
            tmux, claude, gh, root = result
            if isinstance(tmux, TmuxState):
                self.tmux = tmux
            if isinstance(claude, ClaudeState):
                if claude.signed_in is None and self.claude is not None and claude.found:
                    # The periodic look leaves the login out: keep the last answer.
                    claude = dataclasses.replace(claude, signed_in=self.claude.signed_in)
                self.claude = claude
                self._claude_root = root
                if claude.connected:
                    self.connect_error = None  # connected since, by this page or another way
            if isinstance(gh, bool):
                self.gh = gh
        if self._full_owed:
            self._full_owed = False
            self.look(full=True)
        elif isinstance(result, tuple):
            self._follow_project()

    def _follow_project(self) -> None:
        """Ask step 2 again when its answer was for another folder than the chosen project's.

        The fleet starts in the chosen project's root, and a project- or local-scope
        plugin makes "connected?" a question about that folder, not the one asq started
        in (review of #257). A look in flight is caught up when it lands.
        """
        if self.project is not None and self.project.root != self._claude_root:
            self.look(full=False)

    def find_candidates(self) -> None:
        """List step 1's folders off the UI thread, once the shell has read its first frame.

        The page mounts before the shell's first refresh, so the worker waits for
        that frame (at most ``FRAME_WAITS`` times ``FRAME_WAIT_SECONDS``) rather
        than opening the store beside it. A shell that could not read its store
        has no frame to wait for (``FleetApp.store_error``), and then the store is
        asked directly, which says why. The wait is in the worker, where a
        settling test waits for it too.
        """
        app = self.app
        shell = hasattr(app, "snapshot")
        seams = self.seams

        def find() -> Candidates:
            listed = _frame_projects(app)
            waits = 0
            while shell and listed is None and waits < FRAME_WAITS and _frame_coming(app):
                _nap(FRAME_WAIT_SECONDS)
                waits += 1
                listed = _frame_projects(app)
            return seams.candidates(listed)

        self._run("candidates", find)

    def _found(self, result: Any, error: BaseException | None) -> None:
        self._found_ids = self._frame_ids()
        if isinstance(result, Candidates):
            self.candidates = result
        else:
            self.candidates = Candidates(items=(), store_error=_reason(error))
        first = self.candidates.items[0] if self.candidates.items else None
        unpicked = self.project is None and not self._picked and "onboard" not in self.busy
        if unpicked and first is not None and first.here and first.project:
            # Started inside a project that is listed already: there is nothing to add,
            # so nothing to click — the installer's case (it lists the folder first).
            # Only until the user acts: step 1 is listed again on every return to the
            # page and when the frame changes, and picking the old folder then undid
            # *Choose another*, or aimed step 3 at it mid-onboarding (review of #257).
            self.project = first.project
            self._follow_project()
        self._show_candidates()

    # ------------------------------------------------------------------ step 1

    def _show_candidates(self) -> None:
        holder = self.query_one("#project-candidates", Vertical)
        holder.remove_children()
        items = self.candidates.items if self.candidates is not None else ()
        rows: list[Widget] = []
        for index, candidate in enumerate(items):
            button = Button(
                Content(f"Use {candidate.name}"),
                name=str(index),
                classes="use-candidate",
                variant="primary" if index == 0 else "default",
            )
            rows.append(
                Horizontal(button, Static(candidate_detail(candidate)), classes="candidate")
            )
        if rows:
            holder.mount_all(rows)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "welcome-path":
            return
        event.stop()
        self._typed = event.value
        self._use_owed = False  # Enter answers the text it was pressed on
        self._judge()
        self.paint()

    def _judge(self) -> None:
        """Judge the typed folder off the UI thread: ``validate`` runs git and opens the store.

        It ran on every keystroke, on the UI thread, so a slow disk or a network mount
        stalled the shell between keys (review of #257). One judgement runs at a time,
        and text typed meanwhile is judged when it lands (:meth:`_judged`), so the
        verdict always ends on what the box holds — the latest text wins.
        """
        if self._in_flight("validate"):
            return
        text, validate = self._typed, self.seams.validate

        def judge() -> PathVerdict:
            try:
                return validate(text)
            except Exception as exc:  # a verdict must never take the page down
                return _unjudged(text, _reason(exc))

        self._run("validate", judge)

    def _judged(self, result: Any, error: BaseException | None) -> None:
        verdict = (
            result if isinstance(result, PathVerdict) else _unjudged(self._typed, _reason(error))
        )
        if verdict.text != self._typed:
            self._judge()  # the box moved on while that one ran
            return
        self.verdict = verdict
        if self._use_owed:
            self._use_owed = False
            self._use_typed()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "welcome-path":
            return
        event.stop()
        self._use_typed()

    def _use_typed(self) -> None:
        verdict = self.verdict
        if verdict.text != self._typed:
            self._use_owed = True  # still judging what the box holds: used once it lands
            return
        if not verdict.ok or verdict.path is None:
            return
        registered = verdict.registered
        listed = registered if registered is not None and registered.onboarded_at else None
        self.choose(verdict.root or verdict.path, listed)

    def choose(self, root: Path, listed: ProjectInfo | None) -> None:
        """Settle step 1 on ``root``: at once when it is listed, else once it is onboarded."""
        if "onboard" in self.busy:
            return
        self._picked = True
        self.project_note = None
        if listed is not None:
            self._settle(listed)
            return
        self.busy.add("onboard")
        self.project_note = Text(f"Setting up {root.name or root}…", style="dim")
        seams = self.seams
        app = self.app

        def onboard() -> OnboardOutcome:
            def line(text: str) -> None:
                app.call_from_thread(self._onboard_line, text)

            return seams.onboard(root, line)

        self._run("onboard", onboard)
        self.paint()

    def _onboard_line(self, line: str) -> None:
        if "onboard" in self.busy:
            self.project_note = Text(line, style="dim")
            self.paint()

    def _onboarded(self, result: Any, error: BaseException | None) -> None:
        outcome = result if isinstance(result, OnboardOutcome) else None
        if outcome is None or outcome.project_id is None:
            reason = outcome.reason if outcome is not None else _reason(error)
            self.project_note = Text(f"✗ {reason or 'onboarding failed'}", style="red")
            return
        project: ProjectInfo | None
        try:
            project = self.seams.project(outcome.project_id)
        except Exception:  # the store is busy: the report carries the project too
            project = None
        if project is None and outcome.report is not None:
            project = outcome.report.project
        if project is None:
            self.project_note = Text(
                f"✗ {outcome.project_id} was set up but could not be read back", style="red"
            )
            return
        self.post_message(self.Progress(project.id))
        self._settle(project)
        self.find_candidates()

    def _settle(self, project: ProjectInfo) -> None:
        if self.project is None or self.project.id != project.id:
            self.steps = {}
            self.fleet_error = None
        self.project = project
        self.project_note = None
        self._follow_project()
        self.paint()

    # ------------------------------------------------------------------ buttons

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button = event.button
        if button.has_class("use-candidate"):
            event.stop()
            items = self.candidates.items if self.candidates is not None else ()
            index = int(button.name or "-1")
            if 0 <= index < len(items):
                chosen = items[index]
                self.choose(chosen.root, chosen.project)
            return
        if button.id not in self.BUTTONS:
            return
        event.stop()
        if button.id == "welcome-path-use":
            self._use_typed()
        elif button.id == "welcome-change":
            if "onboard" not in self.busy:  # not while a folder is being set up
                self._picked = True
                self.project = None
                self.steps = {}
                self.fleet_error = None
                self.paint()
        elif button.id == "claude-check":
            self.look(full=True)
        elif button.id == "claude-sign-in":
            self.post_message(AccountsSelected())
        elif button.id == "claude-connect":
            self._start_work("connect", self.seams.connect)
        elif button.id == "fleet-manager":
            self._start_fleet("manager")
        elif button.id == "fleet-coders":
            self._start_fleet("coders")
        elif button.id == "fleet-open":
            manager = self._live().get("manager")
            if manager is not None:
                self.opened = manager.agent.id
                self.post_message(AgentSelected(manager.agent.project_id, manager.agent.id))
                self.paint()

    def _start_work(self, name: str, work: Callable[[], object]) -> None:
        if name in self.busy:
            return
        self.busy.add(name)
        if name == "connect":
            self.connect_error = None
        self._run(name, work)
        self.paint()

    def _connected(self, result: Any, error: BaseException | None) -> None:
        if isinstance(result, FixResult) and result.ok:
            self.post_message(self.Connected())
            self.look(full=True)
            return
        reason = result.reason if isinstance(result, FixResult) else _reason(error)
        self.connect_error = reason or "connect failed"

    def _start_fleet(self, which: str) -> None:
        project = self.project
        if project is None or self.busy & FLEET_WORK or not self._ready():
            return
        self.fleet_error = None
        # A refusal answers the start that met it; a new start clears it.
        self.steps = {label: s for label, s in self.steps.items() if s.outcome != "refused"}
        start = self.seams.start
        if which == "manager":
            self._start_work("manager", lambda: start(project, True, 0))
        else:
            self._start_work("coders", lambda: start(project, False, first_run.CODERS))

    def _started(self, result: Any, error: BaseException | None) -> None:
        if not isinstance(result, FleetStart):
            self.fleet_error = f"could not start the fleet: {_reason(error)}"
            return
        for step in result.steps:
            self.steps[step.label] = step
        self._frame_at_start = getattr(self.app, "snapshot", None)
        if self.project is not None:
            self.post_message(self.Progress(self.project.id))

    # ------------------------------------------------------------------ the frame

    def _live(self) -> dict[str, _Live]:
        """The chosen project's agents by label, as the shell's frame sees them, and our starts.

        An agent this page just started is not in the frame until the shell reads
        again, so until then what its start said stands in for it. A frame read
        after that start, for this project and not failed open, is the whole
        answer: an agent missing from it was stopped elsewhere (``fleet stop``
        kills its window and the row leaves the listing), and is not brought back
        from what this page last heard. An ENDED row is never here. A row the
        frame does not see running (its window gone, or tmux unable to say, as
        after a reboot) is here with that state, and never counts as running.
        """
        project = self.project
        if project is None:
            return {}
        live: dict[str, _Live] = {}
        ended: set[str] = set()
        snapshot = getattr(self.app, "snapshot", None)
        frame = getattr(snapshot, "agents", None)
        rows = frame.get(project.id, []) if isinstance(frame, dict) else []
        for status in rows:
            agent = getattr(status, "agent", None)
            if not isinstance(agent, FleetAgent):
                continue
            if agent.ended_at is None:
                state = getattr(status, "state", "unknown")
                live[agent.label] = _Live(agent, state, getattr(status, "detail", None))
            else:
                ended.add(agent.id)
        notices = getattr(snapshot, "notices", None)
        answered = (
            isinstance(frame, dict)
            and project.id in frame
            and not (isinstance(notices, dict) and notices.get(project.id))
            and getattr(snapshot, "stale_since", None) is None
            and self._frame_at_start is not _NO_START
            and snapshot is not self._frame_at_start
        )
        if answered:
            return live
        for step in self.steps.values():
            agent = step.agent
            if agent is not None and step.outcome != "refused" and agent.id not in ended:
                live.setdefault(agent.label, _Live(agent))
        return live

    def _ready(self) -> bool:
        """Steps 1 and 2 done and tmux usable: what step 3's buttons wait for.

        Not while step 1 is still setting a folder up: step 3 starts in the folder that
        onboarding settles on, never the one it is leaving.
        """
        claude, tmux = self.claude, self.tmux
        return (
            self.project is not None
            and "onboard" not in self.busy
            and claude is not None
            and claude.ready
            # Answered for the folder the fleet starts in, not the one asq started in
            # (review of #257): until the look for it lands, step 3 waits.
            and self._claude_root == self.project.root
            and (tmux is None or tmux.ok)
        )

    # ------------------------------------------------------------------ painting

    def paint(self) -> None:
        """Render every card from the state above; the keyboard follows the next step.

        Buttons are disabled only for what they wait on (a project, the hooks),
        never while their own work runs: Textual takes the focus off a widget it
        disables, and the keyboard would land nowhere. ``busy`` refuses a second
        press instead.
        """
        if not self.is_mounted:
            return
        focused = self.screen.focused
        if focused is not None:
            # Nowhere keeps the last answer: the keyboard was on this page when the
            # button it was on went away, and it is still the page's to hand on.
            self._keyboard = self in focused.ancestors
        project_done = self._paint_project()
        claude_done = self._paint_claude(waiting=not project_done)
        self._paint_fleet(waiting=not (project_done and claude_done))
        if self._keyboard:
            self._hand_on()

    def _hand_on(self) -> None:
        """The keyboard was on this page and its widget went away: give it the next step.

        "Went away" is "left the Tab order": hidden, disabled, or in a card that
        is done. A hidden widget can still call itself ``focusable``.
        """
        focused = self.screen.focused
        chain = self.screen.focus_chain
        if focused is not None and focused in chain:
            return
        target = next((widget for widget in chain if self in widget.ancestors), None)
        if target is not None:
            target.focus()

    def next_button(self) -> Widget | None:
        """The first widget Tab reaches on this page: the next thing to do."""
        for widget in self.screen.focus_chain:
            if self in widget.ancestors:
                return widget
        return None

    def _card(self, card_id: str, *, done: bool, waiting: bool, keep_keys: bool = False) -> None:
        card = self.query_one(f"#{card_id}", Vertical)
        card.set_class(done, "-done")
        card.set_class(waiting and not done, "-waiting")
        # A finished card leaves the Tab order — its buttons still answer a click —
        # unless it is the last card, whose buttons are then the page's whole point.
        card.can_focus_children = keep_keys or not done

    def _paint_project(self) -> bool:
        project = self.project
        done = project is not None and "onboard" not in self.busy
        title = Text("1  Project", style="bold")
        status = Text()
        if project is not None:
            title.append("  ✓", style="green")
            status.append("✓ ", style="green")
            status.append(project.root.name or str(project.root), style="bold")
            status.append(f" — {short_path(project.root)}", style="dim")
        else:
            status.append("Pick the folder your agents will work in.")
            if self.candidates is not None and not self.candidates.items:
                status.append("\nNothing to suggest here — type a folder below.", style="dim")
            store_error = self.candidates.store_error if self.candidates is not None else None
            if store_error:
                status.append(
                    f"\nThe registered projects could not be read: {store_error}", style="dim"
                )
        if self.project_note is not None:
            status.append("\n")
            status.append_text(self.project_note)
        self.query_one("#project-title", Static).update(title)
        self.query_one("#project-status", Static).update(status)
        choosing = project is None
        self.query_one("#project-candidates", Vertical).display = choosing
        self.query_one("#project-other", Horizontal).display = choosing
        # Only a verdict on what the box holds now: an older one waits for its successor.
        current = self.verdict.text == self._typed
        verdict = self.query_one("#welcome-path-verdict", Static)
        verdict.display = choosing and current and self.verdict.path is not None
        verdict.update(render_verdict(self.verdict))
        self.query_one("#welcome-path-use", Button).disabled = not (current and self.verdict.ok)
        self.query_one("#welcome-change", Button).display = project is not None
        self._card("step-project", done=done, waiting=False)
        return done

    def _paint_claude(self, *, waiting: bool) -> bool:
        claude = self.claude
        done = claude is not None and claude.ready
        title = Text("2  Claude Code", style="bold")
        if done:
            title.append("  ✓", style="green")
        body = claude_text(claude, platform=self.seams.platform)
        if "connect" in self.busy:
            body.append("\nConnecting…", style="dim")
        elif self.connect_error and not (claude is not None and claude.connected):
            body.append(f"\n✗ {self.connect_error}", style="red")
        self.query_one("#claude-title", Static).update(title)
        self.query_one("#claude-status", Static).update(body)
        found = claude is not None and claude.found
        connect = self.query_one("#claude-connect", Button)
        connect.display = (
            found
            and claude is not None
            and not claude.connected
            and claude.hooks_off is None
            and claude.refused is None
        )
        self.query_one("#claude-sign-in", Button).display = (
            found and claude is not None and claude.signed_in is False
        )
        self.query_one("#claude-check", Button).display = claude is not None and not claude.found
        self._card("step-claude", done=done, waiting=waiting)
        return done

    def _paint_fleet(self, *, waiting: bool) -> None:
        live = self._live()
        manager = live.get("manager")
        manager_running = manager is not None and manager.running
        coders = [row for row in live.values() if row.agent.role == "coder" and row.running]
        up = manager_running and len(coders) >= first_run.CODERS
        ready = self._ready()
        title = Text("3  Fleet", style="bold")
        if up:
            title.append("  ✓", style="green")
        status = Text()
        if not ready and manager is None:
            status.append(self._waiting_for(), style="dim")
        elif "manager" in self.busy:
            status.append("Starting the manager…", style="dim")
        elif manager is None:
            name = self.project.root.name if self.project is not None else "the project"
            status.append(f"Starts the manager in {name}. ")
            status.append(
                "Claude Code asks once whether you trust this folder: answer it in the "
                "manager's pane.",
                style="dim",
            )
        for line in self._fleet_lines(live):
            if status.plain:
                status.append("\n")
            status.append_text(line)
        if manager is not None and not manager_running:
            status.append("\nOpen the manager to restart it.", style="dim")
        elif manager is not None and not up:
            if "coders" in self.busy:
                status.append("\nStarting the coders…", style="dim")
            else:
                status.append(
                    "\nOpen the manager and answer Claude Code's question about trusting this "
                    f"folder; then {self.escape_key.upper()} and w bring you back here to start "
                    "the coders.",
                    style="dim",
                )
        if up:
            status.append(f"\n{FLEET_UP}", style="bold green")
            status.append(" Open the manager and tell it what to build.")
        if self.fleet_error:
            status.append(f"\n✗ {self.fleet_error}", style="red")
        if not self.gh:
            status.append(
                "\ngh is not installed — the coders open pull requests with it: "
                "https://cli.github.com",
                style="dim",
            )
        self.query_one("#fleet-title", Static).update(title)
        self.query_one("#fleet-status", Static).update(status)
        start = self.query_one("#fleet-manager", Button)
        start.display = manager is None
        start.disabled = not ready
        # Before the coders, the next step is the manager's pane (the trust question),
        # so Open is the primary and takes the keyboard; once it has been opened the
        # coders are, and Open leaves the Tab order until the fleet is up. A manager
        # that is not running is restarted from its pane, so Open stays the next step.
        opened = manager is not None and self.opened == manager.agent.id
        coders_next = manager_running and opened and not up
        open_button = self.query_one("#fleet-open", Button)
        open_button.display = manager is not None
        open_button.variant = "default" if coders_next else "primary"
        open_button.can_focus = not coders_next
        coders_button = self.query_one("#fleet-coders", Button)
        coders_button.display = manager_running and not up
        coders_button.disabled = not ready
        coders_button.variant = "primary" if opened else "default"
        self._card("step-fleet", done=up, waiting=waiting, keep_keys=True)

    def _fleet_lines(self, live: dict[str, _Live]) -> list[Text]:
        """One line per agent of the chosen project's fleet, then any refusal.

        An agent the frame does not see running says what the frame says instead,
        never "started" or "running" from what this page last heard.
        """
        lines: list[Text] = []
        shown: set[str] = set()
        coders = sorted(row.agent.label for row in live.values() if row.agent.role == "coder")
        for label in ("manager", *coders):
            row = live.get(label)
            if row is None:
                continue
            shown.add(label)
            if not row.running:
                lines.append(state_line(label, row.state or "unknown", row.detail))
                continue
            agent = row.agent
            step = self.steps.get(label)
            if step is None or step.agent is None or step.agent.id != agent.id:
                step = FleetStep(label, agent.role, "running", agent.id, agent)
            lines.append(step_line(step))
        for label, step in self.steps.items():
            if step.outcome == "refused" and label not in shown:
                lines.append(step_line(step))
        return lines

    def _waiting_for(self) -> str:
        missing: list[str] = []
        if self.project is None:
            missing.append("a project (step 1)")
        claude = self.claude
        if claude is None or not claude.found:
            missing.append("Claude Code installed (step 2)")
        elif not claude.connected:
            missing.append("Claude Code connected (step 2)")
        line = (
            ("The manager starts once there is " + " and ".join(missing) + ".") if missing else ""
        )
        tmux = self.tmux
        if tmux is not None and not tmux.ok:
            fix = f" — install it: {tmux.hint}" if tmux.hint else ""
            line = (line + "\n" if line else "") + f"✗ {tmux.problem}{fix}"
        return line or "Checking this machine…"


def _nap(seconds: float) -> None:
    """The frame wait's sleep: a seam, so a test counts the naps instead of timing a run."""
    time.sleep(seconds)


def _frame_coming(app: object) -> bool:
    """Whether the shell may still read a first frame: it runs, and its store has not failed."""
    return getattr(app, "store_error", None) is None and bool(getattr(app, "is_running", True))


def _frame_projects(app: object) -> list[ProjectInfo] | None:
    """The projects of the shell's last frame (``FleetApp.snapshot``); ``None`` before one."""
    projects = getattr(getattr(app, "snapshot", None), "projects", None)
    if not isinstance(projects, list):
        return None
    return [project for project in projects if isinstance(project, ProjectInfo)]


def _reason(error: BaseException | None) -> str:
    if error is None:
        return "no answer"
    return str(error) or type(error).__name__


def _unjudged(text: str, why: str) -> PathVerdict:
    """The verdict for ``text`` when judging it failed: not a folder to use, and why."""
    return PathVerdict(
        text=text,
        path=None,
        exists=False,
        is_dir=False,
        root=None,
        registered=None,
        is_git=False,
        store_error=why,
    )
