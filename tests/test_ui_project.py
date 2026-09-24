"""The Project view, driven headless: its tabs, the manager button, the board, both forms.

Everything the view reaches for outside the store is monkeypatched at the
module the view imports — ``services.fleet`` for the manager, the two
explainability services for the tracing tab, ``ProjectView(doctor=…)`` for the
Doctor tab — so no test starts tmux, dials a gateway or depends on ``claude``.
What each test asserts is the artefact the claim is about: the spawn call the
button made, the bytes ``config.toml`` holds after Save, the rows the board
table shows for THIS project and not another.

"No test reaches tmux" is ENFORCED here, not promised. The Manager tab mounts a
live ``TerminalPane``, so as soon as a manager exists the view builds a real
``TmuxServer`` and the render loop starts capturing and resizing — and a
``FleetAgent`` with no ``tmux_socket`` names ``asq``, the developer's own fleet
socket, whose pane ``%7`` is somebody's live agent. Two halves, both in
:func:`no_real_tmux`: the view's ``TmuxServer`` is a scripted one that answers
without a subprocess, and the module-level runner every ``TmuxServer`` picks up
at construction records anything that still escapes. The recorder is checked in
teardown because ``TerminalPane.refresh_frame`` swallows exceptions by design —
an assertion raised inside a frame would be eaten by the widget it is testing.

Textual is imported at module level: the ``dev`` extra pins it, and every test
here is about widgets.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import tomllib
from collections.abc import Callable, Coroutine, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import pytest
from textual.app import App, ComposeResult
from textual.geometry import Region
from textual.notifications import Notification, SeverityLevel
from textual.pilot import Pilot
from textual.widget import Widget
from textual.widgets import Button, Checkbox, DataTable, Input, OptionList, Select, Static
from textual.widgets._toast import Toast
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.ui.board import BoardPanel
from aisquare.cli.ui.terminal import TerminalPane
from aisquare.cli.ui.views import project as project_view_module
from aisquare.cli.ui.views.doctor import DoctorView
from aisquare.cli.ui.views.explainability import ExplainabilityView
from aisquare.cli.ui.views.project import ManagerTab, ProjectView
from aisquare.cli.ui.views.settings import SettingsView
from aisquare.core import paths
from aisquare.core import tmux as tmux_core
from aisquare.core.config import ExplainabilityTarget, load_config, save_config
from aisquare.core.store import store_session
from aisquare.core.tmux import Capture, Completed, PaneFacts, TmuxServer
from aisquare.models import (
    CheckStatus,
    DoctorCheck,
    FleetAgent,
    FleetAgentStatus,
    ProjectInfo,
    TeamEvent,
    TeamSession,
)
from aisquare.services import explainability as explainability_service
from aisquare.services import explainability_ops as ops
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from tests.ui_workers import settle_workers

T = TypeVar("T")

PRIVATE_SOCKET = f"asq-test-{os.getpid()}-ui-project"
"""The socket every agent in this file lives on. NOT ``asq``: that is the
default (``models.py``) and the developer's real fleet — see the module
docstring."""


def _stub_checks() -> list[DoctorCheck]:
    return [DoctorCheck(name="stub", status=CheckStatus.ok, detail="no doctor was asked for")]


class Host(App[None]):
    """A bare app around one ``ProjectView`` that records every notification."""

    def __init__(
        self,
        project: ProjectInfo,
        *,
        doctor: Callable[[Path], list[DoctorCheck]] | None = None,
        refresh_seconds: float = 60.0,
    ) -> None:
        super().__init__()
        self._project = project
        # Never the real ``diagnostics.doctor``: it probes git, tmux and claude.
        self._doctor = doctor if doctor is not None else (lambda root: _stub_checks())
        self._refresh_seconds = refresh_seconds
        self.notices: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        yield ProjectView(
            self._project,
            id="project",
            refresh_seconds=self._refresh_seconds,
            doctor=self._doctor,
        )

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        self.notices.append((message, severity))
        super().notify(message, title=title, severity=severity, timeout=timeout, markup=markup)


def drive(
    project: ProjectInfo,
    scenario: Callable[[Pilot[None], Host], Coroutine[Any, Any, T]],
    *,
    doctor: Callable[[Path], list[DoctorCheck]] | None = None,
    refresh_seconds: float = 60.0,
) -> T:
    """Run ``scenario`` against a mounted Project view and return what it observed."""

    async def run() -> T:
        host = Host(project, doctor=doctor, refresh_seconds=refresh_seconds)
        async with host.run_test(size=(120, 50)) as pilot:
            await pilot.pause()
            return await scenario(pilot, host)

    return asyncio.run(run())


async def settle(pilot: Pilot[None]) -> None:
    """Let every worker reach a terminal state, whatever that state is, and its
    state-change handler run.

    An errored worker is the designed outcome here: ``ManagerTab`` runs the
    spawn with ``exit_on_error=False`` and reports the failure from
    ``on_worker_state_changed``, so a test that scripts ``FleetUnavailable``
    reads the error notice off the page. ``settle_workers`` waits without
    raising for it.

    It waits for the workers that EXIST, so the pause comes first: ``Button.press()``
    only posts ``Pressed``, and the handler that starts a worker runs once the
    message has bubbled to the view. Awaited straight after a press, this found
    no worker yet and returned while the press's own status refresh was still
    running — a race the next read lost wherever the refresh's file reads are
    slow: on windows-latest the Explainability tab read the status from before
    its key was attached ("no key of its own"). Measured here with that worker
    held 0.3 s: both tests that attach a key then read the row failed the same way.
    """
    await pilot.pause()
    await settle_workers(pilot.app)
    await pilot.pause()


def shown(widget: Widget) -> str:
    """The text a widget renders — the artefact, not the argument."""
    return str(widget.render())


def fake_agent(project: ProjectInfo, *, pane_id: str = "%7", label: str = "manager") -> FleetAgent:
    return FleetAgent(
        id="agt_01testmanager",
        project_id=project.id,
        label=label,
        role="manager" if label == "manager" else "coder",
        pane_id=pane_id,
        cwd=project.root,
        created_at=datetime.now(tz=UTC),
        tmux_socket=PRIVATE_SOCKET,  # never the real fleet's default
    )


class ScriptedServer(TmuxServer):
    """A ``TmuxServer`` that answers a ``TerminalPane`` without running tmux.

    Subclassed rather than duck-typed so a renamed method on ``TmuxServer``
    breaks this at type-check time (the same reason ``test_doctor_fleet`` does
    it), and every question is recorded so a test can prove the pane really
    attached instead of passing because nothing happened.
    """

    def __init__(self, socket: str) -> None:
        super().__init__(socket, conf=Path("/nonexistent/fleet-tmux.conf"))
        self.captures: list[tuple[str, int, int | None]] = []
        self.resizes: list[tuple[str, int, int]] = []

    def version(self) -> tuple[int, int] | None:
        return (3, 7)

    def capture(
        self, pane_id: str, *, scrollback: int = 0, height: int | None = None, flags: bool = False
    ) -> Capture:
        self.captures.append((pane_id, scrollback, height))
        facts = PaneFacts(
            pane_id=pane_id,
            width=80,
            height=24,
            cursor_x=0,
            cursor_y=0,
            cursor_visible=True,
            alternate_on=False,
            history_size=0,
            dead=False,
            dead_status=None,
            in_mode=False,
            current_command="claude",
            title="",
        )
        return Capture(lines=[f"{pane_id} scripted"], facts=facts, scrollback=0)

    def resize(self, pane_id: str, width: int, height: int) -> None:
        self.resizes.append((pane_id, width, height))


# --- fixtures ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def project() -> ProjectInfo:
    return team_service.activate()


@pytest.fixture(autouse=True)
def no_fleet(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fleet with no manager and no tmux: the view must never reach the real service."""
    monkeypatch.setattr(fleet_service, "manager_of", lambda project: None)
    monkeypatch.setattr(
        fleet_service, "status_of", lambda agent: FleetAgentStatus(agent=agent, state="waiting")
    )


