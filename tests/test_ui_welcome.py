"""The Welcome view, driven headless through Textual's Pilot (roadmap 9.2).

Two hosts. A bare ``Host`` app mounts the page alone with every seam scripted
(``Machine``), for the cards' states and what each button asks for. The real
``FleetApp`` mounts it where ``asq`` does, for what only the shell can show:
Welcome is the first view and never takes the keyboard, Tab from the sidebar
lands on the next step and Enter completes it, ``+`` and ``w`` from the
sidebar, and the sidebar following what the page did. Those run with the
experimental captain's variable unset and set (#240 turns it on for every
test), and none of them reads the real ``PATH`` or reaches a tmux server.

Every assertion reads what a widget shows (``visual.plain``) or the state the
claim is about, and each behaviour has its control in the other direction.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

import pytest
from textual.app import App, ComposeResult
from textual.message import Message
from textual.pilot import Pilot
from textual.widgets import Button, Input, Static

from aisquare.cli.ui import app as app_mod
from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.sidebar import AccountsSelected, AgentSelected, DoctorSection
from aisquare.cli.ui.views import welcome
from aisquare.cli.ui.views.onboard import ProjectOnboarded
from aisquare.cli.ui.views.welcome import FLEET_UP, Seams, WelcomeView
from aisquare.core import claude_accounts as accounts_core
from aisquare.core import tmux as tmux_core
from aisquare.core.store import store_session
from aisquare.core.tmux import Completed
from aisquare.core.workspace import project_id_for
from aisquare.models import (
    CheckStatus,
    DoctorCheck,
    FleetAgent,
    FleetAgentState,
    FleetAgentStatus,
    ProjectInfo,
    SetupReport,
)
from aisquare.services import first_run
from aisquare.services import fleet as fleet_service
from aisquare.services.first_run import (
    Candidate,
    Candidates,
    ClaudeState,
    FleetStart,
    TmuxState,
)
from aisquare.services.onboarding import FixResult, OnboardOutcome, PathVerdict
from tests.pane_harness import asks_a_server, socket_of
from tests.ui_workers import settle_page, settle_until

T = TypeVar("T")
SIZE = (140, 60)
T0 = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
PRIVATE_SOCKET = f"asq-test-{os.getpid()}-welcome"
CAPTAIN = "AISQUARE_EXPERIMENTAL_CAPTAIN"

MISSING = ClaudeState(wanted="claude", source="default")
UNHOOKED = ClaudeState(
    wanted="claude", source="default", binary="/opt/bin/claude", version="2.1.300", signed_in=True
)
READY = dataclasses.replace(UNHOOKED, connected=True)
DETAIL = {"lost": "pane gone", "unknown": "tmux unavailable"}
"""What the fleet's listing says beside those states (``services.fleet._derive``)."""


# --------------------------------------------------------------------------- the machine


def _agent(project: ProjectInfo, label: str, role: str) -> FleetAgent:
    return FleetAgent(
        id=f"agt_{label}",
        project_id=project.id,
        label=label,
        role=role,
        pane_id="%1",
        tmux_socket=PRIVATE_SOCKET,  # never "asq", the developer's own fleet
        cwd=project.root,
        created_at=T0,
    )


@dataclass
class Machine:
    """Everything the page can ask, scripted; and everything it did, recorded."""

    claude: list[ClaudeState] = field(default_factory=lambda: [MISSING])
    """Answers in turn; the last one repeats."""
    claude_at: dict[Path, ClaudeState] = field(default_factory=dict)
    """The answer for a look asked about a chosen project's root, where one is scripted."""
    roots: list[Path | None] = field(default_factory=list)
    """The folder each look asked about: a chosen project's root, or ``None``."""
    tmux: TmuxState = field(default_factory=lambda: TmuxState(found=True, version=(3, 4)))
    gh: bool = True
    found: Candidates | Exception | Callable[[list[ProjectInfo] | None], Candidates] = field(
        default_factory=lambda: Candidates(items=())
    )
    """Step 1's answer: fixed, raised, or computed from the frame it is handed."""
    hold_first_look: threading.Event | None = None
    """When set, the page's first look waits for it — a look still in flight."""
    stored: dict[str, ProjectInfo] = field(default_factory=dict)
    onboard_answer: Callable[[Path], OnboardOutcome] | None = None
    connect_answer: FixResult | None = None
    refuse: dict[str, str] = field(default_factory=dict)
    """Labels ``start`` refuses, with the reason."""
    blind: str | None = None
    """Why the fleet cannot be read, when it cannot: ``start`` refuses before any spawn."""
    looks: list[bool] = field(default_factory=list)
    frames: list[list[ProjectInfo] | None] = field(default_factory=list)
    onboarded: list[Path] = field(default_factory=list)
    connects: int = 0
    starts: list[tuple[str, bool, int]] = field(default_factory=list)
    live: list[FleetAgent] = field(default_factory=list)
    states: dict[str, FleetAgentState] = field(default_factory=dict)
    """What the fleet's listing says of an agent, by label, when not ``waiting``."""
    cap: int = 4
    """``max_agents_per_project``: ``spawn`` refuses at it, counting as ``fleet.spawn`` does."""
    restarted: list[str] = field(default_factory=list)

    def seams(self, platform: str = "linux") -> Seams:
        def claude(sign_in: bool, root: Path | None) -> ClaudeState:
            self.looks.append(sign_in)
            self.roots.append(root)
            if self.hold_first_look is not None and len(self.looks) == 1:
                self.hold_first_look.wait(10)
            if root is not None and root in self.claude_at:
                answer = self.claude_at[root]
            else:
                answer = self.claude[0] if len(self.claude) == 1 else self.claude.pop(0)
            return answer if sign_in else dataclasses.replace(answer, signed_in=None)

        def candidates(listed: list[ProjectInfo] | None) -> Candidates:
            self.frames.append(listed)
            if isinstance(self.found, Exception):
                raise self.found
            if callable(self.found):
                return self.found(listed)
            return self.found

        def onboard(path: Path, on_line: Callable[[str], None]) -> OnboardOutcome:
            self.onboarded.append(path)
            on_line(f"$ aisquare --json init --no-explainability {path}")
            if self.onboard_answer is not None:
                return self.onboard_answer(path)
            project = ProjectInfo(id=project_id_for(path), root=path, onboarded_at=T0)
            self.stored[project.id] = project
            report = SetupReport(home=path / ".home", already_initialized=False, project=project)
            return OnboardOutcome(path=path, project_id=project.id, report=report)

        def connect() -> FixResult:
            self.connects += 1
            if self.connect_answer is not None:
                return self.connect_answer
            self.claude = [READY]
            return FixResult(fix=first_run.connect_fix(), returncode=0)

        def start(project: ProjectInfo, manager: bool, coders: int) -> FleetStart:
            self.starts.append((project.id, manager, coders))
            return first_run.start_fleet(
                project,
                manager=manager,
                coders=coders,
                spawn=self.spawn,
                live=self.listing,
                restart=self.restart,
            )

        return Seams(
            tmux=lambda: self.tmux,
            claude=claude,
            gh=lambda: self.gh,
            candidates=candidates,
            validate=self.validate,
            onboard=onboard,
            project=self.stored.get,
            connect=connect,
            start=start,
            platform=platform,
        )

    def listing(self, project: ProjectInfo) -> list[FleetAgentStatus]:
        if self.blind is not None:
            raise RuntimeError(self.blind)
        return self.statuses(project)

    def statuses(self, project: ProjectInfo) -> list[FleetAgentStatus]:
        """``live`` as ``fleet.list_agents`` reports it for ``project``, each in its state."""
        rows: list[FleetAgentStatus] = []
        for agent in self.live:
            if agent.project_id == project.id:
                state = self.states.get(agent.label, "waiting")
                rows.append(FleetAgentStatus(agent=agent, state=state, detail=DETAIL.get(state)))
        return rows

    def spawn(self, project: ProjectInfo, role: str, **kwargs: Any) -> fleet_service.SpawnReceipt:
        assert kwargs.get("prompt") is None, "Welcome typed into an agent"
        label = kwargs.get("label") or role
        if label in self.refuse:
            raise fleet_service.FleetError(self.refuse[label])
        held = [agent for agent in self.live if agent.project_id == project.id]
        if len(held) >= self.cap:  # every row not ended, running or not
            raise fleet_service.FleetError(
                f"{project.root.name} already runs {len(held)} agents "
                f"(max_agents_per_project = {self.cap}) — stop one, or raise the limit in [fleet]"
            )
        agent = _agent(project, label, role)
        self.live.append(agent)
        return fleet_service.SpawnReceipt(agent=agent, asked_label=None, tmux_session="asq-demo")

    def restart(
        self, project: ProjectInfo, label: str, *, agent_id: str | None = None
    ) -> fleet_service.RestartReceipt:
        """``fleet.restart``: the row ``agent_id`` names ends, and a new one takes its label."""
        old = next(a for a in self.live if a.project_id == project.id and a.label == label)
        assert agent_id == old.id, f"Welcome restarted {label} by its label, not its row"
        new = old.model_copy(update={"id": f"{old.id}-again"})
        self.live.remove(old)
        self.live.append(new)
        self.states.pop(label, None)  # its window is back
        self.restarted.append(label)
        return fleet_service.RestartReceipt(
            replaced=old, started=new, resumed=True, was_running=False, tmux_session="asq-demo"
        )

    def validate(self, text: str) -> PathVerdict:
        path = Path(text).expanduser() if text.strip() else None
        is_dir = path is not None and path.is_dir()
        return PathVerdict(
            text=text,
            path=path,
            exists=is_dir,
            is_dir=is_dir,
            root=path if is_dir else None,
            registered=None,
            is_git=False,
        )


