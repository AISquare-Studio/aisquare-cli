"""The Project view, driven headless: its tabs, the manager button, the board, both forms.

Everything the view reaches for outside the store is monkeypatched at the
module the view imports — ``services.fleet`` for the manager, the two
explainability services for the tracing tab — so no test starts tmux, dials a
gateway or depends on ``claude``.
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
import json
import os
import sqlite3
import sys
import threading
import tomllib
from collections.abc import Callable, Coroutine, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

import pytest
from textual import events
from textual.app import App, ComposeResult
from textual.content import Content
from textual.geometry import Region
from textual.notifications import SeverityLevel
from textual.pilot import Pilot
from textual.widget import Widget
from textual.widgets import (
    Button,
    Checkbox,
    ContentSwitcher,
    DataTable,
    Input,
    OptionList,
    Static,
)
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.ui.board import COLUMN_WIDTH_KEY, DETAIL_HEIGHT_KEY, BoardPanel
from aisquare.cli.ui.divider import Divider
from aisquare.cli.ui.terminal import TerminalPane
from aisquare.cli.ui.views import project as project_view_module
from aisquare.cli.ui.views import settings as settings_view_module
from aisquare.cli.ui.views.agent import AgentView
from aisquare.cli.ui.views.doctor import DoctorView
from aisquare.cli.ui.views.explainability import ExplainabilityView
from aisquare.cli.ui.views.personas_tab import PersonasTab
from aisquare.cli.ui.views.project import ManagerTab, ProjectView
from aisquare.cli.ui.views.settings import SettingsView
from aisquare.core import paths
from aisquare.core import tmux as tmux_core
from aisquare.core.config import ExplainabilityTarget, load_config, save_config
from aisquare.core.store import SqliteStore, store_session
from aisquare.core.tmux import Capture, Completed, PaneFacts, TmuxServer
from aisquare.core.workspace import project_id_for
from aisquare.models import (
    DoctorCheck,
    FleetAgent,
    FleetAgentStatus,
    ProjectInfo,
    TeamEvent,
    TeamSession,
)
from aisquare.services import diagnostics, install_route
from aisquare.services import explainability as explainability_service
from aisquare.services import explainability_ops as ops
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from tests.ci_support import git
from tests.ui_workers import settle_page

T = TypeVar("T")

PRIVATE_SOCKET = f"asq-test-{os.getpid()}-ui-project"
"""The socket every agent in this file lives on. NOT ``asq``: that is the
default (``models.py``) and the developer's real fleet — see the module
docstring."""


class Host(App[None]):
    """A bare app around one ``ProjectView`` that records every notification."""

    def __init__(self, project: ProjectInfo, *, refresh_seconds: float = 60.0) -> None:
        super().__init__()
        self._project = project
        self._refresh_seconds = refresh_seconds
        self.notices: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        yield ProjectView(self._project, id="project", refresh_seconds=self._refresh_seconds)

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
    refresh_seconds: float = 60.0,
) -> T:
    """Run ``scenario`` against a mounted Project view and return what it observed."""

    async def run() -> T:
        host = Host(project, refresh_seconds=refresh_seconds)
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

    One pause and one wait did not close it (review of the accounts stack's fold,
    round 2, F8). The pause's idle check is a guess from CPU use, and a bubbling
    ``Pressed`` is queued on each parent behind the pause's own callback, so the
    handler can still be pending when the pause returns; and the wait snapshots
    the workers once, so one a finishing worker's handler starts is never waited
    for. So it goes round — pause, then wait for what is running — until a pause
    ends with no message queued on the page and no worker of ours unfinished,
    bounded, so a page that never goes quiet fails at its assertion, not here.
    The loop is ``settle_page``, shared with the Accounts page's tests.
    """
    await settle_page(pilot.app)


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
        # No control client: there is no ``tmux -C`` behind a script, and the fixture's
        # ``_tmux`` stand-in would read as the real runner (``control_capable``).
        super().__init__(socket, conf=Path("/nonexistent/fleet-tmux.conf"), control=False)
        self.captures: list[tuple[str, int, int | None]] = []
        self.resizes: list[tuple[str, int, int]] = []

    def server_version(self) -> tuple[int, int] | None:
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

    def at(self, path: Path | None) -> TmuxServer:
        """This server asked for at ``path`` (``tmux -S``): scripted there too, and saying
        where it was asked (:attr:`path`), so no real tmux is run for a recorded row."""
        if path is None or path == self.path:
            return self
        there = ScriptedServer(self.socket)
        there.path = path
        return there