class NoTmux:
    """What the view's ``TmuxServer`` is replaced by, plus the escape recorder."""

    def __init__(self) -> None:
        self.sockets: list[str] = []
        """Every socket the view asked for a server on, in order."""
        self.servers: list[ScriptedServer] = []
        self.ran: list[tuple[str, ...]] = []
        """tmux argv that escaped to the real runner — must stay empty."""

    def server_for(self, socket: str) -> TmuxServer:
        assert socket.startswith("asq-test-"), (
            f"a UI test built a TmuxServer on {socket!r} — the fleet's real socket "
            "is not a test fixture"
        )
        self.sockets.append(socket)
        server = ScriptedServer(socket)
        self.servers.append(server)
        return server


@pytest.fixture(autouse=True)
def no_real_tmux(monkeypatch: pytest.MonkeyPatch) -> Iterator[NoTmux]:
    """No test here may run a tmux command; the Manager tab's pane is scripted.

    See the module docstring for why this is a fixture and not a promise. The
    teardown assertion is the guard: an ``AssertionError`` raised inside a frame
    would be swallowed by ``TerminalPane.refresh_frame``, so what escapes is
    recorded and read afterwards, where nothing can eat it.
    """
    guard = NoTmux()

    def record(argv: Sequence[str], stdin: bytes | None) -> Completed:
        guard.ran.append(tuple(argv))
        return Completed(1, "", "no server running (a UI test must not run tmux)\n")

    monkeypatch.setattr(tmux_core, "_tmux", record)
    monkeypatch.setattr(project_view_module, "TmuxServer", guard.server_for)
    yield guard
    assert not guard.ran, f"a UI test ran tmux: {guard.ran[:3]}"