def here(root: Path, project: ProjectInfo | None = None) -> Candidate:
    return Candidate(root=root, is_git=True, project=project, here=True)


# --------------------------------------------------------------------------- hosts


class Host(App[None]):
    """Mounts the page alone and records every message that reaches the app."""

    def __init__(self, page: WelcomeView) -> None:
        super().__init__()
        self.page = page
        self.received: list[Message] = []

    def compose(self) -> ComposeResult:
        yield self.page

    def on_welcome_view_progress(self, message: WelcomeView.Progress) -> None:
        self.received.append(message)

    def on_accounts_selected(self, message: AccountsSelected) -> None:
        self.received.append(message)

    def on_agent_selected(self, message: AgentSelected) -> None:
        self.received.append(message)


def hosted(
    machine: Machine,
    fn: Callable[[Pilot[None], WelcomeView, Host], Awaitable[T]],
    *,
    platform: str = "linux",
    recheck: float = 0,
) -> T:
    async def run() -> T:
        page = WelcomeView(seams=machine.seams(platform), recheck_seconds=recheck, id="welcome")
        host = Host(page)
        async with host.run_test(size=SIZE) as pilot:
            await settle_page(host)
            return await fn(pilot, page, host)

    return asyncio.run(run())


def shown(widget: Static) -> str:
    """The text a widget renders — the artefact, not the argument."""
    visual = widget.visual
    plain = getattr(visual, "plain", None)
    assert isinstance(plain, str), f"{widget!r} renders a {type(visual).__name__}, not text"
    return plain


def card(page: WelcomeView, static_id: str) -> str:
    return shown(page.query_one(f"#{static_id}", Static))


def visible(page: WelcomeView, button_id: str) -> bool:
    button = page.query_one(f"#{button_id}", Button)
    return bool(button.display) and not button.disabled


async def press(pilot: Pilot[None], page: WelcomeView, button_id: str) -> None:
    page.query_one(f"#{button_id}", Button).press()
    await settle_page(pilot.app)


def candidate_buttons(page: WelcomeView) -> list[str]:
    return [str(button.label) for button in page.query(".use-candidate").results(Button)]


def first_candidate(page: WelcomeView) -> Button:
    return next(iter(page.query(".use-candidate").results(Button)))


# --------------------------------------------------------------------------- step 2


def test_a_missing_claude_names_the_install_and_npm_comes_last() -> None:
    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[str, bool, bool]:
        return (
            card(page, "claude-status"),
            visible(page, "claude-check"),
            visible(page, "fleet-manager"),
        )

    text, check, start = hosted(Machine(), go)
    assert accounts_core.INSTALL_COMMAND in text
    assert text.index(accounts_core.INSTALL_COMMAND) < text.index(accounts_core.INSTALL_ALTERNATIVE)
    assert "Claude Code is not installed" in text
    assert check and not start  # Check again offered; step 3 cannot start yet


def test_each_platform_names_its_own_install() -> None:
    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> str:
        return card(page, "claude-status")

    mac = hosted(Machine(), go, platform="darwin")
    windows = hosted(Machine(), go, platform="win32")
    assert first_run.BREW_COMMAND in mac and "WSL2" not in mac
    assert "WSL2" in windows and first_run.BREW_COMMAND not in windows


def test_an_override_that_is_missing_is_named_not_claude() -> None:
    stand_in = ClaudeState(wanted="/opt/demo/agent", source="env:global")

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> str:
        return card(page, "claude-status")

    text = hosted(Machine(claude=[stand_in]), go)
    assert "/opt/demo/agent" in text and "env:global" in text
    assert accounts_core.INSTALL_COMMAND not in text


def test_check_again_flips_the_card_without_a_restart() -> None:
    machine = Machine(claude=[MISSING, READY])

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[str, str, bool]:
        before = card(page, "claude-status")
        await press(pilot, page, "claude-check")
        return before, card(page, "claude-status"), visible(page, "claude-check")

    before, after, check = hosted(machine, go)
    assert "not installed" in before
    assert "✓ Claude Code 2.1.300" in after and "connected" in after
    assert not check  # nothing left to check again for
    assert machine.looks == [True, True]  # both looks were full ones


def test_the_page_looks_again_by_itself_while_shown() -> None:
    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[str, int]:
        rounds = 0
        while rounds < 60 and "✓ Claude Code" not in card(page, "claude-status"):
            await pilot.pause(0.05)
            rounds += 1
        await settle_page(host)
        return card(page, "claude-status"), rounds

    ticking = Machine(claude=[MISSING, READY])
    text, rounds = hosted(ticking, go, recheck=0.05)
    assert "✓ Claude Code" in text
    assert False in ticking.looks  # the periodic look leaves the login out

    async def wait(pilot: Pilot[None], page: WelcomeView, host: Host) -> str:
        for _ in range(max(rounds * 3, 10)):  # three times what the ticking page needed
            await pilot.pause(0.05)
        await settle_page(host)
        return card(page, "claude-status")

    # Control: with the re-check off, the same wait (and more) changes nothing.
    assert "not installed" in hosted(Machine(claude=[MISSING, READY]), wait, recheck=0)


def test_connect_runs_the_doctor_fix_and_opens_step_three(tmp_path: Path) -> None:
    project = ProjectInfo(id="prj_demo", root=tmp_path, onboarded_at=T0)
    machine = Machine(claude=[UNHOOKED], found=Candidates(items=(here(tmp_path, project),)))

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[bool, bool, str]:
        waiting = visible(page, "fleet-manager")
        await press(pilot, page, "claude-connect")
        return waiting, visible(page, "fleet-manager"), card(page, "claude-status")

    waiting, ready, text = hosted(machine, go)
    assert machine.connects == 1
    assert not waiting and ready  # step 3 waited for the hooks
    assert "✓ connected" in text


def test_a_connect_that_fails_says_why() -> None:
    refused = FixResult(
        fix=first_run.connect_fix(), returncode=1, reason="claude-code is not installed"
    )
    machine = Machine(claude=[UNHOOKED], connect_answer=refused)

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[str, bool]:
        await press(pilot, page, "claude-connect")
        return card(page, "claude-status"), visible(page, "claude-connect")

    text, again = hosted(machine, go)
    assert "✗ claude-code is not installed" in text
    assert again  # Connect is still there to try again


def test_hooks_switched_off_are_named_instead_of_offering_connect(tmp_path: Path) -> None:
    switched_off = dataclasses.replace(UNHOOKED, hooks_off=tmp_path / ".claude" / "settings.json")

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[str, bool]:
        return card(page, "claude-status"), visible(page, "claude-connect")

    text, connect = hosted(Machine(claude=[switched_off]), go)
    assert '"disableAllHooks": true' in text and "Connect cannot change that" in text
    assert not connect
    # Control: hooks merely missing are what Connect is for.
    assert hosted(Machine(claude=[UNHOOKED]), go)[1]