# --- fixtures ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The project's directory: a repository of its own, so that is its root, whatever
    lies above it.

    A plain directory is resolved by git and the marker walk climbing the shared temp
    tree, and resolved twice — by the ``project`` fixture, then by *Save setup*'s worker
    (``key_project``) — it was one project, then another, once a marker or a repository
    appeared above it in between: the key went under the other's id, and the test found
    no file where its notice said it went (a full run at 1ada4bd3).
    """
    work = tmp_path / "repo"
    work.mkdir()
    git(work, "init", "-q")
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
    """Manager, Board, AISquare, Settings, Personas: no Doctor tab — the doctor is the app's
    page."""

    async def scenario(
        pilot: Pilot[None], host: Host
    ) -> tuple[int, list[str], str, list[str], str, int, str]:
        view = host.query_one(ProjectView)
        for pane_id, widget_type in (
            ("#tab-manager", ManagerTab),
            ("#tab-board", BoardPanel),
            ("#tab-explainability", ExplainabilityView),
            ("#tab-settings", SettingsView),
            ("#tab-personas", PersonasTab),
        ):
            assert view.query_one(pane_id).query_one(widget_type)
        ids = [pane.id or "" for pane in view.query("TabPane")]
        labels = [view.get_tab(pane_id).label_text for pane_id in ids]
        first = view.query_one(ExplainabilityView).children[0]
        heading = first.render()
        assert isinstance(heading, Content)
        bold = any(
            span.end - span.start == len(heading.plain) and "bold" in str(span.style)
            for span in heading.spans
        )
        return (
            view.tab_count,
            ids,
            view.active,
            labels,
            f"{first.id}:{'bold' if bold else 'plain'}",
            len(view.query(DoctorView)),
            heading.plain,
        )

    count, ids, active, labels, first_id, doctors, heading = drive(project, scenario)
    assert count == 5
    assert ids == list(ProjectView.TAB_IDS)
    assert active == "tab-manager"  # the manager first: that is where the goal goes
    assert labels == ["Manager", "Board", "AISquare", "Settings", "Personas"]
    assert doctors == 0
    # The AISquare tab opens on an "Explainability" heading, above the status block as before.
    assert first_id == "explainability-heading:bold" and heading == "Explainability"


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


def test_a_share_the_manager_spawn_made_is_told_as_done_not_as_a_warning(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final gate NIT 3: a folder's trust copied into the manager's account is good news; it
    came up as a warning beside the ones that are (a prompt not typed)."""
    shared = "accounts: /work is trusted on plain claude — now on account 2 too"
    warning = "the agent exited before the prompt could be typed"
    fleet: dict[str, FleetAgent | None] = {"manager": None}

    def spawn(target: ProjectInfo, role: str, **_: object) -> fleet_service.SpawnReceipt:
        fleet["manager"] = fake_agent(target)
        return fleet_service.SpawnReceipt(
            agent=fake_agent(target),
            asked_label=None,
            tmux_session="asq-amber-otter",
            notes=[shared, warning],
            shared=[shared],
        )

    monkeypatch.setattr(fleet_service, "spawn", spawn)
    monkeypatch.setattr(fleet_service, "manager_of", lambda target: fleet["manager"])

    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, str]]:
        await pilot.click("#start-manager")
        await settle(pilot)
        return host.notices

    notices = drive(project, scenario)
    assert (shared, "information") in notices
    assert (warning, "warning") in notices


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