@pytest.fixture(autouse=True)
def quiet_explainability(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """No probe dials anything; the client lane answers from a fake; calls are counted."""
    calls = {"shipping_state": 0}

    def shipping_state(target_name: str | None = None) -> explainability_service.ShippingState:
        calls["shipping_state"] += 1
        return explainability_service.ShippingState(
            configured=False,
            gateway_url="",
            has_key=False,
            sdk_installed=False,
            queued=0,
            sent=0,
            dead=0,
            reason="off — nothing is captured (fake)",
        )

    monkeypatch.setattr(explainability_service, "shipping_state", shipping_state)
    monkeypatch.setattr(
        ops,
        "probe_proxy",
        lambda url, timeout=1.5: explainability_service.ProxyProbe(False, "not dialled in tests"),
    )
    return calls


def _config_toml() -> dict[str, Any]:
    return tomllib.loads(paths.config_path().read_text(encoding="utf-8"))


# --- the tabs -----------------------------------------------------------------------------


def test_project_view_has_the_five_tabs_with_their_widgets(project: ProjectInfo) -> None:
    async def scenario(pilot: Pilot[None], host: Host) -> tuple[int, list[str], str]:
        view = host.query_one(ProjectView)
        for pane_id, widget_type in (
            ("#tab-manager", ManagerTab),
            ("#tab-board", BoardPanel),
            ("#tab-doctor", DoctorView),
            ("#tab-explainability", ExplainabilityView),
            ("#tab-settings", SettingsView),
        ):
            assert view.query_one(pane_id).query_one(widget_type)
        ids = [pane.id or "" for pane in view.query("TabPane")]
        return view.tab_count, ids, view.active

    count, ids, active = drive(project, scenario)
    assert count == 5
    assert ids == list(ProjectView.TAB_IDS)
    assert active == "tab-manager"  # the manager first: that is where the goal goes


# --- the Manager tab ----------------------------------------------------------------------


def test_start_manager_button_spawns_once_and_attaches_the_pane(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet: dict[str, FleetAgent | None] = {"manager": None}
    spawns: list[tuple[str, str]] = []

    def spawn(target: ProjectInfo, role: str, **_: object) -> fleet_service.SpawnReceipt:
        spawns.append((target.id, role))
        fleet["manager"] = fake_agent(target)
        return fleet_service.SpawnReceipt(
            agent=fake_agent(target), asked_label=None, tmux_session="asq-amber-otter"
        )

    monkeypatch.setattr(fleet_service, "spawn", spawn)
    monkeypatch.setattr(fleet_service, "manager_of", lambda target: fleet["manager"])

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[bool, bool, bool, bool, str | None]:
        button = host.query_one("#start-manager", Button)
        pane = host.query_one("#manager-pane", TerminalPane)
        before = (button.display, pane.display)
        await pilot.click("#start-manager")
        await settle(pilot)
        return before[0], before[1], button.display, pane.display, pane.pane_id

    button_before, pane_before, button_after, pane_after, pane_id = drive(project, scenario)
    assert (button_before, pane_before) == (True, False)  # no manager: the button, no pane
    assert spawns == [(project.id, "manager")]  # exactly one spawn, of the manager role
    assert (button_after, pane_after) == (False, True)  # now the pane, no button
    assert pane_id == "%7"  # attached to the pane the receipt named


def test_the_manager_pane_is_scripted_and_never_addresses_the_real_fleet(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, no_real_tmux: NoTmux
) -> None:
    """The pane attaches for real — to a private socket, through no subprocess.

    Attaching runs ``_sync_size`` synchronously, which is a ``resize-window``
    against whatever pane ``%7`` is on the socket named. On ``asq`` that is a
    live agent in the developer's own fleet; the default ``FleetAgent`` names
    exactly that socket, which is why every agent here overrides it.
    """
    monkeypatch.setattr(
        fleet_service, "manager_of", lambda target: fake_agent(target, pane_id="%7")
    )

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[str | None, str, tuple[int, int]]:
        pane = host.query_one("#manager-pane", TerminalPane)
        await pilot.pause(0.25)  # past the 100 ms resize debounce
        width, height = pane.content_size
        rows = pane.render_lines(Region(0, 0, width, height))
        return pane.pane_id, rows[0].text.rstrip(), (width, height)

    pane_id, first_row, size = drive(project, scenario)
    assert pane_id == "%7"  # the pane really attached: this is not a vacuous pass
    assert first_row == "%7 scripted"  # …and the frame came from the scripted server
    assert no_real_tmux.sockets == [PRIVATE_SOCKET]  # never "asq"
    assert PRIVATE_SOCKET != "asq" == FleetAgent.model_fields["tmux_socket"].default
    server = no_real_tmux.servers[0]
    assert ("%7", *size) in server.resizes  # the resize the finding is about, contained
    assert server.captures  # and the render loop ran against the fake
    # The recorder is checked in this fixture's teardown; assert it here too, so
    # the failure names this test rather than an error on the way out.
    assert no_real_tmux.ran == []


def test_no_button_and_no_spawn_when_a_manager_already_runs(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative half: a present manager means the pane, and the button never fires."""
    spawns: list[str] = []
    monkeypatch.setattr(
        fleet_service, "manager_of", lambda target: fake_agent(target, pane_id="%3")
    )

    def never(*args: object, **kwargs: object) -> fleet_service.SpawnReceipt:
        spawns.append("spawned")
        pytest.fail("spawn was called although a manager already runs")

    monkeypatch.setattr(fleet_service, "spawn", never)

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[bool, str | None, str]:
        button = host.query_one("#start-manager", Button)
        pane = host.query_one("#manager-pane", TerminalPane)
        await pilot.press("enter")  # whatever has focus, the hidden button must not be it
        await settle(pilot)
        header = str(host.query_one("#manager-header").render())
        return button.display, pane.pane_id, header

    displayed, pane_id, header = drive(project, scenario)
    assert displayed is False
    assert pane_id == "%3"
    assert spawns == []
    assert "waiting" in header  # the state the (patched) service reported


def test_start_manager_reports_a_fleet_error_and_stays_available(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawns: list[str] = []

    def refuse(target: ProjectInfo, role: str, **_: object) -> fleet_service.SpawnReceipt:
        spawns.append(role)
        raise fleet_service.FleetUnavailable("tmux is not installed (or not on PATH)")

    monkeypatch.setattr(fleet_service, "spawn", refuse)

    async def scenario(
        pilot: Pilot[None], host: Host
    ) -> tuple[bool, bool, bool, list[tuple[str, str]]]:
        button = host.query_one("#start-manager", Button)
        await pilot.click("#start-manager")
        await settle(pilot)
        pane_shown = host.query_one("#manager-pane").display
        return button.disabled, button.display, pane_shown, host.notices

    disabled, displayed, pane_shown, notices = drive(project, scenario)
    assert spawns == ["manager"]
    assert disabled is False  # the button is usable again after the refusal
    assert displayed is True and pane_shown is False  # still no manager
    errors = [m for m, severity in notices if severity == "error"]
    assert errors and "tmux is not installed" in errors[0]  # the reason, verbatim


def test_refresh_status_pushes_the_managers_state(project: ProjectInfo) -> None:
    manager = fake_agent(project, pane_id="%11")
    coder = fake_agent(project, pane_id="%12", label="coder-auth")

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[str, str | None, bool, bool]:
        view = host.query_one(ProjectView)
        view.refresh_status(
            [
                FleetAgentStatus(agent=coder, state="working"),
                FleetAgentStatus(agent=manager, state="attention", tmux_session="asq-ruby-fox"),
            ]
        )
        await pilot.pause()
        pane = host.query_one("#manager-pane", TerminalPane)
        header = str(host.query_one("#manager-header").render())
        attached = pane.pane_id
        view.refresh_status([FleetAgentStatus(agent=coder, state="working")])  # coder only
        await pilot.pause()
        return header, attached, host.query_one("#start-manager", Button).display, pane.display

    header, attached, button_back, pane_after = drive(project, scenario)
    assert "NEEDS YOU" in header and "asq-ruby-fox" in header
    assert attached == "%11"  # the manager's pane, not the coder's
    assert button_back is True and pane_after is False  # no manager in the snapshot → button


def test_the_manager_tab_says_the_manager_exited_and_how_to_bring_it_back(
    project: ProjectInfo,
) -> None:
    """#138: an exited manager's row stays listed while its window stands (its last
    screen is readable behind it); the tab must not call that "no manager yet"."""
    now = datetime.now(tz=UTC)
    gone = fake_agent(project, pane_id="%31").model_copy(
        update={"ended_at": now, "exit_status": 130}
    )
    coder = fake_agent(project, pane_id="%32", label="coder-auth")

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[str, bool, bool, str]:
        view = host.query_one(ProjectView)
        view.refresh_status(
            [
                FleetAgentStatus(agent=gone, state="exited", detail="exit 130"),
                FleetAgentStatus(agent=coder, state="working"),
            ]
        )
        await pilot.pause()
        header = str(host.query_one("#manager-header").render())
        button = host.query_one("#start-manager", Button).display
        pane = host.query_one("#manager-pane", TerminalPane).display
        view.refresh_status([FleetAgentStatus(agent=coder, state="working")])  # no such row
        await pilot.pause()
        return header, button, pane, str(host.query_one("#manager-header").render())

    header, button, pane, without = drive(project, scenario)
    assert "manager exited (130)" in header and "Restart on its sidebar row" in header
    assert "aisquare fleet restart manager" in header and "no manager yet" not in header
    assert button is True and pane is False  # the way to a NEW session is right there
    assert "has no manager yet" in without and "exited" not in without  # the control


def test_refresh_routes_a_snapshot_and_a_bare_refresh_only_repaints(project: ProjectInfo) -> None:
    """The plan spells the push ``refresh(snapshot)``; Textual's own ``refresh()`` must survive."""
    manager = fake_agent(project, pane_id="%21")

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[str, str | None, str | None, str]:
        view = host.query_one(ProjectView)
        view.refresh([FleetAgentStatus(agent=manager, state="working")])  # the plan's spelling
        await pilot.pause()
        pane = host.query_one("#manager-pane", TerminalPane)
        header, attached = str(host.query_one("#manager-header").render()), pane.pane_id
        view.refresh()  # the framework's spelling: a repaint, no snapshot
        view.refresh(layout=True)
        await pilot.pause()
        # A view handed a snapshot BEFORE it mounted applies it once the tabs exist.
        early = ProjectView(project, id="early", refresh_seconds=60.0)
        early.refresh_status([FleetAgentStatus(agent=manager, state="attention")])
        await host.mount(early)
        await pilot.pause()
        early_header = str(early.query_one("#manager-header").render())
        return header, attached, pane.pane_id, early_header

    header, attached, after_bare, early_header = drive(project, scenario)
    assert "working" in header and attached == "%21"  # routed to the manager tab
    assert after_bare == "%21"  # a bare refresh() changed nothing
    assert "NEEDS YOU" in early_header  # the pre-mount push landed at mount


# --- the Board tab --------------------------------------------------------------------------


def test_board_tab_shows_this_projects_tasks_and_not_anothers(
    project: ProjectInfo, runner: CliRunner, tmp_path: Path
) -> None:
    runner.invoke(app, ["task", "add", "build the API"])
    runner.invoke(app, ["task", "add", "wire the UI", "--detail", "hook Atlas chat to v3"])
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = team_service.activate(other_dir)
    assert other.id != project.id

    async def rows(pilot: Pilot[None], host: Host) -> int:
        return host.query_one("#tasks", DataTable).row_count

    assert drive(project, rows) == 2
    assert drive(other, rows) == 0  # the panel reads the project it was given, not the cwd


def test_a_hidden_board_tab_stops_polling_and_catches_up_when_shown(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kept view must not read the store behind ``display: none`` forever.

    The fleet UI builds one ``ProjectView`` per project ever selected and keeps
    it, so an ungated 2 s timer is N store reads per tick for the life of the
    app, N-1 of them invisible.
    """
    reads: list[float] = []
    real = team_service.board_data

    def counting(*args: Any, **kwargs: Any) -> Any:
        reads.append(1.0)
        return real(*args, **kwargs)

    monkeypatch.setattr(team_service, "board_data", counting)

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[int, int, int, bool]:
        view = host.query_one(ProjectView)
        panel = host.query_one("#board-panel", BoardPanel)
        at_mount = len(reads)
        await pilot.pause(0.4)  # ~8 ticks at 50 ms, every one behind a hidden tab
        hidden = len(reads)
        on_screen_while_hidden = panel.is_on_screen
        view.active = "tab-board"
        await pilot.pause(0.4)
        return at_mount, hidden, len(reads), on_screen_while_hidden

    at_mount, hidden, after_show, on_screen_while_hidden = drive(
        project, scenario, refresh_seconds=0.05
    )
    assert on_screen_while_hidden is False  # the premise: the panel really was hidden
    assert at_mount == 1  # one read at mount primes the first frame
    assert hidden == at_mount  # …and not one more while nobody can see it
    assert after_show >= hidden + 2  # shown: on_show, then the ticks resume


def test_the_board_names_its_accounts_from_a_worker_never_from_the_ui_thread(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #205, fourth round: the sessions block read the slot labels itself — a
    ``context.db`` open with a busy timeout of seconds and a scan of the account
    directories — on the event loop, at every tick of every open board. The panel reads
    them in a thread worker, at most every ``LABELS_TTL``, and paints the map it has."""
    import threading

    from aisquare.core import claude_accounts as accounts_core
    from aisquare.services import claude_accounts as accounts_service

    two, three = accounts_core.create_account(), accounts_core.create_account()
    now = datetime.now(tz=UTC)
    sessions = [
        TeamSession(
            id=f"sess-on-{account.slot}",
            project_id=project.id,
            role="coder",
            started_at=now,
            last_seen_at=now,
            account=str(account.config_dir),
        )
        for account in (two, three)
    ]
    monkeypatch.setattr(team_service, "board_data", lambda **kwargs: (project, sessions, [], []))
    asked: list[str] = []

    def slot_labels(store: Any = None) -> dict[int, str]:
        asked.append(threading.current_thread().name)
        return {two.slot: "work", three.slot: "home"}

    monkeypatch.setattr(accounts_service, "slot_labels", slot_labels)

    async def scenario(pilot: Pilot[None], host: Host) -> str:
        panel = host.query_one("#board-panel", BoardPanel)
        host.query_one(ProjectView).active = "tab-board"
        await settle(pilot)
        panel.refresh_data()  # two more ticks inside the TTL
        panel.refresh_data()
        await settle(pilot)
        return shown(host.query_one("#sessions", Static))

    painted = drive(project, scenario)
    assert asked and threading.main_thread().name not in asked
    assert len(asked) == 1  # ticks inside the TTL paint the map they have
    assert "work" in painted and "home" in painted


def _event(project: ProjectInfo, index: int) -> TeamEvent:
    return TeamEvent(
        seq=index + 1,
        id=f"evt_{index:04d}",
        project_id=project.id,
        kind="note",
        text=f"line {index}",
        created_at=datetime.now(tz=UTC),
    )


def test_the_feed_keeps_its_last_lines_and_forgets_the_rest(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The feed is the one structure here that only grows; it is now bounded.

    Both directions, because a cap that drops everything would pass a
    one-sided check: at ``FEED_LIMIT`` 5 twelve events leave the last five, and
    the SAME twelve under the shipped limit leave all twelve. Select mode is
    replayed too — it walks ``_feed_order`` through ``_events_by_id``, so an
    eviction that forgot one of the two would raise there rather than lose a row.
    """
    events = [_event(project, index) for index in range(12)]

    def board_data(*args: Any, **kwargs: Any) -> Any:
        return (project, [], [], list(events))

    monkeypatch.setattr(team_service, "board_data", board_data)

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[int, list[str], str]:
        panel = host.query_one("#board-panel", BoardPanel)
        feed = host.query_one("#feed", OptionList)
        ids = [
            option.id or ""
            for option in (feed.get_option_at_index(n) for n in range(feed.option_count))
        ]
        panel.action_toggle_select()  # freeze: the snapshot walks both caches
        await pilot.pause()
        return feed.option_count, ids, shown(host.query_one("#feedstatic", Static))

    # The control FIRST, at the shipped limit: twelve events, twelve rows.
    assert BoardPanel.FEED_LIMIT == 2000
    uncapped, all_ids, everything = drive(project, scenario)
    assert (uncapped, len(all_ids)) == (12, 12)  # nothing is dropped under 2000
    assert "line 0" in everything and "line 11" in everything

    monkeypatch.setattr(BoardPanel, "FEED_LIMIT", 5)
    capped, ids, frozen = drive(project, scenario)
    assert capped == 5
    assert ids == [f"evt_{n:04d}" for n in range(7, 12)]  # the LAST five, in order
    assert "line 11" in frozen and "line 6" not in frozen  # …and select mode agrees


# --- the Doctor tab -------------------------------------------------------------------------


def test_the_doctor_tab_runs_this_projects_checks_when_it_is_opened(project: ProjectInfo) -> None:
    """It was a scaffold nothing filled: ``(no checks yet)``, forever, in every project."""
    roots: list[Path] = []

    def doctor(root: Path) -> list[DoctorCheck]:
        roots.append(root)
        return [
            DoctorCheck(
                name="tmux",
                status=CheckStatus.warn,
                detail="tmux 3.7c, no fleet server",
                fix="aisquare fleet spawn manager",
            )
        ]

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[str, Path | None, str, Path | None]:
        view = host.query_one(ProjectView)
        report = host.query_one("#project-doctor", DoctorView)
        before = shown(report.query_one("#doctor-report", Static))
        cwd_before = report.cwd
        view.active = "tab-doctor"
        await settle(pilot)
        return before, cwd_before, shown(report.query_one("#doctor-report", Static)), report.cwd

    before, cwd_before, after, cwd = drive(project, scenario, doctor=doctor)
    assert "(no checks yet)" in before  # the negative: nothing runs before the tab is opened
    assert roots == [project.root]  # this project's root, not the UI process's cwd
    assert "tmux 3.7c, no fleet server" in after and "(no checks yet)" not in after
    assert "aisquare fleet spawn manager" in after  # the fix line, under its check
    # The fixes run in the project, from the moment the tab exists — not only
    # once a report has landed in it.
    assert cwd_before == project.root == cwd


def test_a_crashed_project_doctor_is_a_report_not_a_traceback(project: ProjectInfo) -> None:
    """The control for the wiring above: the tab survives a doctor that raises."""

    def doctor(root: Path) -> list[DoctorCheck]:
        raise RuntimeError("git rev-parse exploded")

    async def scenario(pilot: Pilot[None], host: Host) -> str:
        host.query_one(ProjectView).active = "tab-doctor"
        await settle(pilot)
        return shown(host.query_one("#doctor-report", Static))

    report = drive(project, scenario, doctor=doctor)
    assert "the checks crashed: RuntimeError: git rev-parse exploded" in report
    assert "aisquare doctor" in report  # …and says how to see the traceback


# --- the Settings tab -----------------------------------------------------------------------


def test_settings_saves_a_changed_permission_mode_to_config_toml(project: ProjectInfo) -> None:
    assert not paths.config_path().exists()  # nothing written before the user saves

    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, str]]:
        host.query_one(ProjectView).active = "tab-settings"
        await pilot.pause()
        host.query_one("#perm-coder", Select).value = "plan"
        host.query_one("#worktree-dir", Input).value = ".fleet-trees"
        host.query_one("#save-settings", Button).press()
        await pilot.pause()
        return host.notices

    notices = drive(project, scenario)
    assert any(m.startswith("✓ fleet settings saved") for m, _ in notices), notices
    fleet_on_disk = _config_toml()["fleet"]
    on_disk = fleet_on_disk["roles"]
    assert on_disk["coder"]["permission_mode"] == "plan"  # the bytes, not the widget
    assert on_disk["coder"]["worktree"] is True  # untouched fields survive the write
    assert on_disk["manager"]["permission_mode"] == "auto"  # only the changed role changed
    assert fleet_on_disk["worktree_dir"] == ".fleet-trees"  # the worktree root (§4.2)
    assert fleet_on_disk["escape_key"] == "f12"  # an untouched fleet field keeps its default
    assert load_config().fleet.roles["coder"].permission_mode == "plan"


def test_settings_rejects_a_bad_agent_cap_and_writes_nothing(project: ProjectInfo) -> None:
    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, str]]:
        host.query_one(ProjectView).active = "tab-settings"
        await pilot.pause()
        host.query_one("#max-agents", Input).value = "0"
        host.query_one("#save-settings", Button).press()
        await pilot.pause()
        return host.notices

    notices = drive(project, scenario)
    assert any("at least 1" in m and severity == "error" for m, severity in notices), notices
    assert not paths.config_path().exists()  # a refused form never reaches the writer


def test_settings_rejects_an_invalid_codename_and_renames_a_valid_one(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    renames: list[tuple[str, str]] = []

    def rename(
        target: ProjectInfo, codename: str, *, notes: list[str] | None = None
    ) -> ProjectInfo:
        # `notes` is the service's fail-open channel: a tmux rename it had to
        # swallow is reported there, and the view shows it.
        renames.append((target.id, codename))
        if notes is not None:
            notes.append("tmux kept the old session name (fake)")
        return target.model_copy(update={"codename": codename})

    monkeypatch.setattr(fleet_service, "rename", rename)

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[list[tuple[str, str]], int, str]:
        host.query_one(ProjectView).active = "tab-settings"
        await pilot.pause()
        field = host.query_one("#codename", Input)
        field.value = "Not Valid!"
        host.query_one("#rename-codename", Button).press()
        await pilot.pause()
        after_invalid = len(renames)
        field.value = "amber-otter"
        host.query_one("#rename-codename", Button).press()
        await pilot.pause()
        return host.notices, after_invalid, field.value

    notices, after_invalid, shown_value = drive(project, scenario)
    assert after_invalid == 0  # the invalid name never reached the service
    assert any("not a valid codename" in m and s == "error" for m, s in notices), notices
    assert renames == [(project.id, "amber-otter")]  # the valid one did, once
    assert shown_value == "amber-otter"
    assert any("is now amber-otter" in m for m, _ in notices)


def test_a_rejected_codename_reaches_the_toast_with_its_brackets(project: ProjectInfo) -> None:
    """A notification's text is DATA, not a Rich template — measured on the toast.

    The user typed the name being quoted back at them, so it is the one string
    in that message that cannot be trusted to markup. Rendered as markup,
    ``'[amber]-otter'`` reaches the screen as ``'-otter'``: the notice names a
    name nobody typed.
    """

    async def scenario(pilot: Pilot[None], host: Host) -> list[Notification]:
        host.query_one(ProjectView).active = "tab-settings"
        await pilot.pause()
        host.query_one("#codename", Input).value = "[amber]-otter"
        host.query_one("#rename-codename", Button).press()
        await pilot.pause()
        return [n for n in host._notifications if "not a valid codename" in n.message]

    notifications = drive(project, scenario)
    assert len(notifications) == 1
    notice = notifications[0]
    assert notice.markup is False
    assert "'[amber]-otter'" in str(Toast(notice).render())  # what the user sees
    # The control: the same text with the flag flipped loses what the user typed,
    # so the assertion above is about that flag and not about Textual being kind.
    as_markup = Notification(message=notice.message, severity=notice.severity, markup=True)
    assert "[amber]" not in str(Toast(as_markup).render())


# --- the Explainability tab -----------------------------------------------------------------


def test_explainability_enable_writes_the_switch_and_disable_clears_it(
    project: ProjectInfo, quiet_explainability: dict[str, int]
) -> None:
    async def scenario(
        pilot: Pilot[None], host: Host
    ) -> tuple[bool, str, bool, list[tuple[str, str]]]:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        view = host.query_one(ExplainabilityView)
        await pilot.click("#explainability-enable")
        await settle(pilot)
        enabled = _config_toml()["explainability"]["enabled"]
        status_after_enable = view.status_text
        await pilot.click("#explainability-disable")
        await settle(pilot)
        return (
            enabled,
            status_after_enable,
            _config_toml()["explainability"]["enabled"],
            host.notices,
        )

    enabled, status, disabled, notices = drive(project, scenario)
    assert enabled is True  # the artefact: config.toml on disk
    assert "enabled:   on" in status  # …and the tab re-read it through the services
    assert disabled is False
    assert quiet_explainability["shipping_state"] >= 3  # mount, after enable, after disable
    assert any(m.startswith("✓ tracing enabled") for m, _ in notices)
    assert any(m.startswith("✓ tracing disabled") for m, _ in notices)
    assert _config_toml()["explainability"]["target"] == "stg"  # nothing else was touched


def test_explainability_ship_drains_through_the_service_and_register_refuses_unconfigured(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    drains: list[int] = []
    rosters: list[object] = []

    def ship_once(limit: int = 500) -> explainability_service.ShipReport:
        drains.append(limit)
        return explainability_service.ShipReport(
            sent=3, runs=("run-1",), reason="shipped 3 records"
        )

    monkeypatch.setattr(explainability_service, "ship_once", ship_once)

    def never(*args: object, **kwargs: object) -> ops.HttpVerdict:
        rosters.append(args)
        pytest.fail("register_roster was called with no gateway configured")

    monkeypatch.setattr(ops, "register_roster", never)

    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, str]]:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        await pilot.click("#explainability-ship")
        await settle(pilot)
        await pilot.click("#explainability-register")
        await settle(pilot)
        return host.notices

    notices = drive(project, scenario)
    assert drains == [500]  # one press, one drain, the CLI's default limit
    assert ("shipped 3 records\nruns: run-1", "information") in notices
    assert rosters == []  # no gateway configured → refused before any request
    assert any("has no gateway URL" in m and s == "error" for m, s in notices), notices


def test_settings_binds_an_account_per_role_and_a_cleared_one_leaves_no_empty_profile(
    project: ProjectInfo,
) -> None:
    """The select beside each role is `team bind <role> --account` with a mouse (#145)."""
    from aisquare.core import claude_accounts as accounts_core

    accounts_core.create_account()  # slot 2, under the isolated AISQUARE_HOME

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[list[str], list[tuple[str, str]]]:
        host.query_one(ProjectView).active = "tab-settings"
        await settle(pilot)  # the slots arrive from a worker
        select = host.query_one("#acct-coder", Select)
        labels = [str(label) for label, _value in select._options]
        select.value = "2"
        host.query_one("#save-settings", Button).press()
        await pilot.pause()
        return labels, host.notices

    labels, notices = drive(project, scenario)
    assert any(m.startswith("✓ fleet settings saved") for m, _ in notices), notices
    assert labels[0] == "(no account binding)"
    assert any(label.startswith("2 · account 2") for label in labels), labels
    assert load_config().team.profiles["coder"].account == "2"  # the binding's one home
    assert _config_toml()["team"]["profiles"]["coder"]["account"] == "2"  # the bytes
    assert "manager" not in load_config().team.profiles  # an untouched role gains no profile

    async def clear(pilot: Pilot[None], host: Host) -> str | None:
        host.query_one(ProjectView).active = "tab-settings"
        await settle(pilot)
        select = host.query_one("#acct-coder", Select)
        shown_value = select.value
        select.value = ""
        host.query_one("#save-settings", Button).press()
        await pilot.pause()
        return str(shown_value)

    shown_value = drive(project, clear)
    assert shown_value == "2"  # the form opened on what the file held
    assert "coder" not in load_config().team.profiles  # nothing else bound: the table goes


def test_the_settings_form_reads_its_file_once_and_the_slots_off_the_ui_thread(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #205, fourth round: the constructor read ``config.toml`` three times —
    ``[fleet]``, ``[accounts]``, the bindings — and listed the accounts, a store open, a
    directory scan and maybe a reconcile write, on the event loop. One read of the file;
    the slots come from a thread worker, and the select keeps what it showed meanwhile."""
    import threading

    from aisquare.cli.ui.views import settings as settings_view
    from aisquare.core import claude_accounts as accounts_core
    from aisquare.services import claude_accounts as accounts_service
    from aisquare.services import settings as settings_service

    accounts_core.create_account()  # slot 2
    settings_service.bind_role("coder", account="2")
    loads: list[str] = []
    for module in (settings_view, fleet_service, accounts_service, settings_service):
        real_load = module.load_config

        def counted(real: Any = real_load, name: str = module.__name__) -> Any:
            loads.append(name)
            return real()

        monkeypatch.setattr(module, "load_config", counted)
    listed: list[str] = []
    real_list = accounts_service.list_accounts

    def list_accounts() -> Any:
        listed.append(threading.current_thread().name)
        return real_list()

    monkeypatch.setattr(accounts_service, "list_accounts", list_accounts)

    form = SettingsView(project)
    assert len(loads) == 1 and listed == []  # the constructor reads the file once, lists nothing

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[list[str], str]:
        host.query_one(ProjectView).active = "tab-settings"
        await settle(pilot)
        select = host.query_one("#acct-coder", Select)
        return [str(label) for label, _value in select._options], str(select.value)

    labels, value = drive(project, scenario)
    assert listed and threading.main_thread().name not in listed
    assert any(label.startswith("2 · account 2") for label in labels), labels
    assert value == "2"  # the binding, shown before the slots arrived and after
    assert form.accounts.pick == "default" and form.fleet.escape_key == "f12"


def test_a_slot_removed_between_two_reads_falls_back_to_the_binding_on_the_form(
    project: ProjectInfo,
) -> None:
    """The slots arrive from a worker, so a select can hold a slot an earlier answer offered
    and the next one does not: set as its value, the select refuses it and the handler
    raises. It falls back to what the file binds instead."""
    import shutil

    from aisquare.core import claude_accounts as accounts_core

    accounts_core.create_account()  # slot 2
    third = accounts_core.create_account()  # slot 3, removed below

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[str, list[str]]:
        host.query_one(ProjectView).active = "tab-settings"
        await settle(pilot)
        select = host.query_one("#acct-coder", Select)
        select.value = "3"
        shutil.rmtree(third.config_dir)
        host.query_one(SettingsView)._load_accounts()
        await settle(pilot)
        return str(select.value), [str(value) for _label, value in select._options]

    value, offered = drive(project, scenario)
    assert value == ""  # nothing bound: "no account binding"
    assert "3" not in offered and "2" in offered


def test_settings_saves_the_accounts_section_and_rejects_a_bad_line(project: ProjectInfo) -> None:
    """`[accounts]` (#146) on the same form, through the same one writer."""

    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, str]]:
        host.query_one(ProjectView).active = "tab-settings"
        await pilot.pause()
        host.query_one("#accounts-pick", Select).value = "headroom"
        host.query_one("#accounts-switch-at", Input).value = "70"
        host.query_one("#accounts-on-limit", Select).value = "switch"
        host.query_one("#accounts-wait-minutes", Input).value = "5"
        host.query_one("#save-settings", Button).press()
        await pilot.pause()
        return host.notices

    notices = drive(project, scenario)
    assert any(m.startswith("✓ fleet settings saved") for m, _ in notices), notices
    on_disk = _config_toml()["accounts"]
    assert on_disk == {
        "pick": "headroom",
        "switch_at": 70,
        "on_limit": "switch",
        "wait_if_reset_within_minutes": 5,
    }
    assert load_config().accounts.pick == "headroom"

    async def bad(pilot: Pilot[None], host: Host) -> tuple[str, list[tuple[str, str]]]:
        host.query_one(ProjectView).active = "tab-settings"
        await pilot.pause()
        shown_pick = str(host.query_one("#accounts-pick", Select).value)
        host.query_one("#accounts-switch-at", Input).value = "250"
        host.query_one("#save-settings", Button).press()
        await pilot.pause()
        return shown_pick, host.notices

    shown_pick, notices = drive(project, bad)
    assert shown_pick == "headroom"  # the form opened on what the file holds
    assert any("between 1 and 100" in m and sev == "error" for m, sev in notices), notices
    assert load_config().accounts.switch_at == 70  # a refused form never reaches the writer


@pytest.mark.parametrize("columns", [120, 200])
def test_start_manager_spawns_at_the_panes_own_size(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, columns: int
) -> None:
    """#149: the window is born the size of the pane about to show it, never the 200x50
    that grew Claude Code's diff panel before the first resize could shrink it.

    At 200 columns too: the width is passed through uncapped (the CHANGELOG says a
    pane 144 or more columns wide still shows the panel), so a clamp anywhere from
    120 to 143 would pass on the 120-column host alone (review of #162, round 2)."""
    fleet: dict[str, FleetAgent | None] = {"manager": None}
    sizes: list[object] = []

    def spawn(target: ProjectInfo, role: str, **kwargs: object) -> fleet_service.SpawnReceipt:
        sizes.append(kwargs.get("size"))
        fleet["manager"] = fake_agent(target)
        return fleet_service.SpawnReceipt(
            agent=fake_agent(target), asked_label=None, tmux_session="asq-amber-otter"
        )

    monkeypatch.setattr(fleet_service, "spawn", spawn)
    monkeypatch.setattr(fleet_service, "manager_of", lambda target: fleet["manager"])

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[int, int]:
        await pilot.resize_terminal(columns, 50)
        await pilot.pause()
        await pilot.click("#start-manager")
        await settle(pilot)
        return host.query_one(ManagerTab).content_size

    tab_width, tab_height = drive(project, scenario)
    assert len(sizes) == 1
    size = sizes[0]
    assert isinstance(size, tuple) and len(size) == 2
    width, height = size
    # The pane is hidden until the manager exists, so the tab's own size stands in:
    # the pane's width, and an estimate of the rows it will have under the header.
    assert width == tab_width and 0 < height < tab_height
    # A clamp under the 144-column line fails that equality only on a tab past the
    # line, which is what the wide host is for. Matched against the tab, not the
    # host, so chrome beside the tab is not this test's business (review of #162,
    # round 1).
    assert columns < 144 or tab_width >= 144
    # Uncapped on purpose: the pane's first attach widens the window to the pane
    # whatever it was born at, so a pane 144 or more columns wide shows Claude Code's
    # panel either way (docs/fleet.md). What stays under that line is a window nobody
    # sized — the headless default (test_tmux, test_fleet_service).


def test_start_manager_reads_the_panes_size_on_the_ui_thread_and_spawns_off_it(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The geometry is read when the button is pressed, the spawn runs in the worker.

    Textual's DOM is not thread-safe: reading a widget's ``content_size`` from the
    worker thread can rebuild the compositor's map off the event loop. So the size
    is taken on the UI thread and handed to the worker, and only the slow part —
    ``fleet.spawn`` and its tmux calls — runs off it.
    """
    fleet: dict[str, FleetAgent | None] = {"manager": None}
    on_ui_thread: dict[str, bool] = {}
    measure = ManagerTab._pane_size

    def pane_size(tab: ManagerTab) -> tuple[int, int] | None:
        on_ui_thread["measure"] = threading.current_thread() is threading.main_thread()
        return measure(tab)

    def spawn(target: ProjectInfo, role: str, **kwargs: object) -> fleet_service.SpawnReceipt:
        on_ui_thread["spawn"] = threading.current_thread() is threading.main_thread()
        fleet["manager"] = fake_agent(target)
        return fleet_service.SpawnReceipt(
            agent=fake_agent(target), asked_label=None, tmux_session="asq-amber-otter"
        )

    monkeypatch.setattr(ManagerTab, "_pane_size", pane_size)
    monkeypatch.setattr(fleet_service, "spawn", spawn)
    monkeypatch.setattr(fleet_service, "manager_of", lambda target: fleet["manager"])

    async def scenario(pilot: Pilot[None], host: Host) -> None:
        await pilot.click("#start-manager")
        await settle(pilot)

    drive(project, scenario)
    assert on_ui_thread == {"measure": True, "spawn": False}


def _attach_in_setup(host: Host, key: str, **fields: str) -> None:
    """Type ``key`` into the Setup form's one key field, tick *this project only*, save.

    ``fields`` fills other Setup inputs by id (``target=``, ``gateway=``, ``key_env=``).
    Pressed, not clicked: the form sits below the fold of a 50-row host.
    """
    for name, value in fields.items():
        host.query_one(f"#explainability-{name.replace('_', '-')}", Input).value = value
    host.query_one("#explainability-key", Input).value = key
    host.query_one("#explainability-key-project", Checkbox).value = True
    host.query_one("#explainability-save", Button).press()


def test_the_explainability_tab_attaches_a_key_to_the_active_project_without_echoing_it(
    project: ProjectInfo, quiet_explainability: dict[str, int]
) -> None:
    """#141: the shell-only gap — a key per project, from the UI. The value goes to a
    mode-600 file and a binding row, not to the machine file; the toast names the
    path, never the key."""

    async def scenario(
        pilot: Pilot[None], host: Host
    ) -> tuple[str, list[tuple[str, str]], str, str]:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        view = host.query_one(ExplainabilityView)
        before = view.status_text
        _attach_in_setup(host, "pk-ui-0123456789")
        await settle(pilot)
        field = host.query_one("#explainability-key", Input)
        return before, list(host.notices), field.value, view.status_text

    before, notices, field_after, status = drive(project, scenario)
    assert "project:" in before and "no key of its own" in before
    attached = [m for m, _ in notices if m.startswith("✓ key attached to")]
    assert len(attached) == 1, notices
    assert "pk-ui-0123456789" not in attached[0]
    assert field_after == "", "the field is cleared"
    path = explainability_service.project_key_path(project.id)
    assert path.read_text(encoding="utf-8") == "pk-ui-0123456789"
    if sys.platform != "win32":  # NTFS keeps one bit of the mode: 0o666 or 0o444
        assert (path.stat().st_mode & 0o777) == 0o600
    with store_session() as store:
        binding = store.project_explainability(project.id)
    assert binding is not None and binding.key_path == path
    assert explainability_service.stored_api_key() is None, "the machine file is not the project's"
    assert "its own key for target" in status and "pk-ui" not in status


def test_the_explainability_tab_has_one_key_field_and_the_box_says_whose_key(
    project: ProjectInfo, quiet_explainability: dict[str, int]
) -> None:
    """#141's *Attach key* field and #131's Setup form met on this tab with a key
    input each, writing to two places under two rules for the deployment. One field
    now: unticked it is the machine key, as the Setup form always wrote it."""

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[list[str | None], bool]:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        view = host.query_one(ExplainabilityView)
        secret_fields = [field.id for field in view.query(Input) if field.password]
        box = host.query_one("#explainability-key-project", Checkbox)
        host.query_one("#explainability-key", Input).value = "AIS_machine_0123456789"
        host.query_one("#explainability-save", Button).press()
        await settle(pilot)
        return secret_fields, box.disabled

    secret_fields, disabled = drive(project, scenario)
    assert secret_fields == ["explainability-key"]
    assert disabled is False, "a page has a project to own a key"
    assert explainability_service.stored_api_key() == "AIS_machine_0123456789"
    with store_session() as store:
        assert store.project_explainability(project.id) is None, "unticked: not the project's"


def test_a_project_key_is_taken_beside_a_target_that_names_its_own_variable(
    project: ProjectInfo, quiet_explainability: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Setup form refuses a MACHINE key beside a custom key variable: the file is
    read only for the default one. A project's own key is the resolver's first rung,
    read whatever variable the target names, so that refusal is not its to make."""
    config = load_config()
    config.explainability.targets = {
        "stg": ExplainabilityTarget(gateway_url="https://stg.example", api_key_env="MY_KEY"),
    }
    save_config(config)
    monkeypatch.delenv("MY_KEY", raising=False)

    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, str]]:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        _attach_in_setup(host, "pk-own-var-0123456789")
        await settle(pilot)
        return list(host.notices)

    notices = drive(project, scenario)
    assert not any("reads its key from $MY_KEY" in m for m, _ in notices), notices
    resolved = ops.resolve_target(load_config().explainability, None, project_id=project.id)
    assert (resolved.api_key, resolved.key_source) == ("pk-own-var-0123456789", "project")


def test_a_project_key_for_a_deployment_nothing_answers_to_is_refused(
    project: ProjectInfo, quiet_explainability: dict[str, int]
) -> None:
    """``key set --target prdo`` is refused before anything is written: a binding to a
    name no target answers to traces nothing (review of #170). The form's deployment
    field is the same question. Typed with a gateway, the target exists after the
    save and the key is bound to it, not to the machine's active one."""

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[list[tuple[str, str]], str]:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        _attach_in_setup(host, "pk-typo-0123456789", target="prdo")
        await settle(pilot)
        refused = list(host.notices)
        kept = host.query_one("#explainability-key", Input).value
        _attach_in_setup(host, "pk-prod-0123456789", target="prod", gateway="https://p.example")
        await settle(pilot)
        return refused, kept

    refused, kept = drive(project, scenario)
    assert any(m.startswith("no target 'prdo' on this machine") for m, _ in refused), refused
    assert kept == "pk-typo-0123456789", "refused before a write began: the field keeps it"
    assert "prdo" not in load_config().explainability.targets
    with store_session() as store:
        binding = store.project_explainability(project.id)
    assert binding is not None and binding.target == "prod"
    assert load_config().explainability.target != "prod", "naming a deployment is not moving to it"
    path = explainability_service.project_key_path(project.id)
    assert path.read_text(encoding="utf-8") == "pk-prod-0123456789"


def test_a_project_key_lands_where_its_destination_does_and_never_over_a_minted_one(
    project: ProjectInfo, quiet_explainability: dict[str, int]
) -> None:
    """#142 on the Setup form. With the deployment field blank, the key is bound to
    the deployment the project's destination names — ``key set``'s default — not to
    the machine's active one. And a key the CLI minted is the CLI's to replace
    (``key set`` revokes it): refused before a write began, the field keeps what
    was typed, and the minted key stays in its file."""
    from aisquare.services import destinations, iam

    config = load_config()
    config.explainability.targets = {"prod": ExplainabilityTarget(gateway_url="https://p.example")}
    save_config(config)
    session = iam.Session(api_url="https://api.aisquare.studio", token="aisq_x", source="env")
    with store_session() as store:
        destinations.choose(
            store,
            project,
            destinations.Workspace(id=42, uid="ws-uid-42", name="acme", role="ADMIN"),
            destinations.Studio(id=301, uid="st-301", name="Frontend"),
            session,
        )
        store.set_project_destination_key(project.id, "key-minted")
    explainability_service.store_project_api_key(project.id, "AIS_minted_key")

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[list[tuple[str, str]], str, str]:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        _attach_in_setup(host, "pk-over-minted-0123456789")
        await settle(pilot)
        refused = list(host.notices)
        kept = host.query_one("#explainability-key", Input).value
        on_disk = explainability_service.project_key_path(project.id).read_text(encoding="utf-8")
        with store_session() as store:
            # What `key set` leaves after revoking the minted key: no uid on the row.
            store.set_project_destination_key(project.id, None)
        _attach_in_setup(host, "pk-hand-0123456789")
        await settle(pilot)
        return refused, kept, on_disk

    refused, kept, on_disk = drive(project, scenario)
    assert any("minted by the CLI" in m and s == "warning" for m, s in refused), refused
    assert kept == "pk-over-minted-0123456789", "refused before a write began: the field keeps it"
    assert on_disk == "AIS_minted_key"
    with store_session() as store:
        binding = store.project_explainability(project.id)
    assert binding is not None and binding.target == "prod", "the destination's deployment"
    assert load_config().explainability.target == "stg", "the machine stays where it was"
    path = explainability_service.project_key_path(project.id)
    assert path.read_text(encoding="utf-8") == "pk-hand-0123456789"


def test_one_save_never_splits_a_project_key_and_its_settings_across_deployments(
    project: ProjectInfo, quiet_explainability: dict[str, int]
) -> None:
    """The deployment field blank, a gateway typed beside the project's key went to
    the machine's target while the key went to the destination's: one press, two
    deployments, and the project's launches never read that gateway (review of
    #172). Refused before a write began; with the deployment typed, both land there."""
    from aisquare.services import destinations, iam

    config = load_config()
    config.explainability.targets = {"prod": ExplainabilityTarget(gateway_url="https://p.example")}
    save_config(config)
    session = iam.Session(api_url="https://api.aisquare.studio", token="aisq_x", source="env")
    with store_session() as store:
        destinations.choose(
            store,
            project,
            destinations.Workspace(id=42, uid="ws-uid-42", name="acme", role="ADMIN"),
            destinations.Studio(id=301, uid="st-301", name="Frontend"),
            session,
        )

    async def scenario(
        pilot: Pilot[None], host: Host
    ) -> tuple[list[tuple[str, str]], tuple[str, str], set[str], bool]:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        _attach_in_setup(host, "pk-split-0123456789", gateway="https://new.example")
        await settle(pilot)
        refused = list(host.notices)
        kept = (
            host.query_one("#explainability-key", Input).value,
            host.query_one("#explainability-gateway", Input).value,
        )
        gateways = {t.gateway_url for t in load_config().explainability.targets.values()}
        bound = explainability_service.project_key_path(project.id).exists()
        # The gateway field still holds what was typed: with the deployment, one save.
        _attach_in_setup(host, "pk-split-0123456789", target="prod")
        await settle(pilot)
        return refused, kept, gateways, bound

    refused, kept, gateways, bound = drive(project, scenario)
    assert any(
        "belongs to target 'prod'" in m and "saved for 'stg'" in m and s == "warning"
        for m, s in refused
    ), refused
    assert kept == ("pk-split-0123456789", "https://new.example"), "the fields keep what was typed"
    assert "https://new.example" not in gateways and not bound, "refused before any write"
    saved = load_config().explainability
    assert saved.targets["prod"].gateway_url == "https://new.example", "typed: both go to prod"
    with store_session() as store:
        binding = store.project_explainability(project.id)
    assert binding is not None and binding.target == "prod"


def test_a_store_that_cannot_say_whether_the_key_was_minted_refuses_the_attach(
    project: ProjectInfo, quiet_explainability: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The minted-key check (#142) runs on the UI thread before any write: a store
    error there is a notice and nothing written — never a raise out of the handler,
    which ends the app, and never a guess that could overwrite a minted key."""
    import sqlite3

    from aisquare.cli.ui.views import explainability as view_module

    def locked() -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(view_module, "store_session", locked)

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[list[tuple[str, str]], str]:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        _attach_in_setup(host, "pk-locked-0123456789")
        await settle(pilot)
        return list(host.notices), host.query_one("#explainability-key", Input).value

    notices, kept = drive(project, scenario)
    refused = [m for m, s in notices if s == "error" and "could not be read" in m]
    assert refused and "database is locked" in refused[0], notices
    assert kept == "pk-locked-0123456789", "refused before a write began: the field keeps it"
    assert not explainability_service.project_key_path(project.id).exists()


def _another_project_pinned(tmp_path: Path) -> ProjectInfo:
    """A second registered project, pinned by ``project switch`` — the one the tab must ignore."""
    from aisquare.core.workspace import pin_project, project_id_for

    root = (tmp_path / "pinned-elsewhere").resolve()
    root.mkdir()
    other = ProjectInfo(id=project_id_for(root), root=root, linked_repos=[])
    with store_session() as store:
        store.onboard_project(other)
    pin_project(other.id)
    return other


def test_the_explainability_tab_is_about_its_own_project_not_the_pinned_one(
    project: ProjectInfo, quiet_explainability: dict[str, int], tmp_path: Path
) -> None:
    """The tab sits on ONE project's page, and that page's agents launch in that
    project — so its key row and the key it attaches are that project's. They read
    the ``project switch`` pin, and attached the key to whichever project was
    pinned while the page named another (review of #170)."""
    other = _another_project_pinned(tmp_path)

    async def scenario(pilot: Pilot[None], host: Host) -> str:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        _attach_in_setup(host, "pk-page-0123456789")
        await settle(pilot)
        return host.query_one(ExplainabilityView).status_text

    status = drive(project, scenario)
    with store_session() as store:
        assert store.project_explainability(project.id) is not None
        assert store.project_explainability(other.id) is None, "never the pinned project"
    name = project.root.name or project.id
    assert f"{name}: its own key for target stg (in use)" in status


def test_the_explainability_tab_registers_the_roster_under_its_projects_key(
    project: ProjectInfo, quiet_explainability: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project key for ANOTHER workspace traces nothing until that workspace knows
    the agents; *Register roster* resolved at machine level only, and on a
    machine with no key of its own refused "not set" right after *Attach key*
    succeeded (review of #170)."""
    config = load_config()
    config.explainability.targets = {
        "stg": ExplainabilityTarget(gateway_url="https://stg.example"),
    }
    save_config(config)
    monkeypatch.delenv("EXPLAINABILITY_API_KEY", raising=False)
    path = explainability_service.store_project_api_key(project.id, "pk-roster-0123456789")
    with store_session() as store:
        store.set_project_explainability(project.id, target="stg", key_path=path, set_by=None)
    keys: list[str | None] = []

    def register_roster(target: ops.ResolvedTarget, names: tuple[str, ...]) -> ops.HttpVerdict:
        keys.append(target.api_key)
        return ops.HttpVerdict(ok=True, status=200, detail="HTTP 200", payload={"agents": []})

    monkeypatch.setattr(ops, "register_roster", register_roster)

    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, str]]:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        await pilot.click("#explainability-register")
        await settle(pilot)
        return host.notices

    notices = drive(project, scenario)
    assert keys == ["pk-roster-0123456789"]
    assert any(m.startswith("✓ registered") for m, _ in notices), notices


def test_under_a_hub_the_explainability_tab_is_about_the_hub_its_launches_join(
    project: ProjectInfo,
    quiet_explainability: dict[str, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AISQUARE_TEAM_HUB`` puts a fleet window's ``launch`` on the hub's board, so its
    agents authenticate with the hub's key — the one ``key set`` binds from any
    repo under the hub. The tab showed, attached and registered under the PAGE's
    key, which no launch from the page read (review of #170)."""
    from aisquare.core.workspace import project_id_for

    config = load_config()
    config.explainability.targets = {
        "stg": ExplainabilityTarget(gateway_url="https://stg.example"),
    }
    save_config(config)
    monkeypatch.delenv("EXPLAINABILITY_API_KEY", raising=False)
    hub = (tmp_path / "hub").resolve()
    hub.mkdir()
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub))
    keys: list[str | None] = []

    def register_roster(target: ops.ResolvedTarget, names: tuple[str, ...]) -> ops.HttpVerdict:
        keys.append(target.api_key)
        return ops.HttpVerdict(ok=True, status=200, detail="HTTP 200", payload={"agents": []})

    monkeypatch.setattr(ops, "register_roster", register_roster)

    async def scenario(pilot: Pilot[None], host: Host) -> str:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        _attach_in_setup(host, "pk-hub-0123456789")
        await settle(pilot)
        await pilot.click("#explainability-register")
        await settle(pilot)
        return host.query_one(ExplainabilityView).status_text

    status = drive(project, scenario)
    with store_session() as store:
        assert store.project_explainability(project_id_for(hub)) is not None
        assert store.project_explainability(project.id) is None, "not the page under a hub"
    assert "hub: its own key for target stg (in use)" in status
    assert keys == ["pk-hub-0123456789"]


def test_the_key_row_names_a_missing_file_instead_of_contradicting_itself(
    project: ProjectInfo, quiet_explainability: dict[str, int]
) -> None:
    """Bound to the active target with its file gone, the row read "its own key for
    target stg (not used for target stg)"; it says what ``key show`` says."""
    from aisquare.cli.ui.views.explainability import status_report

    path = explainability_service.store_project_api_key(project.id, "pk-gone-0123456789")
    with store_session() as store:
        store.set_project_explainability(project.id, target="stg", key_path=path, set_by=None)
    path.unlink()

    row = dict(status_report(project).rows)["project"]

    assert "file MISSING" in row and str(path) in row
    assert "not used for target stg" not in row