def test_a_settings_file_connect_refuses_is_named_instead_of_offering_connect(
    tmp_path: Path,
) -> None:
    """Connect could only fail on a settings.json `agents connect` refuses (not a JSON
    object, or read-only): step 2 says why, as it does for hooks switched off (review of
    #257)."""
    why = f"can't write {tmp_path / '.claude' / 'settings.json'}: this user may not write it"
    refused = dataclasses.replace(UNHOOKED, refused=why)

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[str, bool]:
        return card(page, "claude-status"), visible(page, "claude-connect")

    text, connect = hosted(Machine(claude=[refused]), go)
    assert why in text and "Connect cannot change that" in text, text
    assert not connect
    assert hosted(Machine(claude=[UNHOOKED]), go)[1], "control: missing hooks get Connect"


def test_sign_in_opens_the_accounts_page() -> None:
    signed_out = dataclasses.replace(READY, signed_in=False)

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[bool, list[str]]:
        offered = visible(page, "claude-sign-in")
        if offered:
            await press(pilot, page, "claude-sign-in")
        return offered, [type(message).__name__ for message in host.received]

    offered, received = hosted(Machine(claude=[signed_out]), go)
    assert offered and received == ["AccountsSelected"]
    # Control: signed in, there is no button to press.
    assert hosted(Machine(claude=[READY]), go) == (False, [])


# --------------------------------------------------------------------------- step 1


def test_a_listed_folder_asq_started_in_is_chosen_without_a_click(tmp_path: Path) -> None:
    project = ProjectInfo(id="prj_demo", root=tmp_path / "demo-app", onboarded_at=T0)
    machine = Machine(found=Candidates(items=(here(project.root, project),)))

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[str | None, str]:
        chosen = page.project.id if page.project is not None else None
        return chosen, card(page, "project-status")

    chosen, text = hosted(machine, go)
    assert chosen == "prj_demo" and "✓ demo-app" in text
    assert machine.onboarded == []  # listed: nothing to set up


@dataclass
class Frame:
    """The shell's last frame, as ``FleetApp.snapshot`` holds it: what step 1 reads."""

    projects: list[ProjectInfo]


class FramedHost(Host):
    """A host with a shell's frame, as asq's shell is."""

    def __init__(self, page: WelcomeView, frame: Frame) -> None:
        super().__init__(page)
        self.snapshot = frame


def test_step_one_lists_again_when_the_frame_changed_while_it_was_read(tmp_path: Path) -> None:
    """Step 1 recorded the frame current when its list landed, not the one the list was made
    from, so a project the shell listed during the read was never offered: after *Choose
    another*, the folder just onboarded was missing until `w` (review of #257). A tick
    after the read lists step 1 again from the newer frame."""
    alpha = ProjectInfo(id="prj_alpha", root=tmp_path / "alpha", onboarded_at=T0)
    beta = ProjectInfo(id="prj_beta", root=tmp_path / "beta", onboarded_at=T0)
    frame = Frame(projects=[alpha])

    def listing(listed: list[ProjectInfo] | None) -> Candidates:
        frame.projects = [alpha, beta]  # a refresh that lands while the list is read
        rows = [Candidate(root=p.root, is_git=True, project=p, here=False) for p in listed or []]
        return Candidates(items=tuple(rows))

    machine = Machine(found=listing)

    async def run() -> tuple[list[str], list[str]]:
        page = WelcomeView(seams=machine.seams(), recheck_seconds=0, id="welcome")
        host = FramedHost(page, frame)
        async with host.run_test(size=SIZE):
            await settle_page(host)
            first = candidate_buttons(page)
            page._tick()  # what the page's interval runs
            await settle_page(host)
            return first, candidate_buttons(page)

    first, after_a_tick = asyncio.run(run())
    read = [sorted(p.id for p in listed or []) for listed in machine.frames]
    assert first == ["Use alpha"], "control: the first list is the frame it read"
    assert read == [["prj_alpha"], ["prj_alpha", "prj_beta"]], read
    assert after_a_tick == ["Use alpha", "Use beta"], after_a_tick


def test_choosing_an_unlisted_folder_onboards_it_first(tmp_path: Path) -> None:
    folder = tmp_path / "new-app"
    folder.mkdir()
    machine = Machine(found=Candidates(items=(here(folder),)))

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[list[str], str]:
        buttons = candidate_buttons(page)
        first_candidate(page).press()
        await settle_page(host)
        return buttons, card(page, "project-status")

    buttons, text = hosted(machine, go)
    assert buttons == ["Use new-app"]
    assert machine.onboarded == [folder]
    assert "✓ new-app" in text


def test_onboarding_reports_progress_to_the_shell(tmp_path: Path) -> None:
    folder = tmp_path / "new-app"
    folder.mkdir()
    machine = Machine(found=Candidates(items=(here(folder),)))

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> list[str | None]:
        first_candidate(page).press()
        await settle_page(host)
        return [m.project_id for m in host.received if isinstance(m, WelcomeView.Progress)]

    assert hosted(machine, go) == [project_id_for(folder)]


def test_a_failed_onboarding_says_why_and_chooses_nothing(tmp_path: Path) -> None:
    folder = tmp_path / "new-app"
    folder.mkdir()
    machine = Machine(
        found=Candidates(items=(here(folder),)),
        onboard_answer=lambda path: OnboardOutcome(path=path, reason="init failed: disk full"),
    )

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[str, bool, int]:
        first_candidate(page).press()
        await settle_page(host)
        progress = sum(isinstance(m, WelcomeView.Progress) for m in host.received)
        return card(page, "project-status"), page.project is None, progress

    text, nothing, progress = hosted(machine, go)
    assert "✗ init failed: disk full" in text
    assert nothing and progress == 0


def test_a_typed_folder_is_judged_then_used(tmp_path: Path) -> None:
    folder = tmp_path / "typed"
    folder.mkdir()
    machine = Machine()

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[bool, bool, str]:
        before = visible(page, "welcome-path-use")
        page.query_one("#welcome-path", Input).value = str(folder)
        await settle_page(host)
        enabled = visible(page, "welcome-path-use")
        await press(pilot, page, "welcome-path-use")
        return before, enabled, card(page, "project-status")

    before, enabled, text = hosted(machine, go)
    assert not before and enabled  # the verdict decides the button
    assert machine.onboarded == [folder] and "✓ typed" in text


def test_choose_another_is_not_undone_by_a_return_to_the_page(tmp_path: Path) -> None:
    """Step 1 is listed again on every return (`w`, back from Accounts), and each listing
    picked the folder asq started in again, undoing *Choose another* (review of #257)."""
    machine, project = _ready_machine(tmp_path)

    async def go(
        pilot: Pilot[None], page: WelcomeView, host: Host
    ) -> tuple[ProjectInfo | None, ProjectInfo | None, int]:
        started = page.project
        await press(pilot, page, "welcome-change")
        page.on_show()  # what a return to the page runs
        await settle_page(host)
        return started, page.project, len(machine.frames)

    started, after, listings = hosted(machine, go)
    assert started == project, "control: the folder asq started in is picked at first"
    assert listings == 2, "the return listed step 1 again"
    assert after is None, "Choose another stands"


@dataclass
class HeldStart(Machine):
    """A machine whose spawns wait for ``release``: a start still in flight."""

    release: threading.Event = field(default_factory=threading.Event)
    entered: threading.Event = field(default_factory=threading.Event)

    def spawn(self, project: ProjectInfo, role: str, **kwargs: Any) -> fleet_service.SpawnReceipt:
        self.entered.set()
        self.release.wait(10)
        return super().spawn(project, role, **kwargs)


def test_step_one_holds_its_folder_while_a_start_runs(tmp_path: Path) -> None:
    """*Choose another* was refused only while a folder was being set up. Pressed while
    Start manager ran, then *Use beta*, alpha's start landed on beta's card: "✓ manager —
    started", Open opening alpha's manager, and Start the coders starting coders in beta
    under no manager (review of #257). Step 1 holds its folder until the start lands, as
    during onboarding; ``choose`` is what *Use beta* and an owed Enter run."""
    alpha = ProjectInfo(id="prj_alpha", root=tmp_path / "alpha", onboarded_at=T0)
    beta = ProjectInfo(id="prj_beta", root=tmp_path / "beta", onboarded_at=T0)
    other = Candidate(root=beta.root, is_git=True, project=beta)
    machine = HeldStart(claude=[READY], found=Candidates(items=(here(alpha.root, alpha), other)))

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> list[Any]:
        page.query_one("#fleet-manager", Button).press()
        # The start is held on purpose: settle no worker group while it is.
        await settle_until(host, machine.entered.is_set, group="held")
        page.query_one("#welcome-change", Button).press()
        await settle_page(host, group="held")
        page.choose(beta.root, beta)
        await settle_page(host, group="held")
        seen: list[Any] = [page.project, card(page, "fleet-status")]
        machine.release.set()  # alpha's start lands
        await settle_page(host)
        seen += [page.project, card(page, "fleet-status")]
        await press(pilot, page, "welcome-change")
        seen.append(page.project)
        return seen

    during, starting, after, landed, moved = hosted(machine, go)
    assert during == alpha and "Starting the manager…" in starting, starting
    assert after == alpha and "✓ manager — started" in landed, landed
    assert [(agent.project_id, agent.label) for agent in machine.live] == [("prj_alpha", "manager")]
    assert moved is None, "control: once the start has landed, Choose another works"