def test_the_manager_pane_asks_its_server_at_the_path_its_row_recorded(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Round 29 of #257: the Manager tab's pane and its attach line, and an agent view's pane,
    named the row's server by its socket's name (``tmux -L``). From a shell under another
    ``TMUX_TMPDIR`` that is another server, and its ``%7`` — another agent — was shown and
    typed into. A row recorded elsewhere is asked at its path (``tmux -S``); one from before
    v25, by its name. The attach line has since moved to the Settings tab, with its rule.

    Where, the listed status says (``socket_at``, worked out with the listing's config): each
    frame asked ``fleet_service.socket_at``, which reloaded config.toml on the UI thread for
    every open project (round 29 of #257)."""
    elsewhere = tmp_path / "elsewhere" / "tmux-1000" / PRIVATE_SOCKET  # not where `-L` looks
    recorded = fake_agent(project, pane_id="%7").model_copy(update={"socket_path": elsewhere})
    monkeypatch.setattr(fleet_service, "manager_of", lambda target: recorded)
    monkeypatch.setattr(
        fleet_service,
        "status_of",
        lambda agent: FleetAgentStatus(agent=agent, state="waiting", socket_at=elsewhere),
    )

    async def scenario(pilot: Pilot[None], host: Host) -> TmuxServer | None:
        await pilot.pause(0.25)  # past the 100 ms resize debounce
        return host.query_one("#manager-pane", TerminalPane).server

    server = drive(project, scenario)
    named = fake_agent(project, pane_id="%7")

    def no_config(config: object = None) -> object:
        raise AssertionError("a frame read config.toml on the UI thread")

    monkeypatch.setattr(fleet_service, "settings", no_config)

    def attach_line(agent: FleetAgent, socket_at: Path | None) -> str:
        status = FleetAgentStatus(
            agent=agent, state="waiting", tmux_session="asq-amber-otter", socket_at=socket_at
        )
        return settings_view_module.attach_line([status]).plain

    assert isinstance(server, ScriptedServer) and server.path == elsewhere, server
    assert server.captures, "the frame came from the server at the recorded path"
    assert f"tmux -S {elsewhere} attach -t =asq-amber-otter" in attach_line(recorded, elsewhere)
    assert f"tmux -L {PRIVATE_SOCKET} attach -t =asq-amber-otter" in attach_line(named, None)
    shown = FleetAgentStatus(agent=recorded, socket_at=elsewhere)
    assert AgentView(shown, escape_key="ctrl+]").server.path == elsewhere
    by_name = AgentView(FleetAgentStatus(agent=named), escape_key="ctrl+]")
    assert by_name.server.path is None, "asked by its name"


def test_the_attach_line_quotes_the_path_and_the_socket_name_it_prints(
    project: ProjectInfo, tmp_path: Path
) -> None:
    """Round 29 of #257: the Manager tab pasted the recorded socket path into its attach line
    as it was, so a ``TMUX_TMPDIR`` with a space or a ``$`` gave a line that failed or named
    another path, and so did a free-form ``[fleet] tmux_socket``. Quoted for this shell, it
    reads back as the command it names — on the Settings tab, where the line now is."""
    odd = tmp_path / "my $HOME" / "tmux-1000" / "asq one"
    recorded = fake_agent(project, pane_id="%7").model_copy(
        update={"socket_path": odd, "tmux_socket": "asq one"}
    )

    def attach_argv(socket_at: Path | None) -> list[str]:
        status = FleetAgentStatus(
            agent=recorded, state="waiting", tmux_session="asq-amber-otter", socket_at=socket_at
        )
        line = settings_view_module.attach_line([status]).plain
        assert line.startswith("Attach in a terminal: "), line
        return install_route.split_line(line.removeprefix("Attach in a terminal: "))

    session = ["attach", "-t", "=asq-amber-otter"]
    assert attach_argv(odd) == ["tmux", "-S", str(odd), *session]
    assert attach_argv(None) == ["tmux", "-L", "asq one", *session]


def test_the_settings_tab_shows_how_to_attach_and_says_so_when_there_is_nothing_to(
    project: ProjectInfo, tmp_path: Path
) -> None:
    """The tmux attach command moved off the Manager tab's line to the project's Settings
    tab: a labelled read-only row, from the shell's frames. The manager's session first; a
    ``lost`` row or one held as another server's is no session to attach to, and with none
    the row is a dim note. Before any frame it says nothing: "no live tmux session" there
    may be false (review of #267, 7)."""
    manager = fake_agent(project, pane_id="%11")
    coder = fake_agent(project, pane_id="%12", label="coder-auth")
    elsewhere = tmp_path / "tmux 1000" / "$asq"

    def row(agent: FleetAgent, state: str = "working", **update: Any) -> FleetAgentStatus:
        return FleetAgentStatus.model_validate(
            {"agent": agent, "state": state, "tmux_session": "asq-amber-otter", **update}
        )

    async def scenario(pilot: Pilot[None], host: Host) -> list[str]:
        view = host.query_one(ProjectView)
        view.active = "tab-settings"
        await pilot.pause()
        line = host.query_one("#attach-line", Static)
        seen = [shown(line)]
        for frame in (
            [row(coder), row(manager, "waiting", socket_at=elsewhere)],
            [row(coder, socket_at=elsewhere)],
            [row(coder, "lost"), row(manager, held=True)],
            [],
        ):
            view.refresh_status(frame)
            await pilot.pause()
            seen.append(shown(line))
        return seen

    before, manager_first, coder_only, none_live, no_rows = drive(project, scenario)
    attach = ["attach", "-t", "=asq-amber-otter"]
    command = "Attach in a terminal: "
    for plain in (manager_first, coder_only):
        assert plain.startswith(command)
        assert install_route.split_line(plain.removeprefix(command)) == [
            "tmux",
            "-S",
            str(elsewhere),
            *attach,
        ]
    assert before == "", "no frame yet: nothing said"
    for note in (none_live, no_rows):
        assert "no live tmux session" in note and "tmux -" not in note
    assert settings_view_module.attach_line([row(manager)]).plain == (
        f"{command}tmux -L {PRIVATE_SOCKET} attach -t =asq-amber-otter"
    )
    nothing = settings_view_module.attach_line([])
    assert str(nothing.style) == "dim"


def test_the_attach_line_repaints_only_when_it_changes(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #267, 6: every two-second frame repainted the attach line of every open
    project, with a layout pass, though the command had not changed. A frame that names the
    same command repaints nothing, a new state on the row included; the frame that ends the
    session repaints once, and the next one, which says the same, does not."""
    manager = fake_agent(project, pane_id="%11")
    live = FleetAgentStatus(agent=manager, state="waiting", tmux_session="asq-amber-otter")
    painted: list[str] = []

    async def scenario(pilot: Pilot[None], host: Host) -> list[str]:
        view = host.query_one(ProjectView)
        line = host.query_one("#attach-line", Static)
        real = line.update

        def update(content: Any = "", *, layout: bool = True) -> None:
            painted.append(str(content))
            real(content, layout=layout)

        monkeypatch.setattr(line, "update", update)
        for frame in ([live], [live], [live.model_copy(update={"state": "working"})], [], []):
            view.refresh_status(frame)
            await pilot.pause()
        return painted

    seen = drive(project, scenario)
    assert len(seen) == 2, seen  # the first frame, then the one that ended the session
    assert "attach -t =asq-amber-otter" in seen[0] and "no live tmux session" in seen[1]


def test_the_settings_tab_attaches_where_fleet_attach_does_live_rows_before_ended_ones(
    project: ProjectInfo, tmp_path: Path
) -> None:
    """Review of #267, 1: an ENDED manager is listed while its window stands (#138), and
    ranked by its label alone the Settings tab named its server ahead of a live coder's — not
    the server ``aisquare fleet attach`` reaches (``services.fleet.attach_argv``: the rows
    that have not ended, the manager first, then the ended ones, the latest to end first)."""
    now = datetime.now(tz=UTC)
    old, new, older = tmp_path / "old" / "asq", tmp_path / "new" / "asq", tmp_path / "older"
    manager = fake_agent(project, pane_id="%11")
    coder = fake_agent(project, pane_id="%12", label="coder-auth")
    tester = fake_agent(project, pane_id="%13", label="tester-1")

    def row(agent: FleetAgent, at: Path, ended: datetime | None = None) -> FleetAgentStatus:
        ended_row = agent.model_copy(update={"ended_at": ended, "exit_status": 0})
        return FleetAgentStatus(
            agent=ended_row if ended is not None else agent,
            state="exited" if ended is not None else "working",
            tmux_session="asq-amber-otter",
            socket_at=at,
        )

    def server(rows: list[FleetAgentStatus]) -> str:
        line = settings_view_module.attach_line(rows).plain
        return install_route.split_line(line.removeprefix("Attach in a terminal: "))[2]

    exited_manager = row(manager, old, ended=now)
    assert server([exited_manager, row(coder, new)]) == str(new)  # the live coder's
    assert server([row(coder, new), row(manager, old)]) == str(old)  # a live manager's first
    # No live row: the latest to end, as the 💤 row's attach stands for.
    early = row(tester, older, ended=now - timedelta(minutes=5))
    assert server([early, exited_manager]) == str(old)


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

    async def scenario(
        pilot: Pilot[None], host: Host
    ) -> tuple[str, str, str | None, bool, bool, str]:
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
        attach = shown(host.query_one("#attach-line", Static))
        attached = pane.pane_id
        view.refresh_status([FleetAgentStatus(agent=coder, state="working")])  # coder only
        await pilot.pause()
        return (
            header,
            attach,
            attached,
            host.query_one("#start-manager", Button).display,
            pane.display,
            shown(host.query_one("#attach-line", Static)),
        )

    header, attach, attached, button_back, pane_after, attach_after = drive(project, scenario)
    assert "🔔 attention" in header and "asq-ruby-fox" not in header  # no tmux command here…
    assert "attach -t =asq-ruby-fox" in attach  # …the Settings tab has it, from the same push
    assert attached == "%11"  # the manager's pane, not the coder's
    assert button_back is True and pane_after is False  # no manager in the snapshot → button
    assert "no live tmux session" in attach_after  # the coder has no session to attach to


def test_a_resize_after_the_manager_leaves_keeps_the_no_manager_text(
    project: ProjectInfo,
) -> None:
    """Review of #267 (2b1d37a3): the Manager tab's header refits the info line on every
    resize (``InfoLine``), and once the manager has left it shows the no-manager prose
    instead. The line it last fitted must be forgotten then: kept, the next resize painted
    the departed manager's line back over "has no manager yet"."""
    manager = fake_agent(project, pane_id="%11")

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[str, str, str]:
        view = host.query_one(ProjectView)
        view.refresh_status([FleetAgentStatus(agent=manager, state="working")])
        await settle(pilot)
        header = host.query_one("#manager-header", Static)
        shown_manager = shown(header)
        view.refresh_status([])  # the manager leaves the frame
        await settle(pilot)
        left = shown(header)
        await pilot.resize_terminal(100, 40)  # …and the window is resized
        await settle(pilot)
        return shown_manager, left, shown(header)

    shown_manager, left, resized = drive(project, scenario)
    assert "🧭 manager" in shown_manager and "▶ working" in shown_manager  # not vacuous
    for text in (left, resized):
        assert "has no manager yet" in text, text
        assert "🧭 manager" not in text and "%11" not in text, text  # no stale info line


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
    # Naming the project: alone, it restarted the active project's manager (#259, round 21).
    restart = f"aisquare fleet restart --project {project.codename or project.id} manager"
    assert restart in " ".join(header.split()) and "no manager yet" not in header
    assert button is True and pane is False  # the way to a NEW session is right there
    assert "has no manager yet" in without and "exited" not in without  # the control


def test_a_lost_manager_keeps_its_header_and_is_never_shown_or_typed_into(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, no_real_tmux: NoTmux
) -> None:
    """Review of the #203 final-round fixes, F1: a manager row that outlived its tmux
    server reads ``lost``, and the tab still attached its pane by id. After a reboot
    that id is the next server's, most often ANOTHER project's manager: the tab showed
    that manager under this one's ``✗ lost`` header and forwarded every key into it. A
    lost manager keeps its header and gets no pane, from the tab's own read at mount as
    from the shell's push; the working manager beside it is the control."""
    manager = fake_agent(project, pane_id="%1")
    lost = FleetAgentStatus(agent=manager, state="lost", detail="pane gone")
    monkeypatch.setattr(fleet_service, "manager_of", lambda target: manager)
    monkeypatch.setattr(fleet_service, "status_of", lambda agent: lost)

    async def scenario(
        pilot: Pilot[None], host: Host
    ) -> tuple[str | None, str, list[str], str | None, str | None, str, bool]:
        view = host.query_one(ProjectView)
        pane = host.query_one("#manager-pane", TerminalPane)
        at_mount, header = pane.pane_id, shown(host.query_one("#manager-header"))
        built = list(no_real_tmux.sockets)
        view.refresh_status([FleetAgentStatus(agent=manager, state="working")])
        await pilot.pause()
        working = pane.pane_id
        view.refresh_status([lost])
        await pilot.pause()
        width, height = pane.content_size
        first_row = pane.render_lines(Region(0, 0, width, height))[0].text.rstrip()
        return at_mount, header, built, working, pane.pane_id, first_row, pane.display

    at_mount, header, built, working, after, first_row, displayed = drive(project, scenario)
    assert at_mount is None and built == []  # never attached, no server even built
    assert "✗ lost" in header and "pane gone" in header  # the header still says what it is
    assert working == "%1"  # the control: a manager that is there is shown
    assert after is None  # …and detached once it reads lost
    assert first_row == "(pane gone)" and displayed is True  # not "no agent selected"


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
    assert "🔔 attention" in early_header  # the pre-mount push landed at mount


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
    real = team_service.board_read

    def counting(*args: Any, **kwargs: Any) -> Any:
        reads.append(1.0)
        return real(*args, **kwargs)

    monkeypatch.setattr(team_service, "board_read", counting)

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


def test_b_on_the_board_tab_hides_and_brings_back_the_column_every_time(
    project: ProjectInfo,
) -> None:
    """The fleet UI's Board tab is the same panel as ``board -w``: ``b`` hides the column with
    focus on the tasks, and the next ``b`` still reaches the panel to bring it back."""

    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[bool, str | None]]:
        host.query_one(ProjectView).active = "tab-board"
        await pilot.pause()
        panel = host.query_one("#board-panel", BoardPanel)
        panel.query_one("#tasks").focus()
        await pilot.pause()
        states = []
        for _ in range(3):
            await pilot.press("b")
            await pilot.pause()
            focused = host.focused
            states.append(
                (
                    panel.query_one("#board-side").display,
                    focused.id if focused is not None else None,
                )
            )
        return states

    assert drive(project, scenario) == [(False, "feed"), (True, "tasks"), (False, "feed")]


class TwoProjects(App[None]):
    """Two Project views mounted at once, the second shown — the fleet UI keeps one per project
    ever selected, so every id inside a board is on the screen once per project."""

    def __init__(self, first: ProjectInfo, second: ProjectInfo) -> None:
        super().__init__()
        self._projects = (first, second)

    def compose(self) -> ComposeResult:
        with ContentSwitcher(initial="project-b"):
            for project, view_id in zip(self._projects, ("project-a", "project-b"), strict=True):
                yield ProjectView(project, id=view_id, refresh_seconds=60.0)


def test_a_board_line_resizes_its_own_project_board_and_restores_in_the_tab(
    project: ProjectInfo, tmp_path: Path, isolated_home: Path
) -> None:
    """Each line finds what it sizes beside itself. Looked up across the screen, the shown
    board's line sized the FIRST project's hidden ``#board-side`` and left its own alone."""
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = team_service.activate(other_dir)
    isolated_home.mkdir(parents=True, exist_ok=True)
    (isolated_home / "state.json").write_text(json.dumps({COLUMN_WIDTH_KEY: 60}))

    async def run() -> dict[str, object]:
        host = TwoProjects(project, other)
        async with host.run_test(size=(120, 50)) as pilot:
            first, second = host.query_one("#project-a"), host.query_one("#project-b")
            for view in (first, second):
                assert isinstance(view, ProjectView)
                view.active = "tab-board"
            await settle(pilot)
            for divider in second.query("#side-divider, #detail-divider"):
                await divider.wait_for_refresh()  # the restore queued at mount has run
                await divider.wait_for_refresh()  # and the layout it asked for is done

            def sizes(view: Widget) -> tuple[object, object]:
                return (
                    view.query_one("#board-side").styles.width,
                    view.query_one("#detailwrap").styles.height,
                )

            seen: dict[str, object] = {
                "restored": second.query_one("#board-side").outer_size.width,
                "first_before": sizes(first),
            }
            for kind, x, y in (
                (events.MouseDown, second.query_one("#side-divider").region.x, 10),
                (events.MouseMove, 70, 10),
                (events.MouseUp, 70, 10),
                (events.MouseDown, 40, second.query_one("#detail-divider").region.y),
                (events.MouseMove, 40, 30),
                (events.MouseUp, 40, 30),
            ):
                host.post_message(kind(None, x, y, 0, 0, 1, False, False, False))
                await host.wait_for_refresh()
                await settle(pilot)
            side = second.query_one("#board-side")
            seen["second"] = (
                side.outer_size.width + side.region.x,
                second.query_one("#detailwrap").outer_size.height,
            )
            seen["first_after"] = sizes(first)
            for divider in second.query(Divider):
                # This bare app joins no saver at quit, as the fleet UI's does: wait here.
                saver = divider._autosave
                assert saver is not None and await asyncio.to_thread(saver.settled, 5.0)
            return seen

    seen = asyncio.run(run())
    assert seen["restored"] == 60, "the width on file, shown in the fleet UI's Board tab"
    # The board starts where the tab does; its column now ends at the pointer's column 70,
    # and its detail pane runs from row 31 to the bottom of the 50-row terminal.
    assert seen["second"] == (70, 50 - 31)
    assert seen["first_after"] == seen["first_before"], "the hidden project's board is untouched"
    state = json.loads((isolated_home / "state.json").read_text())
    assert DETAIL_HEIGHT_KEY in state and state[COLUMN_WIDTH_KEY] != 60


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
    monkeypatch.setattr(
        team_service,
        "board_read",
        lambda **kwargs: team_service.BoardRead(project, sessions, [], [], {}),
    )
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

    def board_read(*args: Any, **kwargs: Any) -> Any:
        return team_service.BoardRead(project, [], [], list(events), {})

    monkeypatch.setattr(team_service, "board_read", board_read)

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


# --- no doctor here ------------------------------------------------------------------------


def test_a_project_view_runs_no_doctor_whatever_tab_is_opened(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The doctor runs at start and when its page opens, never per project or per tab: the
    project view's own Doctor tab used to run it on every activation."""
    runs: list[Path | None] = []

    def doctor(*, cwd: Path | None = None) -> list[DoctorCheck]:
        runs.append(cwd)
        return []

    monkeypatch.setattr(diagnostics, "doctor", doctor)

    async def scenario(pilot: Pilot[None], host: Host) -> list[str]:
        view = host.query_one(ProjectView)
        opened = []
        for tab in (*ProjectView.TAB_IDS, "tab-manager"):
            view.active = tab
            await settle(pilot)
            opened.append(view.active)
        return opened

    opened = drive(project, scenario)
    assert opened == [*ProjectView.TAB_IDS, "tab-manager"], "the premise: every tab was opened"
    assert runs == []


# --- the Settings tab -----------------------------------------------------------------------


def test_settings_holds_the_nickname_and_shows_the_codename_without_a_rename(
    project: ProjectInfo,
) -> None:
    """The project's own settings only: its nickname, editable, and its codename as a line —
    the machine-wide form is App settings."""
    named = project.model_copy(update={"codename": "amber-otter", "nickname": "Auth work"})

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[str, str, str, list[str], str]:
        form = SettingsView(named, id="probe-settings")
        await host.mount(form)
        await pilot.pause()
        field = form.query_one("#nickname", Input)
        line = shown(form.query_one("#fleet-id", Static))
        machine = [
            wid
            for wid in ("#codename", "#rename-codename", "#role-perm-coder", "#accounts-pick")
            if form.query(wid)
        ]
        note = shown(form.query_one("#settings-note", Static))
        return field.value, str(field.placeholder), line, machine, note

    value, placeholder, line, machine_fields, note = drive(project, scenario)
    assert value == "Auth work" and placeholder == project.root.name
    assert line == (
        "fleet id: amber-otter — tmux session asq-amber-otter, branches fleet/amber-otter/…"
    )
    assert machine_fields == [], "no codename rename, and nothing machine-wide"
    assert "aisquare project rename sets the same" in note
    assert "`" not in note, "the note is a Text: backticks would show as they are"


def test_settings_saves_a_nickname_and_a_blank_one_clears_it(project: ProjectInfo) -> None:
    """Enter saves what the field holds through ``set_nickname`` — what ``project rename``
    does — the title follows, and the app is told; a blank field clears it."""
    from aisquare.cli.ui.sidebar import ProjectRenamed
    from aisquare.core.store import store_session

    renamed: list[str] = []

    async def scenario(pilot: Pilot[None], host: Host) -> list[object]:
        host.query_one(ProjectView).active = "tab-settings"
        await settle(pilot)
        form = host.query_one(SettingsView)
        original = form.post_message

        def recorded(message: Any) -> bool:
            if isinstance(message, ProjectRenamed):
                renamed.append(message.project.id)
            return original(message)

        form.post_message = recorded  # type: ignore[method-assign]
        field = host.query_one("#nickname", Input)
        seen: list[object] = []
        field.value = "  Auth [wip] 🚀  "
        await pilot.click("#nickname")
        await pilot.press("enter")
        await settle(pilot)
        with store_session() as store:
            seen.append(store.get_project(project.id).nickname)  # type: ignore[union-attr]
        seen.append(field.value)
        seen.append(shown(host.query_one("#settings-title", Static)))
        field.value = ""
        host.query_one("#save-nickname", Button).press()
        await settle(pilot)
        with store_session() as store:
            seen.append(store.get_project(project.id).nickname)  # type: ignore[union-attr]
        seen.append(shown(host.query_one("#settings-title", Static)))
        seen.append(host.notices)
        return seen

    saved, field_value, title, cleared, title_after, notices = drive(project, scenario)
    assert saved == "Auth [wip] 🚀" and field_value == "Auth [wip] 🚀"  # trimmed, kept as typed
    assert title == "project settings — Auth [wip] 🚀"
    assert cleared is None
    assert title_after == f"project settings — {project.root.name}"
    assert renamed == [project.id, project.id], "the app hears of both, to repaint the name"
    assert isinstance(notices, list)
    assert any("is now called Auth [wip] 🚀" in m for m, _ in notices), notices
    assert any("shows its folder name again" in m for m, _ in notices), notices


def test_a_nickname_the_store_refuses_is_said_and_nothing_is_written(
    project: ProjectInfo,
) -> None:
    from aisquare.core.store import store_session

    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, str]]:
        host.query_one(ProjectView).active = "tab-settings"
        await settle(pilot)
        host.query_one("#nickname", Input).value = "two\tparts"
        host.query_one("#save-nickname", Button).press()
        await settle(pilot)
        return host.notices

    notices = drive(project, scenario)
    assert any(
        m.startswith("the nickname was not saved: a nickname is one line") and s == "error"
        for m, s in notices
    ), notices
    with store_session() as store:
        assert store.get_project(project.id).nickname is None  # type: ignore[union-attr]


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
            # Off is the default, which the file does not hold.
            _config_toml().get("explainability", {}).get("enabled", False),
            host.notices,
        )

    enabled, status, disabled, notices = drive(project, scenario)
    assert enabled is True  # the artefact: config.toml on disk
    assert "enabled:   on" in status  # …and the tab re-read it through the services
    assert disabled is False
    assert quiet_explainability["shipping_state"] >= 3  # mount, after enable, after disable
    assert any(m.startswith("✓ tracing enabled") for m, _ in notices)
    assert any(m.startswith("✓ tracing disabled") for m, _ in notices)
    assert load_config().explainability.target == "stg"  # nothing else was touched


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


@pytest.mark.parametrize("above", [None, ".aisquare", ".git"])
def test_the_explainability_tab_attaches_a_key_to_the_active_project_without_echoing_it(
    project: ProjectInfo, quiet_explainability: dict[str, int], work_dir: Path, above: str | None
) -> None:
    """#141: the shell-only gap — a key per project, from the UI. The value goes to a
    mode-600 file and a binding row, not to the machine file; the toast names the
    path, never the key.

    ``above``: a project marker or a repository appears above the project's directory
    after the fixture resolved it and before *Save setup* resolves it again — what a
    full run's shared temp tree may do. The key still lands under the page's project
    (``work_dir``)."""
    if above == ".git":
        git(work_dir.parent, "init", "-q")
    elif above is not None:
        (work_dir.parent / above).mkdir()

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


def test_save_setup_asks_git_and_writes_the_key_off_the_ui_thread(
    project: ProjectInfo, quiet_explainability: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """*Save setup* ran ``key_project`` (``git rev-parse``, 5 s timeouts) and the key's
    store and file writes in the button's handler, where a slow git froze the whole
    app (review of #170, B5/G6). The form's fields are still read on the UI thread."""
    from aisquare.cli.ui.views import explainability as explainability_view

    on_ui_thread: dict[str, list[bool]] = {"git": [], "write": []}
    resolve, write = explainability_view.key_project, ops.attach_project_key

    def key_project(page: ProjectInfo | None) -> ProjectInfo | None:
        on_ui_thread["git"].append(threading.current_thread() is threading.main_thread())
        return resolve(page)

    def attach(*args: Any, **kwargs: Any) -> Any:
        on_ui_thread["write"].append(threading.current_thread() is threading.main_thread())
        return write(*args, **kwargs)

    monkeypatch.setattr(explainability_view, "key_project", key_project)
    monkeypatch.setattr(ops, "attach_project_key", attach)

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[list[tuple[str, str]], str]:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        _attach_in_setup(host, "pk-worker-0123456789")
        await settle(pilot)
        return list(host.notices), host.query_one("#explainability-key", Input).value

    notices, field = drive(project, scenario)
    # The status worker asks `key_project` too, off the UI thread; none of the asks is on it.
    assert on_ui_thread["git"] and not any(on_ui_thread["git"]), on_ui_thread
    assert on_ui_thread["write"] == [False]
    assert any(m.startswith("✓ key attached to") for m, _ in notices), notices
    assert field == "", "cleared once the write began"


def test_a_config_write_made_while_save_setup_runs_is_not_saved_over(
    project: ProjectInfo, quiet_explainability: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """In its worker, *Save setup* read the config, asked git (seconds), then saved the copy
    it had read. *Disable* pressed meanwhile wrote ``enabled = false`` and said "✓ tracing
    disabled", and the save put ``true`` back (review of #170's follow-ups, round 1, F1).
    Enable and Disable are off while it runs, and the config is read once git has
    answered, so another writer's change in that time (here the CLI's ``disable`` in a
    shell) is kept."""
    from aisquare.cli.ui.views import explainability as explainability_view

    config = load_config()
    config.explainability.enabled = True
    save_config(config)
    resolve = explainability_view.key_project
    armed, asked, release = threading.Event(), threading.Event(), threading.Event()

    def slow_git(page: ProjectInfo | None) -> ProjectInfo | None:
        if armed.is_set() and threading.current_thread() is not threading.main_thread():
            armed.clear()
            asked.set()
            release.wait(10)
        return resolve(page)

    monkeypatch.setattr(explainability_view, "key_project", slow_git)
    switches = ("#explainability-enable", "#explainability-disable")

    async def scenario(
        pilot: Pilot[None], host: Host
    ) -> tuple[list[bool], list[bool], list[tuple[str, str]]]:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        armed.set()
        _attach_in_setup(host, "pk-race-0123456789")
        try:
            for _ in range(500):
                if asked.is_set():
                    break
                await pilot.pause(0.01)
            assert asked.is_set(), "the save never asked git"
            during = [host.query_one(switch, Button).disabled for switch in switches]
            host.query_one("#explainability-disable", Button).press()
            await pilot.pause()
            elsewhere = load_config()
            elsewhere.explainability.enabled = False
            save_config(elsewhere)
        finally:
            release.set()
        await settle(pilot)
        after = [host.query_one(switch, Button).disabled for switch in switches]
        return during, after, list(host.notices)

    during, after, notices = drive(project, scenario)
    assert during == [True, True], "the switches are off while the save runs"
    assert after == [False, False], "and on again once it answered"
    assert not any("tracing disabled" in m for m, _ in notices), notices
    assert any(m.startswith("✓ key attached to") for m, _ in notices), notices
    assert load_config().explainability.enabled is False, "the write made meanwhile is kept"


def test_a_key_the_attach_could_not_put_back_is_named_on_the_form(
    project: ProjectInfo, quiet_explainability: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attach's put-back failed, and the writer's note said what the key file holds
    now and what to delete. The form rendered the error with ``f"{exc}"``, which leaves
    the note out (review of #170's follow-ups, round 1, F3)."""
    ops.attach_project_key(
        project, "pk-earlier-0123456789", target=load_config().explainability.target
    )
    writes = explainability_service.store_project_api_key

    def refuse(self: SqliteStore, *_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    def full_disk_for_the_put_back(project_id: str, key: str) -> Path:
        if key == "pk-earlier-0123456789":
            raise OSError(28, "No space left on device")
        return writes(project_id, key)

    monkeypatch.setattr(SqliteStore, "set_project_explainability", refuse)
    monkeypatch.setattr(ops, "store_project_api_key", full_disk_for_the_put_back)

    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, str]]:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        _attach_in_setup(host, "pk-refused-0123456789")
        await settle(pilot)
        return list(host.notices)

    notices = drive(project, scenario)
    [failed] = [m for m, s in notices if s == "error" and "could not be attached" in m]
    assert "database is locked" in failed
    assert "could not be put back as it was" in failed and "it was removed" in failed


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


def test_make_active_does_not_make_a_typo_a_known_deployment_for_the_key(
    project: ProjectInfo, quiet_explainability: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The known-target check ran after ``configure_target`` had made the typed name the
    machine's target, so with 'make active' ticked a typo was in ``known`` and passed:
    the machine moved to a target with no entry and the key was bound to it (review of
    #170's Setup-form merge, G1). A blank field whose deployment an exported variable
    names is the same question (G2)."""

    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, str]]:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        host.query_one("#explainability-switch", Checkbox).value = True
        _attach_in_setup(host, "pk-typo-0123456789", target="prdo")
        await settle(pilot)
        host.query_one("#explainability-switch", Checkbox).value = False
        host.query_one("#explainability-target", Input).value = ""
        monkeypatch.setenv(ops.TARGET_ENV_VAR, "prdo")
        _attach_in_setup(host, "pk-typo-0123456789")
        await settle(pilot)
        return list(host.notices)

    notices = drive(project, scenario)
    refused = [m for m, _ in notices if m.startswith("no target 'prdo' on this machine")]
    assert len(refused) == 2, notices
    assert f"named by ${ops.TARGET_ENV_VAR}" in refused[1]
    assert load_config().explainability.target == "stg", "the machine did not move to the typo"
    with store_session() as store:
        assert store.project_explainability(project.id) is None
    assert not explainability_service.project_key_path(project.id).exists()


def test_make_active_does_not_make_a_typo_known_beside_a_destination(
    project: ProjectInfo, quiet_explainability: dict[str, int]
) -> None:
    """G1 again, on the path a destination (#142) adds: its deployment joined the known
    names through ``known_targets`` on the config ``configure_target`` had just changed,
    so with 'make active' ticked the typed name was the machine's target and passed. The
    machine moved to ``prdo``, which has no entry, and the key was bound to it under two
    success toasts (final review of #203, EX2)."""
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

    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, str]]:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        host.query_one("#explainability-switch", Checkbox).value = True
        _attach_in_setup(host, "pk-typo-0123456789", target="prdo")
        await settle(pilot)
        return list(host.notices)

    notices = drive(project, scenario)
    assert any(m.startswith("no target 'prdo' on this machine") for m, _ in notices), notices
    assert not any("setup saved" in m for m, _ in notices), notices
    assert load_config().explainability.target == "stg", "the machine did not move to the typo"
    with store_session() as store:
        assert store.project_explainability(project.id) is None
    assert not explainability_service.project_key_path(project.id).exists()


def test_a_prefix_no_header_carries_is_refused_with_advice_the_form_can_follow(
    isolated_home: Path,
) -> None:
    """A full name in the prefix field (``arbind kumar``) is refused by the writer, whose
    sentence the form showed as it is. It ended "try 'name-{role}'", and the prefix field
    refuses braces, so the advice could not be followed there (review of the #203
    final-review fixes, F5). Then it named a rendered agent name, ``arbind.kumar-planner``,
    which typed as the prefix named every agent ``arbind.kumar-planner-<role>`` (round 2,
    F3). It names the prefix to type, and typed, the agents are named as meant."""
    from aisquare.cli.ui.views import explainability as explainability_view

    def save(prefix: str) -> explainability_view.SetupOutcome:
        form = explainability_view.SetupForm(
            target="", switch=False, gateway="", proxy="", prefix=prefix, key_env="",
            key="", own=False,
        )  # fmt: skip
        return explainability_view.save_setup(form, None)

    (notice,) = save("arbind kumar").notices
    assert notice.severity == "warning" and "cannot travel in a header" in notice.message
    assert "{" not in notice.message, notice.message
    assert notice.message.endswith("try 'arbind.kumar'"), notice.message
    assert load_config().explainability.targets == {}
    assert save("arbind.kumar").began
    identity = ops.resolve_target(load_config().explainability).agent_name_template
    assert identity == "arbind.kumar-{role}"


def test_a_key_the_projects_launches_do_not_resolve_is_not_called_the_one_they_use(
    project: ProjectInfo, quiet_explainability: dict[str, int]
) -> None:
    """Typed for prod with 'make active' unticked, on a machine that stays on stg, the key
    is bound to prod and the launches keep resolving stg. The toast said "launches in
    this project authenticate the proxy with it" (review of #170's Setup-form merge,
    G3)."""

    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, str]]:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        _attach_in_setup(host, "pk-prod-0123456789", target="prod", gateway="https://p.example")
        await settle(pilot)
        return list(host.notices)

    notices = drive(project, scenario)
    [(attached, severity)] = [(m, s) for m, s in notices if m.startswith("✓ key attached to")]
    assert "authenticate the proxy with it" not in attached, attached
    assert "not used by this project's launches, which resolve target stg" in attached
    assert "tick 'make active'" in attached and severity == "warning"


def test_the_save_again_the_attach_asks_for_is_not_refused(
    project: ProjectInfo, quiet_explainability: dict[str, int]
) -> None:
    """The notice says "tick 'make active' and save again". The save had cleared the key
    and left 'this project only' ticked, so that press met the refusal of a ticked box
    with no key (review of #170's follow-ups, round 1, F2). The box is unticked with the
    key it attached."""

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[bool, list[tuple[str, str]]]:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        _attach_in_setup(host, "pk-prod-0123456789", target="prod", gateway="https://p.example")
        await settle(pilot)
        box = host.query_one("#explainability-key-project", Checkbox).value
        host.query_one("#explainability-switch", Checkbox).value = True
        host.query_one("#explainability-save", Button).press()
        await settle(pilot)
        return box, list(host.notices)

    box, notices = drive(project, scenario)
    assert box is False, "unticked with the key it attached"
    assert not any("attaches the workspace key typed beside it" in m for m, _ in notices), notices
    assert load_config().explainability.target == "prod", "the machine moved, as the notice said"


def test_this_project_only_with_no_key_is_refused_not_ignored(
    project: ProjectInfo, quiet_explainability: dict[str, int]
) -> None:
    """The box ticked and the key blank: the box was dropped without a word and the other
    settings saved as the machine's (review of #170's Setup-form merge, G4)."""

    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, str]]:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        _attach_in_setup(host, "", target="stg", gateway="https://g.example")
        await settle(pilot)
        return list(host.notices)

    notices = drive(project, scenario)
    assert any(
        m.startswith("'this project only' attaches the workspace key") and s == "warning"
        for m, s in notices
    ), notices
    assert "stg" not in load_config().explainability.targets, "nothing was saved"


def test_a_machine_key_beside_its_own_variable_names_the_box_that_would_work(
    project: ProjectInfo, quiet_explainability: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a page, a project's own key is the fix that works beside a custom key variable,
    and the refusal did not say so (review of #170's Setup-form merge, G8)."""
    config = load_config()
    config.explainability.targets = {
        "stg": ExplainabilityTarget(gateway_url="https://stg.example", api_key_env="MY_KEY"),
    }
    save_config(config)
    monkeypatch.delenv("MY_KEY", raising=False)

    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, str]]:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        host.query_one("#explainability-key", Input).value = "AIS_machine_0123456789"
        host.query_one("#explainability-save", Button).press()
        await settle(pilot)
        return list(host.notices)

    notices = drive(project, scenario)
    [refused] = [m for m, _ in notices if "reads its key from $MY_KEY" in m]
    assert "tick 'this project only'" in refused


def test_under_a_hub_the_box_and_the_tab_say_whose_key_it_is(
    project: ProjectInfo,
    quiet_explainability: dict[str, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under AISQUARE_TEAM_HUB the key goes to the hub project, which every project under
    the hub launches with, and the box read "this project only" (G5). A key the page
    had of its own vanished from the tab while its file stayed on disk (B7)."""
    ops.attach_project_key(project, "pk-page-0123456789", target="stg")  # the page's own
    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub))

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[str, list[tuple[str, str]], str]:
        host.query_one(ProjectView).active = "tab-explainability"
        await settle(pilot)
        label = str(host.query_one("#explainability-key-project", Checkbox).label)
        _attach_in_setup(host, "pk-hub-0123456789")
        await settle(pilot)
        _attach_in_setup(host, "", gateway="https://g.example")  # the box, and no key
        await settle(pilot)
        return label, list(host.notices), host.query_one(ExplainabilityView).status_text

    label, notices, status = drive(project, scenario)
    assert label == "the hub (hub) only", label
    assert any(m.startswith("'the hub (hub) only' attaches") for m, _ in notices), notices
    [attached] = [m for m, _ in notices if m.startswith("✓ key attached to")]
    assert attached.startswith("✓ key attached to hub (the AISQUARE_TEAM_HUB project"), attached
    assert "its own key for target stg is not used — its launches join hub" in status, status
    assert f"key clear --project {project.id}" in status


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
    from aisquare.core.workspace import pin_project

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
