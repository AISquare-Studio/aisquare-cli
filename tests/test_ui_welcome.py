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
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import pytest
from textual.app import App, ComposeResult
from textual.message import Message
from textual.pilot import Pilot
from textual.widgets import Button, Input, Static

from aisquare.cli.ui import app as app_mod
from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.sidebar import AccountsSelected, AgentSelected
from aisquare.cli.ui.views import welcome
from aisquare.cli.ui.views.onboard import ProjectOnboarded
from aisquare.cli.ui.views.welcome import FLEET_UP, Seams, WelcomeView
from aisquare.core import claude_accounts as accounts_core
from aisquare.core import tmux as tmux_core
from aisquare.core.store import store_session
from aisquare.core.tmux import Completed
from aisquare.core.workspace import project_id_for
from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo, SetupReport
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
from tests.ui_workers import settle_page

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
    tmux: TmuxState = field(default_factory=lambda: TmuxState(found=True, version=(3, 4)))
    gh: bool = True
    found: Candidates | Exception = field(default_factory=lambda: Candidates(items=()))
    stored: dict[str, ProjectInfo] = field(default_factory=dict)
    onboard_answer: Callable[[Path], OnboardOutcome] | None = None
    connect_answer: FixResult | None = None
    refuse: dict[str, str] = field(default_factory=dict)
    """Labels ``start`` refuses, with the reason."""
    looks: list[bool] = field(default_factory=list)
    frames: list[list[ProjectInfo] | None] = field(default_factory=list)
    onboarded: list[Path] = field(default_factory=list)
    connects: int = 0
    starts: list[tuple[str, bool, int]] = field(default_factory=list)
    live: list[FleetAgent] = field(default_factory=list)

    def seams(self, platform: str = "linux") -> Seams:
        def claude(sign_in: bool) -> ClaudeState:
            self.looks.append(sign_in)
            answer = self.claude[0] if len(self.claude) == 1 else self.claude.pop(0)
            return answer if sign_in else dataclasses.replace(answer, signed_in=None)

        def candidates(listed: list[ProjectInfo] | None) -> Candidates:
            self.frames.append(listed)
            if isinstance(self.found, Exception):
                raise self.found
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
                live=lambda p: list(self.live),
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

    def spawn(self, project: ProjectInfo, role: str, **kwargs: Any) -> fleet_service.SpawnReceipt:
        assert kwargs.get("prompt") is None, "Welcome typed into an agent"
        label = kwargs.get("label") or role
        if label in self.refuse:
            raise fleet_service.FleetError(self.refuse[label])
        agent = _agent(project, label, role)
        self.live.append(agent)
        return fleet_service.SpawnReceipt(agent=agent, asked_label=None, tmux_session="asq-demo")

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
    machine: Machine, fn: Callable[[Pilot[None], FleetApp, WelcomeView], Awaitable[T]]
) -> T:
    """Run ``fn`` against the real shell, its Welcome page scripted by ``machine``."""

    async def run() -> T:
        app = FleetApp(refresh_seconds=3600, doctor=lambda: [], accounts=None)
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

    async def onboarded(
        pilot: Pilot[None], app: FleetApp, *, from_sidebar: bool
    ) -> tuple[str, str | None]:
        await pilot.press("plus")
        await settle_page(app)
        if from_sidebar:
            app.sidebar.focus()
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
        return await onboarded(pilot, app, from_sidebar=False)

    async def from_sidebar(pilot: Pilot[None], app: FleetApp, page: WelcomeView) -> Any:
        return await onboarded(pilot, app, from_sidebar=True)

    current, focused = in_shell(Machine(), from_box)
    assert current == f"project-{project_id_for(root)}"
    assert focused == "start-manager"
    assert in_shell(Machine(), from_sidebar) == (current, "sidebar")


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
        await pilot.press("plus")
        await settle_page(app)
        seen.append(app.content.current)
        app.sidebar.focus()
        await pilot.press("w")
        await settle_page(app)
        seen.append((app.content.current, app.sidebar.selected_key))
        seen.append(machine.looks[looks:])
        return seen

    current, back, looks = in_shell(machine, go)
    assert (current, back) == ("onboard", ("welcome", None))
    assert looks == [True]  # back on screen, the page looked at the machine again


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


def test_the_page_hosted_alone_asks_the_store_itself() -> None:
    machine = Machine()

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> None:
        return None

    hosted(machine, go)
    assert machine.frames == [None]  # no shell, no frame: the service reads the store