def test_step_three_waits_while_step_one_sets_a_folder_up(tmp_path: Path) -> None:
    """An unlisted folder is listed by the store before its snapshot is packed, so the
    shell's next frame listed it mid-onboarding, step 1 was listed again, and the folder
    asq started in came back: Start manager would have started there (review of #257)."""
    machine, _ = _ready_machine(tmp_path)
    other = tmp_path / "project-b"
    other.mkdir()
    release = threading.Event()

    def held(path: Path) -> OnboardOutcome:
        release.wait(10)
        project = ProjectInfo(id=project_id_for(path), root=path, onboarded_at=T0)
        machine.stored[project.id] = project
        report = SetupReport(home=path / ".home", already_initialized=False, project=project)
        return OnboardOutcome(path=path, project_id=project.id, report=report)

    machine.onboard_answer = held

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> list[Any]:
        await press(pilot, page, "welcome-change")
        page.choose(other, None)
        page.find_candidates()  # what the tick runs once the frame lists project-b
        # The onboarding is held on purpose: wait for the listing alone (no such group).
        await settle_until(host, lambda: not page._in_flight("candidates"), group="held")
        await settle_page(host, group="held")
        seen: list[Any] = [page.project, visible(page, "fleet-manager")]
        release.set()
        await settle_page(host)
        seen.append(page.project.root if page.project else None)
        return seen

    during, manager_offered, settled = hosted(machine, go)
    assert during is None, "the folder asq started in is not picked mid-onboarding"
    assert not manager_offered, "with nothing chosen, step 3 offers nothing"
    assert settled == other, "control: the onboarding settles step 1 on the new folder"


@dataclass
class SlowDisk(Machine):
    """A machine whose first judgement of a typed folder takes until ``release`` is set."""

    release: threading.Event = field(default_factory=threading.Event)
    ui: threading.Thread | None = None
    judged: list[tuple[str, bool]] = field(default_factory=list)
    """Each text judged, and whether that ran on the UI thread."""

    def validate(self, text: str) -> PathVerdict:
        on_ui = threading.current_thread() is self.ui
        self.judged.append((text, on_ui))
        if len(self.judged) == 1 and not on_ui:
            self.release.wait(10)
        return super().validate(text)


def test_a_typed_folder_is_judged_off_the_ui_thread_and_the_latest_text_wins(
    tmp_path: Path,
) -> None:
    """Every keystroke ran the git probe and a store open on the UI thread, so a slow disk
    stalled the shell between keys (review of #257). One judgement runs at a time, off the
    UI thread; what is typed meanwhile is judged when it lands, and Enter waits for it."""
    folder = tmp_path / "typed"
    folder.mkdir()
    machine = SlowDisk()

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[str, str]:
        machine.ui = threading.current_thread()
        box = page.query_one("#welcome-path", Input)
        box.focus()
        for text in (str(tmp_path), f"{folder}-not-this", str(folder)):
            box.value = text
            await pilot.pause()
        await pilot.press("enter")
        held = page.verdict.text
        machine.release.set()
        await settle_page(host)
        return held, page.verdict.text

    held, verdict = hosted(machine, go)
    assert machine.judged == [(str(tmp_path), False), (str(folder), False)], machine.judged
    assert (held, verdict) == ("", str(folder)), "no verdict until its judgement lands"
    assert machine.onboarded == [folder], "Enter used the folder once it was judged"


def test_start_manager_waits_while_an_owed_enter_sets_another_folder_up(tmp_path: Path) -> None:
    """Step 3 waits for the folder step 1 is setting up, even with another folder chosen.

    Enter on a typed folder that is still being judged is owed. The first listing can land
    meanwhile and choose the listed folder asq started in, as the user has chosen nothing
    yet; then the owed Enter sets the typed folder up while that one is still chosen.
    Start manager was offered then, and started the manager in the folder step 1 was
    leaving. The test above reaches onboarding with nothing chosen, where step 3 offers
    nothing either way (review of #257).
    """
    started = ProjectInfo(id="prj_demo", root=tmp_path / "demo-app", onboarded_at=T0)
    typed = tmp_path / "typed-app"
    typed.mkdir()
    listing = threading.Event()
    onboarding = threading.Event()

    def listed_late(frame: list[ProjectInfo] | None) -> Candidates:
        listing.wait(10)
        return Candidates(items=(here(started.root, started),))

    machine = SlowDisk(claude=[READY], found=listed_late)

    def held(path: Path) -> OnboardOutcome:
        onboarding.wait(10)
        project = ProjectInfo(id=project_id_for(path), root=path, onboarded_at=T0)
        machine.stored[project.id] = project
        return OnboardOutcome(path=path, project_id=project.id)

    machine.onboard_answer = held

    async def run() -> list[Any]:
        page = WelcomeView(seams=machine.seams(), recheck_seconds=0, id="welcome")
        host = Host(page)
        async with host.run_test(size=SIZE) as pilot:
            machine.ui = threading.current_thread()
            box = page.query_one("#welcome-path", Input)
            box.focus()
            box.value = str(typed)
            await pilot.pause()
            await pilot.press("enter")  # owed: the folder is still being judged
            # The listing and the onboarding are held on purpose: settle no worker group.
            listing.set()
            await settle_until(host, lambda: page.project is not None, group="held")
            machine.release.set()  # the judgement lands, and the owed Enter runs
            await settle_until(host, lambda: "onboard" in page.busy, group="held")
            seen: list[Any] = [page.project, visible(page, "fleet-manager")]
            page.query_one("#fleet-manager", Button).press()
            await settle_until(host, lambda: not page._in_flight("manager"), group="held")
            seen.append(list(machine.starts))
            onboarding.set()
            await settle_page(host)
            seen += [page.project.root if page.project else None, visible(page, "fleet-manager")]
            return seen

    chosen, offered, starts, settled, offered_after = asyncio.run(run())
    assert chosen == started, "premise: demo-app is chosen while typed-app is set up"
    assert not offered, "Start manager waits for the folder being set up"
    assert starts == [], "nothing starts in the folder step 1 is leaving"
    assert (settled, offered_after) == (typed, True), "control: then it starts in typed-app"


def test_names_reach_the_screen_as_they_are(tmp_path: Path) -> None:
    folder = tmp_path / "[archive]"
    folder.mkdir()
    machine = Machine(found=Candidates(items=(here(folder),)))

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> list[str]:
        return candidate_buttons(page)

    assert hosted(machine, go) == ["Use [archive]"]


def test_a_store_that_will_not_open_costs_the_list_only(tmp_path: Path) -> None:
    machine = Machine(claude=[READY], found=RuntimeError("database disk image is malformed"))

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[str, str, bool]:
        return card(page, "project-status"), card(page, "claude-status"), page.is_mounted

    project_text, claude_text, alive = hosted(machine, go)
    assert "database disk image is malformed" in project_text
    assert "✓ Claude Code" in claude_text and alive  # the rest of the page kept going


# --------------------------------------------------------------------------- step 3


def _ready_machine(tmp_path: Path, **overrides: Any) -> tuple[Machine, ProjectInfo]:
    project = ProjectInfo(id="prj_demo", root=tmp_path / "demo-app", onboarded_at=T0)
    machine = Machine(claude=[READY], found=Candidates(items=(here(project.root, project),)))
    for key, value in overrides.items():
        setattr(machine, key, value)
    return machine, project


@pytest.mark.parametrize("connected_in", ["the folder asq started in", "the chosen project"])
def test_step_two_answers_for_the_folder_step_one_chose(tmp_path: Path, connected_in: str) -> None:
    """Step 2 asked about the folder asq started in, while step 3 starts the fleet in the
    project step 1 chose: with a project-scope plugin in one and not the other, it said
    connected for a fleet that would run no aisquare, or Connect for one that would
    (review of #257). Step 2 answers for the chosen root, and step 3 waits for it."""
    machine, project = _ready_machine(tmp_path)
    here_only = connected_in == "the folder asq started in"
    machine.claude = [READY if here_only else UNHOOKED]
    machine.claude_at = {project.root: UNHOOKED if here_only else READY}

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[str, bool, bool]:
        return (
            card(page, "claude-status"),
            visible(page, "claude-connect"),
            visible(page, "fleet-manager"),
        )

    text, connect_offered, manager_offered = hosted(machine, go)
    assert machine.roots[-1] == project.root, f"asked about {machine.roots}"
    assert connect_offered is here_only, text
    assert manager_offered is not here_only, text


def test_the_manager_then_the_coders_and_the_fleet_is_up(tmp_path: Path) -> None:
    machine, _ = _ready_machine(tmp_path)

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> list[Any]:
        seen: list[Any] = []
        await press(pilot, page, "fleet-manager")
        seen.append(card(page, "fleet-status"))
        seen.append((visible(page, "fleet-open"), visible(page, "fleet-coders")))
        await press(pilot, page, "fleet-coders")
        seen.append(card(page, "fleet-status"))
        seen.append(visible(page, "fleet-coders"))
        seen.append([m.project_id for m in host.received if isinstance(m, WelcomeView.Progress)])
        return seen

    after_manager, buttons, after_coders, coders_left, progress = hosted(machine, go)
    assert machine.starts == [("prj_demo", True, 0), ("prj_demo", False, 2)]
    assert "✓ manager — started" in after_manager and FLEET_UP not in after_manager
    assert "trust" in after_manager  # the one-time question is named before the coders
    assert buttons == (True, True)
    assert "✓ coder-1 — started" in after_coders and "✓ coder-2 — started" in after_coders
    assert FLEET_UP in after_coders and not coders_left
    assert progress == ["prj_demo", "prj_demo"]


def test_step_three_is_idempotent_against_a_live_fleet(tmp_path: Path) -> None:
    machine, project = _ready_machine(tmp_path)
    machine.live = [_agent(project, "manager", "manager"), _agent(project, "coder-1", "coder")]

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> str:
        await press(pilot, page, "fleet-manager")
        await press(pilot, page, "fleet-coders")
        return card(page, "fleet-status")

    text = hosted(machine, go)
    labels = [agent.label for agent in machine.live]
    assert labels == ["manager", "coder-1", "coder-2"]  # one coder added, no second manager
    assert "✓ manager — running" in text and "✓ coder-2 — started" in text


def test_a_refusal_is_shown_inline(tmp_path: Path) -> None:
    machine, _ = _ready_machine(tmp_path, refuse={"manager": "tmux is not installed"})

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[str, bool]:
        await press(pilot, page, "fleet-manager")
        return card(page, "fleet-status"), visible(page, "fleet-manager")

    text, again = hosted(machine, go)
    assert "✗ manager: tmux is not installed" in text and again


def test_step_three_waits_for_tmux_and_names_its_install(tmp_path: Path) -> None:
    machine, _ = _ready_machine(
        tmp_path,
        tmux=TmuxState(found=False, problem="tmux is not installed", hint="apt install tmux"),
    )

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[str, bool]:
        return card(page, "fleet-status"), visible(page, "fleet-manager")

    text, start = hosted(machine, go)
    assert "tmux is not installed — install it: apt install tmux" in text and not start


def test_open_the_manager_selects_its_row(tmp_path: Path) -> None:
    machine, _ = _ready_machine(tmp_path)

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> list[tuple[str, str]]:
        await press(pilot, page, "fleet-manager")
        await press(pilot, page, "fleet-open")
        return [(m.project_id, m.agent_id) for m in host.received if isinstance(m, AgentSelected)]

    assert hosted(machine, go) == [("prj_demo", "agt_manager")]


def test_the_keyboard_follows_the_trust_question_then_the_coders(tmp_path: Path) -> None:
    """After Start manager the manager's pane is next (Claude Code's trust question), then
    the coders: focus is on each in turn, and on the visible primary button each time."""
    machine, _ = _ready_machine(tmp_path)

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> list[tuple[str, str]]:
        seen: list[tuple[str, str]] = []

        def where() -> tuple[str, str]:
            focused = host.focused
            assert isinstance(focused, Button) and focused in host.screen.focus_chain
            return str(focused.id), str(focused.variant)

        page.query_one("#fleet-manager", Button).focus()
        for _ in range(3):  # Start manager; Open the manager; Start the coders
            await pilot.press("enter")
            await settle_page(host)
            seen.append(where())
        return seen

    after_start, after_open, after_coders = hosted(machine, go)
    assert after_start == ("fleet-open", "primary")  # the trust question comes first
    assert after_open == ("fleet-coders", "primary")  # then the coders
    assert after_coders == ("fleet-open", "primary")  # and with the fleet up, the manager
    assert [m for m in machine.starts] == [("prj_demo", True, 0), ("prj_demo", False, 2)]


def test_tmux_installed_while_the_page_is_shown_opens_step_three(tmp_path: Path) -> None:
    missing = TmuxState(found=False, problem="tmux is not installed", hint="apt install tmux")

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[bool, bool]:
        before = visible(page, "fleet-manager")
        machine.tmux = TmuxState(found=True, version=(3, 4))  # installed in another terminal
        for _ in range(60):
            await pilot.pause(0.05)
            if visible(page, "fleet-manager"):
                break
        await settle_page(host)
        return before, visible(page, "fleet-manager")

    machine, _ = _ready_machine(tmp_path, tmux=missing)
    assert hosted(machine, go, recheck=0.05) == (False, True)
    # Control: with the re-check off, nothing notices.
    machine, _ = _ready_machine(tmp_path, tmux=missing)
    assert hosted(machine, go, recheck=0) == (False, False)


def test_connect_works_while_a_folder_is_being_set_up(tmp_path: Path) -> None:
    """Steps 1 and 2 are independent: onboarding a folder does not swallow Connect."""
    folder = tmp_path / "slow-app"
    folder.mkdir()
    gate = threading.Event()
    machine = Machine(claude=[UNHOOKED], found=Candidates(items=(here(folder),)))

    def slow(path: Path) -> OnboardOutcome:
        gate.wait(10)
        project = ProjectInfo(id=project_id_for(path), root=path, onboarded_at=T0)
        machine.stored[project.id] = project
        return OnboardOutcome(path=path, project_id=project.id)

    machine.onboard_answer = slow

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[int, bool, str]:
        first_candidate(page).press()
        await pilot.pause()
        page.query_one("#claude-connect", Button).press()
        for _ in range(100):
            await pilot.pause(0.02)
            if machine.connects:
                break
        during = machine.connects
        gate.set()
        await settle_page(host)
        return during, page.project is not None, card(page, "claude-status")

    during, chosen, text = hosted(machine, go)
    assert during == 1  # Connect ran while init and doctor were still running
    assert chosen and "✓ connected" in text


def test_a_full_look_asked_for_during_another_is_owed_not_dropped() -> None:
    hold = threading.Event()
    machine = Machine(claude=[MISSING], hold_first_look=hold)

    async def run() -> list[bool]:
        page = WelcomeView(seams=machine.seams(), recheck_seconds=0, id="welcome")
        host = Host(page)
        async with host.run_test(size=SIZE) as pilot:
            for _ in range(100):  # the mount's look is in flight, and held
                await pilot.pause(0.01)
                if machine.looks:
                    break
            page.look(full=True)  # what Check again, Connect and a return to the page ask
            hold.set()
            await settle_page(host)
            return list(machine.looks)

    assert asyncio.run(run()) == [True, True]


def test_old_error_lines_go_once_their_problem_has(tmp_path: Path) -> None:
    refused = FixResult(
        fix=first_run.connect_fix(), returncode=1, reason="claude-code is not installed"
    )
    machine, _ = _ready_machine(
        tmp_path, claude=[UNHOOKED], connect_answer=refused, blind="database is locked"
    )

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> list[str]:
        seen: list[str] = []
        await press(pilot, page, "claude-connect")
        seen.append(card(page, "claude-status"))
        machine.claude = [READY]  # connected another way: a terminal, or Sign in
        page.look(full=True)
        await settle_page(host)
        seen.append(card(page, "claude-status"))
        await press(pilot, page, "fleet-manager")
        seen.append(card(page, "fleet-status"))
        machine.blind = None  # the store answers again
        await press(pilot, page, "fleet-manager")
        seen.append(card(page, "fleet-status"))
        return seen

    failed, connected, refused_line, started = hosted(machine, go)
    assert "✗ claude-code is not installed" in failed
    assert "✓ connected" in connected and "claude-code is not installed" not in connected
    # A refusal no agent's label can replace: it goes with the next start.
    assert "✗ fleet: could not read the fleet: database is locked" in refused_line
    assert "✓ manager — started" in started and "could not read the fleet" not in started


def test_gh_missing_is_a_note_not_a_block(tmp_path: Path) -> None:
    machine, _ = _ready_machine(tmp_path, gh=False)

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[str, bool]:
        return card(page, "fleet-status"), visible(page, "fleet-manager")

    text, start = hosted(machine, go)
    assert "gh is not installed" in text and start


# --------------------------------------------------------------------------- in the shell


@pytest.fixture(params=[None, "1"], ids=["captain-unset", "captain-on"])
def captain(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str | None:
    """#240 turns the experimental captain on for every test: these hold either way."""
    value: str | None = request.param
    if value is None:
        monkeypatch.delenv(CAPTAIN, raising=False)
    else:
        monkeypatch.setenv(CAPTAIN, value)
    return value


@pytest.fixture(autouse=True)
def no_real_tmux(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, ...]]]:
    """Every tmux command these tests cause must address :data:`PRIVATE_SOCKET` (or none)."""
    ran: list[tuple[str, ...]] = []

    def record(argv: Sequence[str], stdin: bytes | None) -> Completed:
        ran.append(tuple(argv))
        return Completed(1, "", "no server running (a UI test addresses no real fleet)\n")

    monkeypatch.setattr(tmux_core, "_tmux", record)
    yield ran
    wrong = [argv for argv in ran if asks_a_server(argv) and socket_of(argv) != PRIVATE_SOCKET]
    assert not wrong, f"a UI test addressed a tmux socket that is not the test's: {wrong[:2]}"


@pytest.fixture
def fleet_rows(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[FleetAgentStatus]]:
    """What ``fleet_service.list_agents`` answers per project id — the shell's agent rows."""
    rows: dict[str, list[FleetAgentStatus]] = {}

    def fake(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        return list(rows.get(project.id, []))

    monkeypatch.setattr(fleet_service, "list_agents", fake)
    return rows


def in_shell(
    machine: Machine,
    fn: Callable[[Pilot[None], FleetApp, WelcomeView], Awaitable[T]],
    *,
    doctor: Callable[[], list[DoctorCheck]] = lambda: [],
) -> T:
    """Run ``fn`` against the real shell, its Welcome page scripted by ``machine``."""

    async def run() -> T:
        app = FleetApp(refresh_seconds=3600, doctor=doctor, accounts=None)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await settle_page(app)
            return await fn(pilot, app, app.query_one("#welcome", WelcomeView))

    return asyncio.run(run())


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> Callable[[Machine], None]:
    def use(machine: Machine) -> None:
        monkeypatch.setattr(welcome, "DEFAULT_SEAMS", machine.seams())

    return use


def test_welcome_is_the_first_view_and_never_takes_the_keyboard(
    captain: str | None,
    fleet_rows: dict[str, list[FleetAgentStatus]],
    scripted: Callable[[Machine], None],
    tmp_path: Path,
) -> None:
    machine, _ = _ready_machine(tmp_path)
    scripted(machine)

    async def go(pilot: Pilot[None], app: FleetApp, page: WelcomeView) -> list[Any]:
        seen: list[Any] = [app.content.current, type(app.focused).__name__]
        seen.append(page.next_button() is not None)  # there IS a button it could have taken
        await pilot.press("q")
        await settle_page(app)
        seen.append(app.return_code)
        return seen

    current, focused, could, code = in_shell(machine, go)
    assert (current, focused, could) == ("welcome", "Sidebar", True)
    assert code == 0  # control: the app's keys are live, so q from the sidebar quits


def test_tab_from_the_sidebar_lands_on_the_next_step(
    captain: str | None,
    fleet_rows: dict[str, list[FleetAgentStatus]],
    scripted: Callable[[Machine], None],
    tmp_path: Path,
) -> None:
    async def go(pilot: Pilot[None], app: FleetApp, page: WelcomeView) -> str | None:
        await pilot.press("tab")
        await settle_page(app)
        return app.focused.id if app.focused is not None else None

    ready, _ = _ready_machine(tmp_path)
    scripted(ready)
    assert in_shell(ready, go) == "fleet-manager"  # steps 1 and 2 are done
    unhooked, _ = _ready_machine(tmp_path, claude=[UNHOOKED])
    scripted(unhooked)
    assert in_shell(unhooked, go) == "claude-connect"  # control: step 2 is next


def test_the_keyboard_alone_gets_from_a_new_folder_to_a_fleet(
    captain: str | None,
    fleet_rows: dict[str, list[FleetAgentStatus]],
    scripted: Callable[[Machine], None],
    tmp_path: Path,
) -> None:
    folder = tmp_path / "demo-app"
    folder.mkdir()
    machine = Machine(claude=[UNHOOKED], found=Candidates(items=(here(folder),)))
    scripted(machine)

    async def go(pilot: Pilot[None], app: FleetApp, page: WelcomeView) -> list[str | None]:
        focus: list[str | None] = []
        for keys in (("tab", "enter"), ("enter",), ("enter",), ("tab", "enter")):
            await pilot.press(*keys)
            await settle_page(app)
            focus.append(app.focused.id if app.focused is not None else None)
        focus.append(card(page, "fleet-status"))
        return focus

    *focus, status = in_shell(machine, go)
    assert machine.onboarded == [folder]  # Tab, Enter: the folder
    assert machine.connects == 1  # Enter: Connect
    assert machine.starts == [  # Enter: the manager; Tab, Enter: the coders
        (project_id_for(folder), True, 0),
        (project_id_for(folder), False, 2),
    ]
    assert FLEET_UP in (status or "")
    assert focus == ["claude-connect", "fleet-manager", "fleet-open", "fleet-open"]


def test_a_typed_folder_hands_the_keyboard_to_the_next_step(
    captain: str | None,
    fleet_rows: dict[str, list[FleetAgentStatus]],
    scripted: Callable[[Machine], None],
    tmp_path: Path,
) -> None:
    """The path box hides once its folder is set up; the keyboard must not stay in it."""
    folder = tmp_path / "typed-app"
    folder.mkdir()
    machine = Machine(claude=[UNHOOKED])
    scripted(machine)

    async def go(pilot: Pilot[None], app: FleetApp, page: WelcomeView) -> tuple[str | None, bool]:
        field_ = page.query_one("#welcome-path", Input)
        field_.focus()
        field_.value = str(folder)
        await settle_page(app)
        await pilot.press("enter")
        await settle_page(app)
        focused = app.focused
        return (focused.id if focused else None), focused in app.screen.focus_chain

    focused, on_screen = in_shell(machine, go)
    assert machine.onboarded == [folder]
    assert (focused, on_screen) == ("claude-connect", True)


def test_after_onboarding_through_plus_the_keyboard_lands_on_start_manager(
    captain: str | None,
    fleet_rows: dict[str, list[FleetAgentStatus]],
    scripted: Callable[[Machine], None],
    tmp_path: Path,
) -> None:
    """Today's ``+`` flow: the Onboard view's path box is hidden once the project opens.

    It kept the keyboard, so keys went into a box nobody could see and the footer
    offered nothing (lane F's demo tape needed Tab three times to reach Start
    manager). The shell hands it to the project's Start manager instead; a keyboard
    the user put somewhere visible is left alone (the control).
    """
    scripted(Machine())
    root = tmp_path / "acme-api"

    async def onboarded(pilot: Pilot[None], app: FleetApp, *, where: str) -> tuple[str, str | None]:
        await pilot.press("plus")
        await settle_page(app)
        if where == "sidebar":
            app.sidebar.focus()
        elif where == "welcome":
            # Back to Welcome while onboarding runs, and onto one of its buttons.
            await pilot.press("w")
            await settle_page(app)
            app.query_one("#claude-check", Button).focus()
        else:
            app.query_one("#onboard-path", Input).focus()
        await settle_page(app)
        with store_session() as store:  # what the Onboard view's `init` did
            project = store.onboard_project(ProjectInfo(id=project_id_for(root), root=root))
        app.post_message(ProjectOnboarded(project.id, root))
        await settle_page(app)
        focused = app.focused
        return str(app.content.current), (focused.id if focused is not None else None)

    async def from_box(pilot: Pilot[None], app: FleetApp, page: WelcomeView) -> Any:
        return await onboarded(pilot, app, where="box")

    async def from_sidebar(pilot: Pilot[None], app: FleetApp, page: WelcomeView) -> Any:
        return await onboarded(pilot, app, where="sidebar")

    async def from_welcome(pilot: Pilot[None], app: FleetApp, page: WelcomeView) -> Any:
        return await onboarded(pilot, app, where="welcome")

    current, focused = in_shell(Machine(), from_box)
    assert current == f"project-{project_id_for(root)}"
    assert focused == "start-manager"
    assert in_shell(Machine(), from_sidebar) == (current, "sidebar")
    # A keyboard that had left the Onboard view is not taken, even though the switch
    # to the project hid the button it is on (Textual's own Hide then releases it):
    # its next Enter must not start a manager.
    elsewhere, left = in_shell(Machine(), from_welcome)
    assert elsewhere == current and left in (None, "claude-check")


def test_plus_from_the_sidebar_opens_onboarding_and_w_comes_back(
    captain: str | None,
    fleet_rows: dict[str, list[FleetAgentStatus]],
    scripted: Callable[[Machine], None],
) -> None:
    machine = Machine()
    scripted(machine)

    async def go(pilot: Pilot[None], app: FleetApp, page: WelcomeView) -> list[Any]:
        seen: list[Any] = []
        looks = len(machine.looks)
        frames = len(machine.frames)
        await pilot.press("plus")
        await settle_page(app)
        seen.append(app.content.current)
        app.sidebar.focus()
        await pilot.press("w")
        await settle_page(app)
        seen.append((app.content.current, app.sidebar.selected_key))
        seen.append(machine.looks[looks:])
        seen.append(len(machine.frames) - frames)
        return seen

    current, back, looks, listed = in_shell(machine, go)
    assert (current, back) == ("onboard", ("welcome", None))
    assert looks == [True]  # back on screen, the page looked at the machine again
    assert listed == 1  # …and listed step 1's folders again


def test_plus_and_w_are_the_sidebars_not_the_pages(
    captain: str | None,
    fleet_rows: dict[str, list[FleetAgentStatus]],
    scripted: Callable[[Machine], None],
) -> None:
    """Like ``q`` and ``t``, the two keys are live only while the sidebar has focus.

    A Button consumes no letter, so with one focused the app's binding is what
    would answer: the gate (``SIDEBAR_ACTIONS``) is the only thing between ``+``
    and the Onboard view there. An Input types them as text.
    """
    machine = Machine()
    scripted(machine)

    async def go(pilot: Pilot[None], app: FleetApp, page: WelcomeView) -> list[Any]:
        seen: list[Any] = []
        button = page.query_one("#claude-check", Button)
        button.focus()
        await pilot.press("plus", "w")
        await settle_page(app)
        seen.append((app.focused is button, str(app.content.current)))
        field_ = page.query_one("#welcome-path", Input)
        field_.focus()
        await pilot.press("plus", "w")
        await settle_page(app)
        seen.append((field_.value, str(app.content.current)))
        return seen

    assert in_shell(machine, go) == [(True, "welcome"), ("+w", "welcome")]


def test_what_the_page_adds_shows_in_the_sidebar_and_the_page_stays(
    captain: str | None,
    fleet_rows: dict[str, list[FleetAgentStatus]],
    scripted: Callable[[Machine], None],
    tmp_path: Path,
) -> None:
    folder = tmp_path / "demo-app"
    folder.mkdir()

    machine = Machine(claude=[READY], found=Candidates(items=(here(folder),)))

    def register(path: Path) -> OnboardOutcome:
        with store_session() as store:
            project = store.onboard_project(ProjectInfo(id=project_id_for(path), root=path))
        machine.stored[project.id] = project
        return OnboardOutcome(path=path, project_id=project.id)

    machine.onboard_answer = register
    scripted(machine)

    async def go(pilot: Pilot[None], app: FleetApp, page: WelcomeView) -> tuple[list[str], str]:
        first_candidate(page).press()
        await settle_page(app)
        listed = [p.id for p in app.snapshot.projects] if app.snapshot is not None else []
        return listed, str(app.content.current)

    listed, current = in_shell(machine, go)
    assert listed == [project_id_for(folder)]  # refreshed now, not at the next tick
    assert current == "welcome"  # unlike `+`, the page does not navigate away


def doctor_lines(app: FleetApp) -> list[str]:
    """The ⚠/✗ lines the sidebar's Doctor section shows under its counts."""
    section = app.query_one(DoctorSection)
    return [shown(line) for line in section.query(".doctor-line").results(Static) if line.display]


def test_connect_brings_the_sidebars_doctor_section_up_to_date(
    captain: str | None,
    fleet_rows: dict[str, list[FleetAgentStatus]],
    scripted: Callable[[Machine], None],
    tmp_path: Path,
) -> None:
    """Connect is the doctor's own fix, run from Welcome, and the shell's report follows it.

    It never told the shell, so the sidebar kept "⚠ claude-code: … hooks are missing"
    under a step 2 that said connected, until ``r`` or a selection ran the checks again
    (review of #257). A fix in the Doctor view has always refreshed the section.
    """
    machine, _ = _ready_machine(tmp_path, claude=[UNHOOKED])
    scripted(machine)
    runs: list[bool] = []

    def doctor() -> list[DoctorCheck]:
        connected = machine.connects > 0
        runs.append(connected)
        if connected:
            return [DoctorCheck(name="claude-code", status=CheckStatus.ok, detail="connected")]
        return [
            DoctorCheck(
                name="claude-code",
                status=CheckStatus.warn,
                detail="Claude Code hooks are missing",
                fix="aisquare agents connect claude-code",
            )
        ]

    async def go(pilot: Pilot[None], app: FleetApp, page: WelcomeView) -> list[list[str]]:
        before = doctor_lines(app)
        await press(pilot, page, "claude-connect")
        return [before, doctor_lines(app)]

    before, after = in_shell(machine, go, doctor=doctor)
    assert before == ["⚠ claude-code: Claude Code hooks are missing"], "control: warned first"
    assert machine.connects == 1 and runs == [False, True], runs
    assert after == [], "the section dropped the warning Connect answered"


def test_step_one_reads_the_shells_frame_not_a_second_store_open(
    captain: str | None,
    fleet_rows: dict[str, list[FleetAgentStatus]],
    scripted: Callable[[Machine], None],
    tmp_path: Path,
) -> None:
    with store_session() as store:
        store.onboard_project(ProjectInfo(id="prj_seeded", root=tmp_path / "seeded"))
    machine = Machine()
    scripted(machine)

    async def go(pilot: Pilot[None], app: FleetApp, page: WelcomeView) -> None:
        return None

    in_shell(machine, go)
    assert [[p.id for p in frame] if frame is not None else None for frame in machine.frames] == [
        ["prj_seeded"]
    ]


def test_a_shell_whose_store_will_not_open_still_shows_the_page(
    captain: str | None,
    fleet_rows: dict[str, list[FleetAgentStatus]],
    scripted: Callable[[Machine], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def locked() -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(app_mod, "store_session", locked)
    naps: list[float] = []

    def nap(seconds: float) -> None:
        naps.append(seconds)
        time.sleep(seconds)

    monkeypatch.setattr(welcome, "_nap", nap)
    machine = Machine(claude=[READY], found=sqlite3.OperationalError("database is locked"))
    scripted(machine)

    async def go(pilot: Pilot[None], app: FleetApp, page: WelcomeView) -> tuple[str, str, bool]:
        failed = app.store_error is not None and app.snapshot is None
        return card(page, "project-status"), card(page, "claude-status"), failed

    project_text, claude_text, failed = in_shell(machine, go)
    assert failed  # the shell could not read its store, so it has no frame
    assert machine.frames == [None]  # the page asked the store itself…
    assert len(naps) < welcome.FRAME_WAITS  # …as soon as the shell said so, not after 5 s
    assert "database is locked" in project_text and "✓ Claude Code" in claude_text


def test_an_agent_stopped_elsewhere_is_not_brought_back(
    captain: str | None,
    scripted: Callable[[Machine], None],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The frame read after a start is the whole answer, so a stop elsewhere shows.

    ``fleet stop`` (the CLI, or the agent view's Stop) kills the window, and the row
    leaves the listing altogether. Kept from what the start said, the manager stayed
    "started", Start manager stayed hidden, and Start the coders would have started
    two coders under no manager.
    """
    machine, project = _ready_machine(tmp_path)
    with store_session() as store:
        store.onboard_project(ProjectInfo(id=project.id, root=project.root))

    def listing(p: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        return machine.statuses(p)

    monkeypatch.setattr(fleet_service, "list_agents", listing)
    scripted(machine)

    async def go(pilot: Pilot[None], app: FleetApp, page: WelcomeView) -> list[Any]:
        await press(pilot, page, "fleet-manager")
        seen: list[Any] = [visible(page, "fleet-coders")]  # the manager is live
        machine.live.clear()  # stopped elsewhere: window gone, row off the listing
        app.refresh_data()
        page.paint()  # what the page's refresh tick does
        seen += [visible(page, "fleet-manager"), visible(page, "fleet-coders")]
        seen.append(card(page, "fleet-status"))
        return seen

    coders_before, start_after, coders_after, status = in_shell(machine, go)
    assert coders_before  # control: a live manager, by the frame, offers the coders
    assert start_after and not coders_after
    assert "manager — started" not in status


def test_a_clock_that_steps_back_does_not_keep_a_stopped_agent_started(
    captain: str | None,
    scripted: Callable[[Machine], None],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Frames and starts were ordered by wall-clock time. After the clock stepped back
    (DST ending, an NTP correction), every frame read older than the start for up to an
    hour, and a manager stopped elsewhere stayed "started" (review of #257)."""
    machine, project = _ready_machine(tmp_path)
    with store_session() as store:
        store.onboard_project(ProjectInfo(id=project.id, root=project.root))

    def listing(p: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        return machine.statuses(p)

    monkeypatch.setattr(fleet_service, "list_agents", listing)
    scripted(machine)

    async def go(pilot: Pilot[None], app: FleetApp, page: WelcomeView) -> tuple[bool, str]:
        await press(pilot, page, "fleet-manager")
        machine.live.clear()  # stopped elsewhere
        app.refresh_data()
        assert app.snapshot is not None
        an_hour_back = app.snapshot.taken_at - timedelta(hours=1)
        app.snapshot = dataclasses.replace(app.snapshot, taken_at=an_hour_back)
        page.paint()
        return visible(page, "fleet-manager"), card(page, "fleet-status")

    start_again, status = in_shell(machine, go)
    assert start_again, "the frame read after the start answers, whatever the clock says"
    assert "manager — started" not in status


def listed_by(machine: Machine, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shell lists ``project``, and its frame reads the agents ``machine`` holds."""
    with store_session() as store:
        store.onboard_project(ProjectInfo(id=project.id, root=project.root))

    def listing(p: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        return machine.statuses(p)

    monkeypatch.setattr(fleet_service, "list_agents", listing)


def test_coders_whose_windows_are_gone_are_not_counted_and_are_restarted(
    captain: str | None,
    scripted: Callable[[Machine], None],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Step 3 counted every row that had not ended as running (review of #257). With the
    coders' windows gone the sidebar showed ✗, while step 3 kept "Your fleet is up." and hid
    Start the coders, so nothing replaced them. Each lost coder still holds a place under the
    cap, so coders started beside both met it on every press, as after a reboot and the
    manager's Restart (coder-4 refused at the default 4): they are restarted under their own
    labels instead, and the card says so at once, before the shell reads its next frame."""
    machine, project = _ready_machine(tmp_path)
    listed_by(machine, project, monkeypatch)
    scripted(machine)

    async def go(pilot: Pilot[None], app: FleetApp, page: WelcomeView) -> list[Any]:
        await press(pilot, page, "fleet-manager")
        await press(pilot, page, "fleet-coders")
        seen: list[Any] = [card(page, "fleet-status")]
        machine.states.update({"coder-1": "lost", "coder-2": "lost"})  # their windows gone
        app.refresh_data()
        page.paint()  # what the page's refresh tick does
        seen += [card(page, "fleet-status"), visible(page, "fleet-coders")]
        await press(pilot, page, "fleet-coders")
        seen += [card(page, "fleet-status"), visible(page, "fleet-coders")]
        return seen

    up, lost, offered, restarted, offered_after = in_shell(machine, go)
    assert FLEET_UP in up and "✓ coder-1 — started" in up, "control: three running agents"
    assert "✗ coder-1 — lost (pane gone)" in lost and FLEET_UP not in lost, lost
    assert offered, "Start the coders is offered again"
    assert machine.restarted == ["coder-1", "coder-2"]
    assert sorted(agent.label for agent in machine.live) == ["coder-1", "coder-2", "manager"]
    assert "✓ coder-1 — started" in restarted and "✓ coder-2 — started" in restarted, restarted
    assert "lost" not in restarted and FLEET_UP in restarted, restarted
    assert not offered_after


@pytest.mark.parametrize("state", ["unknown", "lost"])
def test_a_fleet_whose_server_is_gone_is_not_called_up(
    captain: str | None,
    scripted: Callable[[Machine], None],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: FleetAgentState,
) -> None:
    """asq opened again after a reboot or a kill-server: every row reads ``unknown`` (tmux
    cannot be asked), or ``lost`` once a new server runs. Step 3 said all three were
    running and the fleet was up, with both start buttons hidden (review of #257). It
    now says what the frame says, and Open leads to the manager's Restart."""
    machine, project = _ready_machine(tmp_path)
    labels = {"manager": "manager", "coder-1": "coder", "coder-2": "coder"}
    machine.live = [_agent(project, label, role) for label, role in labels.items()]
    listed_by(machine, project, monkeypatch)
    scripted(machine)

    async def go(pilot: Pilot[None], app: FleetApp, page: WelcomeView) -> list[Any]:
        buttons = [visible(page, b) for b in ("fleet-manager", "fleet-open", "fleet-coders")]
        next_step = page.next_button()
        return [card(page, "fleet-title"), card(page, "fleet-status"), buttons, next_step]

    title, status, _, _ = in_shell(machine, go)
    assert "✓" in title and FLEET_UP in status, "control: three running agents are a fleet"
    machine.states = dict.fromkeys(labels, state)
    title, status, buttons, next_step = in_shell(machine, go)
    line = {
        "unknown": "· manager — state unknown (tmux unavailable)",
        "lost": "✗ manager — lost (pane gone)",
    }[state]
    assert line in status and "Open the manager to restart it." in status, status
    assert FLEET_UP not in status and "running" not in status and "✓" not in title, status
    assert buttons == [False, True, False], "Open the manager, not Start manager"
    assert next_step is not None and next_step.id == "fleet-open"


def test_step_one_follows_the_frame_when_a_folder_is_listed_elsewhere(
    captain: str | None,
    fleet_rows: dict[str, list[FleetAgentStatus]],
    scripted: Callable[[Machine], None],
    tmp_path: Path,
) -> None:
    root = tmp_path / "app"
    root.mkdir()
    pid = project_id_for(root)

    def found(listed: list[ProjectInfo] | None) -> Candidates:
        project = next((p for p in listed or [] if p.id == pid), None)
        return Candidates(items=(here(root, project),))

    machine = Machine(claude=[READY], found=found)
    scripted(machine)

    async def go(pilot: Pilot[None], app: FleetApp, page: WelcomeView) -> list[str | None]:
        seen = [page.project.id if page.project else None]
        with store_session() as store:  # `aisquare init` in another terminal
            store.onboard_project(ProjectInfo(id=pid, root=root))
        app.refresh_data()
        page._tick()  # the page's refresh tick
        await settle_page(app)
        seen.append(page.project.id if page.project else None)
        return seen

    assert in_shell(machine, go) == [None, pid]


def test_the_page_hosted_alone_asks_the_store_itself() -> None:
    machine = Machine()

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> None:
        return None

    hosted(machine, go)
    assert machine.frames == [None]  # no shell, no frame: the service reads the store
