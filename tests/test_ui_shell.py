"""The fleet UI shell: sidebar, content switching, focus model, theme, fail-open.

docs/plans/fleet-tui.md §4, §4.1, §4.3. Driven headless with ``App.run_test``
and a ``Pilot`` at 140x40, as ``test_watch.py`` drives the board. The store is
the real one in the isolated home (projects are seeded through
``store_session``); the fleet service's ``list_agents`` is scripted per test,
because the fleet's lifecycle is another work package and this file is about
what the shell does with whatever it is handed.

Every assertion reads what the widget SHOWS (``visual.plain``), not the string
that was passed in — CONTRIBUTING's rule about asserting the artefact the claim
is about — and every behaviour has a control in the other direction: the row
that must NOT show the chip, the key that must NOT quit, the bell that must NOT
ring on the first frame.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

import pytest
from textual import Logger, events
from textual._xterm_parser import XTermParser
from textual.app import App, ComposeResult, ScreenStackError
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.geometry import Region
from textual.pilot import Pilot
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Input, OptionList, Static, Switch, Tabs
from textual.widgets._toast import Toast
from textual.worker import Worker, WorkerState

from aisquare.cli.ui import app as app_mod
from aisquare.cli.ui import app as ui_app
from aisquare.cli.ui.app import SIDEBAR_WIDTH_KEY, FleetApp, HeadroomReader, HelpScreen, Panes
from aisquare.cli.ui.autosave import Autosave
from aisquare.cli.ui.click_guard import GuardedButton
from aisquare.cli.ui.divider import WIDEST_ASK, Divider, cells
from aisquare.cli.ui.groups import DropProject, GroupPicker
from aisquare.cli.ui.remote_control import RemoteController
from aisquare.cli.ui.sidebar import (
    RESIZE_STEP,
    STATE_CHIP,
    STOP_STATES,
    Activatable,
    AgentRow,
    CardChips,
    Disclosure,
    GroupHeader,
    ProjectCard,
    ProjectTitle,
    SectionLabel,
    SettingsRow,
    ordered_agents,
    short_path,
)
from aisquare.cli.ui.stop import StopAgentScreen
from aisquare.cli.ui.terminal import (
    DUPLICATE_PRESS_WINDOW,
    EscapeToSidebar,
    SelectionHost,
    TerminalPane,
    route_selection_gesture,
)
from aisquare.cli.ui.theme import ThemePicker
from aisquare.cli.ui.views import explainability as explainability_view
from aisquare.cli.ui.views.agent import AgentView
from aisquare.cli.ui.views.doctor import DoctorRefreshed, DoctorView
from aisquare.cli.ui.views.explainability import ExplainabilityView
from aisquare.cli.ui.views.onboard import OnboardFailed, ProjectOnboarded
from aisquare.cli.ui.views.project import ManagerTab, ProjectView
from aisquare.cli.watch import _load_saved_theme
from aisquare.core import state_file
from aisquare.core import tmux as tmux_core
from aisquare.core.atomic import write_replacing
from aisquare.core.config import load_config, save_config
from aisquare.core.locking import lock_exclusive, unlock
from aisquare.core.state_file import update_state
from aisquare.core.store import ContextStore, SqliteStore, store_session
from aisquare.core.tmux import Completed, TmuxServer
from aisquare.models import (
    CheckStatus,
    ClaudeAccount,
    DoctorCheck,
    FleetAgent,
    FleetAgentStatus,
    HeadroomSummary,
    LaunchSpec,
    ProjectInfo,
    TeamSession,
)
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import explainability as explainability_service
from aisquare.services import fleet as fleet_service
from aisquare.services import project_groups as groups_service
from aisquare.services.captain import state as captain_state
from tests import guard_clock, toast_log
from tests.fleet_fakes import list_agents_as
from tests.pane_harness import FakePane, FakeTmux, asks_a_server, move, press, release, socket_of
from tests.ui_workers import settle_page, settle_until_held, watch_until

T = TypeVar("T")
SIZE = (140, 40)
T0 = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
PRIVATE_SOCKET = f"asq-test-{os.getpid()}-ui-shell"
"""A socket nobody serves. ``FleetAgent.tmux_socket`` defaults to ``asq`` — the
fleet's REAL socket — and clicking an agent row mounts an ``AgentView`` whose
``TerminalPane`` captures and RESIZES that pane for real. Scripted rows carry
this instead, and ``no_real_tmux`` below refuses anything else."""
Script = dict[str, list[FleetAgentStatus]]


# --- fixtures and helpers -------------------------------------------------------------


def seed(tmp_path: Path, *specs: tuple[str, str, str | None]) -> list[ProjectInfo]:
    """Register projects ``(id, root relative to tmp_path, codename)`` in the isolated store."""
    projects: list[ProjectInfo] = []
    with store_session() as store:
        for project_id, rel, codename in specs:
            project = ProjectInfo(id=project_id, root=tmp_path / rel)
            project = store.onboard_project(project)  # added on purpose: shown (#139)
            if codename:
                project = store.set_codename(project_id, codename)
            projects.append(project)
    return projects


def status(
    project_id: str,
    label: str,
    role: str,
    state: str,
    *,
    minute: int = 0,
    exit_status: int | None = None,
) -> FleetAgentStatus:
    """A scripted agent; the id is derived so a test can address its row. A ``captain`` row
    is launched as the captain's start launches it (``fleet.is_the_captains_row``)."""
    spec = (
        LaunchSpec(binary="claude", extra_args=["--strict-mcp-config", "--tools", ""])
        if role == "captain"
        else None
    )
    agent = FleetAgent(
        id=f"agt_{project_id.removeprefix('prj_')}_{label}",
        project_id=project_id,
        label=label,
        role=role,
        pane_id="%1",
        tmux_socket=PRIVATE_SOCKET,  # never "asq", the developer's own fleet
        cwd=Path("/w"),
        created_at=T0 + timedelta(minutes=minute),
        ended_at=T0 + timedelta(minutes=minute + 1) if exit_status is not None else None,
        exit_status=exit_status,
        launch_spec=spec,
    )
    return FleetAgentStatus(agent=agent, state=state)


@pytest.fixture(autouse=True)
def no_real_tmux(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, ...]]]:
    """Every tmux command this file causes must address :data:`PRIVATE_SOCKET`.

    Clicking an agent row mounts an ``AgentView`` whose pane builds
    ``TmuxServer(status.agent.tmux_socket)`` and then really captures and
    RESIZES that pane — so a scripted row on the default ``asq`` would reach
    into the developer's live fleet (measured: three tests here issue
    ``capture-pane`` and ``resize-window``). The runner is replaced, and what
    it was asked to run is read AFTER the test: an assertion raised inside a
    frame would be swallowed by ``TerminalPane.refresh_frame``, so the recorder
    is the guard rather than an assert in the seam.
    """
    ran: list[tuple[str, ...]] = []

    def record(argv: Sequence[str], stdin: bytes | None) -> Completed:
        ran.append(tuple(argv))
        return Completed(1, "", "no server running (a UI test addresses no real fleet)\n")

    monkeypatch.setattr(tmux_core, "_tmux", record)
    yield ran
    wrong = [argv for argv in ran if asks_a_server(argv) and socket_of(argv) != PRIVATE_SOCKET]
    assert not wrong, f"a UI test addressed a tmux socket that is not the test's: {wrong[:2]}"


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> Script:
    """What ``fleet_service.list_agents`` answers, per project id — mutable mid-test."""
    agents: Script = {}

    def fake(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        return list(agents.get(project.id, []))

    list_agents_as(monkeypatch, fake)
    return agents


def drive(
    fn: Callable[[Pilot[None]], Awaitable[T]],
    *,
    doctor: Callable[[], list[DoctorCheck]] | None = None,
    notifications: bool = False,
    size: tuple[int, int] = SIZE,
    headroom: HeadroomReader | None = None,
) -> T:
    """Run ``fn`` against a mounted ``FleetApp`` (no timer refresh; a stub doctor).

    ``notifications`` opts the screen's ``ToastRack`` in — ``run_test`` leaves it
    out by default, and without it a ``notify`` goes nowhere to be read.
    ``size`` is the terminal's; a test about a laptop passes a narrow one.
    ``headroom`` stands in for the slow usage read; the app's own when not given.

    The start-up doctor run has been painted before ``fn`` starts. It is a thread
    worker, and a test that ended before its result was handled had that result
    handled while ``run_test`` tore the app down (``run_test`` shuts the app down
    beside its running message loop): ``show_doctor`` then queried a screen that
    was already gone, and the test failed on a ``NoMatches`` it never asked about.
    """

    async def run() -> T:
        app = (
            FleetApp(refresh_seconds=3600, doctor=doctor or (lambda: []))
            if headroom is None
            else FleetApp(refresh_seconds=3600, doctor=doctor or (lambda: []), headroom=headroom)
        )
        async with app.run_test(size=size, notifications=notifications) as pilot:
            await pilot.pause()
            await settle(app)
            return await fn(pilot)

    return asyncio.run(run())


def shown(widget: Static) -> str:
    """The text a widget renders — the artefact, not the argument."""
    visual = widget.visual
    plain = getattr(visual, "plain", None)
    assert isinstance(plain, str), f"{widget!r} renders a {type(visual).__name__}, not text"
    return plain


def composited(widget: Static) -> str:
    """The one strip Textual composites for a ``height: 1`` row — what the eye gets.

    ``shown`` reads the widget's visual, which is the WHOLE text it was handed; a
    row that wraps and clips shows less than that, and only the strip sees it.
    """
    (strip,) = widget.render_lines(Region(0, 0, widget.size.width, 1))
    return strip.text


async def settle(app: FleetApp) -> None:
    """Let the app go quiet: nothing queued on it or its screen, every worker of ours done.

    This was ``workers.wait_for_complete()`` over the workers that existed when it
    was called, and that lost to three things a loaded runner does:

    - after ``Button.press()`` the Save handler may not have run yet, so there was
      no worker to wait for and the test read the form before the save began;
    - a worker's result reaches the page as a ``StateChanged`` the view handles
      later, and its toast is further off still: ``notify`` posts to the app, the
      app queues the toast rack's ``show``, and ``show`` mounts the toast. The
      wait returned when the worker did, and on windows-latest
      ``test_a_malformed_gateway_does_not_take_the_ui_down`` found no toast
      (pull_request run 36118978773, first attempt);
    - it RAISES for a worker that errored while still registered, and a crashing
      doctor is an outcome a test reads.

    ``settle_page`` goes round until a pause ends with nothing queued and no worker
    of ours running, which covers all three. It leaves Textual's ``_loader`` alone,
    as this did: the Onboard view's ``DirectoryTree`` keeps one for its whole life.
    """
    await settle_page(app)


async def refreshed(app: FleetApp) -> None:
    """A refresh, as the two-second timer runs it, and its frame painted.

    ``refresh_data`` reads the fleet in a thread worker, and one ``pilot.pause()``
    after it can return before that read has painted: on windows-latest the
    captain's quick action was still offered after its row had turned ``exited``
    (CI run 39863195249). The test went on reading the frame before. Only the fleet
    read is waited for, so a worker a test holds on purpose (the captain view's bar)
    does not hold this up.
    """
    app.refresh_data()
    await settle_page(app, group=app_mod.FLEET_WORKER)


async def _toast_list(app: FleetApp) -> list[str]:
    """Every toast on screen once the app has gone quiet, oldest first.

    A toast is the last hop of a chain, so it is read after ``settle``, never
    after a pause: whatever raised it (a handler, a worker's result, a saver's
    refusal) calls ``notify``, which posts to the app; the app queues the toast
    rack's ``show``; and ``show`` mounts the toast. A read after one pause, or
    after a wait for the worker alone, found no toast on windows-latest.
    """
    await settle(app)
    return [toast.render().plain for toast in app.screen.query(Toast)]


async def _toasts(app: FleetApp) -> str:
    """Every toast on screen once the app has gone quiet, oldest first, as one string —
    one save can raise more than one."""
    return " | ".join(await _toast_list(app))


async def open_doctor(pilot: Pilot[None]) -> None:
    """Open the Doctor page as a person now does: *⚙ Settings*, then the hub's *Doctor*."""
    app = fleet_app(pilot)
    await pilot.click(app.query_one(SettingsRow))
    await settle(app)
    await pilot.click("#hub-doctor")
    await settle(app)


def fleet_app(pilot: Pilot[None]) -> FleetApp:
    app = pilot.app
    assert isinstance(app, FleetApp)
    return app


_needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")


@_needs_tmux
def test_the_no_tmux_guard_is_reachable(
    no_real_tmux: list[tuple[str, ...]], tmp_path: Path, script: Script
) -> None:
    """The guard SEES the pane's tmux calls — one no test reaches protects nothing.

    Skipped without tmux, as every other tmux-dependent test here is: on such a
    machine ``TmuxServer.binary()`` raises ``TmuxUnavailable`` at
    ``shutil.which("tmux")``, before an argv exists, the pane fails open to
    ``(tmux unavailable)`` and the recorder is handed nothing. That is the guard
    being unreachable, not the socket rule being wrong — and asserting it there
    made this the one test in the fleet's set that FAILED instead of skipping
    (measured on a PATH without tmux: ``1 failed, 28 passed``). The rule's other
    half is checked below and needs no tmux at all.
    """
    seed(tmp_path, ("prj_a", "alpha", "amber-otter"))
    script["prj_a"] = [status("prj_a", "coder-1", "coder", "working")]

    async def go(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        await pilot.click(row_for(app, "agt_a_coder-1"))
        await settle(app)

    drive(go)

    assert no_real_tmux, "no tmux call recorded — the guard inspects nothing here"
    served = [argv for argv in no_real_tmux if asks_a_server(argv)]
    assert served, "no tmux SERVER was addressed — the guard inspects nothing here"
    assert {socket_of(argv) for argv in served} == {PRIVATE_SOCKET}


def test_the_no_tmux_guard_rejects_the_real_fleets_socket() -> None:
    """The negative half, on the rule itself — and it must still SEE a good argv."""
    assert socket_of(("tmux", "-L", "asq", "capture-pane")) != PRIVATE_SOCKET
    assert socket_of(("tmux", "-L", PRIVATE_SOCKET, "capture-pane")) == PRIVATE_SOCKET
    assert socket_of(("tmux", "-V")) is None  # an argv naming no socket is not the test's…
    assert not asks_a_server(("tmux", "-V"))  # …and a version query reaches no server to guard
    assert asks_a_server(("tmux", "-L", "asq", "capture-pane"))
    assert FleetAgent.model_fields["tmux_socket"].default == "asq" != PRIVATE_SOCKET


def card_for(app: FleetApp, project_id: str) -> ProjectCard:
    return app.query_one(f"#card-{project_id}", ProjectCard)


def row_for(app: FleetApp, agent_id: str) -> AgentRow:
    return app.query_one(f"#agent-row-{agent_id}", AgentRow)


# --- pure helpers ------------------------------------------------------------------


def test_short_path_collapses_the_home_directory_only() -> None:
    home = Path("/home/me")
    assert short_path(home / "work" / "api", home) == "~/work/api"
    # "untouched" means `str(path)`, which is the platform's spelling — the `~/`
    # branch above is `as_posix()` on purpose and stays a forward-slash literal.
    outside = Path("/srv/api")
    assert short_path(outside, home) == str(outside)  # not under home: untouched


def test_ordered_agents_puts_the_manager_first_then_by_creation() -> None:
    late_manager = status("prj_a", "manager", "manager", "waiting", minute=9)
    first = status("prj_a", "coder-auth", "coder", "working", minute=1)
    second = status("prj_a", "tester-1", "tester", "working", minute=2)
    labels = [s.agent.label for s in ordered_agents([second, late_manager, first])]
    assert labels == ["manager", "coder-auth", "tester-1"]
    assert labels != ["tester-1", "manager", "coder-auth"]  # the input order did not leak


# --- the sidebar ---------------------------------------------------------------------


def test_projects_render_as_alternating_cards(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None), ("prj_c", "gamma", None))

    async def go(pilot: Pilot[None]) -> list[tuple[str, bool, bool, str]]:
        app = fleet_app(pilot)
        return [
            (
                card.project.id,
                card.has_class("even"),
                card.has_class("odd"),
                shown(card.query_one(ProjectTitle)),
            )
            for card in app.query(ProjectCard)
        ]

    cards = drive(go)
    assert [c[0] for c in cards] == ["prj_a", "prj_b", "prj_c"]  # store order (by name)
    assert [c[1] for c in cards] == [True, False, True]
    assert [c[2] for c in cards] == [False, True, False]
    assert all(even != odd for _, even, odd, _ in cards)  # never both, never neither
    assert [c[3].split()[1] for c in cards] == ["alpha", "beta", "gamma"]


def test_agent_rows_show_role_icon_state_chip_and_exit_status(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    script["prj_a"] = [
        status("prj_a", "coder-auth", "coder", "working", minute=1),
        status("prj_a", "manager", "manager", "waiting"),
        status("prj_a", "tester-1", "tester", "exited", minute=2, exit_status=3),
        status("prj_a", "scout-1", "scout", "attention", minute=3),
    ]

    async def go(pilot: Pilot[None]) -> tuple[list[str], str]:
        app = fleet_app(pilot)
        rows = [shown(row) for row in card_for(app, "prj_a").query(AgentRow)]
        return rows, shown(card_for(app, "prj_a").query_one(CardChips))

    rows, chips = drive(go)
    assert [r.split()[1] for r in rows] == ["manager", "coder-auth", "tester-1", "scout-1"]
    manager, coder, tester, scout = rows
    assert manager.startswith("🧭") and manager.rstrip().endswith("⏸")
    assert coder.startswith("🔨") and coder.rstrip().endswith("▶")
    assert tester.startswith("🧪") and tester.rstrip().endswith("💤 exited(3)")  # #138: the word
    assert scout.startswith("🤖") and "🔔" in scout  # unknown role: the custom icon
    assert "(3)" not in coder and "💤" not in coder and "exited" not in coder  # only on that row
    assert "🔔" not in manager and "▶" not in manager
    # The card's chips: three alive (the exited one is not), one needing the user.
    assert chips.rstrip().endswith("3 · 🔔1")


def on_screen(row: AgentRow) -> str:
    """The strip a row composites across its whole width, its padding included.

    :func:`composited` crops to ``size``, the content area: a padded row loses as many cells
    off its end as its padding is wide — the end being what these tests are about.
    """
    (strip,) = row.render_lines(Region(0, 0, row.outer_size.width, 1))
    return strip.text


def on_slot(row: FleetAgentStatus, slot: int) -> FleetAgentStatus:
    """``row`` spawned on account ``slot`` (``FleetAgent.account_slot``)."""
    return row.model_copy(update={"agent": row.agent.model_copy(update={"account_slot": slot})})


def session_use(*by_slot: tuple[int, float]) -> Callable[..., HeadroomSummary]:
    """A headroom read answering ``by_slot`` as each account's five-hour window USED."""

    def read(accounts: Sequence[ClaudeAccount] | None) -> HeadroomSummary:
        return HeadroomSummary(level="good", percent_left=50.0, session_by_slot=dict(by_slot))

    return read


def test_an_agent_row_gives_way_in_its_label_never_in_its_account_and_session_use(
    tmp_path: Path, script: Script
) -> None:
    """The owner's option C: ``#<slot>·<pct>%`` after the state chip — the slot number here,
    for want of room. At the default width of 30 a long label is ellipsised so the suffix is
    whole; with no figure the slot alone, with no slot nothing, and an exited row keeps its
    ``💤 exited(N)``."""
    seed(tmp_path, ("prj_a", "alpha", None))
    script["prj_a"] = [
        on_slot(status("prj_a", "manager", "manager", "waiting"), 1),
        on_slot(status("prj_a", "coder-01k8zq7mab", "coder", "working", minute=1), 3),
        on_slot(status("prj_a", "coder-auth", "coder", "working", minute=2), 4),
        status("prj_a", "scout-1", "scout", "attention", minute=3),
        on_slot(status("prj_a", "tester-1", "tester", "exited", minute=4, exit_status=3), 3),
    ]

    async def go(pilot: Pilot[None]) -> tuple[int, list[str]]:
        app = fleet_app(pilot)
        await settle(app)  # the headroom read follows the first accounts frame
        rows = [on_screen(row) for row in card_for(app, "prj_a").query(AgentRow)]
        return app.sidebar.size.width, rows

    width, rows = drive(go, headroom=session_use((1, 42.0), (3, 87.0)))
    assert width == 30
    manager, long, coder, scout, tester = (row.strip() for row in rows)
    assert manager.startswith("🧭 manager") and manager.endswith("⏸ #1·42%")
    assert long.endswith("▶ #3·87%"), long  # the whole suffix…
    assert "…" in long and "coder-01k8zq7mab" not in long  # …and the label gave way
    assert long.startswith("🔨 coder-01k8")
    assert coder.endswith("▶ #4") and "%" not in coder  # no figure for slot 4: the slot alone
    assert "#" not in scout and scout.endswith("🔔")  # no slot: nothing
    assert tester.endswith("💤 exited(3)") and "#" not in tester


def as_persona(row: FleetAgentStatus, persona: str) -> FleetAgentStatus:
    """``row`` spawned as ``persona`` (``FleetAgent.persona``)."""
    return row.model_copy(update={"agent": row.agent.model_copy(update={"persona": persona})})


@pytest.mark.parametrize("size", [(90, 40), (120, 40)])
def test_a_persona_badge_never_takes_the_session_figure_off_an_agent_row(
    tmp_path: Path, script: Script, size: tuple[int, int]
) -> None:
    """Review of #267's merge, M-F1: at the default sidebar width a row with both a slot and a
    persona read ``⏸ #1·42…`` — the badge pushed the figure under the row's ellipsis and was
    never seen itself. The owner's order: the label's padding gives way first, then the badge
    is cut (``…``), and the chip and ``#<slot>·<pct>%`` are never cut."""
    seed(tmp_path, ("prj_a", "alpha", None))
    script["prj_a"] = [
        as_persona(on_slot(status("prj_a", "manager", "manager", "waiting"), 1), "skeptic"),
        as_persona(
            on_slot(status("prj_a", "coder-auth", "coder", "working", minute=1), 3),
            "a-very-long-persona-name",
        ),
    ]

    async def go(pilot: Pilot[None]) -> tuple[int, list[str]]:
        app = fleet_app(pilot)
        await settle(app)
        rows = [on_screen(row) for row in card_for(app, "prj_a").query(AgentRow)]
        return app.sidebar.size.width, rows

    width, rows = drive(go, headroom=session_use((1, 42.0), (3, 87.0)), size=size)
    assert width == 30
    manager, coder = (row.strip() for row in rows)
    assert manager.startswith("🧭 manager") and "⏸ #1·42% · sk" in manager, manager
    assert "#1·42…" not in manager
    assert coder.startswith("🔨 coder-auth"), coder
    assert coder.endswith("▶ #3·87%") or "▶ #3·87% · " in coder, coder
    assert "a-very-long-persona-name" not in coder


def test_the_session_use_rides_the_slow_headroom_read_never_the_tick(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance (e): the figures on the rows and the info lines are the headroom read's
    (``HeadroomSummary.session_by_slot``), handed to the widgets when it lands. Ticks of the
    two-second clock read no usage, and any other usage read is recorded — not raised: every
    usage read runs in a thread worker that does not exit on error, where a raise only marks
    the worker failed (review of #267, 3). A view opened between two reads shows the last
    figure at once; one already open takes the next read's figure as it lands, a modal on
    top or not (review of #267, 2 and 5). The manager's Agent view and the Manager tab show
    the same line (acceptance (a), mounted)."""
    seed(tmp_path, ("prj_a", "alpha", "amber-otter"))
    script["prj_a"] = [
        on_slot(status("prj_a", "manager", "manager", "waiting"), 2),
        on_slot(status("prj_a", "coder-auth", "coder", "working", minute=1), 2),
    ]
    reads: list[float] = []
    figure = [71.0]

    def headroom(accounts: Sequence[ClaudeAccount] | None) -> HeadroomSummary:
        reads.append(figure[0])
        return session_use((2, figure[0]))(accounts)

    elsewhere: list[str] = []

    def reader(name: str) -> Callable[..., object]:
        def read(*args: object, **kwargs: object) -> object:
            elsewhere.append(name)
            raise AssertionError(f"usage was read outside the headroom read: {name}")

        return read

    for name in ("cached_usage", "cached_usage_with_trends", "read_usage", "usage"):
        monkeypatch.setattr(accounts_service, name, reader(name))
    monkeypatch.setattr(accounts_service, "read_fleet_headroom", reader("read_fleet_headroom"))

    def headers(app: FleetApp) -> tuple[str, str]:
        agent = shown(app.query_one("#agent-agt_a_manager #agent-header", Static))
        manager = shown(app.query_one("#project-prj_a #manager-header", Static))
        return agent, manager.splitlines()[0]

    async def go(pilot: Pilot[None]) -> tuple[list[float], tuple[str, str], tuple[str, str], str]:
        app = fleet_app(pilot)
        await settle(app)
        await pilot.click(row_for(app, "agt_a_manager"))
        await settle(app)
        await pilot.click(card_for(app, "prj_a").query_one(ProjectTitle))
        await settle(app)
        for _ in range(3):
            app.refresh_data()
            await settle(app)
        opened = headers(app)
        # The next slow read, with both views open and a modal over them.
        figure[0] = 64.0
        app.action_help()
        await settle(app)
        assert isinstance(app.screen, HelpScreen)
        app.refresh_headroom()
        await settle(app)
        app.pop_screen()
        await settle(app)
        return list(reads), opened, headers(app), on_screen(row_for(app, "agt_a_coder-auth"))

    seen, opened, after, row = drive(go, headroom=headroom)
    assert seen == [71.0, 64.0], "one read at start, one asked for: the ticks read no usage"
    assert elsewhere == [], f"usage read outside the headroom read: {elsewhere}"
    agent_header, manager_first = opened
    assert "account 2 · 71%" in agent_header
    assert manager_first == agent_header
    assert "account 2 · 64%" in after[0] and "71%" not in after[0], after
    assert after[1] == after[0]
    assert row.rstrip().endswith("▶ #2·64%")


def test_in_a_90_column_terminal_the_agent_view_gives_way_in_its_label_first(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's rulings 2 and 3 on #267, in the real app in a 90-column terminal: the Agent
    view's info line gets about 37 cells. The account and its use come right after the state,
    and when the line does not fit, the LABEL gives way first — the labels ``fleet spawn``
    gives (``<role>-<task id[:8]>``) pushed ``work · 42%`` off the line — so ``<icon>
    <label…>  <chip> <state>  <account> · <pct>%`` stays whole and the rest is cut after it.
    The label keeps six characters; beside an unaliased ``plain claude · 42%`` or
    ``account 4 · 42%`` even that is too wide, and the line is cut at its end. At 140
    columns the label is whole again."""
    seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None))
    session = TeamSession(
        id="s-1",
        project_id="prj_a",
        role="coder",
        started_at=T0,
        last_seen_at=T0,
        model="claude-opus-5-5",
        effort="high",
    )

    def row(
        project_id: str, label: str, role: str, state: str, slot: int, minute: int
    ) -> FleetAgentStatus:
        spawned = on_slot(status(project_id, label, role, state, minute=minute), slot)
        return spawned.model_copy(update={"session": session})

    script["prj_a"] = [
        row("prj_a", "coder-01k8zq7m", "coder", "working", 3, 1),
        row("prj_a", "coder-01m5d34v", "coder", "working", 1, 2),
        row("prj_a", "tester-01m5d34v", "tester", "waiting", 4, 3),
    ]
    script["prj_b"] = [row("prj_b", "coder-01m5d34v", "coder", "attention", 2, 1)]
    # Slots 1 and 4 unaliased: the registry's built-in names, "plain claude" and "account 4".
    monkeypatch.setattr(accounts_service, "slot_labels", lambda store=None: {2: "work", 3: "max"})

    async def bar(pilot: Pilot[None], agent_id: str, *, wider_than: int = 0) -> tuple[int, str]:
        app = fleet_app(pilot)
        await pilot.click(row_for(app, agent_id))
        await settle(app)
        header = app.query_one(f"#agent-{agent_id} #agent-header", Static)
        # A resize reaches the screen from a 1/120 s timer that settle() does not wait for:
        # on windows-latest the 140-column read measured the 90-column header (#401).
        await settle_until_held(
            app, lambda: header.content_size.width > wider_than, what="the header's new width"
        )
        (strip,) = header.render_lines(Region(0, 0, header.outer_size.width, 1))
        return header.content_size.width, strip.text.strip()

    async def go(pilot: Pilot[None]) -> dict[str, tuple[int, str]]:
        app = fleet_app(pilot)
        await settle(app)
        seen = {
            agent_id: await bar(pilot, agent_id)
            for agent_id in (
                "agt_a_coder-01k8zq7m",
                "agt_a_coder-01m5d34v",
                "agt_a_tester-01m5d34v",
                "agt_b_coder-01m5d34v",
            )
        }
        await pilot.resize_terminal(140, 30)
        await settle(app)
        seen["wide"] = await bar(pilot, "agt_a_coder-01k8zq7m", wider_than=60)
        return seen

    seen = drive(
        go, headroom=session_use((1, 42.0), (2, 42.0), (3, 42.0), (4, 42.0)), size=(90, 30)
    )
    widths = {width for agent_id, (width, _) in seen.items() if agent_id != "wide"}
    assert widths == {37}, seen  # about 38 cells, as the owner measured
    assert seen["agt_a_coder-01k8zq7m"][1] == "🔨 coder-01k8…  ▶ working  max · 42%…"
    assert seen["agt_b_coder-01m5d34v"][1] == "🔨 coder-…  🔔 attention  work · 42%…"
    # Too narrow even at the label's floor: the floor, then the line cut at its end.
    assert seen["agt_a_coder-01m5d34v"][1] == "🔨 coder-…  ▶ working  plain claude …"
    assert seen["agt_a_tester-01m5d34v"][1] == "🧪 tester…  ⏸ waiting  account 4 · 4…"
    width, wide = seen["wide"]
    assert width > 60 and wide.startswith("🔨 coder-01k8zq7m  ▶ working  max · 42%  claude"), wide


def test_at_90_columns_the_manager_tab_cuts_its_info_line_and_shows_its_hint_whole(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's ruling on #267, in the real app in a 90-column terminal (a 57-cell header):
    the Manager tab's info line is cut on its one row with ``…``, as the Agent view's is — a
    long detail (the folder-trust prompt's) used to wrap it over two to four rows — while the
    dim hint under it shows whole. Under one nowrap rule for both, the hint was cut before
    "F12 returns to the sidebar" (review of #267). With the manager gone, the prose that says
    so wraps and nothing is cut. Acceptance (a), mounted: the Manager tab's line is the
    manager's Agent view line, text and styles."""
    seed(tmp_path, ("prj_a", "alpha", "amber-otter"))
    row = on_slot(status("prj_a", "manager", "manager", "attention"), 2)
    session = TeamSession(
        id="s-manager",
        project_id="prj_a",
        role="manager",
        started_at=T0,
        last_seen_at=T0,
        model="claude-opus-5-5",
        effort="xhigh",
    )
    detail = fleet_service.trust_prompt_detail(tmp_path / "a [deep] folder" / "repo")
    script["prj_a"] = [row.model_copy(update={"session": session, "detail": detail})]
    monkeypatch.setattr(accounts_service, "slot_labels", lambda store=None: {2: "work"})

    def rows(widget: Widget) -> list[str]:
        width, height = widget.outer_size
        lines = widget.render_lines(Region(0, 0, width, height))
        return [strip.text.strip() for strip in lines]

    async def go(pilot: Pilot[None]) -> tuple[int, list[str], list[str], bool, list[str], bool]:
        app = fleet_app(pilot)
        await settle(app)
        await pilot.click(row_for(app, "agt_a_manager"))
        await settle(app)
        agent_header = app.query_one("#agent-agt_a_manager #agent-header", Static)
        await pilot.click(card_for(app, "prj_a").query_one(ProjectTitle))
        await settle(app)
        app.refresh_data()
        await settle(app)
        header = app.query_one("#project-prj_a #manager-header", Static)
        hint = app.query_one("#project-prj_a #manager-hint", Static)
        same = header.content == agent_header.content  # plain and spans alike
        seen = header.content_size.width, rows(header), rows(hint), same
        script["prj_a"] = []
        app.refresh_data()
        await settle(app)
        return *seen, rows(header), hint.display

    width, line, hint, same, gone, hint_after = drive(
        go, headroom=session_use((2, 42.0)), size=(90, 30)
    )
    assert width == 57, width  # a 90-column terminal, the default sidebar
    assert len(line) == 1, line  # the info line on one row…
    assert line[0].startswith("🧭 manager  🔔 attention  work · 42%  claude"), line
    assert line[0].endswith("…"), line  # …cut at its end, and the cut is said
    assert same, "the Manager tab's line is the Agent view's, text and styles"
    assert "F12 returns to the sidebar" in " ".join(hint), hint  # the hint, whole
    assert " ".join(hint).startswith("type to the manager as you would to any Claude session")
    assert len(gone) > 2 and not any(r.endswith("…") for r in gone), gone  # prose wraps
    assert "has no manager yet" in " ".join(gone) and hint_after is False


def test_a_project_opened_between_two_ticks_names_its_session_at_once(
    tmp_path: Path, script: Script
) -> None:
    """Review of #267, 7: a new Project view waited for the next tick for its rows, and its
    Settings tab said "no live tmux session" until then while the frame the app already
    held named one. The view is handed that frame as it is built."""
    seed(tmp_path, ("prj_a", "alpha", "amber-otter"))
    manager = status("prj_a", "manager", "manager", "waiting")
    script["prj_a"] = [manager.model_copy(update={"tmux_session": "asq-amber-otter"})]

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        await pilot.click(card_for(app, "prj_a").query_one(ProjectTitle))
        await settle(app)  # no tick: the app refreshes every hour here
        return shown(app.query_one("#project-prj_a #attach-line", Static))

    line = drive(go)
    assert line == f"Attach in a terminal: tmux -L {PRIVATE_SOCKET} attach -t =asq-amber-otter"


def test_the_card_names_the_project_not_its_codename_and_a_path_tells_twins_apart(
    tmp_path: Path, script: Script
) -> None:
    """The codename names the tmux session and the branches; the card says the name a
    person knows the project by (its nickname, else its folder), and nothing else."""
    seed(
        tmp_path,
        ("prj_w", "work/api", "amber-otter"),
        ("prj_o", "oss/api", "ruby-fox"),
        ("prj_u", "unique", None),
    )

    async def go(pilot: Pilot[None]) -> dict[str, tuple[str, bool, str]]:
        app = fleet_app(pilot)
        out: dict[str, tuple[str, bool, str]] = {}
        for card in app.query(ProjectCard):
            subtitle = card.query_one(".card-subtitle", Static)
            out[card.project.id] = (
                shown(card.query_one(ProjectTitle)),
                subtitle.display,
                shown(subtitle),
            )
        return out

    cards = drive(go)
    assert [cards[pid][0] for pid in ("prj_w", "prj_o", "prj_u")] == [
        "🗂 api",
        "🗂 api",
        "🗂 unique",
    ]
    # The two `api` projects are told apart by their path; the unique one is not decorated.
    assert cards["prj_w"][1] and cards["prj_w"][2].endswith("work/api")
    assert cards["prj_o"][1] and cards["prj_o"][2].endswith("oss/api")
    assert cards["prj_u"][1] is False and cards["prj_u"][2] == ""


def test_a_long_project_name_is_cut_with_an_ellipsis_not_wrapped_out_of_sight(
    tmp_path: Path, script: Script
) -> None:
    """What the ROW shows, not what the widget holds — ``shown()`` cannot see this one.

    Reported 2026-09-05 from a live fleet (WSL2, tmux 3.2): the selected project
    row was the disclosure, the folder glyph, then a highlighted band with no
    name and no codename, while the manager row under it was fine. Reproduced
    headless against a copy of that store: ``ProjectTitle.visual.plain`` was
    ``'🗂 AISquare-Explainability-SDK  cosmic-narwhal  1'`` — the data was never
    the bug — and the one strip Textual composited for the row was
    ``'🗂                        '``, in textual-dark, textual-light, nord and
    gruvbox alike. ``project_title_text`` builds its Rich ``Text`` with
    ``no_wrap=True, overflow="ellipsis"``, but Textual's
    ``Content.from_rich_text`` keeps only the plain text and the spans; the
    widget's CSS ``text-wrap`` decides, its default is ``wrap``, the 27-cell name
    did not fit the 25-cell title and wrapped onto a second line that
    ``Activatable { height: 1 }`` clipped. Every ``shown()`` assertion in this
    file read the full title and passed (#86).
    """
    seed(
        tmp_path,
        ("prj_l", "AISquare-Explainability-SDK", "cosmic-narwhal"),
        ("prj_s", "api", "amber-otter"),
    )
    script["prj_l"] = [status("prj_l", "manager", "manager", "waiting")]

    async def go(pilot: Pilot[None]) -> tuple[str, str, str, bool, str, str]:
        app = fleet_app(pilot)
        title = card_for(app, "prj_l").query_one(ProjectTitle)
        await pilot.click(title)  # the selected, highlighted row the report was about
        await settle(app)
        return (
            shown(title),
            composited(title),
            composited(card_for(app, "prj_s").query_one(ProjectTitle)),
            title.has_class("selected"),
            composited(row_for(app, "agt_l_manager")),
            composited(card_for(app, "prj_l").query_one(CardChips)),
        )

    held, long_row, short_row, selected, manager_row, chips = drive(go)
    assert selected
    assert held == "🗂 AISquare-Explainability-SDK"  # the data, intact
    assert long_row.startswith("🗂 AISquare-Explainab")  # the NAME reaches the row
    assert long_row.rstrip().endswith("…")  # and the cut is declared, not silent
    assert chips.strip() == "1", "the name is what is cut, never the chips after it"
    # Controls: a name that fits is shown whole and uncut; the agent row still renders.
    assert short_row.rstrip() == "🗂 api"
    assert manager_row.split()[:2] == ["🧭", "manager"]


def test_disclosure_collapses_the_agent_rows_without_selecting(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    script["prj_a"] = [status("prj_a", "manager", "manager", "waiting")]

    async def go(pilot: Pilot[None]) -> tuple[list[bool], list[str], str | None]:
        app = fleet_app(pilot)
        card = card_for(app, "prj_a")
        rows = card.query_one("#agents")
        glyphs = [shown(card.query_one(Disclosure))]
        states = [rows.display]
        await pilot.click(card.query_one(Disclosure))
        await pilot.pause()
        states.append(rows.display)
        glyphs.append(shown(card.query_one(Disclosure)))
        await pilot.click(card.query_one(Disclosure))
        await pilot.pause()
        states.append(rows.display)
        glyphs.append(shown(card.query_one(Disclosure)))
        return states, glyphs, app.content.current

    states, glyphs, current = drive(go)
    assert states == [True, False, True]
    assert glyphs == ["▾", "▸", "▾"]
    assert current == "welcome"  # the disclosure is not a selection


# --- selection → content -------------------------------------------------------------


def test_clicking_a_project_opens_its_project_view_once(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None))

    async def go(pilot: Pilot[None]) -> tuple[str | None, list[str], int, bool]:
        app = fleet_app(pilot)
        before = app.content.current
        await pilot.click(card_for(app, "prj_b").query_one(ProjectTitle))
        await settle(app)
        shown_ids = [app.content.current or ""]
        view = app.current_view()
        assert isinstance(view, ProjectView) and view.project.id == "prj_b"
        await pilot.click(card_for(app, "prj_a").query_one(ProjectTitle))
        await settle(app)
        shown_ids.append(app.content.current or "")
        await pilot.click(card_for(app, "prj_b").query_one(ProjectTitle))  # back again
        await settle(app)
        shown_ids.append(app.content.current or "")
        selected = card_for(app, "prj_b").query_one(ProjectTitle).has_class("selected")
        other = card_for(app, "prj_a").query_one(ProjectTitle).has_class("selected")
        assert not other
        return before, shown_ids, len(app.query(ProjectView)), selected

    before, shown_ids, views, selected = drive(go)
    assert before == "welcome"
    assert shown_ids == ["project-prj_b", "project-prj_a", "project-prj_b"]
    assert views == 2  # one view per project, reused on the second visit — not three
    assert selected


def scripted_pane(ran: list[tuple[str, ...]], rows: list[str]) -> FakeTmux:
    """The shared fake tmux, answering one pane's frames so a real ``AgentView``
    in a real ``FleetApp`` shows real rows.

    ``no_real_tmux`` stubs every command into a failure, which is right for
    tests about routing and wrong for one about SELECTING text — there is
    nothing on screen to select. The fake answers the one call the pane makes
    per frame (``capture-pane`` + ``display-message`` in a single process) and
    follows the ``resize-window`` that precedes it, exactly as the real server
    would; ``record=ran`` keeps the socket guard reading every argv.
    """
    tmux = FakeTmux(record=ran)
    tmux.panes["%1"] = FakePane(screen=list(rows), width=40, height=len(rows))
    return tmux


async def _agent_pane(pilot: Pilot[None]) -> tuple[TerminalPane, Static]:
    """Open the scripted agent and wait until its pane has painted a frame."""
    app = fleet_app(pilot)
    await pilot.click(row_for(app, "agt_a_coder-auth"))
    await settle(app)
    view = app.current_view()
    assert isinstance(view, AgentView)
    pane = view.query_one(TerminalPane)
    deadline = time.monotonic() + 3.0
    while pane.frames < 1 or "second row" not in pane_text(pane):
        assert time.monotonic() < deadline, "the pane never painted the scripted rows"
        await pilot.pause()
    return pane, view.query_one("#agent-header", Static)


def pane_text(pane: TerminalPane) -> str:
    width, height = pane.content_size
    return "\n".join(strip.text for strip in pane.render_lines(Region(0, 0, width, height)))


@_needs_tmux
def test_a_drag_from_the_agent_header_into_the_pane_copies_through_the_app(
    tmp_path: Path,
    script: Script,
    no_real_tmux: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The app is what turns the end of a selection gesture into a copy.

    ``SelectionHost.on_event`` — ``FleetApp``'s, by inheritance — is the only
    thing that makes a drag crossing the pane's edge copy, and nothing used to
    exercise it: replacing the app's handler with ``return`` left the whole
    suite green, because every test of that gesture re-implemented the handler
    on its own test ``Host`` (review of #120, round 7). This drives the real
    app, the real ``AgentView``, and the real header, with the events the
    driver would post.
    """
    seed(tmp_path, ("prj_a", "alpha", None))
    script["prj_a"] = [status("prj_a", "coder-auth", "coder", "working")]
    monkeypatch.setattr(
        tmux_core, "_tmux", scripted_pane(no_real_tmux, ["red plain", "second row", "third row"])
    )

    async def go(pilot: Pilot[None]) -> tuple[str, int, str, int]:
        app = fleet_app(pilot)
        pane, header = await _agent_pane(pilot)
        await press(pilot, header, (1, 0))
        await move(pilot, pane, (5, 1), button=1)
        await release(pilot, pane, (5, 1))
        await pilot.pause()
        crossed, toasts = app.clipboard, len(app._notifications)
        # The negative half: a gesture that touches no row of the pane must
        # leave both the clipboard and the toast count exactly as they were.
        app.screen.clear_selection()
        await pilot.pause()
        await press(pilot, header, (1, 0))
        await move(pilot, header, (6, 0), button=1)
        await release(pilot, header, (6, 0))
        await pilot.pause()
        return crossed, toasts, app.clipboard, len(app._notifications)

    crossed, toasts, after, toasts_after = drive(go, notifications=True)
    assert crossed == "red plain\nsecon", crossed
    assert toasts == 1, "one copy, one toast"
    assert after == crossed and toasts_after == toasts, (
        "a gesture over no pane row must not re-copy a standing selection"
    )


@_needs_tmux
def test_a_right_button_drag_from_the_agent_header_copies_nothing_through_the_app(
    tmp_path: Path,
    script: Script,
    no_real_tmux: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The app's record of the press is the whole of the button fix: a pane
    only sees a press that lands ON it, so without the app a right-button drag
    begun on the header reads as a left one and copies. Nothing reached that
    handler once — replacing its body left the suite green (review of #120,
    round 8) — and it has since moved into ``SelectionHost.on_event``."""
    seed(tmp_path, ("prj_a", "alpha", None))
    script["prj_a"] = [status("prj_a", "coder-auth", "coder", "working")]
    monkeypatch.setattr(
        tmux_core, "_tmux", scripted_pane(no_real_tmux, ["red plain", "second row", "third row"])
    )

    async def go(pilot: Pilot[None]) -> tuple[str, int, bool]:
        app = fleet_app(pilot)
        pane, header = await _agent_pane(pilot)
        await press(pilot, header, (1, 0), button=3)
        await move(pilot, pane, (5, 1), button=3)
        await release(pilot, pane, (5, 1), button=3)
        await pilot.pause()
        return app.clipboard, len(app._notifications), pane.text_selection is not None

    clipboard, toasts, highlighted = drive(go, notifications=True)
    assert highlighted, "the premise: the gesture did select text in the pane"
    assert clipboard == "" and toasts == 0, "a right-button drag is not a copy request"


@_needs_tmux
def test_one_panes_failure_does_not_stop_the_others_being_told(
    tmp_path: Path,
    script: Script,
    no_real_tmux: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard around the fan-out, both halves: a pane that raises is logged
    rather than swallowed silently, and its neighbours still hear the gesture.
    This PR's history is an unguarded exception in a mouse handler taking the
    app down (review of #120, round 8)."""
    seed(tmp_path, ("prj_a", "alpha", None))
    script["prj_a"] = [
        status("prj_a", "coder-auth", "coder", "working"),
        status("prj_a", "coder-two", "coder", "working", minute=1),
    ]
    monkeypatch.setattr(
        tmux_core, "_tmux", scripted_pane(no_real_tmux, ["red plain", "second row", "third row"])
    )

    logged: list[str] = []
    original_call = Logger.__call__

    def record(self: Logger, *args: object, **kwargs: object) -> None:
        logged.append(" ".join(str(a) for a in args))
        original_call(self, *args, **kwargs)

    monkeypatch.setattr(Logger, "__call__", record)

    async def go(pilot: Pilot[None]) -> tuple[int, str, int, list[str], list[str]]:
        app = fleet_app(pilot)
        await pilot.click(row_for(app, "agt_a_coder-two"))
        await settle(app)
        pane, header = await _agent_pane(pilot)  # opens coder-auth, leaves both mounted
        panes = list(app.screen.query(TerminalPane))
        assert len(panes) >= 2, "the app keeps a view per opened agent mounted"

        async def cross() -> None:
            await press(pilot, header, (1, 0))
            await move(pilot, pane, (5, 1), button=1)
            await release(pilot, pane, (5, 1))
            await pilot.pause()

        # The negative half first, while every pane still works.
        logged.clear()
        await cross()
        quiet = list(logged)
        app.screen.selections = {}
        await pilot.pause()

        def boom(button: int | None = None) -> None:
            raise RuntimeError("this pane is mid-teardown")

        broken = next(other for other in panes if other is not pane)
        monkeypatch.setattr(broken, "selection_gesture_ended", boom)
        logged.clear()
        await cross()
        return len(panes), app.clipboard, len(app._notifications), quiet, list(logged)

    panes, clipboard, toasts, quiet, recorded = drive(go, notifications=True)
    assert panes >= 2
    assert clipboard == "red plain\nsecon", "the working pane still copied"
    assert toasts == 2, "one toast per crossing gesture, the failing pane notwithstanding"
    assert not [line for line in quiet if "selection gesture" in line], quiet
    assert any("mid-teardown" in line for line in recorded), (
        f"the failure must leave a trace, not be swallowed: {recorded}"
    )


@_needs_tmux
def test_a_screen_that_cannot_be_queried_is_logged_not_a_crash(
    tmp_path: Path,
    script: Script,
    no_real_tmux: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the fan-out guard. Resolving the screen is what raises
    when a stack is being torn down, and nothing exercised it — the guard and
    its log line were both mutation-green (review of the tenth version)."""
    seed(tmp_path, ("prj_a", "alpha", None))
    script["prj_a"] = [status("prj_a", "coder-auth", "coder", "working")]
    monkeypatch.setattr(
        tmux_core, "_tmux", scripted_pane(no_real_tmux, ["red plain", "second row", "third row"])
    )
    logged: list[str] = []
    original_call = Logger.__call__

    def record(self: Logger, *args: object, **kwargs: object) -> None:
        logged.append(" ".join(str(a) for a in args))
        original_call(self, *args, **kwargs)

    monkeypatch.setattr(Logger, "__call__", record)

    async def go(pilot: Pilot[None]) -> tuple[list[str], bool]:
        app = fleet_app(pilot)
        await _agent_pane(pilot)

        def no_screen(self: FleetApp) -> object:
            raise ScreenStackError("the screen stack is empty")

        monkeypatch.setattr(type(app), "screen", property(no_screen))
        logged.clear()
        route_selection_gesture(app, 1)
        return list(logged), app.is_running

    recorded, alive = drive(go)
    assert alive, "the app survives a screen it cannot resolve"
    assert any("no screen to tell" in line for line in recorded), recorded


def test_the_shell_and_the_test_host_share_the_gesture_handlers() -> None:
    """The pane tests' ``Host`` once mirrored ``FleetApp``'s gesture handlers by
    hand and fell behind, which hid two regressions (reviews of #120, rounds 6
    and 9). Both derive from ``SelectionHost`` now and add nothing of their own
    to the press, the release, the copy key or the clipboard — so a change to
    the shell's gesture path is a change to what the pane tests exercise."""
    from tests.test_terminal_pane import Host

    for app in (FleetApp, Host):
        assert issubclass(app, SelectionHost), app
        for name in ("on_event", "get_default_screen", "copy_to_clipboard", "on_mouse_down"):
            assert name not in vars(app), f"{app.__name__} overrides {name}"
            assert name not in vars(app) and "on_text_selected" not in vars(app)


@_needs_tmux
def test_ctrl_c_from_the_sidebar_copies_what_the_drag_copied(
    tmp_path: Path,
    script: Script,
    no_real_tmux: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 10 of the #135 review, in the real shell. A drag from the agent
    header into the pane leaves focus in the sidebar, and the pane's toast
    promises that ctrl+c copies again — but the key reached Textual's own
    ``screen.copy_text``, which joined the header line onto the pane's text. The
    default screen is ``PaneScreen`` now: the pane copies, exactly as its
    release did, and clears its highlight."""
    seed(tmp_path, ("prj_a", "alpha", None))
    script["prj_a"] = [status("prj_a", "coder-auth", "coder", "working")]
    monkeypatch.setattr(
        tmux_core, "_tmux", scripted_pane(no_real_tmux, ["red plain", "second row", "third row"])
    )

    async def go(pilot: Pilot[None]) -> tuple[str, str, int, bool, bool]:
        app = fleet_app(pilot)
        pane, header = await _agent_pane(pilot)
        app.sidebar.focus()
        await pilot.pause()
        await press(pilot, header, (1, 0))
        await move(pilot, pane, (5, 1), button=1)
        await release(pilot, pane, (5, 1))
        dragged = app.clipboard
        in_sidebar = app.focused is app.sidebar or app.sidebar in (
            app.focused.ancestors if app.focused else []
        )
        await pilot.press("ctrl+c")
        await settle(app)  # the screen's binding: the key bubbles up from the sidebar to it
        return (
            dragged,
            app.clipboard,
            len(app._notifications),
            in_sidebar,
            pane.text_selection is None,
        )

    dragged, again, toasts, in_sidebar, cleared = drive(go, notifications=True)
    assert dragged == "red plain\nsecon"
    assert in_sidebar, "the premise: focus never left the sidebar"
    assert again == dragged, "ctrl+c copied the pane's text, not the header line joined onto it"
    assert toasts == 2, "and said so, as the release did"
    assert cleared, "and cleared the highlight, as the pane's own ctrl+c does"


def test_clicking_an_agent_opens_its_agent_view(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    script["prj_a"] = [
        status("prj_a", "manager", "manager", "waiting"),
        status("prj_a", "coder-auth", "coder", "working", minute=1),
    ]

    async def go(pilot: Pilot[None]) -> tuple[str | None, str, str | None, bool, bool]:
        app = fleet_app(pilot)
        before = app.content.current
        await pilot.click(row_for(app, "agt_a_coder-auth"))
        await settle(app)
        view = app.current_view()
        assert isinstance(view, AgentView)
        pane = view.query_one(TerminalPane)
        return (
            before,
            view.status.agent.label,
            pane.pane_id,
            row_for(app, "agt_a_coder-auth").has_class("selected"),
            row_for(app, "agt_a_manager").has_class("selected"),
        )

    before, label, pane_id, coder_selected, manager_selected = drive(go)
    assert before == "welcome"
    assert label == "coder-auth" and pane_id == "%1"
    assert coder_selected and not manager_selected


def test_plus_opens_onboarding(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> tuple[str | None, str | None]:
        app = fleet_app(pilot)
        before = app.content.current
        await pilot.click("#add-project")
        await settle(app)
        return before, app.content.current

    assert drive(go) == ("welcome", "onboard")


def test_the_hubs_doctor_link_opens_the_doctor_page_and_runs_the_checks(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    calls: list[int] = []

    def doctor() -> list[DoctorCheck]:
        calls.append(1)
        return [
            DoctorCheck(name="python", status=CheckStatus.ok, detail="Python 3.14"),
            DoctorCheck(
                name="tmux",
                status=CheckStatus.warn,
                detail="tmux not found",
                fix="brew install tmux",
            ),
            DoctorCheck(
                name="home", status=CheckStatus.fail, detail="~/.aisquare is missing", fix="init"
            ),
        ]

    async def go(pilot: Pilot[None]) -> tuple[int, str | None, str, str | None, str | None, int]:
        app = fleet_app(pilot)
        await settle(app)
        await pilot.pause()
        calls_after_mount = len(calls)
        before = app.content.current
        await open_doctor(pilot)
        await pilot.pause()
        await settle(app)
        await pilot.pause()
        report = shown(app.query_one("#doctor", DoctorView).query_one("#doctor-report", Static))
        return (
            calls_after_mount,
            before,
            report,
            app.content.current,
            app.sidebar.selected_key,
            len(calls),
        )

    at_mount, before, report, current, selected, total = drive(go, doctor=doctor)
    assert at_mount == 1  # the checks run at start-up…
    assert before == "welcome"
    assert total == 2  # …and opening the page runs them again
    assert current == "doctor" and selected == "settings"  # a page of Settings'
    assert "tmux not found" in report and "→ brew install tmux" in report
    assert "Python 3.14" in report and "~/.aisquare is missing" in report


def test_a_crashing_doctor_is_reported_not_raised(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))

    def broken() -> list[DoctorCheck]:
        raise RuntimeError("boom")

    async def go(pilot: Pilot[None]) -> tuple[str, int | None]:
        app = fleet_app(pilot)
        await settle(app)
        await pilot.pause()
        report = shown(app.query_one("#doctor", DoctorView).query_one("#doctor-report", Static))
        return report, app.return_code

    report, return_code = drive(go, doctor=broken)
    assert "the checks crashed: RuntimeError: boom" in report  # on the Doctor page
    assert return_code is None  # the app kept running

    async def healthy(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        await settle(app)
        await pilot.pause()
        return shown(app.query_one("#doctor", DoctorView).query_one("#doctor-report", Static))

    assert "crashed" not in drive(healthy)  # a working doctor reports no such thing


FACTS_WARN = DoctorCheck(
    name="facts",
    status=CheckStatus.warn,
    detail="no project facts seeded",
    fix="Seed them: aisquare project onboard",
)
"""A finding whose fix is one of ours and is PROJECT-scoped — the button needs a cwd. No
doctor row offers one today; this stands in for one."""
CONNECT_WARN = DoctorCheck(
    name="claude-code",
    status=CheckStatus.warn,
    detail="hooks are missing",
    fix="(Re)connect it: aisquare agents connect claude-code",
)


def _fix_buttons(view: DoctorView) -> list[tuple[str, bool]]:
    return [(str(b.label), b.disabled) for b in view.query("#doctor-fixes Button").results(Button)]


def test_the_doctor_page_arms_project_fixes_for_the_project_it_ran_for(
    tmp_path: Path, script: Script
) -> None:
    """§0 item 4: a project fix is one click. Without a cwd every one renders disabled.

    ``DoctorView`` disables ``scope == "project"`` fixes when ``cwd is None``, so
    the shell hands it the root its report was run in. Selecting a project runs
    nothing and arms nothing — the report on the page is still the machine's —
    and opening the page runs the checks for that project, armed in its root.
    """
    projects = seed(tmp_path, ("prj_a", "alpha", None))
    Buttons = list[tuple[str, bool]]

    async def go(
        pilot: Pilot[None],
    ) -> tuple[Buttons, Path | None, Buttons, Path | None, Buttons, Path | None, int]:
        app = fleet_app(pilot)
        await settle(app)
        await pilot.pause()
        shell_doctor = app.query_one("#doctor", DoctorView)
        globally = (_fix_buttons(shell_doctor), shell_doctor.cwd)  # control: nothing selected yet
        await pilot.click(card_for(app, "prj_a").query_one(ProjectTitle))
        await pilot.pause()
        await settle(app)
        await pilot.pause()
        selected = (_fix_buttons(shell_doctor), shell_doctor.cwd)
        await open_doctor(pilot)
        await pilot.pause()
        await settle(app)
        await pilot.pause()
        return (
            *globally,
            *selected,
            _fix_buttons(shell_doctor),
            shell_doctor.cwd,
            len(app.query(DoctorView)),
        )

    checks = [FACTS_WARN, CONNECT_WARN]
    global_buttons, global_cwd, selected_buttons, selected_cwd, page_buttons, page_cwd, views = (
        drive(go, doctor=lambda: list(checks))
    )
    refused = [
        ("aisquare project onboard --refresh", True),  # no project: correctly refused
        ("aisquare agents connect claude-code", False),
    ]
    assert global_cwd is None and global_buttons == refused
    assert selected_cwd is None and selected_buttons == refused, (
        "selecting a project leaves the machine's report — and its refusals — as they were"
    )
    assert page_cwd == projects[0].root
    assert page_buttons == [
        ("aisquare project onboard --refresh", False),  # run for the project: armed
        ("aisquare agents connect claude-code", False),
    ]
    assert views == 1, "the project page has no Doctor tab of its own"


def test_a_doctor_refreshed_message_updates_the_shells_report(
    tmp_path: Path, script: Script
) -> None:
    """A one-click fix re-runs the doctor inside the view; the shell must follow it."""
    seed(tmp_path, ("prj_a", "alpha", None))
    calls: list[int] = []

    def doctor() -> list[DoctorCheck]:
        calls.append(1)
        return [FACTS_WARN]

    def counts(app: FleetApp) -> str:
        """The report the shell holds (its Doctor page shows it), as ``✓ n  ⚠ n``."""
        ok = sum(check.status is CheckStatus.ok for check in app.doctor_checks)
        warn = sum(check.status is CheckStatus.warn for check in app.doctor_checks)
        return f"✓ {ok}  ⚠ {warn}"

    async def go(pilot: Pilot[None]) -> tuple[str, int, str, int, str, int]:
        app = fleet_app(pilot)
        await settle(app)
        await pilot.pause()
        before = (counts(app), len(calls))
        # The report the view was handed is about the scope on screen: adopt it.
        app.post_message(
            DoctorRefreshed(
                [DoctorCheck(name="facts", status=CheckStatus.ok, detail="seeded 2 minutes ago")],
                None,
            )
        )
        await pilot.pause()
        await settle(app)
        await pilot.pause()
        adopted = (counts(app), len(calls))
        # A report about another root (the Onboard view's) says ours is stale.
        app.post_message(DoctorRefreshed([], tmp_path / "elsewhere"))
        await pilot.pause()
        await settle(app)
        await pilot.pause()
        return (*before, *adopted, counts(app), len(calls))

    before, calls_before, adopted, calls_adopted, rerun, calls_rerun = drive(go, doctor=doctor)
    assert "⚠ 1" in before and "✓ 0" in before and calls_before == 1
    assert "✓ 1" in adopted and "⚠ 0" in adopted, "the fixed check is reflected in the counts"
    assert calls_adopted == 1, "the matching report was adopted — the checks did not run again"
    assert calls_rerun == 2, "a report about another root re-runs ours instead"
    assert "⚠ 1" in rerun  # …and shows what OUR doctor says, not the foreign report's ✓ 0


def test_the_doctor_runs_at_start_and_then_only_when_asked(tmp_path: Path, script: Script) -> None:
    """The human: the doctor "is not continuously running but runs just once when the app
    starts and then again when someone opens it". Selecting projects, switching a project's
    tabs and the refresh tick never run it; opening its page, its Re-run button and a fix
    reported from another root each run it once."""
    seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None))
    calls: list[int] = []

    def doctor() -> list[DoctorCheck]:
        calls.append(1)
        return []

    async def go(pilot: Pilot[None]) -> dict[str, int]:
        app = fleet_app(pilot)
        await settle(app)
        seen = {"start": len(calls)}
        for card in ("prj_a", "prj_b", "prj_a"):
            await pilot.click(card_for(app, card).query_one(ProjectTitle))
            await settle(app)
            view = app.current_view()
            assert isinstance(view, ProjectView)
            for tab in (*ProjectView.TAB_IDS, "tab-manager"):
                view.active = tab
                await settle(app)
        app.refresh_data()  # what the refresh tick runs
        app.refresh_data()
        await settle(app)
        seen["selecting"] = len(calls)
        await open_doctor(pilot)
        await settle(app)
        seen["opened"] = len(calls)
        await pilot.click("#doctor-rerun")
        await settle(app)
        seen["rerun"] = len(calls)
        app.post_message(DoctorRefreshed([], tmp_path / "elsewhere"))  # a fix in another root
        await settle(app)
        seen["fixed"] = len(calls)
        return seen

    assert drive(go, doctor=doctor) == {
        "start": 1,
        "selecting": 1,
        "opened": 2,
        "rerun": 3,
        "fixed": 4,
    }


@pytest.mark.parametrize("remembered", ["project:prj_a", "doctor:prj_a"])
def test_the_one_run_at_start_is_about_the_project_asq_reopens(
    tmp_path: Path, script: Script, remembered: str
) -> None:
    """At start the doctor ran for the machine, then again for the project the restored view
    selected: two runs, the first thrown away. Now one, about that project — and a reopened
    Doctor page's own run is that one."""
    projects = seed(tmp_path, ("prj_a", "alpha", None))
    ui_app._remember_ui_state(ui_app.SELECTED_KEY, remembered)
    calls: list[int] = []

    def doctor() -> list[DoctorCheck]:
        calls.append(1)
        return []

    async def go(pilot: Pilot[None]) -> tuple[str | None, int, Path | None]:
        app = fleet_app(pilot)
        await settle(app)
        await pilot.pause()
        await settle(app)
        return app.content.current, len(calls), app.query_one("#doctor", DoctorView).cwd

    current, count, cwd = drive(go, doctor=doctor)
    assert current == ("project-prj_a" if remembered.startswith("project") else "doctor")
    assert count == 1
    assert cwd == projects[0].root


class SpyApp(FleetApp):
    """The shell plus the doctor ``Worker`` objects its handler was told about.

    A worker cannot be reached from outside once ``run_worker`` returns, and the
    claim below is about ONE run's result arriving under another run's scope —
    so the runs are collected here rather than read off a private attribute.
    """

    def __init__(self, **options: object) -> None:
        super().__init__(**options)  # type: ignore[arg-type]
        self.doctor_runs: list[Worker[object]] = []

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name == "doctor" and event.worker not in self.doctor_runs:
            self.doctor_runs.append(event.worker)
        super().on_worker_state_changed(event)


def test_a_doctor_report_is_painted_only_in_the_scope_it_ran_for(
    tmp_path: Path, script: Script
) -> None:
    """A report finished in project A must never be painted with project B's root.

    ``show_doctor`` re-read the scope at PAINT time and the worker filter was
    the name alone, so a SUCCESS queued across a change of scope handed the old
    project's findings to the new project's ``DoctorView.cwd`` — a one-click
    project fix would then have run in the wrong root. The scope changes when
    the Doctor page opens for another project, and a worker past RUNNING is
    past cancelling. Replaying that queued message IS the race, without racing.
    """
    projects = seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None))
    alpha, beta = projects
    runs: list[str] = []

    def doctor() -> list[DoctorCheck]:
        runs.append(f"run-{len(runs) + 1}")
        return [DoctorCheck(name=runs[-1], status=CheckStatus.warn, detail="a finding", fix="")]

    async def run() -> tuple[object, str, str, str, str, Path | None]:
        app = SpyApp(refresh_seconds=3600, doctor=doctor)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await settle(app)
            await pilot.pause()
            for card in ("prj_a", "prj_b"):
                await pilot.click(card_for(app, card).query_one(ProjectTitle))
                await settle(app)
                await open_doctor(pilot)  # the page, for that project
                await settle(app)
                await pilot.pause()
                if card == "prj_a":
                    stale = app.doctor_runs[-1]  # the run whose scope is alpha
            view = app.query_one("#doctor", DoctorView)
            report = view.query_one("#doctor-report", Static)
            painted = shown(report)
            # Exactly what a SUCCESS queued across the selection change is.
            app.post_message(Worker.StateChanged(stale, WorkerState.SUCCESS))
            await pilot.pause()
            await pilot.pause()
            after_replay = shown(report)
            # Control: the CURRENT run's own message still paints. Wipe the view
            # first, or a refused replay and a no-op replay look identical.
            view.show([])
            await pilot.pause()
            wiped = shown(report)
            app.post_message(Worker.StateChanged(app.doctor_runs[-1], WorkerState.SUCCESS))
            await pilot.pause()
            await pilot.pause()
            return stale.result, painted, after_replay, wiped, shown(report), view.cwd

    tagged, painted, after_replay, wiped, repainted, cwd = asyncio.run(run())
    assert runs == ["run-1", "run-2", "run-3"]  # mount (global), alpha's page, beta's page
    assert isinstance(tagged, tuple), "the result must carry the scope it ran for"
    assert tagged[0] == alpha.root and [c.name for c in tagged[1]] == ["run-2"]
    assert "run-3" in painted and "run-2" not in painted  # beta's report is on screen
    assert after_replay == painted, "alpha's report must not be painted under beta's scope"
    assert "run-3" not in wiped  # the wipe took, so the control can show something
    assert "run-3" in repainted, "the current scope's own report still paints"
    assert cwd == beta.root  # …and the fixes still run in the scope that is shown
    """CONTRIBUTING: no markup in data. A toast parses markup unless told not to."""
    seed(tmp_path, ("prj_a", "alpha", None))
    path = tmp_path / "[archive]" / "repo"

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        app.post_message(OnboardFailed(path, "init failed: store_unopenable"))
        return await _toasts(app)

    rendered = drive(go, notifications=True)
    assert rendered == f"{path}: init failed: store_unopenable"
    assert "[archive]" in rendered
    # Control: the same string rendered AS MARKUP loses the bracketed segment —
    # the failure this assertion exists to catch, measured here.
    #
    # Derived FROM `rendered`, not rebuilt from the parts. A control assembled
    # out of literals stops being a control: it keeps passing if the product
    # drops the path, changes the separator between path and reason, or reorders
    # the message — none of which this test would then notice.
    #
    # The one thing neutralised is the separator, because Rich's escape
    # character is `\` and that is also the Windows one: `...\[archive]\repo`
    # reads as an ESCAPED bracket, survives markup parsing untouched, and the
    # control would prove nothing on the platform where it looks most alarming.
    as_markup = rendered.replace("\\", "/")
    # The segment has to BE there before "markup ate it" means anything —
    # otherwise an `as_markup` that never contained it satisfies both sides of
    # the comparison and the control passes having proved nothing. Same
    # manufacture-then-assert shape as winacl's "the leak was not manufactured".
    assert "[archive]" in as_markup, as_markup
    assert Content.from_markup(as_markup).plain == as_markup.replace("[archive]", "")


def test_the_setup_form_wires_a_machine_without_a_shell(tmp_path: Path, script: Script) -> None:
    """#131's second half: the tab could SEE the key was missing and not set it.

    An external adopter's first contact with tracing was a runbook of flags, one
    of which — the hosted proxy beside the gateway — decides whether their Runs
    arrive and cannot be guessed. Typing a gateway is enough to get it.
    """
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        await app.content.add_content(ExplainabilityView(id="tracing"), set_current=True)
        await pilot.pause()
        await settle(app)
        app.screen.query_one("#explainability-target", Input).value = "stg"
        app.screen.query_one("#explainability-gateway", Input).value = "https://g.example"
        app.screen.query_one("#explainability-prefix", Input).value = "nishil"
        app.screen.query_one("#explainability-key", Input).value = "AIS_written_by_the_form"
        app.screen.query_one("#explainability-save", Button).press()
        await pilot.pause()
        await settle(app)

    drive(go, notifications=True)

    target = load_config().explainability.targets["stg"]
    assert target.gateway_url == "https://g.example"
    assert target.proxy_url == "https://g.example:9443", "the hosted proxy, offered not demanded"
    assert target.agent_name_template == "nishil-{role}"
    assert explainability_service.stored_api_key() == "AIS_written_by_the_form"


def _setup(app: Any, **fields: str) -> None:
    """Type into the Setup form's fields by id (``key-env`` as ``key_env=``)."""
    for name, value in fields.items():
        app.screen.query_one(f"#explainability-{name.replace('_', '-')}", Input).value = value


def test_a_configured_proxy_is_never_replaced_by_the_suggestion(
    tmp_path: Path, script: Script
) -> None:
    """Review blocker #3: the test was the BLANK FIELD, not the stored value.

    A target with a deliberate ``proxy_url`` whose gateway the operator merely
    corrects (gateway typed, proxy left blank) had its proxy silently replaced
    — the opposite of the "a blank field changes nothing" contract printed above
    this very form, which ``test_configure_target_applies_only_what_was_given``
    asserts one layer down.
    """
    seed(tmp_path, ("prj_a", "alpha", None))
    config = load_config()
    explainability_service.configure_target(
        config,
        target_name="stg",
        gateway_url="https://old.example",
        proxy_url="http://127.0.0.1:9090",
        enable=False,
    )
    save_config(config)

    async def go(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        await app.content.add_content(ExplainabilityView(id="tracing"), set_current=True)
        await pilot.pause()
        await settle(app)
        _setup(app, target="stg", gateway="https://new.example")
        app.screen.query_one("#explainability-save", Button).press()
        await pilot.pause()
        await settle(app)

    drive(go, notifications=True)
    target = load_config().explainability.targets["stg"]
    assert target.gateway_url == "https://new.example", "the correction lands"
    assert target.proxy_url == "http://127.0.0.1:9090", "the deliberate proxy survives it"


def test_the_suggestion_still_fills_an_empty_proxy(tmp_path: Path, script: Script) -> None:
    """The negative half of #3: with nothing to overwrite, the offer stands."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        await app.content.add_content(ExplainabilityView(id="tracing"), set_current=True)
        await pilot.pause()
        await settle(app)
        _setup(app, target="stg", gateway="https://g.example")
        app.screen.query_one("#explainability-save", Button).press()
        await pilot.pause()
        await settle(app)

    drive(go, notifications=True)
    assert load_config().explainability.targets["stg"].proxy_url == "https://g.example:9443"


def test_a_malformed_gateway_does_not_take_the_ui_down(tmp_path: Path, script: Script) -> None:
    """Review blocker #2: ``ValueError`` escaping a ``Button.Pressed`` handler.

    Every other failure in ``_save_setup`` — read, write, key — is caught and
    turned into a notice; this one propagated.
    """
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        await app.content.add_content(ExplainabilityView(id="tracing"), set_current=True)
        await pilot.pause()
        await settle(app)
        _setup(app, target="stg", gateway="http://[::1")
        app.screen.query_one("#explainability-save", Button).press()
        await pilot.pause()
        await settle(app)
        return await _toasts(app)

    assert drive(go, notifications=True)  # the app is alive to be read at all


def test_a_schemeless_gateway_is_refused_rather_than_stored(tmp_path: Path, script: Script) -> None:
    """Review #12: the stranded state this PR prevents, reached through the form.

    A bare host parses with the whole string as the PATH, so there is no host:
    no suggestion is offered, the proxy stays at the loopback default, and
    ``is_loopback`` reads the empty host as local — suppressing the very caution
    that would have flagged it. Configured, green and stranded, four characters
    from correct.
    """
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        await app.content.add_content(ExplainabilityView(id="tracing"), set_current=True)
        await pilot.pause()
        await settle(app)
        _setup(app, target="stg", gateway="stg-x.aisquare.studio")
        app.screen.query_one("#explainability-save", Button).press()
        await pilot.pause()
        await settle(app)
        return await _toasts(app)

    rendered = drive(go, notifications=True)
    assert "scheme" in rendered and "https://stg-x.aisquare.studio" in rendered
    assert load_config().explainability.targets == {}, "nothing is stored"


@pytest.mark.parametrize("prefix", ["nishil-{role}", "nishil}", "team-{env}-{role}"])
def test_a_prefix_with_braces_is_refused_not_repaired(
    tmp_path: Path, script: Script, prefix: str
) -> None:
    """Review #10 and blocker B, re-decided in the #132 follow-ups. The field
    asks for a NAME; an operator who has read the ``--identity`` examples types
    ``nishil-{role}``, and composed again that is ``nishil-coder-coder``. The
    first cuts REPAIRED the input — stripped at the first brace and stored what
    preceded it — so the tab kept a template the operator never typed
    (``team-{env}-{role}`` became ``team-{role}``) while the CLI's ``--identity``
    refused the same input. One answer now: refused, with the reason, and
    nothing stored."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        await app.content.add_content(ExplainabilityView(id="tracing"), set_current=True)
        await pilot.pause()
        await settle(app)
        _setup(app, target="stg", prefix=prefix)
        app.screen.query_one("#explainability-save", Button).press()
        await pilot.pause()
        await settle(app)
        return await _toasts(app)

    rendered = drive(go, notifications=True)
    assert "is a name, not a template" in rendered
    assert "stg" not in load_config().explainability.targets, "refused: nothing was stored"


def test_the_form_can_name_the_key_variable(tmp_path: Path, script: Script) -> None:
    """Review #11: ``configure_target`` already took ``key_env`` and the form was
    its one caller omitting it, so a target configured with ``--key-env MY_VAR``
    got a success toast and no effect."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        await app.content.add_content(ExplainabilityView(id="tracing"), set_current=True)
        await pilot.pause()
        await settle(app)
        _setup(app, target="stg", key_env="MY_WORKSPACE_KEY")
        app.screen.query_one("#explainability-save", Button).press()
        await pilot.pause()
        await settle(app)

    drive(go, notifications=True)
    assert load_config().explainability.targets["stg"].api_key_env == "MY_WORKSPACE_KEY"


def test_saving_setup_does_not_by_itself_turn_tracing_on(tmp_path: Path, script: Script) -> None:
    """Consent stays a button (#50's boundary): configuring is not enabling."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        await app.content.add_content(ExplainabilityView(id="tracing"), set_current=True)
        await pilot.pause()
        await settle(app)
        app.screen.query_one("#explainability-gateway", Input).value = "https://g.example"
        app.screen.query_one("#explainability-save", Button).press()
        await pilot.pause()
        await settle(app)

    drive(go, notifications=True)
    assert load_config().explainability.enabled is False


def test_the_typed_key_is_never_rendered_back(tmp_path: Path, script: Script) -> None:
    """The field is cleared after a save. A masked Input still holds the value,
    and this view's own docstring rules the key out of a full-screen UI."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        await app.content.add_content(ExplainabilityView(id="tracing"), set_current=True)
        await pilot.pause()
        await settle(app)
        app.screen.query_one("#explainability-key", Input).value = "AIS_secret"
        app.screen.query_one("#explainability-save", Button).press()
        await pilot.pause()
        await settle(app)
        return app.screen.query_one("#explainability-key", Input).value

    assert drive(go, notifications=True) == ""


def test_a_blank_form_changes_nothing_and_says_so(tmp_path: Path, script: Script) -> None:
    """The negative half: pressing Save with nothing typed is not a write."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        await app.content.add_content(ExplainabilityView(id="tracing"), set_current=True)
        await pilot.pause()
        await settle(app)
        app.screen.query_one("#explainability-save", Button).press()
        return await _toasts(app)

    assert "nothing to save" in drive(go, notifications=True)
    assert load_config().explainability.targets == {}


def test_the_form_diagnoses_a_key_variable_no_shell_can_export_by_name(
    tmp_path: Path, script: Script
) -> None:
    """Round 7 of #203. The form's own key guard ran before the writer's
    validation, so a ``$EXPLAINABILITY_API_KEY`` paste with a key typed beside it
    was diagnosed as "export $$EXPLAINABILITY_API_KEY" — a sentence nobody can
    act on — while ``key_env_problem``, which names the exact fault, never ran.
    The writer's question first."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        await app.content.add_content(ExplainabilityView(id="tracing"), set_current=True)
        await pilot.pause()
        await settle(app)
        _setup(app, target="stg", key_env="$EXPLAINABILITY_API_KEY", key="wk-secret")
        app.screen.query_one("#explainability-save", Button).press()
        await pilot.pause()
        await settle(app)
        return await _toasts(app)

    rendered = drive(go, notifications=True)
    assert "without the $" in rendered and "EXPLAINABILITY_API_KEY" in rendered
    assert "$$" not in rendered, "never a variable nobody can export"
    assert "stg" not in load_config().explainability.targets, "refused: nothing stored"


def test_a_key_typed_for_a_target_that_names_its_own_variable_is_refused(
    tmp_path: Path, script: Script
) -> None:
    """Review blocker C. ``resolve_target`` reads the key file only for the
    default variable, so key + custom key variable in one save wrote a file
    nothing reads and named a variable nothing exports: ``✓ setup saved`` over
    ``$MY_WORKSPACE_KEY is NOT set``. The whole save is refused, with the reason."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        await app.content.add_content(ExplainabilityView(id="tracing"), set_current=True)
        await pilot.pause()
        await settle(app)
        _setup(app, target="stg", key_env="MY_WORKSPACE_KEY", key="AIS_typed")
        app.screen.query_one("#explainability-save", Button).press()
        await pilot.pause()
        await settle(app)
        return await _toasts(app)

    rendered = drive(go, notifications=True)
    assert "MY_WORKSPACE_KEY" in rendered and "never be used" in rendered
    assert load_config().explainability.targets == {}, "refused whole, not half"
    assert explainability_service.stored_api_key() is None, "and no key file was written"


def test_a_key_typed_for_a_target_that_already_names_its_own_variable_is_refused(
    tmp_path: Path, script: Script
) -> None:
    """The deeper half of C: the variable was stored last month and the field
    is blank today. The rule is judged against what the target will read from,
    not against the field."""
    seed(tmp_path, ("prj_a", "alpha", None))
    config = load_config()
    explainability_service.configure_target(
        config,
        target_name="prod",
        gateway_url="https://prod.example",
        key_env="PROD_KEY",
        enable=False,
    )
    save_config(config)

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        await app.content.add_content(ExplainabilityView(id="tracing"), set_current=True)
        await pilot.pause()
        await settle(app)
        _setup(app, target="prod", key="AIS_typed")
        app.screen.query_one("#explainability-save", Button).press()
        await pilot.pause()
        await settle(app)
        return await _toasts(app)

    rendered = drive(go, notifications=True)
    assert "PROD_KEY" in rendered
    assert explainability_service.stored_api_key() is None


def test_a_key_beside_the_default_variable_is_stored(tmp_path: Path, script: Script) -> None:
    """The negative half of C: naming the default variable explicitly is fine."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        await app.content.add_content(ExplainabilityView(id="tracing"), set_current=True)
        await pilot.pause()
        await settle(app)
        _setup(app, target="stg", key_env="EXPLAINABILITY_API_KEY", key="AIS_typed")
        app.screen.query_one("#explainability-save", Button).press()
        await pilot.pause()
        await settle(app)

    drive(go, notifications=True)
    assert explainability_service.stored_api_key() == "AIS_typed"
    assert load_config().explainability.targets["stg"].api_key_env == "EXPLAINABILITY_API_KEY"


def test_the_deployment_field_does_not_move_the_machine(tmp_path: Path, script: Script) -> None:
    """Review blocker D. An operator on stg correcting prod's gateway had moved
    the machine to prod — traffic to a deployment nobody chose, this tab's own
    headline failure from the other side. The entry is written; the machine
    stays; the toast says both and names the switch."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        await app.content.add_content(ExplainabilityView(id="tracing"), set_current=True)
        await pilot.pause()
        await settle(app)
        _setup(app, target="prod", gateway="https://prod.example")
        app.screen.query_one("#explainability-save", Button).press()
        await pilot.pause()
        await settle(app)
        return await _toasts(app)

    rendered = drive(go, notifications=True)
    settings = load_config().explainability
    assert settings.targets["prod"].gateway_url == "https://prod.example", "the correction lands"
    assert settings.target == "stg", "the machine stays where it was"
    assert "stays on 'stg'" in rendered and "make active" in rendered


def test_ticking_make_active_is_the_switch(tmp_path: Path, script: Script) -> None:
    """The affordance D asked for: moving the machine is a separate, visible act."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        await app.content.add_content(ExplainabilityView(id="tracing"), set_current=True)
        await pilot.pause()
        await settle(app)
        _setup(app, target="prod", gateway="https://prod.example")
        app.screen.query_one("#explainability-switch", Checkbox).value = True
        app.screen.query_one("#explainability-save", Button).press()
        await pilot.pause()
        await settle(app)

    drive(go, notifications=True)
    settings = load_config().explainability
    assert settings.target == "prod"
    assert settings.targets["prod"].gateway_url == "https://prod.example"


def test_a_deployment_name_alone_writes_nothing_and_says_so(tmp_path: Path, script: Script) -> None:
    """The sharper half of D: typing ONLY a name used to flip ``settings.target``
    while writing no entry, under a ``✓ setup saved`` toast."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        await app.content.add_content(ExplainabilityView(id="tracing"), set_current=True)
        await pilot.pause()
        await settle(app)
        _setup(app, target="prod")
        app.screen.query_one("#explainability-save", Button).press()
        return await _toasts(app)

    rendered = drive(go, notifications=True)
    settings = load_config().explainability
    assert settings.target == "stg" and settings.targets == {}
    assert "nothing to save" in rendered and "'prod'" in rendered


def test_a_deliberate_top_level_proxy_is_not_shadowed_by_the_suggestion(
    tmp_path: Path, script: Script
) -> None:
    """Review follow-up F. A top-level ``[explainability] proxy_url`` that is not
    the shipped default is a choice (``_proxy_source`` says ``config``), and the
    hosted suggestion must not be written over it as a per-target value."""
    seed(tmp_path, ("prj_a", "alpha", None))
    config = load_config()
    config.explainability.proxy_url = "http://127.0.0.1:9190"
    save_config(config)

    async def go(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        await app.content.add_content(ExplainabilityView(id="tracing"), set_current=True)
        await pilot.pause()
        await settle(app)
        _setup(app, target="stg", gateway="https://g.example")
        app.screen.query_one("#explainability-save", Button).press()
        await pilot.pause()
        await settle(app)

    drive(go, notifications=True)
    settings = load_config().explainability
    assert settings.targets["stg"].gateway_url == "https://g.example"
    assert settings.targets["stg"].proxy_url is None, "no suggestion over a chosen proxy"
    assert settings.proxy_url == "http://127.0.0.1:9190"


def test_a_malformed_gateway_is_told_apart_from_a_schemeless_one(
    tmp_path: Path, script: Script
) -> None:
    """Review follow-up I. ``http://[::1`` got "needs a scheme — try
    https://http://[::1"; the regression test asserted only that a toast
    appeared, so the wrong advice was uncovered."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        await app.content.add_content(ExplainabilityView(id="tracing"), set_current=True)
        await pilot.pause()
        await settle(app)
        _setup(app, target="stg", gateway="http://[::1")
        app.screen.query_one("#explainability-save", Button).press()
        await pilot.pause()
        await settle(app)
        return await _toasts(app)

    rendered = drive(go, notifications=True)
    assert "cannot be parsed" in rendered
    assert "https://http://" not in rendered
    assert load_config().explainability.targets == {}


def test_the_key_field_is_cleared_even_when_the_key_write_fails(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review follow-up M. A failed ``store_api_key`` returned before the field
    was cleared, so the plaintext stayed live in the widget for the session —
    against the view's own "never shown back". Cleared the moment a write
    begins, and the notice says to type it again."""
    seed(tmp_path, ("prj_a", "alpha", None))

    def refuse(_key: str) -> Path:
        raise OSError("disk says no")

    monkeypatch.setattr(explainability_service, "store_api_key", refuse)

    async def go(pilot: Pilot[None]) -> tuple[str, str]:
        app = fleet_app(pilot)
        await app.content.add_content(ExplainabilityView(id="tracing"), set_current=True)
        await pilot.pause()
        await settle(app)
        _setup(app, target="stg", gateway="https://g.example", key="AIS_secret")
        app.screen.query_one("#explainability-save", Button).press()
        await pilot.pause()
        await settle(app)
        return app.screen.query_one("#explainability-key", Input).value, await _toasts(app)

    value, rendered = drive(go, notifications=True)
    assert value == ""
    assert "could not be written" in rendered and "type it again" in rendered
    assert load_config().explainability.targets["stg"].gateway_url == "https://g.example"


def test_the_explainability_views_toasts_keep_bracketed_data(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one view the markup sweep missed. CONTRIBUTING: no markup in data.

    Its ``notify`` calls interpolate an OS error, a path, env values and gateway
    prose. Parsed as markup, ``'/home/me/[work]/.aisquare/config.toml'`` reaches
    the screen naming a directory that does not exist — and a ``[/x]`` anywhere
    in the same data raises ``MarkupError`` inside ``Toast.render``. The test
    lives here because the other Explainability tests are another package's
    file; the claim is the shell toast's, one view over.
    """
    seed(tmp_path, ("prj_a", "alpha", None))
    refused = tmp_path / "[work]" / ".aisquare" / "config.toml"

    def refuse(config: object) -> None:
        raise OSError(30, "Read-only file system", str(refused))

    monkeypatch.setattr(explainability_view, "save_config", refuse)

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        await app.content.add_content(ExplainabilityView(id="tracing"), set_current=True)
        await pilot.pause()
        await settle(app)  # the mount's status worker (tracing off: nothing is dialled)
        await pilot.click("#explainability-enable")
        return await _toasts(app)

    rendered = drive(go, notifications=True)
    # The message carries the OSError, and `OSError.__str__` renders its filename
    # through `repr()` — which doubles every backslash. So a raw `str(refused)`
    # is not in the toast on Windows even though the path is: the same escaped-
    # rendering trap as the `json.dumps` assertions ported earlier. Comparing
    # against the error's own text asserts the same thing on both platforms.
    assert str(OSError(30, "Read-only file system", str(refused))) in rendered
    assert "[work]" in rendered
    assert rendered.startswith("could not write the config:")
    # Control: the same string parsed AS MARKUP loses the bracketed directory —
    # the failure this assertion exists to catch, measured here.
    #
    # From `rendered` with only the separator neutralised, for the reason the
    # sibling test above records — a control rebuilt from literals would keep
    # passing after the product stopped emitting the path at all.
    as_markup = rendered.replace("\\", "/")
    assert "[work]" in as_markup, as_markup  # present before markup is asked to eat it
    assert "[work]" not in Content.from_markup(as_markup).plain


def test_project_onboarded_refreshes_and_selects_the_project(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> tuple[int, int, str | None]:
        app = fleet_app(pilot)
        cards_before = len(app.query(ProjectCard))
        seed(tmp_path, ("prj_n", "newcomer", None))  # what the onboard worker's `init` did
        app.post_message(ProjectOnboarded("prj_n", tmp_path / "newcomer"))
        await settle(app)
        return cards_before, len(app.query(ProjectCard)), app.content.current

    before, after, current = drive(go)
    assert (before, after) == (1, 2)
    assert current == "project-prj_n"


# --- focus model (§4.3) ------------------------------------------------------------


class RecordingPane(TerminalPane):
    """The scaffold pane plus a key log — proves what reached it. It does NOT stop the
    event, so the app's bindings are asked about every key; the gate under test is
    ``FleetApp.check_action``, not a pane that swallows keys."""

    def __init__(self) -> None:
        super().__init__("%9", id="recording-pane")
        self.keys: list[str] = []

    def on_key(self, event: events.Key) -> None:
        self.keys.append(event.key)


def test_q_quits_from_the_sidebar_but_reaches_a_focused_terminal_pane(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    probes = ("q", "ctrl+q", "ctrl+c", "f1", "t", "question_mark", "r")

    async def go(pilot: Pilot[None]) -> tuple[list[str], int | None, str, str, int | None]:
        app = fleet_app(pilot)
        pane = RecordingPane()
        await app.content.add_content(pane, set_current=True)
        pane.focus()
        await pilot.pause()
        assert isinstance(app.focused, TerminalPane)
        for key in probes:
            await pilot.press(key)
        # Every read waits for the app to go quiet. The app's keys are not priority
        # bindings: each runs once the key has bubbled from the focused widget back up
        # to the app, so after one pause a `q` that did quit could still be on its way,
        # and read then, "did not quit" passes whatever the pane does with the key.
        await settle(app)
        keys = list(pane.keys)
        alive = app.return_code
        screen_with_pane = type(app.screen).__name__
        # Positive control: the same keys are live once the sidebar has focus.
        app.sidebar.focus()
        await pilot.pause()
        await pilot.press("f1")
        await settle(app)
        screen_from_sidebar = type(app.screen).__name__
        await pilot.press("escape")
        await settle(app)
        await pilot.press("q")
        await settle(app)
        return keys, alive, screen_with_pane, screen_from_sidebar, app.return_code

    keys, alive, with_pane, from_sidebar, after_q = drive(go)
    assert keys == list(probes)  # every probe reached the pane, none was eaten
    assert alive is None  # …and q / ctrl+q did not quit
    # The default screen is the app's ``PaneScreen`` (the copy key outside a pane):
    # no palette (f1), theme picker (t) or help (?) opened over it.
    assert with_pane == "PaneScreen"
    assert from_sidebar == "CommandPalette"
    assert after_q == 0


def test_the_app_keys_are_refused_while_focus_is_in_a_view(tmp_path: Path, script: Script) -> None:
    """A form widget is not a ``TerminalPane`` and does not consume a letter either.

    Of everything the views mount only ``Input`` implements
    ``check_consume_key``, so with a ``Button`` or a ``Switch`` focused ``q``
    reached the app's binding and exited the fleet UI — taking the unsaved form
    with it (measured against the running app: ``q`` with the Settings tab's
    permission-mode ``Select`` focused set ``_exit``). The sidebar is the
    control: there the very same keys still quit, theme and help. ``?`` is the one
    exception: help opens from a form too — it neither quits nor changes anything,
    and it is how the board's icons are explained (an agent's pane still gets it:
    ``test_q_quits_from_the_sidebar_but_reaches_a_focused_terminal_pane``).
    """
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(
        pilot: Pilot[None],
    ) -> tuple[list[str], list[int | None], list[str], list[str], int | None]:
        app = fleet_app(pilot)
        button, switch = Button("save", id="probe-button"), Switch(id="probe-switch")
        await app.content.add_content(Vertical(button, switch, id="probe-form"), set_current=True)
        await pilot.pause()
        focused: list[str] = []
        codes: list[int | None] = []
        screens: list[str] = []
        helps: list[str] = []
        for widget in (button, switch):
            widget.focus()
            await pilot.pause()
            focused.append(type(app.focused).__name__)
            await pilot.press("q", "t", "f1")
            await settle(app)  # a refused key has bubbled all the way up and been refused
            codes.append(app.return_code)
            screens.append(type(app.screen).__name__)
            await pilot.press("question_mark")
            await settle(app)
            helps.append(type(app.screen).__name__)
            await pilot.press("escape")
            await settle(app)
        # The rule is "focus is IN the sidebar", not "focus IS the sidebar": a
        # focusable row would keep the keys live. The sidebar has none today, so
        # the probe is synthetic on purpose — it exercises the rule, not a widget.
        inside = Button("in the sidebar", id="probe-inside")
        await app.sidebar.mount(inside)
        inside.focus()
        await pilot.pause()
        focused.append(type(app.focused).__name__)
        await pilot.press("question_mark")
        await settle(app)
        screens.append(type(app.screen).__name__)
        await pilot.press("escape")
        await settle(app)
        # The control: the same keys from the sidebar itself do what they always did.
        app.sidebar.focus()
        await pilot.pause()
        await pilot.press("question_mark")
        await settle(app)
        screens.append(type(app.screen).__name__)
        await pilot.press("escape")
        await settle(app)
        await pilot.press("q")
        await settle(app)
        return focused, codes, screens, helps, app.return_code

    focused, codes, screens, helps, quit_code = drive(go)
    # The claim first: a quit here also stops the rest of the probe, and the
    # reader should read "q quit from a form", not "the second probe lost focus".
    assert codes == [None, None], "q in a form must not quit the fleet UI"
    assert focused == ["Button", "Switch", "Button"]  # the probes really had focus
    assert screens == ["PaneScreen", "PaneScreen", "HelpScreen", "HelpScreen"]
    assert helps == ["HelpScreen", "HelpScreen"], "? opens help from a form as well"
    assert quit_code == 0  # …and the sidebar still quits


def test_escape_hatch_focuses_the_sidebar(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> tuple[str, str]:
        app = fleet_app(pilot)
        pane = RecordingPane()
        await app.content.add_content(pane, set_current=True)
        pane.focus()
        await pilot.pause()
        before = type(app.focused).__name__
        pane.post_message(EscapeToSidebar())
        await settle(app)
        return before, type(app.focused).__name__

    assert drive(go) == ("RecordingPane", "Sidebar")


def test_help_opens_from_the_sidebar_and_closes(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> tuple[bool, bool, bool, str]:
        app = fleet_app(pilot)
        closed_before = not isinstance(app.screen, HelpScreen)
        await pilot.press("question_mark")
        await settle(app)
        opened = isinstance(app.screen, HelpScreen)
        keys = shown(app.screen.query_one("#helpbox Static", Static))
        await pilot.press("escape")
        await settle(app)
        return closed_before, opened, isinstance(app.screen, HelpScreen), keys

    closed_before, opened, still_open, keys = drive(go)
    assert (closed_before, opened, still_open) == (True, True, False)
    # The sidebar's arranging keys (#140) are all show=False in the footer: here is
    # the one place they are found (review of #171, round 1).
    for key in ("shift+↑ ↓", "g p space", "shift+click", "drag title"):
        assert key in keys, key
    # What sits beside and under ⚙ Settings is explained here too (review of #261, m2/m9).
    assert "✗ 1  ⚠ 2" in keys and "what the doctor's last run found" in keys
    assert "usage could not be read" in keys and "accounts that need a login" in keys


def test_help_opens_from_the_board_and_explains_every_task_icon(
    tmp_path: Path, script: Script
) -> None:
    """``?`` was live only from the sidebar, so the board's icons had nowhere to be explained
    from where they are seen. From the tasks table it opens the help, whose Board section
    lists the board's keys and one line per icon — the mapping the table draws from."""
    from aisquare.cli.ui.board import STATUS_ICONS

    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> tuple[str, bool, str]:
        app = fleet_app(pilot)
        await pilot.click(card_for(app, "prj_a").query_one(ProjectTitle))
        await settle(app)
        view = app.current_view()
        assert isinstance(view, ProjectView)
        view.active = "tab-board"
        await settle(app)
        view.query_one("#tasks").focus()
        await pilot.pause()
        focused = type(app.focused).__name__
        await pilot.press("question_mark")
        await settle(app)
        opened = isinstance(app.screen, HelpScreen)
        text = shown(app.screen.query_one("#helpbox Static", Static)) if opened else ""
        return focused, opened, text

    focused, opened, text = drive(go)
    assert focused == "PickableTable", "the premise: the keyboard is on the board's tasks"
    assert opened
    board = text[text.index("Board") :]
    for glyph, _style, meaning in STATUS_ICONS.values():
        assert glyph in board and meaning in board, meaning
    assert "done archive" in board and "drag a line to resize" in board


def test_help_from_the_board_opens_at_its_section_with_every_icon_in_view(
    tmp_path: Path, script: Script
) -> None:
    """At 160x45 the help's top and the board's icon legend do not fit on one screen, and
    from the board the legend sat ~45 lines down, below the fold (validator of PR #261,
    NIT 2). From the board — its tasks, or the tab row on it — the help opens with the
    Board section at its top, every icon painted; from the sidebar it opens at its top."""
    from aisquare.cli.ui.board import STATUS_ICONS
    from tests.test_ui_remote import painted

    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> list[str]:
        app = fleet_app(pilot)
        await pilot.click(card_for(app, "prj_a").query_one(ProjectTitle))
        await settle(app)
        view = app.current_view()
        assert isinstance(view, ProjectView)
        view.active = "tab-board"
        await settle(app)
        seen: list[str] = []
        for widget in (view.query_one("#tasks"), view.query_one(Tabs), app.sidebar):
            widget.focus()
            await pilot.pause()
            await pilot.press("question_mark")
            await settle(app)
            assert isinstance(app.screen, HelpScreen)
            seen.append("\n".join(painted(app)))
            await pilot.press("escape")
            await settle(app)
        return seen

    *from_the_board, from_the_sidebar = drive(go, size=(160, 45))
    meanings = [meaning for _glyph, _style, meaning in STATUS_ICONS.values()]
    for screen in from_the_board:
        assert "Board (a project's Board tab" in screen
        assert all(meaning in screen for meaning in meanings), screen
        # The heading is the box's first row: the key line right above it is scrolled off.
        # Its line in the text was not its row on screen — keys above it wrap (8 at 160x45).
        assert "this help — from anywhere" not in screen, screen
    assert "aisquare fleet — keys" in from_the_sidebar
    assert not all(meaning in from_the_sidebar for meaning in meanings), (
        "the premise: from the top, the legend is below the fold"
    )


def test_keyboard_cursor_walks_the_rows_and_enter_activates(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    script["prj_a"] = [status("prj_a", "manager", "manager", "waiting")]

    async def go(pilot: Pilot[None]) -> tuple[int, list[str], str | None]:
        app = fleet_app(pilot)
        app.sidebar.focus()
        await pilot.pause()
        cursors_before = len(app.query(".cursor"))
        walk: list[str] = []
        await pilot.press("up")  # at the top already: stays on the first row
        await settle(app)
        walk.append(type(app.query_one(".cursor")).__name__)
        await pilot.press("down")
        await settle(app)
        walk.append(type(app.query_one(".cursor")).__name__)
        await pilot.press("down")
        await settle(app)
        walk.append(type(app.query_one(".cursor")).__name__)
        await pilot.press("up")
        await settle(app)
        walk.append(type(app.query_one(".cursor")).__name__)
        await pilot.press("enter")
        await settle(app)
        return cursors_before, walk, app.content.current

    before, walk, current = drive(go)
    assert before == 0  # no cursor until a key is pressed
    assert walk == ["AddButton", "ProjectTitle", "AgentRow", "ProjectTitle"]
    assert current == "project-prj_a"  # Enter on the title opened the project
    assert len(walk) == len(set(walk)) + 1  # the cursor moved, it did not stick


def test_clicking_a_row_leaves_focus_on_the_sidebar_so_the_arrows_keep_working(
    tmp_path: Path, script: Script
) -> None:
    """A row is a non-focusable Static: mouse-down focuses its nearest focusable ancestor.

    With a focusable ``#projects`` that was the scroll, whose own up/down
    bindings then ate the arrows as soon as the list overflowed — the documented
    ``↑ ↓ Enter`` model died after any click. Measured on the unfixed tree:
    focus landed on ``VerticalScroll``, no ``.cursor`` row existed and each
    ``down`` moved ``scroll_y`` by one instead.
    """
    specs = [(f"prj_{index:02d}", f"p{index:02d}", None) for index in range(15)]
    seed(tmp_path, *specs)
    for project_id, _, _ in specs:
        script[project_id] = [status(project_id, "manager", "manager", "waiting")]

    async def go(pilot: Pilot[None]) -> tuple[float, str, list[str], list[float], str]:
        app = fleet_app(pilot)
        holder = app.sidebar.query_one("#projects", VerticalScroll)
        overflow = holder.max_scroll_y  # the precondition of the bug: the list scrolls
        await pilot.click(card_for(app, "prj_00").query_one(ProjectTitle))
        await settle(app)
        focused = type(app.focused).__name__
        walk: list[str] = []
        scrolls: list[float] = []
        for _ in range(2):
            await pilot.press("down")
            await settle(app)
            # Not query_one: with the arrows eaten there is no cursor at all, and
            # the claim should read as a diff, not as a NoMatches from the probe.
            cursors = app.query(".cursor").results(Activatable)
            walk.append(next((row.selection_key for row in cursors), "(no cursor)"))
            scrolls.append(holder.scroll_y)
        # Control: a widget that IS focusable still takes the click — focus was
        # not nailed to the sidebar, only handed the rows' clicks.
        pane = RecordingPane()
        await app.content.add_content(pane, set_current=True)
        await pilot.pause()  # let it lay out, or the click lands on the old view
        await pilot.click(pane)
        await pilot.pause()
        return overflow, focused, walk, scrolls, type(app.focused).__name__

    overflow, focused, walk, scrolls, on_pane = drive(go)
    assert overflow > 0, "the list must overflow, or the bug cannot show"
    assert focused == "Sidebar"
    assert walk == ["agent:agt_00_manager", "spawn:prj_00"]  # the arrows moved the cursor…
    assert scrolls == [0.0, 0.0]  # …and not the scroll
    assert on_pane == "RecordingPane"


def test_collapsing_the_card_under_the_cursor_leaves_one_cursor_and_moves_beside_it(
    tmp_path: Path, script: Script
) -> None:
    """Exactly one row may render as the keyboard cursor, collapsed cards included.

    ``_move_cursor`` re-applied the class over the VISIBLE rows only, so a row
    hidden inside a collapsed card kept it forever: two rows highlighted, and
    because the stale anchor was not in the visible list the next arrow jumped
    to the FIRST row instead of continuing beside the card. Both halves show up
    in the same walk.
    """
    seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None))
    script["prj_a"] = [status("prj_a", "manager", "manager", "waiting")]
    script["prj_b"] = [status("prj_b", "manager", "manager", "waiting")]

    async def go(pilot: Pilot[None]) -> tuple[list[str], list[str], list[str], list[str]]:
        app = fleet_app(pilot)

        def cursors() -> list[str]:
            return [row.selection_key for row in app.query(".cursor").results(Activatable)]

        app.sidebar.focus()
        await pilot.pause()
        for _ in range(3):  # add → project:prj_a → agent:agt_a_manager
            await pilot.press("down")
            await settle(app)
        walked = cursors()
        await pilot.click(card_for(app, "prj_a").query_one(Disclosure))  # hides the cursor's row
        await pilot.pause()
        collapsed = cursors()
        await pilot.press("down")
        await settle(app)
        moved = cursors()
        # Control: the walk goes on from there, one cursor at a time.
        await pilot.press("down")
        await settle(app)
        return walked, collapsed, moved, cursors()

    walked, collapsed, moved, onwards = drive(go)
    assert walked == ["agent:agt_a_manager"]
    assert collapsed == walked  # the hidden row keeps it until the cursor next moves
    assert moved == ["project:prj_b"], "one cursor row, and it resumed beside the collapsed card"
    assert onwards == ["agent:agt_b_manager"]


def test_the_cursor_skips_the_rows_of_a_collapsed_card(tmp_path: Path, script: Script) -> None:
    """Collapsing hides the rows through their ``#agents`` holder, not their own flag."""

    def walk(*, collapse: bool) -> list[str]:
        async def go(pilot: Pilot[None]) -> list[str]:
            app = fleet_app(pilot)
            card = card_for(app, "prj_a")
            if collapse:
                await pilot.click(card.query_one(Disclosure))
                await pilot.pause()
            assert card.query_one("#agents").display is not collapse
            app.sidebar.focus()
            await pilot.pause()
            keys: list[str] = []
            for _ in range(4):
                await pilot.press("down")
                await settle(app)
                keys.append(app.query_one(".cursor", Activatable).selection_key)
            return keys

        return drive(go)

    seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None))
    script["prj_a"] = [status("prj_a", "manager", "manager", "waiting")]
    script["prj_b"] = [status("prj_b", "manager", "manager", "waiting")]

    collapsed = walk(collapse=True)
    assert collapsed == ["add", "project:prj_a", "project:prj_b", "agent:agt_b_manager"]
    assert not any(key.endswith("agt_a_manager") or key == "spawn:prj_a" for key in collapsed), (
        "the cursor must not land on a row hidden inside the collapsed card"
    )
    # Control: expanded, the very same keys walk through those rows.
    assert walk(collapse=False) == [
        "add",
        "project:prj_a",
        "agent:agt_a_manager",
        "spawn:prj_a",
    ]


# --- bell, rebuilds, failing open -------------------------------------------------


def test_bell_rings_once_on_a_transition_into_attention(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    rings: list[int] = []

    def fake_bell(self: FleetApp) -> None:
        rings.append(1)

    monkeypatch.setattr(FleetApp, "bell", fake_bell)
    script["prj_a"] = [status("prj_a", "manager", "manager", "waiting")]

    async def go(pilot: Pilot[None]) -> list[int]:
        app = fleet_app(pilot)
        counts = [len(rings)]
        script["prj_a"] = [status("prj_a", "manager", "manager", "attention")]
        app.refresh_data()
        await settle(app)
        counts.append(len(rings))
        app.refresh_data()  # still attention: no second ring
        await settle(app)
        counts.append(len(rings))
        script["prj_a"] = [status("prj_a", "manager", "manager", "working")]
        app.refresh_data()
        await settle(app)
        counts.append(len(rings))
        script["prj_a"] = [status("prj_a", "manager", "manager", "attention")]
        app.refresh_data()  # a fresh transition rings again
        await settle(app)
        counts.append(len(rings))
        return counts

    assert drive(go) == [0, 1, 1, 1, 2]

    # Negative control: an agent that is ALREADY in attention on the first frame
    # is news the user opened the UI to see, not a transition — no bell.
    rings.clear()
    script["prj_a"] = [status("prj_a", "manager", "manager", "attention")]

    async def first_frame(pilot: Pilot[None]) -> int:
        fleet_app(pilot).refresh_data()
        await settle(fleet_app(pilot))
        return len(rings)

    assert drive(first_frame) == 0


def test_rebuilds_update_in_place_and_keep_the_selection(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    script["prj_a"] = [
        status("prj_a", "manager", "manager", "waiting"),
        status("prj_a", "coder-auth", "coder", "working", minute=1),
    ]

    async def go(pilot: Pilot[None]) -> tuple[bool, bool, bool, str, list[str], bool]:
        app = fleet_app(pilot)
        card = card_for(app, "prj_a")
        await pilot.click(row_for(app, "agt_a_coder-auth"))
        await settle(app)
        row = row_for(app, "agt_a_coder-auth")
        assert row.has_class("selected")
        # Tick 1: the coder changes state; the manager leaves; a tester arrives.
        script["prj_a"] = [
            status("prj_a", "coder-auth", "coder", "waiting", minute=1),
            status("prj_a", "tester-1", "tester", "working", minute=2),
        ]
        app.refresh_data()
        await settle(app)
        same_card = card_for(app, "prj_a") is card
        same_row = row_for(app, "agt_a_coder-auth") is row
        still_selected = row.has_class("selected")
        labels = [shown(r).split()[1] for r in card.query(AgentRow)]
        manager_gone = not app.query("#agent-row-agt_a_manager")
        return same_card, same_row, still_selected, shown(row), labels, manager_gone

    same_card, same_row, still_selected, coder_text, labels, manager_gone = drive(go)
    assert same_card and same_row  # updated in place — the widgets were not re-created
    assert still_selected
    assert coder_text.rstrip().endswith("⏸")  # …yet the new state is painted
    assert labels == ["coder-auth", "tester-1"]  # the newcomer mounted, in order
    assert manager_gone


def test_store_errors_keep_the_last_frame_and_say_so(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None))

    def broken() -> Iterator[ContextStore]:
        raise sqlite3.OperationalError("database is locked")

    async def go(pilot: Pilot[None]) -> tuple[list[str], list[str], str, bool, bool, bool]:
        app = fleet_app(pilot)
        assert app.snapshot is not None
        frame = app.snapshot
        notice = app.sidebar.query_one("#projects-notice", Static)
        quiet_before = not notice.display
        monkeypatch.setattr(app_mod, "store_session", broken)
        app.refresh_data()
        await settle(app)
        assert app.snapshot is not None
        kept = [p.id for p in app.snapshot.projects]
        cards = [c.project.id for c in app.query(ProjectCard)]
        stale = app.snapshot.stale_since is not None and app.snapshot.taken_at == frame.taken_at
        text = shown(notice) if notice.display else ""
        monkeypatch.setattr(app_mod, "store_session", store_session)
        app.refresh_data()
        await settle(app)
        recovered = not notice.display and app.snapshot.stale_since is None
        return kept, cards, text, stale, quiet_before, recovered

    kept, cards, text, stale, quiet_before, recovered = drive(go)
    assert kept == ["prj_a", "prj_b"] and cards == kept  # the frame survived the outage
    assert stale
    assert quiet_before and "store unreadable" in text  # …and was labelled as such
    assert recovered


FLEET_HOLD_S = 10.0
"""How long a scripted fleet read waits for its gate before giving up on the test."""


def test_the_tick_reads_the_store_and_the_agents_off_the_ui_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``refresh_data`` opened the store and asked ``list_agents`` (tmux, ~11 ms a project)
    on the event loop every two seconds: ~44 ms with four projects, and every click,
    scroll or key that landed then waited for it (measured 2026-10-20, "everything feels
    sluggish, happens after a tick"). Both run in a thread now, and the frame they read
    is still painted. The first frame, read at mount before anything is drawn, is not
    the tick's and is not counted."""
    seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None))
    # The captain's home board too, so its read lists the home's agents as well (review
    # of #272, m4): without it, the captain's read found no home and asked nothing.
    home = captain_state.home_project()
    threads: dict[str, list[threading.Thread]] = {"store": [], "list_agents": []}
    listed: list[str] = []
    answer = ["waiting"]

    def fake(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        threads["list_agents"].append(threading.current_thread())
        listed.append(project.id)
        if project.id == home.id:
            return []
        return [status(project.id, "manager", "manager", answer[0])]

    @contextmanager
    def recorded() -> Iterator[ContextStore]:
        threads["store"].append(threading.current_thread())
        with store_session() as store:
            yield store

    list_agents_as(monkeypatch, fake)

    async def go(pilot: Pilot[None]) -> tuple[str, str, bool]:
        app = fleet_app(pilot)
        for seen in threads.values():
            seen.clear()
        listed.clear()
        monkeypatch.setattr(app_mod, "store_session", recorded)
        answer[0] = "working"
        app.refresh_data()  # what the two-second timer runs
        await settle(app)
        rows = shown(row_for(app, "agt_a_manager")), shown(row_for(app, "agt_b_manager"))
        return *rows, app.captain_on

    row_a, row_b, captain = drive(go)
    # With the captain on, the read also looks up its home board, in the fleet's own store
    # session (review of #272, m5: it was a second one), and lists the home's agents
    # (``_read_captain``): one more list_agents, off the UI thread as well (m4).
    assert len(threads["store"]) == 1, threads
    assert sorted(listed) == sorted(["prj_a", "prj_b", *([home.id] if captain else [])])
    main = threading.main_thread()
    assert all(t is not main for seen in threads.values() for t in seen), threads
    assert row_a.rstrip().endswith("▶") and row_b.rstrip().endswith("▶")  # …and painted


def test_a_tick_that_changed_nothing_repaints_no_row_and_a_change_repaints_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With 50 listed projects every tick repainted every card, 15 ms of the UI thread at
    50 (#83): a card shown what it already shows is skipped, and one agent's change costs
    that agent's row."""
    seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None), ("prj_c", "gamma", None))
    answer = {"prj_a": "waiting", "prj_b": "waiting", "prj_c": "waiting"}

    def fake(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        return [status(project.id, "manager", "manager", answer[project.id])]

    list_agents_as(monkeypatch, fake)
    painted: list[str] = []
    show = AgentRow.show

    def counting(row: AgentRow, fresh: FleetAgentStatus) -> None:
        painted.append(fresh.agent.id)
        show(row, fresh)

    monkeypatch.setattr(AgentRow, "show", counting)

    async def go(pilot: Pilot[None]) -> tuple[list[str], list[str], str]:
        app = fleet_app(pilot)
        app.refresh_data()
        await settle(app)
        painted.clear()
        app.refresh_data()  # the same answer
        await settle(app)
        same = list(painted)
        painted.clear()
        answer["prj_b"] = "working"
        app.refresh_data()
        await settle(app)
        return same, list(painted), shown(row_for(app, "agt_b_manager"))

    same, changed, row_b = drive(go)
    assert same == []
    assert changed == ["agt_b_manager"]
    assert row_b.rstrip().endswith("▶")


def test_ticks_while_a_fleet_read_is_in_flight_start_no_second_read_and_cost_one_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One fleet read at a time, as the accounts' read (``refresh_accounts``): a read held
    by tmux or a busy store outlives the tick, and an ``exclusive`` worker per tick would
    pile a thread up per tick, since a cancelled thread runs on. A tick that finds a read
    in flight starts nothing and owes one read, made once the first has painted: three
    ticks during one wait cost ONE more read, and it sees what changed meanwhile."""
    seed(tmp_path, ("prj_a", "alpha", None))
    answer = ["waiting"]
    reads: list[str] = []  # what each read was told, in order
    running, peak = [0], [0]
    counting = threading.Lock()
    hold, asked, gate = threading.Event(), threading.Event(), threading.Event()

    def fake(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        with counting:
            told = answer[0]
            reads.append(told)
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        try:
            if hold.is_set():
                hold.clear()
                asked.set()
                gate.wait(FLEET_HOLD_S)  # as tmux, or a writer holding the store, would
            return [status(project.id, "manager", "manager", told)]
        finally:
            with counting:
                running[0] -= 1

    list_agents_as(monkeypatch, fake)

    async def go(pilot: Pilot[None]) -> tuple[int, str, str]:
        app = fleet_app(pilot)
        reads.clear()
        answer[0] = "working"
        hold.set()
        try:
            app.refresh_data()  # a tick: its read waits
            assert await asyncio.to_thread(asked.wait, FLEET_HOLD_S), "the tick read"
            answer[0] = "attention"  # what changed while it waited
            app.refresh_data()  # three more ticks while it waits
            app.refresh_data()
            app.refresh_data()
            await pilot.pause()
            in_flight = sum(
                1 for w in app.workers if w.name == app_mod.FLEET_WORKER and not w.is_finished
            )
            while_held = shown(row_for(app, "agt_a_manager"))
        finally:
            gate.set()
        await settle(app)
        return in_flight, while_held, shown(row_for(app, "agt_a_manager"))

    in_flight, while_held, after = drive(go)
    assert in_flight == 1, f"{in_flight} fleet reads in flight"
    assert peak[0] == 1, f"{peak[0]} reads at once"
    assert while_held.rstrip().endswith("⏸")  # the frame shown is kept while the read waits
    assert reads == ["working", "attention"], reads  # the held read, then ONE owed read
    assert "🔔" in after  # …whose frame is the one painted last


def test_a_frame_painted_late_leaves_the_owed_read_to_the_read_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of 738d5594: a read that has ended is painted when the app gets to its
    ``StateChanged``, and a tick can come first. That tick finds no read in flight and
    starts the next, and a call while that one runs owes one more. The late frame then
    started the owed read itself, beside the read in flight: two reads at once (10 of 10
    runs). The read in flight starts it once it has painted, one read at a time, and the
    calls still cost one read after it."""
    seed(tmp_path, ("prj_a", "alpha", None))
    asked = {n: threading.Event() for n in range(1, 5)}
    gates = {n: threading.Event() for n in range(1, 5)}
    reads: list[int] = []  # the reads the test numbered, in order
    running, peak = [0], [0]
    counting = threading.Lock()
    numbering = threading.Event()

    def fake(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        with counting:
            n = 0
            if numbering.is_set():
                reads.append(len(reads) + 1)
                n = reads[-1]
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        try:
            if n:
                asked[n].set()
                gates[n].wait(FLEET_HOLD_S)  # as tmux, or a writer holding the store, would
            return [status(project.id, "manager", "manager", "working")]
        finally:
            with counting:
                running[0] -= 1

    list_agents_as(monkeypatch, fake)

    async def go(pilot: Pilot[None]) -> int:
        app = fleet_app(pilot)
        painted = asyncio.Event()  # the first read's frame, and what it started after it
        numbering.set()
        try:
            app.refresh_data(then=painted.set)  # a tick: its read waits
            assert await asyncio.to_thread(asked[1].wait, FLEET_HOLD_S), "the tick read"
            [first] = [
                w for w in app.workers if w.name == app_mod.FLEET_WORKER and not w.is_finished
            ]

            async def tick_ahead_of_the_frame() -> None:
                # The app handles nothing else while this callback runs, so the first
                # read's StateChanged queues behind the tick and the call below.
                gates[1].set()
                await first.wait()
                app.refresh_data()  # finds no read in flight, and starts one
                assert await asyncio.to_thread(asked[2].wait, FLEET_HOLD_S), "the next read"
                app.refresh_data()  # a call while it runs: one read owed

            app.call_later(tick_ahead_of_the_frame)
            await asyncio.wait_for(painted.wait(), FLEET_HOLD_S)
            in_flight = sum(
                1 for w in app.workers if w.name == app_mod.FLEET_WORKER and not w.is_finished
            )
        finally:
            for gate in gates.values():
                gate.set()
        await settle(app)
        return in_flight

    in_flight = drive(go)
    assert in_flight == 1, f"{in_flight} fleet reads in flight"
    assert peak[0] == 1, f"{peak[0]} reads at once"
    assert reads == [1, 2, 3], reads  # the read, the tick's, and ONE owed read after it


def test_a_fleet_read_that_hangs_labels_the_frame_kept_once_it_has_waited_long_enough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read that HANGS, not one that fails — a store on a hung mount, a wedged tmux at
    30 s a call — left the sidebar silently stale: each tick only owed one more read,
    and nothing raised to label the frame (review of #272). Before the read moved off
    the UI thread the whole UI froze, which was worse but obvious. Once a read has been
    in flight :data:`~aisquare.cli.ui.app.FLEET_STALE_AFTER`, a tick says the frame shown
    is an old one; the read's own frame takes the notice away.

    The bound is a minute and the read's start is moved back past it, not a 0.3 s bound
    raced in real time: under load the "before" tick came after 0.3 s and was labelled
    (the local gate at cc8d5e0c, and 1 in 3 alone)."""
    seed(tmp_path, ("prj_a", "alpha", None))
    monkeypatch.setattr(app_mod, "FLEET_STALE_AFTER", 60.0)
    hold, asked, gate = threading.Event(), threading.Event(), threading.Event()

    def fake(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        if hold.is_set():
            hold.clear()
            asked.set()
            gate.wait(FLEET_HOLD_S)
        return [status(project.id, "manager", "manager", "working")]

    list_agents_as(monkeypatch, fake)

    async def go(pilot: Pilot[None]) -> tuple[bool, str, str, bool]:
        app = fleet_app(pilot)
        notice = app.sidebar.query_one("#projects-notice", Static)
        hold.set()
        try:
            app.refresh_data()  # a tick whose read hangs
            assert await asyncio.to_thread(asked.wait, FLEET_HOLD_S), "the tick read"
            app.refresh_data()  # a tick before the bound: nothing to say yet
            await pilot.pause()
            early = bool(notice.display)
            app._fleet_since -= 61.0  # the read has now been in flight past the bound
            app.refresh_data()  # a tick after it
            await pilot.pause()
            late = shown(notice) if notice.display else ""
            stale = app.snapshot is not None and app.snapshot.stale_since is not None
        finally:
            gate.set()
        await settle(app)
        return early, late, shown(notice) if notice.display else "", stale

    early, late, after, stale = drive(go)
    assert not early
    assert "showing the frame from" in late, late
    assert stale
    assert after == "", after  # the read's own frame, painted at last, clears it


def test_agent_errors_fail_open_with_the_cost_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None))
    healthy = [status("prj_b", "manager", "manager", "waiting")]

    def fake(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        if project.id == "prj_a":
            raise fleet_service.FleetUnavailable("tmux 3.1 is too old (need 3.2)")
        return list(healthy)

    list_agents_as(monkeypatch, fake)

    async def go(pilot: Pilot[None]) -> tuple[int, bool, str, int, bool, dict[str, str]]:
        app = fleet_app(pilot)
        broken_card, fine_card = card_for(app, "prj_a"), card_for(app, "prj_b")
        broken_notice = broken_card.query_one(".card-notice", Static)
        fine_notice = fine_card.query_one(".card-notice", Static)
        assert app.snapshot is not None
        return (
            len(broken_card.query(AgentRow)),
            broken_notice.display,
            shown(broken_notice),
            len(fine_card.query(AgentRow)),
            fine_notice.display,
            dict(app.snapshot.notices),
        )

    rows_a, shown_a, text_a, rows_b, shown_b, notices = drive(go)
    assert rows_a == 0 and shown_a and "tmux 3.1 is too old" in text_a
    assert rows_b == 1 and not shown_b  # the healthy project is untouched by the other's failure
    assert set(notices) == {"prj_a"}


def test_a_fleet_read_that_failed_open_keeps_the_live_manager_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read that could not ask must not reach the screen as "there is no manager".

    ``refresh_data`` records ``agents[id] = []`` plus a notice when a fleet read
    fails open (one momentarily locked sqlite db is enough — ``list_agents``
    opens its own session). Handed on, that empty list made ``ManagerTab.show``
    detach the RUNNING manager's pane, hide it and paint the Start-manager
    button. The control is the same push with the fleet ANSWERING none, which
    must still tear the pane down.
    """
    seed(tmp_path, ("prj_a", "alpha", None))
    live = [status("prj_a", "manager", "manager", "working")]
    failure: list[Exception] = []

    def fake(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        if failure:
            raise failure[0]
        return list(live)

    list_agents_as(monkeypatch, fake)
    State = tuple[str | None, bool, bool, str]

    async def go(pilot: Pilot[None]) -> tuple[State, State, str, State]:
        app = fleet_app(pilot)
        await pilot.click(card_for(app, "prj_a").query_one(ProjectTitle))
        await settle(app)
        view = app.current_view()
        assert isinstance(view, ProjectView)
        tab = view.query_one(ManagerTab)
        header = tab.query_one("#manager-header", Static)
        pane = tab.query_one(TerminalPane)
        button = tab.query_one("#start-manager", Button)

        def state() -> State:
            return pane.pane_id, pane.display, button.display, shown(header)

        app.refresh_data()  # the first frame the freshly opened view is handed
        await settle(app)
        attached = state()
        failure.append(fleet_service.FleetUnavailable("tmux 3.1 is too old (need 3.2)"))
        app.refresh_data()
        await settle(app)
        failed_open = state()
        card_notice = shown(card_for(app, "prj_a").query_one(".card-notice", Static))
        # The control: the fleet answers, and its answer is that there is none.
        failure.clear()
        live.clear()
        app.refresh_data()
        await settle(app)
        return attached, failed_open, card_notice, state()

    attached, failed_open, card_notice, answered = drive(go)
    assert attached[:3] == ("%1", True, False) and "manager" in attached[3]
    assert failed_open[:3] == ("%1", True, False), "the live pane survived a read that failed open"
    assert "has no manager yet" not in failed_open[3]
    assert failed_open[3] == attached[3]  # …and the header still names the manager
    assert "tmux 3.1 is too old" in card_notice  # the cost is shown, on the project's card
    assert answered[:3] == (None, False, True)  # the fleet ANSWERED none: the pane goes
    assert "has no manager yet" in answered[3]


def test_open_views_are_fed_each_frame_and_survive_a_vanished_row(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    script["prj_a"] = [
        status("prj_a", "manager", "manager", "waiting"),
        status("prj_a", "coder-auth", "coder", "working", minute=1),
    ]

    async def go(pilot: Pilot[None]) -> tuple[str, str, str, str | None, str | None, int | None]:
        app = fleet_app(pilot)
        await pilot.click(row_for(app, "agt_a_coder-auth"))
        await settle(app)
        view = app.current_view()
        assert isinstance(view, AgentView)
        opened_with = view.status.state
        # The agent flips to attention: the open view must see it on the next frame.
        script["prj_a"] = [
            status("prj_a", "manager", "manager", "waiting"),
            status("prj_a", "coder-auth", "coder", "attention", minute=1),
        ]
        app.refresh_data()
        await settle(app)
        after_change = view.status.state
        # Negative control: the manager's row changed too, but no view is open for it —
        # and the project's codename arrives, which the project view (once opened) shows.
        await pilot.click(card_for(app, "prj_a").query_one(ProjectTitle))
        await settle(app)
        project_view = app.current_view()
        assert isinstance(project_view, ProjectView)
        codename_before = project_view.project.codename
        with store_session() as store:
            store.set_codename("prj_a", "amber-otter")
        # The agent leaves the frame entirely: the view keeps its last row, nothing raises.
        script["prj_a"] = [status("prj_a", "manager", "manager", "waiting")]
        app.refresh_data()
        await settle(app)
        return (
            opened_with,
            after_change,
            view.status.state,
            codename_before,
            project_view.project.codename,
            app.return_code,
        )

    opened, changed, kept, codename_before, codename_after, code = drive(go)
    assert opened == "working" and changed == "attention"  # the open view followed the agent
    assert kept == "attention"  # a vanished row is not erased — the last known state stays
    assert codename_before is None and codename_after == "amber-otter"
    assert code is None


def test_r_refreshes_from_the_sidebar_but_is_forwarded_from_a_pane(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    script["prj_a"] = [status("prj_a", "manager", "manager", "waiting")]

    async def go(pilot: Pilot[None]) -> tuple[str, str, str, list[str]]:
        app = fleet_app(pilot)
        row = row_for(app, "agt_a_manager")
        first = shown(row)
        script["prj_a"] = [status("prj_a", "manager", "manager", "working")]
        app.sidebar.focus()
        await pilot.press("r")
        await settle(app)
        from_sidebar = shown(row)
        # The same key with a pane focused reaches the pane and refreshes nothing.
        pane = RecordingPane()
        await app.content.add_content(pane, set_current=True)
        pane.focus()
        await pilot.pause()
        script["prj_a"] = [status("prj_a", "manager", "manager", "attention")]
        await pilot.press("r")
        # Quiet before the read, as above: a refresh the key set off after one pause
        # could land after it, and "nothing refreshed" would pass without being tested.
        await settle(app)
        return first, from_sidebar, shown(row), list(pane.keys)

    first, from_sidebar, from_pane, keys = drive(go)
    assert first.rstrip().endswith("⏸")
    assert from_sidebar.rstrip().endswith("▶")  # r re-read the fleet
    assert from_pane.rstrip().endswith("▶") and "🔔" not in from_pane  # …and did not, here
    assert keys == ["r"]


def test_r_refreshes_the_fleet_and_neither_r_nor_a_runs_the_doctor(
    tmp_path: Path, script: Script, project_capture: None
) -> None:
    """`r` is "refresh now" — the fleet. The doctor runs at start, when its page opens,
    after a fix and from its Re-run button: seconds of probing a key press must not cost.
    `a` changes which cards are shown, not what the doctor finds."""
    seed(tmp_path, ("prj_a", "alpha", None))
    calls: list[int] = []

    def doctor() -> list[DoctorCheck]:
        calls.append(1)
        return []

    async def go(pilot: Pilot[None]) -> tuple[int, int, int, int]:
        app = fleet_app(pilot)
        await settle(app)
        reads: list[int] = []
        read = app.refresh_data

        def counted(*, then: Callable[[], object] | None = None) -> None:
            reads.append(1)
            read(then=then)

        app.refresh_data = counted  # type: ignore[method-assign]
        app.sidebar.focus()
        at_mount = len(calls)
        await pilot.press("r")
        await pilot.pause()
        await settle(app)
        after_r, reads_after_r = len(calls), len(reads)
        await pilot.press("a")
        await pilot.pause()
        await settle(app)
        return at_mount, reads_after_r, after_r, len(calls)

    at_mount, fleet_reads, after_r, after_a = drive(go, doctor=doctor)
    assert at_mount == 1
    assert fleet_reads >= 1, "r re-read the fleet"
    assert after_r == 1, "r does not run the doctor"
    assert after_a == 1, "nor does a"


# --- theme -------------------------------------------------------------------------


def test_theme_picker_applies_live_and_autosaves(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))

    async def browse(pilot: Pilot[None]) -> tuple[bool, str, str, str]:
        app = fleet_app(pilot)
        initial = str(app.theme)
        await pilot.press("t")
        await settle(app)
        assert isinstance(app.screen, ThemePicker)
        await pilot.press("down")  # browsing applies instantly…
        await pilot.press("down")
        await settle(app)  # the picker applies the theme from its highlight, a message
        still_open = isinstance(app.screen, ThemePicker)
        applied = str(app.theme)
        await pilot.press("escape")  # …until the explicit close
        await settle(app)
        assert not isinstance(app.screen, ThemePicker)
        return still_open, initial, applied, str(app.theme)

    still_open, initial, applied, final = drive(browse)
    assert still_open  # selection does NOT close the dialog
    assert applied != initial and applied == final  # the browsed theme stuck
    saved = json.loads((isolated_home / "state.json").read_text())["board_theme"]
    assert saved == final  # autosaved, under the board's key — one preference for both UIs

    async def relaunch(pilot: Pilot[None]) -> str:
        return str(fleet_app(pilot).theme)

    assert drive(relaunch) == final  # restored on the next launch


# --- stopping an agent --------------------------------------------------------------


def ended(agent: FleetAgent, *, exit_status: int = 0) -> FleetAgent:
    """The row ``services.fleet.stop`` answers with: the same agent, ended."""
    return agent.model_copy(update={"ended_at": datetime.now(tz=UTC), "exit_status": exit_status})


class StopRecorder:
    """Stands in for ``services.fleet.stop``: records every call, answers a receipt.

    What these tests assert is what the SERVICE was asked — the project, the
    label, ``force`` and the row id the stop is pinned to — never the sentence
    the dialog showed on the way there. The answer is the release train's
    :class:`services.fleet.StopReceipt`: the ended row, and ``release_failed``
    when the store refused to release the ended session's claims.
    """

    def __init__(self, agent: FleetAgent) -> None:
        self.agent = agent
        self.calls: list[tuple[str, str, bool, str | None]] = []
        self.refuse: Exception | None = None
        self.release_failed: str | None = None

    def __call__(
        self,
        project: ProjectInfo,
        label: str,
        *,
        force: bool = False,
        grace: float = 5.0,
        agent_id: str | None = None,
        actor: str | None = None,
    ) -> fleet_service.StopReceipt:
        self.calls.append((project.id, label, force, agent_id))
        if self.refuse is not None:
            raise self.refuse
        return fleet_service.StopReceipt(ended(self.agent), [], self.release_failed)


def stopper(monkeypatch: pytest.MonkeyPatch, agent: FleetAgent) -> StopRecorder:
    recorder = StopRecorder(agent)
    monkeypatch.setattr(fleet_service, "stop", recorder)
    return recorder


async def open_stop_dialog(pilot: Pilot[None], app: FleetApp, agent_id: str) -> StopAgentScreen:
    """Open the agent's view and press ITS Stop button — the dialog the shell pushes.

    The button is taken from the view ON SCREEN rather than by a bare
    ``#agent-stop`` selector: the ``ContentSwitcher`` keeps every agent view it
    has ever mounted, so a test that opens a second agent would otherwise click
    the first view's button, which is not on screen and opens nothing.

    The click lands as a person's would, a moment after the view came up: past the click
    guard's window (``click_guard``), which the guard's own clock is moved on for.
    """
    from aisquare.cli.ui import click_guard

    await pilot.click(row_for(app, agent_id))
    await pilot.pause()
    await settle(app)
    view = app.current_view()
    assert isinstance(view, AgentView), f"the row opened {type(view).__name__}"
    real = click_guard.clock
    click_guard.clock = lambda: real() + click_guard.CLICK_GUARD + 1
    try:
        await pilot.click(view.query_one("#agent-stop", Button))
        await pilot.pause()
    finally:
        click_guard.clock = real
    screen = app.screen
    assert isinstance(screen, StopAgentScreen), f"Stop opened {type(screen).__name__}"
    return screen


def test_stop_from_an_agent_view_still_asks_after_its_projects_read_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #240, "also confirmed": a refresh whose read of the project failed lists
    none of its agents (``refresh_data`` fails open, the cost on the card), and Stop on
    the agent's still-open view was then refused as "no longer listed" while the agent
    ran. The dialog opens on the row the view shows; the service decides."""
    from aisquare.cli.ui import click_guard

    seed(tmp_path, ("prj_a", "alpha", None))
    live = [status("prj_a", "coder-auth", "coder", "working")]
    failing: list[Exception] = []

    def fake(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        if failing:
            raise failing[0]
        return list(live)

    list_agents_as(monkeypatch, fake)

    async def go(pilot: Pilot[None]) -> tuple[str, list[str]]:
        app = fleet_app(pilot)
        await pilot.click(row_for(app, "agt_a_coder-auth"))
        await pilot.pause()
        view = app.current_view()
        assert isinstance(view, AgentView)
        failing.append(fleet_service.FleetUnavailable("database is locked"))
        await refreshed(app)
        real = click_guard.clock
        click_guard.clock = lambda: real() + click_guard.CLICK_GUARD + 1
        try:
            await pilot.click(view.query_one("#agent-stop", Button))
            await pilot.pause()
        finally:
            click_guard.clock = real
        said = [str(n.message) for n in app._notifications]
        return type(app.screen).__name__, said

    screen, said = drive(go)
    assert screen == "StopAgentScreen", said
    assert not any("no longer listed" in line for line in said)


def test_the_agent_view_offers_stop_exactly_while_there_is_a_process(
    tmp_path: Path, script: Script
) -> None:
    """Every state in ``STOP_STATES`` shows Stop and no other state does.

    That constant is ``ALIVE_STATES`` — the card's "agents alive" rule — plus 💤
    exited, whose dead window Stop removes (#138), so the view, the chip and
    this test cannot drift from each other: a state added to it must gain the
    button, a state taken out of it must lose it. ✗ lost has neither a process
    nor a window to remove: a lost row is ``fleet reap``'s business.
    """
    seed(tmp_path, ("prj_a", "alpha", None))
    states = sorted(STATE_CHIP)
    script["prj_a"] = [
        status(
            "prj_a",
            f"agent-{state}",
            "coder",
            state,
            minute=index,
            exit_status=0 if state == "exited" else None,
        )
        for index, state in enumerate(states)
    ]

    async def go(pilot: Pilot[None]) -> set[str]:
        app = fleet_app(pilot)
        offered: set[str] = set()
        for state in states:
            await pilot.click(row_for(app, f"agt_a_agent-{state}"))
            await pilot.pause()
            view = app.current_view()
            assert isinstance(view, AgentView)
            if view.query_one("#agent-stop", Button).display:
                offered.add(state)
        return offered

    offered = drive(go)
    assert offered == set(STOP_STATES)
    assert set(STOP_STATES) < set(states), "the premise: some states offer no Stop"
    assert "exited" in STOP_STATES and "lost" not in STOP_STATES


def test_confirming_stop_asks_the_service_once_with_force_false(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    live = status("prj_a", "coder-auth", "coder", "working")
    script["prj_a"] = [live]
    recorder = stopper(monkeypatch, live.agent)

    async def go(pilot: Pilot[None]) -> bool:
        app = fleet_app(pilot)
        await open_stop_dialog(pilot, app, "agt_a_coder-auth")
        await pilot.click("#stop-confirm")
        await settle(app)
        await pilot.pause()
        return isinstance(app.screen, StopAgentScreen)

    still_open = drive(go)
    assert recorder.calls == [
        ("prj_a", "coder-auth", False, "agt_a_coder-auth")
    ]  # the graceful /exit, exactly once
    assert not still_open  # a stop that worked closes its own dialog


def test_force_asks_the_service_to_skip_the_graceful_exit(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    live = status("prj_a", "coder-auth", "coder", "working")
    script["prj_a"] = [live]
    recorder = stopper(monkeypatch, live.agent)

    async def go(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        await open_stop_dialog(pilot, app, "agt_a_coder-auth")
        await pilot.click("#stop-force")
        await settle(app)
        await pilot.pause()

    drive(go)
    assert recorder.calls == [("prj_a", "coder-auth", True, "agt_a_coder-auth")]


def test_cancelling_the_stop_dialog_calls_nothing(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    live = status("prj_a", "coder-auth", "coder", "working")
    script["prj_a"] = [live]
    recorder = stopper(monkeypatch, live.agent)

    async def go(pilot: Pilot[None]) -> tuple[bool, str]:
        app = fleet_app(pilot)
        await open_stop_dialog(pilot, app, "agt_a_coder-auth")
        await pilot.click("#stop-cancel")
        await pilot.pause()
        return isinstance(app.screen, StopAgentScreen), type(app.current_view()).__name__

    still_open, view = drive(go)
    assert recorder.calls == []  # Cancel asks the service nothing at all
    assert not still_open and view == "AgentView"


def test_a_refusal_stays_in_the_dialog_with_its_reason_intact(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``stop`` refuses when tmux cannot confirm the pane died; the dialog must SAY so.

    The service leaves the row live on purpose rather than printing ✓ over an
    agent that is still running. A dialog that closed on that answer — or an app
    that died on it — would undo exactly that honesty, so the reason stays on the
    status line, the dialog stays open, and the app keeps taking keys.
    """
    seed(tmp_path, ("prj_a", "alpha", None))
    live = status("prj_a", "coder-auth", "coder", "working")
    script["prj_a"] = [live]
    recorder = stopper(monkeypatch, live.agent)
    recorder.refuse = fleet_service.FleetError(
        "could not stop 'coder-auth' ([red] pane 3 refused, tmux did not answer) — its pane "
        "is still alive, so its row is left live"
    )

    async def go(pilot: Pilot[None]) -> tuple[str, bool, bool, str]:
        app = fleet_app(pilot)
        screen = await open_stop_dialog(pilot, app, "agt_a_coder-auth")
        await pilot.click("#stop-confirm")
        await settle(app)
        await pilot.pause()
        note = shown(screen.query_one("#stop-status", Static))
        open_after = isinstance(app.screen, StopAgentScreen)
        await pilot.press("escape")  # the app still answers keys after a refusal
        await pilot.pause()
        return (
            note,
            open_after,
            isinstance(app.screen, StopAgentScreen),
            type(app.current_view()).__name__,
        )

    note, open_after, open_at_end, view = drive(go)
    assert "tmux did not answer" in note and "[red]" in note
    assert open_after, "the refusal must not close the dialog"
    assert not open_at_end and view == "AgentView"  # …and the app is alive to close it
    # Control: the same reason rendered AS MARKUP loses the tag — and `[red]` is a
    # REAL Rich style, so this cannot be argued to survive by accident the way an
    # unknown tag might. It is the failure the Text on that status line prevents.
    assert "[red]" not in Content.from_markup(note).plain


def test_a_successful_stop_toasts_the_ended_row_re_reads_and_leaves_its_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    live = status("prj_a", "coder-auth", "coder", "working")
    reads: list[str] = []
    stopped: list[bool] = []

    def list_agents(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        reads.append(project.id)
        if stopped:
            return [FleetAgentStatus(agent=ended(live.agent), state="exited")]
        return [live]

    def stop(
        project: ProjectInfo,
        label: str,
        *,
        force: bool = False,
        grace: float = 5.0,
        agent_id: str | None = None,
        actor: str | None = None,
    ) -> fleet_service.StopReceipt:
        stopped.append(True)
        return fleet_service.StopReceipt(ended(live.agent), [])

    list_agents_as(monkeypatch, list_agents)
    monkeypatch.setattr(fleet_service, "stop", stop)

    async def go(pilot: Pilot[None]) -> tuple[list[str], int, str | None]:
        app = fleet_app(pilot)
        dialog = await open_stop_dialog(pilot, app, "agt_a_coder-auth")
        before = len(reads)
        # Pressed on the dialog, and the toast waited for: a click the moment the dialog
        # was pushed, then a settle and two pauses, saw no toast on windows-latest (run
        # 40234523081 at e28697f1), as the take-over test's click once did (c6adca04).
        dialog.query_one("#stop-confirm", Button).press()
        wanted = f"✓ stopped coder-auth ({live.agent.id})"
        shown: list[str] = []

        def toasted() -> bool:
            # Every toast, not the first: the scripted pane has no tmux behind it and
            # raises its own "(pane gone)" here, which is the harness, not the claim.
            shown[:] = [toast.render().plain for toast in app.screen.query(Toast)]
            return wanted in shown

        # Looked for on the loop's own clock, the toast kept as it was seen, the page settled
        # after: a toast is gone 6 s after its notify, and on windows-latest the settle that
        # used to come first outlasted it (shard 5/10 at 658bf0c6, twice).
        await watch_until(toasted)
        await settle_page(app)
        return shown, len(reads) - before, app.content.current

    toasts, re_reads, current = drive(go, notifications=True)
    assert f"✓ stopped coder-auth ({live.agent.id})" in toasts
    assert re_reads == 1, "the sidebar re-reads the fleet once, through the shell's own refresh"
    assert current == "project-prj_a"  # the stopped agent's view is not left polling a dead pane


def test_x_on_the_selected_agent_row_opens_the_same_stop_dialog(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key is the SIDEBAR's: the rows are non-focusable Statics by design."""
    seed(tmp_path, ("prj_a", "alpha", None))
    live = status("prj_a", "coder-auth", "coder", "working")
    script["prj_a"] = [live]
    stopper(monkeypatch, live.agent)

    async def go(pilot: Pilot[None]) -> tuple[str, str | None]:
        app = fleet_app(pilot)
        await pilot.click(row_for(app, "agt_a_coder-auth"))
        await pilot.pause()
        app.sidebar.focus()
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
        screen = app.screen
        label = screen.label if isinstance(screen, StopAgentScreen) else None
        return type(screen).__name__, label

    assert drive(go) == ("StopAgentScreen", "coder-auth")


def test_x_ignores_a_selection_that_is_not_an_agent(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    live = status("prj_a", "coder-auth", "coder", "working")
    script["prj_a"] = [live]
    recorder = stopper(monkeypatch, live.agent)

    async def go(pilot: Pilot[None]) -> tuple[bool, bool]:
        app = fleet_app(pilot)
        app.sidebar.focus()
        await pilot.press("x")  # nothing selected yet
        await pilot.pause()
        with_nothing = isinstance(app.screen, StopAgentScreen)
        await pilot.click(card_for(app, "prj_a").query_one(ProjectTitle))
        await pilot.pause()
        app.sidebar.focus()
        await pilot.press("x")  # a PROJECT is selected
        await pilot.pause()
        return with_nothing, isinstance(app.screen, StopAgentScreen)

    assert drive(go) == (False, False)
    assert recorder.calls == []


def test_x_offers_no_stop_where_the_button_offers_none(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two controls, ONE rule: the key asks ``STOP_STATES`` exactly as the button does.

    A lost row is still an ordinary selectable ``AgentRow``, so a key that did
    not ask offered precisely the Stop the view had just refused. And ``stop``
    does not decline such a row: its pane is gone, so there is no ``/exit`` and
    no grace wait — it ends the row outright, which is ``fleet reap``'s outcome,
    and reap is outside this task's boundaries.

    Found by coder3a-1's probe before this PR opened, and reproduced by runner2-1
    one state WIDER: ✗ lost disagreed as well as 💤 exited. The states are
    derived from the constants — every ``STATE_CHIP`` key the rule leaves out —
    rather than typed out here: the rule IS the constant, and a test naming one
    state would drift from it. Since the release train's `remain-on-exit`
    (#138) 💤 exited is IN the rule (Stop removes its dead window), so this is
    ✗ lost alone today.
    """
    seed(tmp_path, ("prj_a", "alpha", None))
    dead = sorted(set(STATE_CHIP) - STOP_STATES)  # ✗ lost
    script["prj_a"] = [
        status(
            "prj_a",
            f"agent-{state}",
            "coder",
            state,
            minute=index,
            exit_status=0 if state == "exited" else None,
        )
        for index, state in enumerate(dead)
    ]
    recorder = stopper(monkeypatch, script["prj_a"][0].agent)

    async def go(pilot: Pilot[None]) -> dict[str, tuple[bool, bool]]:
        app = fleet_app(pilot)
        offered: dict[str, tuple[bool, bool]] = {}
        for state in dead:
            await pilot.click(row_for(app, f"agt_a_agent-{state}"))
            await pilot.pause()
            view = app.current_view()
            assert isinstance(view, AgentView), f"the row opened {type(view).__name__}"
            button = view.query_one("#agent-stop", Button).display
            app.sidebar.focus()
            await pilot.press("x")
            await pilot.pause()
            offered[state] = (button, isinstance(app.screen, StopAgentScreen))
        return offered

    offered = drive(go)
    assert dead, "the premise: some states have no process"
    # One equality over both dead states, not a pair of negatives: a control that
    # starts offering a stop in either state shows up as a diff, not as a silence.
    assert offered == {state: (False, False) for state in dead}
    assert recorder.calls == []  # and the service is never asked to stop a dead row


def test_stop_on_an_exited_row_removes_its_dead_window_through_the_same_dialog(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#138 met P21. The release train keeps an exited agent's window for its last
    screen (`remain-on-exit`), and Stop on the 💤 row is how the UI removes it — so the
    row IS offered, by button and by ``x``, through the same dialog and the same
    service call, pinned to the row. But there is no ``/exit`` and no grace to promise:
    the question says what will happen, and *Force*, which only skips the ``/exit``,
    is not offered."""
    seed(tmp_path, ("prj_a", "alpha", None))
    gone = status("prj_a", "coder-auth", "coder", "exited", exit_status=0)
    script["prj_a"] = [gone]
    recorder = stopper(monkeypatch, gone.agent)

    async def go(pilot: Pilot[None]) -> tuple[str, bool, bool, bool]:
        app = fleet_app(pilot)
        screen = await open_stop_dialog(pilot, app, "agt_a_coder-auth")
        question = screen.question().plain
        force_offered = screen.query_one("#stop-force", Button).display
        await pilot.click("#stop-cancel")
        await pilot.pause()
        app.sidebar.focus()
        await pilot.press("x")  # the row is still selected: a cancel leaves its view open
        await pilot.pause()
        x_opens = isinstance(app.screen, StopAgentScreen)
        await pilot.click("#stop-confirm")
        await settle(app)
        await pilot.pause()
        return question, force_offered, x_opens, not isinstance(app.screen, StopAgentScreen)

    question, force_offered, x_opens, closed = drive(go)
    assert "dead window" in question and "/exit" not in question  # nothing to /exit
    assert force_offered is False
    assert x_opens, "x asks the same rule as the button: an exited row is offered"
    assert closed and recorder.calls == [("prj_a", "coder-auth", False, "agt_a_coder-auth")]


def test_the_dialog_says_when_the_target_is_the_projects_manager(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stopping the manager is not like stopping a coder, so the question says so."""
    seed(tmp_path, ("prj_a", "alpha", None))
    manager = status("prj_a", "manager", "manager", "working")
    coder = status("prj_a", "coder-auth", "coder", "working", minute=1)
    script["prj_a"] = [manager, coder]
    stopper(monkeypatch, manager.agent)

    async def go(pilot: Pilot[None]) -> tuple[str, str]:
        app = fleet_app(pilot)
        asked: list[str] = []
        for agent_id in ("agt_a_manager", "agt_a_coder-auth"):
            screen = await open_stop_dialog(pilot, app, agent_id)
            asked.append(shown(screen.query_one("#stop-question", Static)))
            await pilot.click("#stop-cancel")
            await pilot.pause()
        return asked[0], asked[1]

    about_manager, about_coder = drive(go)
    assert "the project's manager" in about_manager
    assert "the agents it started keep running" in about_manager  # what the warning is FOR
    # Control: a coder's dialog says none of it — the line is about this target,
    # not a sentence every stop carries.
    assert "manager" not in about_coder


def test_the_app_stays_live_while_a_stop_is_in_flight(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``stop`` BLOCKS — /exit, a 5 s grace, then the kill. Inline, that is a frozen app.

    Every other assertion in this file passes on a Stop that runs on the UI
    thread: the service is still called with the right arguments, the dialog
    still closes, the toast still says the right words — and the whole app is
    dead for five seconds while it happens.

    So the service here blocks until this test releases it, and the test proves a
    frame rendered BEFORE the release. Run inline, the click could not return
    until the service gave up on its own, which is what ``timed_out`` records —
    a red, rather than a hang.
    """
    seed(tmp_path, ("prj_a", "alpha", None))
    live = status("prj_a", "coder-auth", "coder", "working")
    script["prj_a"] = [live]
    entered = threading.Event()
    release = threading.Event()
    timed_out: list[bool] = []

    def blocking_stop(
        project: ProjectInfo,
        label: str,
        *,
        force: bool = False,
        grace: float = 5.0,
        agent_id: str | None = None,
        actor: str | None = None,
    ) -> fleet_service.StopReceipt:
        entered.set()
        if not release.wait(10):
            timed_out.append(True)  # nobody let it go: the UI thread was inside it
        return fleet_service.StopReceipt(ended(live.agent), [])

    monkeypatch.setattr(fleet_service, "stop", blocking_stop)

    async def go(pilot: Pilot[None]) -> tuple[bool, bool, str, bool]:
        app = fleet_app(pilot)
        await open_stop_dialog(pilot, app, "agt_a_coder-auth")
        await pilot.click("#stop-confirm")
        await pilot.pause()
        started = entered.wait(5)  # the worker really reached the service
        # …and the app is still painting frames while the service holds a thread.
        in_flight = isinstance(app.screen, StopAgentScreen)
        note = shown(app.screen.query_one("#stop-status", Static))
        release.set()
        await settle(app)
        await pilot.pause()
        return started, in_flight, note, isinstance(app.screen, StopAgentScreen)

    started, in_flight, note, open_at_end = drive(go)
    assert timed_out == [], "the stop ran on the UI thread: the app could not release it"
    assert started and in_flight  # the dialog is up and repainting mid-call
    assert note == "stopping coder-auth …"  # a frame rendered WHILE the service blocked
    assert not open_at_end  # and the answer still landed when it came


def test_a_refusal_re_reads_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fleet is re-read on success only.

    A reload after a refusal reports a stop that did not happen, and repaints the
    row the service deliberately left live as though something had changed.
    """
    seed(tmp_path, ("prj_a", "alpha", None))
    live = status("prj_a", "coder-auth", "coder", "working")
    reads: list[str] = []

    def list_agents(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        reads.append(project.id)
        return [live]

    def refusing_stop(
        project: ProjectInfo,
        label: str,
        *,
        force: bool = False,
        grace: float = 5.0,
        agent_id: str | None = None,
        actor: str | None = None,
    ) -> fleet_service.StopReceipt:
        raise fleet_service.FleetError("tmux did not answer, so its row is left live")

    list_agents_as(monkeypatch, list_agents)
    monkeypatch.setattr(fleet_service, "stop", refusing_stop)

    async def go(pilot: Pilot[None]) -> tuple[int, bool]:
        app = fleet_app(pilot)
        await open_stop_dialog(pilot, app, "agt_a_coder-auth")
        before = len(reads)
        await pilot.click("#stop-confirm")
        await settle(app)
        await pilot.pause()
        await pilot.pause()
        return len(reads) - before, row_for(app, "agt_a_coder-auth").status.state == "working"

    re_reads, still_live = drive(go)
    assert re_reads == 0, "a refused stop must not re-read the fleet"
    assert still_live  # the row the service left live is still shown live


# --- the partition (#137) -----------------------------------------------------------------


def _state(isolated_home: Path) -> dict[str, object]:
    path = isolated_home / "state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _write_state(isolated_home: Path, data: object) -> Path:
    isolated_home.mkdir(parents=True, exist_ok=True)
    path = isolated_home / "state.json"
    path.write_text(json.dumps(data) + "\n")
    return path


async def _through_the_app(pilot: Pilot[None]) -> None:
    """Pause once the APP has handled what was posted to it, not merely the screen.

    ``pilot.pause()`` waits for the screen and its widgets, not for the app's own queue, and
    an event posted to the app (``_mouse``, ``Pilot.resize_terminal``, a queued key) could still
    be waiting there when the pause returned. On the Windows leg that read a held-key burst
    (run 36042754454) and a resize (run 36047190212) before the app had handed either on. A
    callback the app queues behind the event runs only once the app has handed it to the
    screen.

    What the screen does with it can be a message too: a key the sidebar binds posts
    ``ResizeSidebar``, which bubbles to the partition, and a pause after the hand-on did not
    wait for that (the burst read 46 for 50 with every message held 5 ms). So the app is
    then let go quiet, and the last pause of that lays the result out.
    """
    await pilot.app.wait_for_refresh()
    await settle_page(pilot.app)


async def _mouse(
    pilot: Pilot[None], kind: type[events.MouseEvent], x: int, y: int, *, button: int = 1
) -> None:
    """One mouse event the way the driver delivers it — through ``App.on_event``.

    ``Pilot``'s mouse helpers hand their events straight to ``screen._forward_event``
    ("Bypass event processing in App.on_event"), so nothing they post reaches the
    click-chain counter that turns two releases into a double click, and a
    ``hover`` never carries a button. A drag is a press, moves WITH the button
    held and a release; this is the only way to make one.
    """
    pilot.app.post_message(kind(None, x, y, 0, 0, button, False, False, False))
    await _through_the_app(pilot)


async def _drag(pilot: Pilot[None], from_x: int, to_x: int, y: int = 5) -> None:
    await _mouse(pilot, events.MouseDown, from_x, y)
    await _mouse(pilot, events.MouseMove, to_x, y)
    await _mouse(pilot, events.MouseUp, to_x, y)


async def _settled(pilot: Pilot[None]) -> None:
    """Let every save land — by the savers' own state, never the clock: the debounce fires and
    the drain finishes (or a refusal stands) before this returns.

    The app goes quiet first. A saver that has not been asked yet is settled, and a key's
    width reaches the saver through a message that bubbles to the partition, so read too
    early the savers were idle and the file had no width (``(34, None)`` with every message
    held 5 ms). It goes quiet again after, so a refusal's toast is on screen.
    """
    app = fleet_app(pilot)
    await settle(app)
    for saver in (app._theme_autosave, app.query_one(Divider)._autosave):
        if saver is not None:
            assert await asyncio.to_thread(saver.settled, 5.0), "a save never settled"
    await settle(app)


async def _asked(pilot: Pilot[None], width: int) -> None:
    """Wait, bounded, until the width's saver has been asked for ``width``, and no longer.

    For a test that quits inside the debounce. The width reaches the saver as
    ``ResizeSidebar``, which the sidebar's ``>`` posts and which bubbles to the partition,
    so a test that returned straight after the press quit before it was handled: nothing
    was due at quit, and the width was neither reported nor written (with every message
    held 10 ms). ``settle`` would wait for it too, but it waits until the app is quiet, and
    that can outlast the debounce the quit has to land inside.
    """
    saver = fleet_app(pilot).query_one(Divider)._autosave
    assert saver is not None
    deadline = time.monotonic() + 5.0
    while saver.latest != width:
        assert time.monotonic() < deadline, f"the saver was never asked for {width}"
        await pilot.pause()


async def _restored(pilot: Pilot[None]) -> None:
    """Wait for a saved width to be applied AND laid out, by the screen's refreshes, not a pause.

    ``Divider.on_mount`` applies the saved width with ``call_after_refresh``, so it lands after
    the first refresh, and the layout that shows it comes one refresh later. One
    ``pilot.pause()`` usually spans both. On the Windows leg of run 36033742930 it did not: a
    relaunch over a saved ``10**400`` read the navigator at its declared 30, and the same
    test had passed on the run before. Each ``wait_for_refresh`` returns only after an
    after-refresh callback queued behind the previous one has run."""
    await pilot.pause()
    divider = fleet_app(pilot).query_one(Divider)
    await divider.wait_for_refresh()  # the restore queued at mount has run
    await divider.wait_for_refresh()  # and the layout it asked for has been done


def _declared(app: FleetApp) -> int:
    """The navigator's width with nothing saved and no gesture: the stylesheet's."""
    width = cells(app.sidebar.styles.base.width)
    assert width is not None
    return width


def _floor(app: FleetApp) -> int:
    width = cells(app.sidebar.styles.min_width)
    assert width is not None
    return width


def test_the_navigators_ceiling_keeps_the_content_usable_and_the_navigator_readable() -> None:
    floor = 24
    assert (
        Panes.sidebar_ceiling(SIZE[0], floor) == SIZE[0] - 1 - Panes.MIN_CONTENT
    )  # one is the divider's
    assert Panes.sidebar_ceiling(50, floor) == floor, (
        "too narrow for both minimums: the navigator's wins"
    )


def test_dragging_the_divider_resizes_the_sidebar_within_bounds_and_remembers_it(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """The partition was `Sidebar { width: 30 }` and nothing could move it (#137)."""
    seed(tmp_path, ("prj_a", "alpha", None))
    # The other surfaces' keys are in the file FIRST: a save must merge, not replace.
    _write_state(isolated_home, {"board_theme": "nord", "active_project_id": "prj_abc"})

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        seen: dict[str, object] = {"declared": _declared(app), "floor": _floor(app)}
        seen["before"] = app.sidebar.outer_size.width
        await _mouse(pilot, events.MouseDown, app.sidebar.outer_size.width, 5)
        await _mouse(pilot, events.MouseMove, 40, 5)
        seen["mid"] = app.sidebar.outer_size.width
        await _mouse(pilot, events.MouseMove, 50, 5)
        await _mouse(pilot, events.MouseUp, 50, 5)
        seen["widened"] = app.sidebar.outer_size.width
        seen["selections"] = dict(app.screen.selections)
        await _settled(pilot)
        seen["saved_after_drag"] = _state(isolated_home).get(SIDEBAR_WIDTH_KEY)
        # Past both bounds: clamped, never a broken layout.
        await _drag(pilot, 50, 3)
        seen["narrowest"] = app.sidebar.outer_size.width
        await _drag(pilot, app.sidebar.outer_size.width, SIZE[0] - 2)
        seen["widest"] = app.sidebar.outer_size.width
        seen["content"] = app.content.outer_size.width
        seen["divider_x"] = app.query_one(Divider).region.x
        # And back to a width that is neither bound, so the relaunch below proves
        # the number itself came back — not whatever the ceiling clamps to.
        await _drag(pilot, app.sidebar.outer_size.width, 61)
        await _settled(pilot)
        seen["state"] = _state(isolated_home)
        return seen

    seen = drive(go)
    declared, floor = seen["declared"], seen["floor"]
    assert isinstance(floor, int)
    assert seen["before"] == declared, "nothing saved: the stylesheet's width"
    assert seen["mid"] == 40, "the width follows the pointer while dragging"
    assert seen["widened"] == 50
    assert seen["selections"] == {}, "a drag on the handle selects no text"
    assert seen["saved_after_drag"] == 50, "the release is the save"
    assert seen["narrowest"] == floor
    widest = Panes.sidebar_ceiling(SIZE[0], floor)
    assert seen["widest"] == widest
    assert seen["content"] == Panes.MIN_CONTENT and seen["divider_x"] == widest
    assert seen["state"] == {
        "board_theme": "nord",
        "active_project_id": "prj_abc",
        SIDEBAR_WIDTH_KEY: 61,
    }

    async def relaunch(pilot: Pilot[None]) -> int:
        await _restored(pilot)
        return fleet_app(pilot).sidebar.outer_size.width

    assert drive(relaunch) == 61, "restored on the next launch — the width itself"
    _write_state(isolated_home, {SIDEBAR_WIDTH_KEY: 500})
    assert drive(relaunch) == widest, "a saved width past the ceiling is bounded by it"
    _write_state(isolated_home, {SIDEBAR_WIDTH_KEY: "wide"})
    assert drive(relaunch) == declared, "a saved width that is not a number is ignored"


def test_a_terminal_that_shrinks_re_clamps_the_navigator_and_keeps_the_divider_on_screen(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """The content's minimum held only at drag time: at 80 columns a 99-wide navigator
    left the agent's pane one column and the handle off screen, with no mouse route back."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        seen: dict[str, object] = {"floor": _floor(app)}
        await _drag(pilot, app.sidebar.outer_size.width, SIZE[0] - 2)
        seen["wide"] = app.sidebar.outer_size.width
        await pilot.resize_terminal(80, SIZE[1])
        await _through_the_app(pilot)
        seen["shrunk"] = (
            app.sidebar.outer_size.width,
            app.content.outer_size.width,
            app.query_one(Divider).region.x,
        )
        await pilot.resize_terminal(*SIZE)
        await _through_the_app(pilot)
        seen["back"] = app.sidebar.outer_size.width
        # A key at the small size steps from what is ON SCREEN, not from the wide screen's number.
        await pilot.resize_terminal(80, SIZE[1])
        await _through_the_app(pilot)
        app.sidebar.focus()
        await pilot.press("less_than_sign")
        await settle(app)
        seen["stepped"] = app.sidebar.outer_size.width
        await _settled(pilot)
        seen["saved"] = _state(isolated_home).get(SIDEBAR_WIDTH_KEY)
        return seen

    seen = drive(go)
    floor = seen["floor"]
    assert isinstance(floor, int)
    assert seen["wide"] == Panes.sidebar_ceiling(SIZE[0], floor)
    small = Panes.sidebar_ceiling(80, floor)
    assert seen["shrunk"] == (small, Panes.MIN_CONTENT, small), "re-clamped, the handle in reach"
    assert seen["back"] == seen["wide"], "room again: the width the user asked for returns"
    assert seen["stepped"] == small - RESIZE_STEP and seen["saved"] == small - RESIZE_STEP


def test_a_drag_survives_a_right_button_and_ends_on_a_lost_release_or_a_pushed_screen(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """The capture and `-dragging` were released only by a left `MouseUp`: a release that
    never arrived left every mouse event in the app routed here and the navigator following
    the bare pointer, a screen pushed mid-drag left the column lit for good, and a right
    click mid-drag (a paste gesture) ended and committed the drag."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        divider = app.query_one(Divider)
        seen: dict[str, object] = {}

        def state() -> tuple[bool, bool, int]:
            return (
                divider.has_class("-dragging"),
                app.mouse_captured is divider,
                app.sidebar.outer_size.width,
            )

        await _mouse(pilot, events.MouseDown, app.sidebar.outer_size.width, 5)
        await _mouse(pilot, events.MouseMove, 40, 5)
        await _mouse(pilot, events.MouseUp, 40, 5, button=3)
        seen["after_right_release"] = state()
        await _mouse(pilot, events.MouseMove, 44, 5)
        await _mouse(pilot, events.MouseUp, 44, 5)
        seen["after_left_release"] = state()
        # The release never arrives (let go outside the terminal): the first move
        # with no button held ends the drag where it got to.
        await _mouse(pilot, events.MouseDown, 44, 5)
        await _mouse(pilot, events.MouseMove, 50, 5)
        await _mouse(pilot, events.MouseMove, 95, 5, button=0)
        seen["after_lost_release"] = state()
        await _mouse(pilot, events.MouseMove, 100, 5, button=0)
        seen["after_bare_move"] = state()
        # A screen pushed mid-drag takes the capture away: the drag is over, nothing stays lit.
        await _mouse(pilot, events.MouseDown, 50, 5)
        await _mouse(pilot, events.MouseMove, 60, 5)
        await app.push_screen(HelpScreen(app.escape_key))
        await pilot.pause()
        seen["after_push"] = (divider.has_class("-dragging"), app.mouse_captured)
        await app.pop_screen()
        await _settled(pilot)
        seen["saved"] = _state(isolated_home).get(SIDEBAR_WIDTH_KEY)
        return seen

    seen = drive(go)
    assert seen["after_right_release"] == (True, True, 40), (
        "a right click is not the end of a left drag"
    )
    assert seen["after_left_release"] == (False, False, 44)
    assert seen["after_lost_release"] == (False, False, 50), (
        "where the drag got to, not where the bare pointer went"
    )
    assert seen["after_bare_move"] == (False, False, 50)
    assert seen["after_push"] == (False, None)
    assert seen["saved"] == 60, (
        "whatever ended the drag, the width it reached is the one remembered"
    )


def test_a_divider_drag_whose_release_was_lost_ends_at_the_next_press_where_no_motion_reports_it(
    tmp_path: Path, script: Script, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final review of #203, F3: the divider's side of the card handle's round-2 fix. A
    terminal that reports no motion without a button never sends the move that ends a
    lost release; the next report is the next press, long past DUPLICATE_PRESS_WINDOW.
    The divider still held the mouse, so that press came to it and armed a new drag,
    and the release, over a title at the left edge, set the width: the navigator went
    to its floor, the floor was saved, and api did not open. The press ends the old
    drag where it got to instead, and the click is api's."""
    seed(tmp_path, ("prj_a", "api", None), ("prj_b", "cli", None))
    now = {"t": 100.0}
    monkeypatch.setattr("aisquare.cli.ui.terminal._monotonic", lambda: now["t"])

    async def go(pilot: Pilot[None]) -> tuple[int, object, int, object, str | None, object]:
        app = fleet_app(pilot)
        await _mouse(pilot, events.MouseDown, app.sidebar.outer_size.width, 5)
        await _mouse(pilot, events.MouseMove, 60, 5)
        held = app.sidebar.outer_size.width, app.mouse_captured
        now["t"] += DUPLICATE_PRESS_WINDOW + 1.0  # let go outside; nothing reported it
        await _click(pilot, card_for(app, "prj_a").query_one(ProjectTitle))
        await _settled(pilot)
        view = app.current_view()
        return (
            *held,
            app.sidebar.outer_size.width,
            app.mouse_captured,
            view.id if view is not None else None,
            _state(isolated_home).get(SIDEBAR_WIDTH_KEY),
        )

    width, captured, after, released, opened, saved = drive(go)
    assert width == 60 and isinstance(captured, Divider), "the premise: a drag still held"
    assert after == 60 and saved == 60, "the drag ended where it got to, and that is saved"
    assert released is None, "the divider let the mouse go"
    assert opened == "project-prj_a", "the click is the click it was"


def test_a_tap_after_a_drag_keeps_the_drag_and_two_clicks_reset_and_forget_the_width(
    tmp_path: Path, script: Script, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Textual counts a drag's release as a click (the handle follows the pointer, so the
    release lands on the widget the press did): a tap on the handle within half a second
    read as `chain == 2`, threw the drag away and saved the reset. `pilot.click(times=2)`
    could not see it — Pilot builds `Click(chain=2)` itself, past `App.on_event`."""
    seed(tmp_path, ("prj_a", "alpha", None))
    # The tap and the click after it are one double click, and the save is read between
    # them: `_settled` waits out the debounce and the write. With every message held 20 ms
    # that outlasted Textual's half-second window, and the two read as single clicks. The
    # window's length is not what this tests, so it is wide enough for a loaded runner.
    monkeypatch.setattr(FleetApp, "CLICK_CHAIN_TIME_THRESHOLD", 5.0)

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        seen: dict[str, object] = {"declared": _declared(app)}
        await _drag(pilot, app.sidebar.outer_size.width, 60)
        await _mouse(pilot, events.MouseDown, 60, 5)  # a tap right after: Textual says chain == 2
        await _mouse(pilot, events.MouseUp, 60, 5)
        seen["tapped"] = app.sidebar.outer_size.width
        await _settled(pilot)
        seen["saved"] = _state(isolated_home).get(SIDEBAR_WIDTH_KEY)
        await _mouse(
            pilot, events.MouseDown, 60, 5
        )  # the tap and this one: two clicks that were clicks
        await _mouse(pilot, events.MouseUp, 60, 5)
        seen["reset"] = app.sidebar.outer_size.width
        await _settled(pilot)
        seen["state"] = _state(isolated_home)
        return seen

    seen = drive(go)
    assert seen["tapped"] == 60, "a tap on the handle after a drag is not half a double click"
    assert seen["saved"] == 60
    assert seen["reset"] == seen["declared"], "two clicks that were clicks: the stylesheet's width"
    state = seen["state"]
    assert isinstance(state, dict)
    assert SIDEBAR_WIDTH_KEY not in state, (
        "a reset forgets the preference, it does not save a number"
    )


def test_the_keyboard_steps_the_partition_from_the_sidebar_and_a_held_key_is_one_save(
    tmp_path: Path, script: Script, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    script["prj_a"] = [status("prj_a", "coder-auth", "coder", "working")]
    writes: list[tuple[str, object]] = []

    def counting(key: str, value: object) -> None:
        writes.append((key, value))
        update_state(key, value)

    monkeypatch.setattr("aisquare.cli.ui.autosave.update_state", counting)

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        seen: dict[str, object] = {"declared": _declared(app)}
        app.sidebar.focus()
        widths: list[int] = []
        for key in ("greater_than_sign", "greater_than_sign", "less_than_sign", "equals_sign"):
            await pilot.press(key)
            await settle(app)
            widths.append(app.sidebar.outer_size.width)
        seen["widths"] = widths
        await _settled(pilot)
        seen["state_after_reset"] = _state(isolated_home)
        # Autorepeat: five presses queued back to back, no layout pass between them.
        before = len(writes)
        for _ in range(5):
            app.post_message(events.Key("greater_than_sign", ">"))
        await _through_the_app(pilot)  # queued on the app, which a bare pause does not wait for
        seen["burst"] = app.sidebar.outer_size.width
        await _settled(pilot)
        seen["burst_writes"] = writes[before:]
        # With a pane focused the same keys are text for the agent, not a resize.
        pane = RecordingPane()
        await app.content.add_content(pane, set_current=True)
        pane.focus()
        await pilot.pause()
        await pilot.press("greater_than_sign")
        await settle(app)
        seen["with_pane"] = (app.sidebar.outer_size.width, list(pane.keys))
        # The fallback exists for terminals without mouse reporting; ? is where they look.
        app.sidebar.focus()
        await pilot.press("question_mark")
        await settle(app)
        seen["help"] = shown(app.screen.query_one("#helpbox Static", Static))
        await pilot.press("escape")
        await settle(app)
        return seen

    seen = drive(go)
    declared = seen["declared"]
    assert isinstance(declared, int)
    step = RESIZE_STEP
    assert seen["widths"] == [declared + step, declared + 2 * step, declared + step, declared]
    state = seen["state_after_reset"]
    assert isinstance(state, dict) and SIDEBAR_WIDTH_KEY not in state, "reset: nothing to remember"
    assert seen["burst"] == declared + 5 * step, "a held key loses no step"
    assert seen["burst_writes"] == [(SIDEBAR_WIDTH_KEY, declared + 5 * step)], "one write per burst"
    assert seen["with_pane"] == (declared + 5 * step, ["greater_than_sign"]), (
        "a focused pane keeps the key — and the partition stays where it was"
    )
    help_text = seen["help"]
    assert isinstance(help_text, str)
    assert "> < =" in help_text and "divider" in help_text, (
        "the keys are where a keyboard user looks"
    )


def test_the_divider_is_the_line_and_lights_while_the_navigator_has_focus(
    tmp_path: Path, script: Script
) -> None:
    """The handle sat beside the sidebar's `border-right`, so the line the eye saw was the
    inert one a column over — and with no `render` the column spelt `Divider#divider`
    down the screen."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        divider = app.query_one(Divider)
        seen: dict[str, object] = {
            "declared": _declared(app),
            "painted": [strip.text for strip in divider.render_lines(Region(0, 0, 1, 3))],
            "columns": (app.sidebar.region.right, divider.region.x, app.content.region.x),
            "pointer": str(divider.styles.pointer),
            "sidebar_border": app.sidebar.styles.border_right[0],
        }
        app.sidebar.focus()
        await pilot.pause()
        seen["lit_with_sidebar"] = divider.has_class("-neighbour-focused")
        pane = RecordingPane()
        await app.content.add_content(pane, set_current=True)
        pane.focus()
        await pilot.pause()
        seen["lit_with_pane"] = divider.has_class("-neighbour-focused")
        app.sidebar.focus()
        await pilot.pause()
        seen["lit_again"] = divider.has_class("-neighbour-focused")
        return seen

    seen = drive(go)
    declared = seen["declared"]
    assert isinstance(declared, int)
    assert seen["painted"] == ["│", "│", "│"], "a line, not the widget's CSS identifier"
    assert seen["columns"] == (declared, declared, declared + 1), (
        "one column between the panes: the line the eye sees is the one the hand grabs"
    )
    assert seen["sidebar_border"] == ""
    assert seen["pointer"] == "ew-resize"
    assert (seen["lit_with_sidebar"], seen["lit_with_pane"], seen["lit_again"]) == (
        True,
        False,
        True,
    )


def test_a_state_file_that_is_not_an_object_is_left_alone_and_said_so_once(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """The width's reader raised `AttributeError` on such a file from `on_mount` — the UI
    did not start — and its writer replaced the file wholesale, theme and pin included."""
    seed(tmp_path, ("prj_a", "alpha", None))
    body = '["was", "a", "list"]\n'
    path = _write_state(isolated_home, ["was", "a", "list"])
    assert path.read_text() == body

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)  # the launch itself is the first claim
        await _drag(pilot, app.sidebar.outer_size.width, 50)
        await _settled(pilot)
        await _drag(pilot, 50, 60)
        await _settled(pilot)
        return {
            "width": app.sidebar.outer_size.width,
            "toasts": await _toast_list(app),
            "file": path.read_text(),
            "leftovers": sorted(
                p.name
                for p in isolated_home.iterdir()
                if p.name.startswith("state") and not p.name.endswith(".lock")
            ),
        }

    seen = drive(go, notifications=True)
    assert seen["width"] == 60, "the UI works; only the memory is refused"
    assert seen["file"] == body, "left byte for byte as it was — the keys in it are the user's"
    toasts = seen["toasts"]
    assert isinstance(toasts, list) and len(toasts) == 1, "said once, not once per gesture"
    assert "state.json" in toasts[0] and "could not be saved" in toasts[0]
    assert seen["leftovers"] == ["state.json"], "no temp file left behind"


def test_a_saved_width_is_bounded_before_it_reaches_the_stylesheet(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """`on_mount` wrote the file's number straight into `styles.width` before anything bounded
    it: 100000000 was a `MemoryError` in Rich's cell splitting, 1e400 an `OverflowError` out of
    the style setter, and 500 gave the content pane one frame at zero columns — a size the
    agent's pane forwards to tmux. The file is user-editable, and the TUI that would let you
    fix it was the thing that would not start."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> tuple[int, int, int | None, int]:
        app = fleet_app(pilot)
        await _restored(pilot)
        return (
            app.sidebar.outer_size.width,
            app.content.outer_size.width,
            cells(app.sidebar.styles.inline.width),
            _floor(app),
        )

    _write_state(isolated_home, {SIDEBAR_WIDTH_KEY: 100_000_000})
    sidebar, content, inline, floor = drive(go)
    ceiling = Panes.sidebar_ceiling(SIZE[0], floor)
    assert (sidebar, content) == (ceiling, Panes.MIN_CONTENT), "the layout bounds what is shown"
    assert inline == WIDEST_ASK, "the ask reaches the stylesheet with only the absolute cap"
    _write_state(
        isolated_home, {SIDEBAR_WIDTH_KEY: 10**400}
    )  # json gives a big int; float() overflowed
    assert drive(go)[0] == ceiling


def test_a_press_that_never_moved_writes_nothing_and_a_right_click_counts_for_nothing(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """A bare click settled the width it found and wrote `sidebar_width: 30` to a fresh file (so
    a reset was undone by the next tap); a press cut short by a pushed screen did the same; and
    a right click at the handle made the next single left click a "double" that reset the
    navigator and deleted the key."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        seen: dict[str, object] = {"declared": _declared(app)}
        x = app.sidebar.outer_size.width
        await _mouse(pilot, events.MouseDown, x, 5)
        await _mouse(pilot, events.MouseUp, x, 5)
        await _settled(pilot)
        seen["after_click"] = _state(isolated_home)
        await _mouse(pilot, events.MouseDown, x, 5)
        await app.push_screen(HelpScreen(app.escape_key))
        await pilot.pause()
        await app.pop_screen()
        await _settled(pilot)
        seen["after_cut_press"] = _state(isolated_home)
        app.sidebar.focus()
        await pilot.press("greater_than_sign")
        await _settled(pilot)
        x = app.sidebar.outer_size.width
        seen["after_key"] = (x, _state(isolated_home).get(SIDEBAR_WIDTH_KEY))
        await _mouse(pilot, events.MouseDown, x, 5, button=3)
        await _mouse(pilot, events.MouseUp, x, 5, button=3)
        await _mouse(pilot, events.MouseDown, x, 5)
        await _mouse(pilot, events.MouseUp, x, 5)
        await _settled(pilot)
        seen["after_right_then_left"] = (
            app.sidebar.outer_size.width,
            _state(isolated_home).get(SIDEBAR_WIDTH_KEY),
        )
        return seen

    seen = drive(go)
    declared = seen["declared"]
    assert isinstance(declared, int)
    assert seen["after_click"] == {}, "a click asked for nothing"
    assert seen["after_cut_press"] == {}, "neither did a press a pushed screen cut short"
    assert seen["after_key"] == (declared + RESIZE_STEP, declared + RESIZE_STEP)
    assert seen["after_right_then_left"] == (declared + RESIZE_STEP, declared + RESIZE_STEP), (
        "a right click and a left click are not a double click"
    )


def test_a_gesture_on_a_narrow_terminal_keeps_a_wider_remembered_width(
    tmp_path: Path, script: Script, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restored on an 80-column laptop, a monitor's 90 shows as the ceiling, 39. A `>` the
    ceiling swallowed used to settle 39 over the 90 — nothing moved on screen and the
    preference was gone for good; and a restore that stored the CLAMPED 39 as the ask never
    gave the 90 back when the room returned. The ceiling bounds what is shown, not what is
    remembered — and not what is asked for."""
    seed(tmp_path, ("prj_a", "alpha", None))
    _write_state(isolated_home, {SIDEBAR_WIDTH_KEY: 90})

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        await _restored(pilot)
        seen: dict[str, object] = {"floor": _floor(app), "shown": app.sidebar.outer_size.width}
        app.sidebar.focus()

        def now() -> tuple[int, object]:
            return app.sidebar.outer_size.width, _state(isolated_home).get(SIDEBAR_WIDTH_KEY)

        # Rule 1 on its own: a step the ceiling swallows does not settle — with the
        # ceiling rule stubbed out, so it cannot be the one catching this.
        with monkeypatch.context() as stubbed:
            stubbed.setattr(Divider, "_ceiling_under", lambda self, width, remembered: False)
            await pilot.press("greater_than_sign")
            await _settled(pilot)
            seen["after_wider"] = now()
        # Rule 2 on its own: a drag that MOVES and then lands on the ceiling in one gesture.
        x = app.sidebar.outer_size.width
        await _mouse(pilot, events.MouseDown, x, 5)
        await _mouse(pilot, events.MouseMove, 30, 5)  # in: moved
        await _mouse(pilot, events.MouseMove, 70, 5)  # back out, past the ceiling
        await _mouse(pilot, events.MouseUp, 70, 5)
        await _settled(pilot)
        seen["after_drag"] = now()
        # The ask survives: room again, and the 90 is what is shown.
        await pilot.resize_terminal(140, SIZE[1])
        await _through_the_app(pilot)
        seen["after_room"] = now()
        await pilot.resize_terminal(80, SIZE[1])
        await _through_the_app(pilot)
        await pilot.press("less_than_sign")  # a step the screen shows is a real gesture
        await _settled(pilot)
        seen["after_narrower"] = now()
        return seen

    seen = drive(go, size=(80, SIZE[1]))
    floor = seen["floor"]
    assert isinstance(floor, int)
    small = Panes.sidebar_ceiling(80, floor)
    assert seen["shown"] == small
    assert seen["after_wider"] == (small, 90), "a step the ceiling swallowed writes nothing"
    assert seen["after_drag"] == (small, 90), (
        "a drag that lands on the ceiling is 'as wide as this screen allows'"
    )
    assert seen["after_room"] == (90, 90), "restored on a narrow terminal, the ask comes back"
    assert seen["after_narrower"] == (small - RESIZE_STEP, small - RESIZE_STEP)


def test_a_double_click_with_a_one_row_drift_is_still_a_double_click(
    tmp_path: Path, script: Script, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_moved` was set by any motion event: on a tall one-column handle a one-row hand drift
    between press and release is routine, moved nothing, and made the reset take three clicks."""
    seed(tmp_path, ("prj_a", "alpha", None))
    # Settled click by click, a loaded runner can outlast the half-second window
    # (test_another_button_breaks_the_double_click_chain); its length is not what this tests.
    monkeypatch.setattr(FleetApp, "CLICK_CHAIN_TIME_THRESHOLD", 5.0)

    async def go(pilot: Pilot[None]) -> tuple[int, int, int]:
        app = fleet_app(pilot)
        declared = _declared(app)
        app.sidebar.focus()
        await pilot.press("greater_than_sign")
        await settle(app)
        x = app.sidebar.outer_size.width
        await _mouse(pilot, events.MouseDown, x, 5)
        await _mouse(pilot, events.MouseMove, x, 6)  # a row down, the same column: nothing moves
        await _mouse(pilot, events.MouseUp, x, 6)
        first = app.sidebar.outer_size.width
        await _mouse(pilot, events.MouseDown, x, 6)
        await _mouse(pilot, events.MouseUp, x, 6)
        return declared, first, app.sidebar.outer_size.width

    declared, first, second = drive(go)
    assert first == declared + RESIZE_STEP, "the drifting click is a click: it moved nothing"
    assert second == declared, "two clicks, one of them with a row of drift"


def test_a_refused_theme_save_is_said_once(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """The save's refusal was dropped at both call sites: the picker showed the theme applied,
    the file refused it, and nothing said so — while the width, saving to the same file in the
    same session, did. The save is debounced and runs on a worker, like the width's."""
    seed(tmp_path, ("prj_a", "alpha", None))
    path = _write_state(isolated_home, ["was", "a", "list"])
    body = path.read_text()

    async def go(pilot: Pilot[None]) -> tuple[list[str], str]:
        app = fleet_app(pilot)
        app.theme = "nord"
        await _settled(pilot)
        app.theme = "dracula"
        await _settled(pilot)
        return await _toast_list(app), path.read_text()

    toasts, file = drive(go, notifications=True)
    assert len(toasts) == 1, "said once, not once per pick"
    assert "state.json" in toasts[0] and "the theme could not be saved" in toasts[0]
    assert file == body


def test_another_button_breaks_the_double_click_chain(
    tmp_path: Path, script: Script, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Textual's chain counter is button-agnostic: a middle click between two left clicks fifteen
    rows apart, or a right click between two at one cell, made the second left click
    `chain == 2` — and a time check on the previous left click let both through."""
    seed(tmp_path, ("prj_a", "alpha", None))
    # Each click here is settled before the next, and on a loaded ubuntu runner the last two
    # outlasted Textual's half-second window and read as single clicks (CI run 38181938628).
    # The window's length is not what this tests: wide enough, the button alone decides.
    monkeypatch.setattr(FleetApp, "CLICK_CHAIN_TIME_THRESHOLD", 5.0)

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        seen: dict[str, object] = {"declared": _declared(app)}
        app.sidebar.focus()
        await pilot.press("greater_than_sign")
        await _settled(pilot)
        x = app.sidebar.outer_size.width
        await _mouse(pilot, events.MouseDown, x, 5)
        await _mouse(pilot, events.MouseUp, x, 5)
        await _mouse(pilot, events.MouseDown, x, 20, button=2)
        await _mouse(pilot, events.MouseUp, x, 20, button=2)
        await _mouse(pilot, events.MouseDown, x, 20)
        await _mouse(pilot, events.MouseUp, x, 20)
        seen["left_middle_left"] = app.sidebar.outer_size.width
        await _mouse(pilot, events.MouseDown, x, 8)
        await _mouse(pilot, events.MouseUp, x, 8)
        await _mouse(pilot, events.MouseDown, x, 8, button=3)
        await _mouse(pilot, events.MouseUp, x, 8, button=3)
        await _mouse(pilot, events.MouseDown, x, 8)
        await _mouse(pilot, events.MouseUp, x, 8)
        seen["left_right_left"] = app.sidebar.outer_size.width
        await _mouse(pilot, events.MouseDown, x, 8)  # and the real thing, right after
        await _mouse(pilot, events.MouseUp, x, 8)
        seen["left_left"] = app.sidebar.outer_size.width
        await _settled(pilot)
        seen["state"] = _state(isolated_home)
        return seen

    seen = drive(go)
    declared = seen["declared"]
    assert isinstance(declared, int)
    assert seen["left_middle_left"] == declared + RESIZE_STEP
    assert seen["left_right_left"] == declared + RESIZE_STEP
    assert seen["left_left"] == declared, "two left clicks at one cell, close together: the reset"
    state = seen["state"]
    assert isinstance(state, dict) and SIDEBAR_WIDTH_KEY not in state


def test_a_width_the_file_already_has_is_not_rewritten_and_a_save_due_at_quit_is_not_lost(
    tmp_path: Path, script: Script, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    # `<` then `>` must both land inside one debounce, and no length of debounce promises
    # that: a key press waits for the process to look idle, up to a second twice over, and on
    # two ambient legs of #203 (runs 36184588899 and 36184598254) a 0.5 s debounce ran out
    # between the keys and 30 was written. So this test's debounce never ends on its own. The
    # test ends it with the call the timer makes (`wake`), once the saver holds the width.
    monkeypatch.setattr(Autosave, "DEBOUNCE", 3600.0)
    rewrites: list[object] = []

    def spy(target: Path, body: str, *, keep_mode: bool = True, durable: bool = True) -> None:
        rewrites.append(json.loads(body).get(SIDEBAR_WIDTH_KEY))
        write_replacing(target, body, keep_mode=keep_mode, durable=durable)

    monkeypatch.setattr(state_file, "write_replacing", spy)

    async def go(pilot: Pilot[None]) -> int:
        app = fleet_app(pilot)
        declared = _declared(app)
        saver = app.query_one(Divider)._autosave
        assert saver is not None

        async def debounce_ends(width: int) -> None:
            await _asked(pilot, width)
            saver.wake()
            await _settled(pilot)

        app.sidebar.focus()
        await pilot.press("greater_than_sign")
        await debounce_ends(declared + RESIZE_STEP)  # one write: 34
        await pilot.press("less_than_sign")
        await _asked(pilot, declared)  # 30 queued...
        await pilot.press("greater_than_sign")  # ...and back to what the file says: handed
        await debounce_ends(declared + RESIZE_STEP)  # over, not rewritten — the file decides
        await pilot.press("greater_than_sign")  # 38 queued — and the app quits inside the debounce
        await _asked(pilot, declared + 2 * RESIZE_STEP)
        return declared

    declared = drive(go)
    step = RESIZE_STEP
    assert rewrites == [declared + step, declared + 2 * step]
    assert _state(isolated_home)[SIDEBAR_WIDTH_KEY] == declared + 2 * step, "flushed at quit"


class _Split(App[None]):
    """A divider whose neighbour may collapse to nothing: `min-width: 0` is a floor of 0."""

    CSS = """
    #left { width: 10; min-width: 0; height: 1fr; }
    #right { width: 1fr; height: 1fr; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Static("L", id="left")
            yield Divider("#left", id="handle")
            yield Static("R", id="right")


def test_a_min_width_of_zero_is_a_floor_of_zero(tmp_path: Path, script: Script) -> None:
    """`cells(...) or 1` read a declared `min-width: 0` as 1 — at both sites."""

    async def collapse() -> tuple[tuple[int, int], int]:
        async with _Split().run_test(size=(40, 6)) as pilot:
            await pilot.pause()
            handle = pilot.app.query_one(Divider)
            bounds = handle.bounds()
            await _mouse(pilot, events.MouseDown, 10, 2)
            await _mouse(pilot, events.MouseMove, 0, 2)
            await _mouse(pilot, events.MouseUp, 0, 2)
            return bounds, pilot.app.query_one("#left").outer_size.width

    bounds, collapsed = asyncio.run(collapse())
    assert bounds == (0, 39), (
        "the floor is the stylesheet's 0; the ceiling the screen less this column"
    )
    assert collapsed == 0

    async def ceiling(pilot: Pilot[None]) -> int | None:
        app = fleet_app(pilot)
        app.sidebar.styles.min_width = 0
        await pilot.resize_terminal(1 + Panes.MIN_CONTENT, SIZE[1])  # room for the content alone
        await _through_the_app(pilot)
        return cells(app.sidebar.styles.max_width)

    assert drive(ceiling) == 0, "the container's ceiling honours a floor of 0 too"


def test_a_gesture_at_the_ceiling_with_a_huge_width_on_file_does_not_reach_the_style_setter(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """The ceiling rule wrote the UNCAPPED remembered width into `styles.width`: with `10**400`
    on file a drag that landed on the ceiling died with `OverflowError` — the launch had been
    made safe, the first gesture had not."""
    seed(tmp_path, ("prj_a", "alpha", None))
    _write_state(isolated_home, {SIDEBAR_WIDTH_KEY: 10**400})

    async def go(pilot: Pilot[None]) -> tuple[int, int | None, object]:
        app = fleet_app(pilot)
        await _restored(pilot)
        x = app.sidebar.outer_size.width  # the ceiling
        await _mouse(pilot, events.MouseDown, x, 5)
        await _mouse(pilot, events.MouseMove, 60, 5)
        await _mouse(pilot, events.MouseMove, 130, 5)
        await _mouse(pilot, events.MouseUp, 130, 5)
        await _settled(pilot)
        return (
            app.sidebar.outer_size.width,
            cells(app.sidebar.styles.inline.width),
            _state(isolated_home)[SIDEBAR_WIDTH_KEY],
        )

    shown, inline, on_file = drive(go)
    assert shown == Panes.sidebar_ceiling(SIZE[0], 24)
    assert inline == WIDEST_ASK, "the ask put back by the ceiling rule is the capped one"
    assert on_file == 10**400, "and the file is left as the user wrote it"


def test_a_drag_that_ends_off_the_handle_breaks_the_double_click_chain(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """Textual sends a `Click` only when the release lands on the widget the press did; a drag
    out past the ceiling ends on the content, so `on_click` never ran and the chain stayed armed
    from the click before it — one later left click reset the width and deleted the key."""
    seed(tmp_path, ("prj_a", "alpha", None))
    ceiling = Panes.sidebar_ceiling(SIZE[0], 24)
    _write_state(isolated_home, {SIDEBAR_WIDTH_KEY: ceiling})

    async def go(pilot: Pilot[None]) -> tuple[int, object]:
        app = fleet_app(pilot)
        await pilot.pause()
        await _mouse(pilot, events.MouseDown, ceiling, 5)  # a still left click at the handle
        await _mouse(pilot, events.MouseUp, ceiling, 5)
        await _mouse(pilot, events.MouseDown, ceiling, 5)  # a drag in and back out past the ceiling
        await _mouse(pilot, events.MouseMove, 60, 5)
        await _mouse(pilot, events.MouseMove, 130, 5)
        await _mouse(pilot, events.MouseUp, 130, 5)  # released on the content: no Click
        await _mouse(pilot, events.MouseDown, ceiling, 5)  # one left click, within the chain window
        await _mouse(pilot, events.MouseUp, ceiling, 5)
        await _settled(pilot)
        return app.sidebar.outer_size.width, _state(isolated_home).get(SIDEBAR_WIDTH_KEY)

    assert drive(go) == (ceiling, ceiling), "a drag breaks the chain, Click or no Click"


def test_the_fleet_ui_flushes_a_theme_picked_inside_the_debounce_at_quit(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """The theme's save is debounced like the width's; a pick and a `q` within a tenth of a
    second must still land — the width's quit-time flush was tested, the theme's was not."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> None:
        fleet_app(pilot).theme = "nord"  # and out, before the debounce fires

    drive(go)
    assert _state(isolated_home)["board_theme"] == "nord"


def test_the_fleet_ui_reads_the_saved_theme_once_at_start_up(
    tmp_path: Path, script: Script, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of the #167 fold, F8: the theme's saver read `state.json` in `FleetApp.__init__`
    for a start value nothing consults, and `restore_theme` read it again at mount."""
    seed(tmp_path, ("prj_a", "alpha", None))
    _write_state(isolated_home, {"board_theme": "nord"})
    reads: list[str | None] = []

    def load() -> str | None:
        reads.append(_load_saved_theme())
        return reads[-1]

    monkeypatch.setattr("aisquare.cli.ui.theme._load_saved_theme", load)

    async def go(pilot: Pilot[None]) -> str:
        return fleet_app(pilot).theme

    assert drive(go) == "nord", "the saved theme is still restored"
    assert reads == ["nord"]


def test_the_ceiling_rule_reads_what_the_saver_was_last_asked_not_the_last_confirmed_write(
    tmp_path: Path, script: Script, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """120 on file, the ceiling 99. Drag to 60 and release; while 60 is in flight drag back out
    past the ceiling. Read against the last CONFIRMED write (120), the rule kept the ask at 120
    and wrote nothing, and the in-flight 60 landed: shown 99, ask 120, file 60."""
    seed(tmp_path, ("prj_a", "alpha", None))
    _write_state(isolated_home, {SIDEBAR_WIDTH_KEY: 120})
    real_update = update_state

    def slow(key: str, value: object) -> None:
        time.sleep(0.4)
        real_update(key, value)

    monkeypatch.setattr("aisquare.cli.ui.autosave.update_state", slow)

    async def go(pilot: Pilot[None]) -> tuple[int, int | None, object]:
        app = fleet_app(pilot)
        await pilot.pause()
        ceiling = app.sidebar.outer_size.width
        await _drag(pilot, ceiling, 60)
        await pilot.pause(Autosave.DEBOUNCE + 0.1)  # 60 is being written now
        await _drag(pilot, 60, 130)  # back out, past the ceiling
        await _settled(pilot)
        return (
            app.sidebar.outer_size.width,
            app.query_one(Divider)._ask,
            _state(isolated_home)[SIDEBAR_WIDTH_KEY],
        )

    shown, ask, on_file = drive(go)
    ceiling = Panes.sidebar_ceiling(SIZE[0], 24)
    assert (shown, ask, on_file) == (ceiling, ceiling, ceiling), "screen, ask and file agree"


def test_a_refusal_that_arrives_at_quit_is_reported_after_the_run_not_as_a_traceback(
    tmp_path: Path,
    script: Script,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With the lock held through the quit, the width's refusal lands during `flush_all`'s join,
    when the divider is already detached: it used to print `Exception in callback …
    NoActiveAppError` under the shell prompt and say nothing else. Now the app carries the line
    for the launcher to print, and the toast's callback runs in the loop's own context."""
    seed(tmp_path, ("prj_a", "alpha", None))
    monkeypatch.setattr(state_file, "LOCK_WAIT_S", 0.3)
    update_state("board_theme", "nord")  # creates the lock file
    fd = os.open(isolated_home / "state.json.lock", os.O_RDONLY)
    lock_exclusive(fd)
    apps: list[FleetApp] = []

    async def go(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        apps.append(app)
        app.sidebar.focus()
        await pilot.press("greater_than_sign")  # and quit inside the debounce
        await _asked(pilot, _declared(app) + RESIZE_STEP)

    try:
        with caplog.at_level(logging.ERROR):
            drive(go)
    finally:
        unlock(fd)
        os.close(fd)
    (line,) = apps[0].unsaved
    assert line.startswith("the navigator's width was not saved: ")
    assert "state.json.lock is held by another process" in line
    assert "NoActiveAppError" not in caplog.text and "Exception in callback" not in caplog.text


def test_run_ui_says_what_the_quit_could_not_save(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _Quit:
        def __init__(self, **options: object) -> None:
            self.unsaved = ["the theme was not saved: state.json.lock is held by another process"]
            self.remote = RemoteController()  # off: nothing for run_ui to wait for

        def run(self) -> None:
            return None

    monkeypatch.setattr(app_mod, "FleetApp", _Quit)
    app_mod.run_ui()
    err = capsys.readouterr().err
    assert "⚠ the theme was not saved: state.json.lock is held by another process" in err


def test_a_quit_in_the_middle_of_a_drag_keeps_the_width_the_drag_reached(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """`on_unmount` cleared `_dragging` and settled nothing: `q` (or the terminal closing) with the
    button still held showed 60 and left `state.json` empty."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        await _mouse(pilot, events.MouseDown, app.sidebar.outer_size.width, 5)
        await _mouse(pilot, events.MouseMove, 60, 5)  # and the app exits with the button held

    drive(go)
    assert _state(isolated_home)[SIDEBAR_WIDTH_KEY] == 60


def test_a_quit_in_the_middle_of_a_drag_at_the_ceiling_keeps_the_wider_width_on_file(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """120 on file, the ceiling 99 (review of the #167 fold, F4). A drag in to 60 and back out
    past the ceiling, then `q` with the button still held: `on_unmount` remembered the 99 it
    reached without the ceiling rule, and 99 replaced 120 — which a release at the same place
    keeps (`test_the_ceiling_rule_reads_what_the_saver_was_last_asked_...`)."""
    seed(tmp_path, ("prj_a", "alpha", None))
    _write_state(isolated_home, {SIDEBAR_WIDTH_KEY: 120})

    async def go(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        await pilot.pause()
        await _mouse(pilot, events.MouseDown, app.sidebar.outer_size.width, 5)
        await _mouse(pilot, events.MouseMove, 60, 5)
        await _mouse(pilot, events.MouseMove, 130, 5)  # and the app exits with the button held

    drive(go)
    assert _state(isolated_home)[SIDEBAR_WIDTH_KEY] == 120


def test_hiding_the_divider_mid_drag_ends_the_drag(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """`Hide` is one of the capture's exits the module docstring names; nothing pinned it."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> tuple[bool, object, int, object]:
        app = fleet_app(pilot)
        divider = app.query_one(Divider)
        await _mouse(pilot, events.MouseDown, app.sidebar.outer_size.width, 5)
        await _mouse(pilot, events.MouseMove, 60, 5)
        divider.display = False
        # `Hide` is posted by the layout pass that the pause ends with, so it is handled
        # in a pause after that one: the app is let go quiet before the drag is read.
        await settle(app)
        state = (divider.has_class("-dragging"), app.mouse_captured, app.sidebar.outer_size.width)
        divider.display = True
        await _settled(pilot)
        return (*state, _state(isolated_home).get(SIDEBAR_WIDTH_KEY))

    assert drive(go) == (False, None, 60, 60)


@_needs_tmux
def test_selecting_an_agent_focuses_its_pane_so_typing_reaches_the_agent_not_the_app(
    tmp_path: Path, script: Script, no_real_tmux: list[tuple[str, ...]]
) -> None:
    """#147 (2026-09-13 addendum): selecting a row showed the pane and left focus in
    the sidebar, where `q` quits — a sentence typed at what looked like Claude Code
    closed the fleet UI on its first q. The selected agent's pane takes the keyboard;
    F12 stays the way back."""
    seed(tmp_path, ("prj_a", "alpha", "amber-otter"))
    script["prj_a"] = [status("prj_a", "coder-auth", "coder", "working")]

    async def go(pilot: Pilot[None]) -> tuple[bool, int | None, bool]:
        app = fleet_app(pilot)
        await pilot.click(row_for(app, "agt_a_coder-auth"))
        await settle(app)
        await pilot.pause()
        view = app.current_view()
        assert isinstance(view, AgentView)
        focused_pane = app.focused is view.pane
        await pilot.press("q")
        await settle(app)
        alive = app.return_code
        await pilot.press("f12")
        await settle(app)  # the pane posts EscapeToSidebar, and the app moves the focus
        return focused_pane, alive, app.focused is view.pane

    focused_pane, alive, still_in_pane = drive(go)
    assert focused_pane, "the pane, not the sidebar, has the keyboard after a selection"
    assert alive is None, "q went to the agent, not to the app"
    # Typed text goes as hex (``send-keys -H``): ``q`` is ``71``.
    typed = [argv for argv in no_real_tmux if "send-keys" in argv and argv[-2:] == ("-H", "71")]
    assert typed, f"q was forwarded to tmux: {no_real_tmux[-3:]}"
    assert not still_in_pane, "F12 is the deliberate way back to the sidebar"


def test_restart_from_the_agent_view_selects_the_new_row_in_the_shell(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#138 end to end through the real app: the exited row's view offers Restart,
    the service is called with the row's project and the pane's size, the frame is
    re-read, and the NEW row is what the shell shows and highlights — with its pane
    holding the keyboard, as a selected agent's does (#147)."""
    seed(tmp_path, ("prj_a", "alpha", "amber-otter"))
    exited = status("prj_a", "manager", "manager", "exited", exit_status=130)
    script["prj_a"] = [exited]
    started = status("prj_a", "manager", "manager", "waiting", minute=5)
    started = started.model_copy(
        update={"agent": started.agent.model_copy(update={"id": "agt_a_new"})}
    )
    calls: list[tuple[str, str, tuple[int, int] | None]] = []

    def fake_restart(
        project: ProjectInfo, label: str, *, size: tuple[int, int] | None = None, **kw: object
    ) -> fleet_service.RestartReceipt:
        calls.append((project.id, label, size))
        script["prj_a"] = [started]  # what the next listing answers
        return fleet_service.RestartReceipt(
            replaced=exited.agent, started=started.agent, resumed=True, was_running=False,
            tmux_session="asq-amber-otter",
        )  # fmt: skip

    monkeypatch.setattr(fleet_service, "restart", fake_restart)
    clock = guard_clock.install(monkeypatch)

    async def go(
        pilot: Pilot[None],
    ) -> tuple[str | None, str | None, list[str], list[str], bool]:
        app = fleet_app(pilot)
        await pilot.click(row_for(app, "agt_a_manager"))
        await settle(app)
        view = app.current_view()
        assert isinstance(view, AgentView)
        stop_shown = view.query_one("#agent-stop", Button).display
        clock.step()  # the 💤 row read, then Restart: past the click guard's window
        await pilot.click("#agent-restart")
        await settle(app)
        await pilot.pause()
        await pilot.pause()
        current = app.current_view()
        # The pane's own "(pane gone)" toast is there too: read them all.
        toasts = await _toast_list(app)
        rows = [shown(row) for row in card_for(app, "prj_a").query(AgentRow)]
        typing_reaches = isinstance(current, AgentView) and app.focused is current.pane
        assert stop_shown is True  # the 💤 row's Stop removes its dead window
        return (
            (current.id if current else None),
            app.sidebar.selected_key,
            toasts,
            rows,
            typing_reaches,
        )

    current, selected, toasts, rows, typing_reaches = drive(go, notifications=True)
    assert calls and calls[0][:2] == ("prj_a", "manager")
    assert current == "agent-agt_a_new" and selected == "agent:agt_a_new"
    assert typing_reaches, "the new pane has the keyboard, not nobody (where q quits the UI)"
    assert any("✓ restarted manager (alpha) — resumed its session" in toast for toast in toasts), (
        toasts
    )
    assert len(rows) == 1 and rows[0].rstrip().endswith("⏸")  # the old 💤 exited row is gone


def _replacement(old: FleetAgentStatus, agent_id: str) -> FleetAgentStatus:
    """The row a restart or a switch starts for ``old``: its label, a new id and pane."""
    agent = old.agent.model_copy(
        update={
            "id": agent_id,
            "pane_id": "%2",
            "created_at": old.agent.created_at + timedelta(minutes=5),
        }
    )
    return FleetAgentStatus(agent=agent, state="waiting")


_MESSAGES_ONLY = "no-worker-has-this-group"
"""``settle_page``'s ``group`` for a test that holds a worker on purpose: the messages
settle, and the held worker is not waited for."""


def test_a_restart_that_ends_after_the_user_moved_on_leaves_them_where_they_are(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User feedback (2026-10-19): "when I am in the pane of manager 1 and manager 0
    restarts, it takes me back to manager 0's pane, and my mouse is either on stop or
    restart". A restart started from manager 0's view ends after the user opened manager
    1's, and the shell showed, highlighted, remembered and focused the new manager 0 all
    the same. Now nothing of the user's moves: the restart is a toast naming its project,
    and the sidebar lists the new row."""
    seed(tmp_path, ("prj_a", "alpha", "amber-otter"), ("prj_b", "beta", "brave-lynx"))
    zero = status("prj_a", "manager", "manager", "working")
    one = status("prj_b", "manager", "manager", "working")
    script["prj_a"], script["prj_b"] = [zero], [one]
    started = _replacement(zero, "agt_a_new")
    release = threading.Event()

    def slow_restart(
        project: ProjectInfo, label: str, **kw: object
    ) -> fleet_service.RestartReceipt:
        release.wait(10)  # the user moves on while it runs
        script["prj_a"] = [started]
        return fleet_service.RestartReceipt(
            replaced=zero.agent, started=started.agent, resumed=True, was_running=True,
            tmux_session="asq-amber-otter",
        )  # fmt: skip

    monkeypatch.setattr(fleet_service, "restart", slow_restart)

    async def go(
        pilot: Pilot[None],
    ) -> tuple[str | None, str | None, str | None, bool, list[str], list[str]]:
        app = fleet_app(pilot)
        told = toast_log.record(app)
        await pilot.click(row_for(app, "agt_a_manager"))
        await settle(app)
        view = app.current_view()
        assert isinstance(view, AgentView)
        view.query_one("#agent-restart", Button).press()
        await settle_page(app, group=_MESSAGES_ONLY)
        await pilot.click(row_for(app, "agt_b_manager"))
        await settle_page(app, group=_MESSAGES_ONLY)
        mine = app.current_view()
        assert isinstance(mine, AgentView) and app.focused is mine.pane
        release.set()
        await settle(app)
        toasts = list(told)
        await pilot.pause()
        current = app.current_view()
        rows = [row.status.agent.id for row in card_for(app, "prj_a").query(AgentRow)]
        return (
            (current.id if current else None),
            app.sidebar.selected_key,
            ui_app._ui_state(ui_app.SELECTED_KEY),
            app.focused is mine.pane,
            toasts,
            rows,
        )

    current, selected, remembered, typing_reaches, toasts, rows = drive(go, notifications=True)
    assert current == "agent-agt_b_manager", "the restart took the screen back to its row"
    assert selected == "agent:agt_b_manager"
    assert remembered == "agent:prj_b/agt_b_manager"
    assert typing_reaches, "the keyboard stays with the pane the user is typing into"
    assert any("✓ restarted manager (alpha) — resumed its session" in t for t in toasts), toasts
    assert rows == ["agt_a_new"]  # the replaced row is updated, quietly


def _replaced_out_of_the_ui(script: Script, old: FleetAgentStatus) -> None:
    script[old.agent.project_id] = [_replacement(old, "agt_a_new")]


def _vanished(script: Script, old: FleetAgentStatus) -> None:
    script[old.agent.project_id] = []


@pytest.mark.parametrize(
    ("shown_row", "change"),
    [
        pytest.param("agent:agt_b_manager", _replaced_out_of_the_ui, id="another-agent-replaced"),
        pytest.param("agent:agt_a_manager", _replaced_out_of_the_ui, id="the-shown-agent-replaced"),
        pytest.param("agent:agt_a_manager", _vanished, id="the-shown-row-vanishes"),
        pytest.param("project:prj_a", _replaced_out_of_the_ui, id="its-manager-tab-replaced"),
    ],
)
def test_a_row_replaced_outside_the_ui_moves_nothing_of_the_users(
    tmp_path: Path,
    script: Script,
    shown_row: str,
    change: Callable[[Script, FleetAgentStatus], None],
) -> None:
    """The other ways a row is replaced reach asq only as its next frame: ``fleet restart``
    and ``fleet switch`` from a shell, the hand-over that ``[accounts] on_limit =
    "switch"`` starts from a hook (its own process), and the phone's Restart and Switch
    (Remote's thread). A frame repaints the sidebar and feeds the open views, and never
    shows, highlights, remembers or focuses anything: the user stays on the view they
    had, a dead one included, which keeps its last screen until they pick the new row.
    The Manager tab follows its project's new manager in place, as it always did."""
    seed(tmp_path, ("prj_a", "alpha", "amber-otter"), ("prj_b", "beta", "brave-lynx"))
    zero = status("prj_a", "manager", "manager", "working")
    script["prj_a"] = [zero]
    script["prj_b"] = [status("prj_b", "manager", "manager", "working")]
    kind, ident = shown_row.split(":", 1)

    async def go(pilot: Pilot[None]) -> tuple[object, object]:
        app = fleet_app(pilot)
        target = (
            row_for(app, ident) if kind == "agent" else card_for(app, ident).query_one(ProjectTitle)
        )
        await pilot.click(target)
        await settle(app)

        def where() -> tuple[str | None, str | None, str | None, Widget | None]:
            current = app.current_view()
            return (
                (current.id if current else None),
                app.sidebar.selected_key,
                ui_app._ui_state(ui_app.SELECTED_KEY),
                app.focused,
            )

        before = where()
        change(script, zero)
        app.refresh_data()
        await settle(app)
        await pilot.pause()
        return before, where()

    before, after = drive(go)
    assert after == before


def _acting_fleet(
    monkeypatch: pytest.MonkeyPatch, script: Script, one: FleetAgentStatus
) -> list[tuple[str, str]]:
    """Fake ``restart`` and ``stop`` that record what they were asked to act on."""
    acted: list[tuple[str, str]] = []

    def fake_restart(
        project: ProjectInfo, label: str, **kw: object
    ) -> fleet_service.RestartReceipt:
        acted.append(("restart", project.id))
        started = _replacement(one, "agt_b_new")
        script["prj_b"] = [started]
        return fleet_service.RestartReceipt(
            replaced=one.agent, started=started.agent, resumed=True, was_running=True,
            tmux_session="asq-brave-lynx",
        )  # fmt: skip

    def fake_stop(project: ProjectInfo, label: str, **kw: object) -> fleet_service.StopReceipt:
        acted.append(("stop", project.id))
        return fleet_service.StopReceipt(one.agent, [])

    monkeypatch.setattr(fleet_service, "restart", fake_restart)
    monkeypatch.setattr(fleet_service, "stop", fake_stop)
    return acted


def test_a_click_on_stop_or_restart_as_the_view_switches_in_acts_on_nothing(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User feedback (2026-10-19), the other half: a view that came up under a resting
    pointer took the click aimed at the one before it, on its Stop or Restart. Within
    CLICK_GUARD of the view switching in, a click on either starts no worker and says
    so; the same click after it acts."""
    seed(tmp_path, ("prj_a", "alpha", "amber-otter"), ("prj_b", "beta", "brave-lynx"))
    script["prj_a"] = [status("prj_a", "manager", "manager", "working")]
    one = status("prj_b", "manager", "manager", "working")
    script["prj_b"] = [one]
    acted = _acting_fleet(monkeypatch, script, one)
    clock = guard_clock.install(monkeypatch)

    async def go(pilot: Pilot[None]) -> tuple[list[tuple[str, str]], list[str]]:
        app = fleet_app(pilot)
        told = toast_log.record(app)
        await pilot.click(row_for(app, "agt_a_manager"))
        await settle(app)
        clock.step()  # a while on manager 0's pane
        await pilot.click(row_for(app, "agt_b_manager"))
        await settle(app)
        view = app.current_view()
        assert isinstance(view, AgentView) and view.status.agent.id == "agt_b_manager"
        await pilot.click(view.query_one("#agent-restart", Button))
        await pilot.click(view.query_one("#agent-stop", Button))
        await settle(app)
        toasts = list(told)
        ignored = list(acted)
        clock.step()
        await pilot.click(view.query_one("#agent-restart", Button))
        await settle(app)
        return ignored, toasts

    ignored, toasts = drive(go, notifications=True)
    assert ignored == [], "a click as the view switched in restarted or stopped an agent"
    for verb in ("Restart", "Stop"):
        notice = f"ignored a click on {verb} — the view had just changed under the pointer"
        assert toasts.count(notice) == 1, toasts
    assert acted == [("restart", "prj_b")], "the click after the window restarts as it did"


def test_enter_on_a_focused_restart_is_not_guarded(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key is a deliberate gesture: Enter on the focused Restart acts at once, however
    recently the view came up."""
    seed(tmp_path, ("prj_b", "beta", "brave-lynx"))
    one = status("prj_b", "manager", "manager", "working")
    script["prj_b"] = [one]
    acted = _acting_fleet(monkeypatch, script, one)
    guard_clock.install(monkeypatch)

    async def go(pilot: Pilot[None]) -> bool:
        app = fleet_app(pilot)
        await pilot.click(row_for(app, "agt_b_manager"))
        await settle(app)
        view = app.current_view()
        assert isinstance(view, AgentView)
        restart = view.query_one("#agent-restart", GuardedButton)
        guarding = restart.guarding
        restart.focus()
        await pilot.press("enter")
        await settle(app)
        return guarding

    assert drive(go) is True, "the window was open when Enter was pressed"
    assert acted == [("restart", "prj_b")]


def test_the_sidebar_hides_captured_directories_until_a_shows_them(
    tmp_path: Path, script: Script, project_capture: None
) -> None:
    """#139: every directory a hooked session ran in used to be a card. Now only the
    projects added on purpose are; `a` shows the captured ones too, marked."""
    seed(tmp_path, ("prj_a", "alpha", None))
    with store_session() as store:
        store.ensure_project(ProjectInfo(id="prj_scratch", root=tmp_path / "scratch"))  # a hook

    async def go(pilot: Pilot[None]) -> tuple[list[str], list[str], str, list[str], str]:
        app = fleet_app(pilot)
        before = [card.project.id for card in app.query(ProjectCard)]
        app.sidebar.focus()
        await pilot.press("a")
        await settle(app)  # the app's binding, reached once the key has bubbled up to it
        with_captured = [card.project.id for card in app.query(ProjectCard)]
        title = shown_text(app, "prj_scratch")
        await pilot.press("a")
        await settle(app)
        after = [card.project.id for card in app.query(ProjectCard)]
        await pilot.press("question_mark")
        await settle(app)
        return before, with_captured, title, after, shown(app.screen.query_one(Static))

    def shown_text(app: FleetApp, project_id: str) -> str:
        return shown(card_for(app, project_id).query_one(CardChips))

    before, with_captured, title, after, keys = drive(go)
    assert before == ["prj_a"], "a captured directory is not a card"
    assert with_captured == ["prj_a", "prj_scratch"]
    assert "captured" in title
    assert after == ["prj_a"], "a hides them again"
    assert "captured directories" in keys, "the key is on the ? screen (it has no footer label)"


def test_with_capture_off_no_captured_directory_is_listed_whatever_was_saved(
    tmp_path: Path, script: Script
) -> None:
    """``[projects] capture`` off, the default since 0.8.1: the rows captured before stay
    in the store, and the shell lists none of them — not even with ``a``'s toggle saved
    on, which is how ~5 of them came back on a machine that had pressed it once. ``a`` is
    no key then, and the ? screen does not offer it."""
    seed(tmp_path, ("prj_a", "alpha", None))
    with store_session() as store:
        store.ensure_project(ProjectInfo(id="prj_scratch", root=tmp_path / "scratch"))  # 0.8.0
        store.set_ui_state("fleet.show_captured", "1")

    async def go(pilot: Pilot[None]) -> tuple[list[str], list[str], str]:
        app = fleet_app(pilot)
        await settle(app)
        listed = [card.project.id for card in app.query(ProjectCard)]
        app.sidebar.focus()
        await pilot.press("a")
        await settle(app)
        after_a = [card.project.id for card in app.query(ProjectCard)]
        await pilot.press("question_mark")
        await settle(app)
        return listed, after_a, shown(app.screen.query_one(Static))

    listed, after_a, keys = drive(go)
    assert listed == ["prj_a"], "the saved toggle lists nothing once capture is off"
    assert after_a == ["prj_a"], "and `a` shows nothing"
    assert "captured directories" not in keys
    with store_session() as store:
        assert store.ui_state("fleet.show_captured") == "1", "kept, for capture turned on again"
        assert [p.id for p in store.captured_projects()] == ["prj_scratch"], "nothing deleted"


def test_the_shell_reopens_what_was_open_when_its_row_is_still_there(
    tmp_path: Path, script: Script
) -> None:
    """#144: the UI restored exactly one thing at mount, the theme. What was open —
    a project, an agent, a page — is remembered in the store's ui_state and comes
    back; an agent whose row is gone falls back to its project."""
    seed(tmp_path, ("prj_a", "alpha", None))
    script["prj_a"] = [status("prj_a", "coder-auth", "coder", "working")]

    async def open_agent(pilot: Pilot[None]) -> str | None:
        app = fleet_app(pilot)
        await pilot.click(row_for(app, "agt_a_coder-auth"))
        await settle(app)
        view = app.current_view()
        return view.id if view else None

    assert drive(open_agent) == "agent-agt_a_coder-auth"
    with store_session() as store:
        assert store.ui_state("fleet.selected") == "agent:prj_a/agt_a_coder-auth"

    async def relaunch(pilot: Pilot[None]) -> tuple[str | None, str | None]:
        app = fleet_app(pilot)
        await pilot.pause()
        await pilot.pause()
        view = app.current_view()
        return (view.id if view else None), app.sidebar.selected_key

    assert drive(relaunch) == ("agent-agt_a_coder-auth", "agent:agt_a_coder-auth")

    script["prj_a"] = []  # the agent's row is gone: its project is the fallback
    assert drive(relaunch) == ("project-prj_a", "project:prj_a")

    with store_session() as store:
        store.set_ui_state("fleet.selected", "project:prj_gone")  # nothing to reopen
    assert drive(relaunch) == ("welcome", None)
    with store_session() as store:
        assert store.ui_state("fleet.selected") is None, "a stale memory is dropped, not retried"


def test_the_shell_remembers_the_captured_toggle_and_reopens_a_page(
    tmp_path: Path, script: Script, project_capture: None
) -> None:
    """Review of #169: what the shell remembers beyond a row came back untested —
    the captured directories shown with `a`, and every page under ⚙ Settings."""
    seed(tmp_path, ("prj_a", "alpha", None))
    with store_session() as store:
        store.ensure_project(ProjectInfo(id="prj_scratch", root=tmp_path / "scratch"))  # a hook

    async def press_a(pilot: Pilot[None]) -> None:
        fleet_app(pilot).sidebar.focus()
        await pilot.press("a")
        await settle(fleet_app(pilot))  # the binding has stored the toggle before the app quits

    async def relaunch(pilot: Pilot[None]) -> tuple[list[str], str | None, str | None]:
        app = fleet_app(pilot)
        await pilot.pause()
        await pilot.pause()
        view = app.current_view()
        cards = [card.project.id for card in app.query(ProjectCard)]
        return cards, (view.id if view else None), app.sidebar.selected_key

    drive(press_a)
    with store_session() as store:
        assert store.ui_state("fleet.show_captured") == "1"
    assert drive(relaunch)[0] == ["prj_a", "prj_scratch"], "the toggle survives a relaunch"
    drive(press_a)
    with store_session() as store:
        assert store.ui_state("fleet.show_captured") is None, "hiding them again is remembered"
    assert drive(relaunch)[0] == ["prj_a"]

    # Every page under ⚙ Settings reopens with Settings the highlighted row — the two
    # remembered before the hub existed (`accounts`, `doctor:`) as much as the new ones.
    for remembered, page in (
        ("settings", "settings-hub"),
        ("app-settings", "app-settings"),
        ("accounts", "accounts"),
        ("doctor:", "doctor"),
    ):
        with store_session() as store:
            store.set_ui_state("fleet.selected", remembered)
        assert drive(relaunch)[1:] == (page, "settings"), remembered


# --- groups, pins and manual order (#140) ---------------------------------------------------


def _cards(app: FleetApp) -> list[str]:
    """The visible cards and group headers in sidebar order."""
    holder = app.sidebar.query_one("#projects")
    out: list[str] = []
    for child in holder.children:
        if isinstance(child, ProjectCard) and child.display:
            out.append(child.project.id)
        elif isinstance(child, GroupHeader):
            out.append(f"group:{child.group.name}")
        elif isinstance(child, SectionLabel):
            out.append("pinned")
    return out


def test_the_sidebar_shows_groups_pins_and_manual_order_and_the_keys_move_them(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """#140 by keyboard alone: shift+↓ moves, p pins, space folds, u undoes — every
    gesture through the service, every change surviving a relaunch."""
    seed(tmp_path, ("prj_a", "api", None), ("prj_b", "cli", None), ("prj_c", "docs", None))
    script["prj_a"] = [status("prj_a", "manager", "manager", "working")]
    with store_session() as store:
        groups_service.create_group(store, "tools", ["prj_b", "prj_c"])

    async def go(
        pilot: Pilot[None],
    ) -> tuple[list[str], list[str], list[str], list[str], str, list[str], list[str]]:
        app = fleet_app(pilot)
        initial = _cards(app)
        app.sidebar.focus()
        app.sidebar.select("project:prj_c")  # the cursor's anchor
        await pilot.press("shift+up")
        await settle(app)
        moved = _cards(app)
        await pilot.press("p")
        await settle(app)
        pinned = _cards(app)
        await pilot.press("u")
        await settle(app)
        undone = _cards(app)
        app.sidebar.select("group:" + store_group_id("tools"))
        await pilot.press("space")
        await settle(app)
        folded = _cards(app)
        header = app.sidebar.query_one(GroupHeader)
        rollup = shown(header)
        await pilot.press("space")
        await settle(app)
        return initial, moved, pinned, undone, rollup, folded, _cards(app)

    def store_group_id(name: str) -> str:
        with store_session() as store:
            return groups_service.resolve_group(store, name).id

    initial, moved, pinned, undone, rollup, folded, unfolded = drive(go)
    # The group first, then the loose project; shift+↑ stepped docs above cli;
    # p moved it into the Pinned section; u put it back where it was.
    assert initial == ["group:tools", "prj_b", "prj_c", "prj_a"]
    assert moved == ["group:tools", "prj_c", "prj_b", "prj_a"]
    assert pinned == ["pinned", "prj_c", "group:tools", "prj_b", "prj_a"]
    assert undone == ["group:tools", "prj_c", "prj_b", "prj_a"]
    assert folded == ["group:tools", "prj_a"], "a folded group hides its members"
    assert rollup.startswith("▸ 📁 tools"), rollup
    assert unfolded == ["group:tools", "prj_c", "prj_b", "prj_a"]

    async def relaunch(pilot: Pilot[None]) -> list[str]:
        await pilot.pause()
        return _cards(fleet_app(pilot))

    assert drive(relaunch) == unfolded, "the arrangement is the store's, not the session's"


def test_a_group_header_rolls_up_its_members_agents(tmp_path: Path, script: Script) -> None:
    """Every member counts, the pinned one too: it is listed under Pinned, not under the
    header, and it is still the group's — left out, the header hid its bell (review of #171)."""
    seed(tmp_path, ("prj_a", "api", None), ("prj_b", "cli", None))
    script["prj_a"] = [status("prj_a", "manager", "manager", "working")]
    script["prj_b"] = [status("prj_b", "coder-1", "coder", "attention")]
    with store_session() as store:
        groups_service.create_group(store, "all", ["prj_a", "prj_b"])
        groups_service.pin(store, "prj_b")

    async def go(pilot: Pilot[None]) -> tuple[str, list[str]]:
        app = fleet_app(pilot)
        return shown(app.query_one(GroupHeader)), _cards(app)

    header, cards = drive(go)
    assert cards == ["pinned", "prj_b", "group:all", "prj_a"], "prj_b is listed once, pinned"
    assert "📁 all" in header and header.rstrip().endswith("2 · 🔔1"), header


async def _as_the_terminal_sends(
    pilot: Pilot[None],
    kind: type[events.MouseEvent],
    widget: Widget,
    offset: tuple[int, int] = (1, 0),
    *,
    shift: bool = False,
    button: int = 1,
) -> None:
    """Post one mouse event (button 1 unless told) the way the terminal driver does: to the APP.

    ``App.on_event`` is where a MouseUp over the pressed widget becomes a Click —
    in the same call that queues the MouseUp, routed by the capture standing at
    that moment. ``pilot.click`` skips it (it forwards a ready-made Click to the
    screen, pausing between events), so a sidebar that held the mouse from the
    press swallowed every real click on a title while the suite stayed green
    (review of #171, round 1).

    Posted to the app, so it is waited for through the app, as ``_mouse`` is: a
    bare pause does not wait for the app's own queue.
    """
    x, y = widget.region.offset + offset
    app = pilot.app
    app.post_message(kind(None, x, y, 0, 0, button, shift, False, False, screen_x=x, screen_y=y))
    await _through_the_app(pilot)


async def _click(pilot: Pilot[None], widget: Widget, *, shift: bool = False) -> None:
    """A real click and what it set off: a title's click is a message the sidebar posts
    to the app, whose handler opens the view, so it is over once the app is quiet."""
    await _as_the_terminal_sends(pilot, events.MouseDown, widget, shift=shift)
    await _as_the_terminal_sends(pilot, events.MouseUp, widget, shift=shift)
    await settle(fleet_app(pilot))


async def _drag_onto(
    pilot: Pilot[None], source: Widget, target: Widget, offset: tuple[int, int] = (1, 0)
) -> None:
    """Press on ``source``, move onto ``target`` with the button held, release there, and
    wait for the app to go quiet: the drop is a message the sidebar posts, as a click is."""
    await _as_the_terminal_sends(pilot, events.MouseDown, source)
    await _as_the_terminal_sends(pilot, events.MouseMove, target, offset)
    await _as_the_terminal_sends(pilot, events.MouseUp, target, offset)
    await settle(fleet_app(pilot))


def test_dragging_a_card_onto_a_group_header_groups_it_and_the_picker_groups_a_selection(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    seed(tmp_path, ("prj_a", "api", None), ("prj_b", "cli", None), ("prj_c", "docs", None))
    with store_session() as store:
        groups_service.create_group(store, "tools", ["prj_b"])

    async def go(
        pilot: Pilot[None],
    ) -> tuple[list[str], list[str], str | None, list[str], str | None, list[str]]:
        app = fleet_app(pilot)
        before = _cards(app)
        # Drag docs (a loose card) onto the group header.
        title = card_for(app, "prj_c").query_one(ProjectTitle)
        await _drag_onto(pilot, title, app.sidebar.query_one(GroupHeader), offset=(3, 0))
        await pilot.pause()
        dragged = _cards(app)
        # A press and release without motion opens the project, as before.
        await _click(pilot, card_for(app, "prj_a").query_one(ProjectTitle))
        view = app.current_view()
        opened = view.id if view is not None else None
        # shift+click marks two cards — and opens neither: Textual runs the base
        # class's on_click too, unless the mark prevents it.
        await _click(pilot, card_for(app, "prj_a").query_one(ProjectTitle), shift=True)
        await _click(pilot, card_for(app, "prj_c").query_one(ProjectTitle), shift=True)
        marked = sorted(c.project.id for c in app.query(ProjectCard) if c.has_class("marked"))
        view = app.current_view()
        after_marks = view.id if view is not None else None
        # shift+g groups them into a NEW group through the picker.
        app.sidebar.focus()
        await pilot.press("shift+g")
        await settle(app)
        assert isinstance(app.screen, GroupPicker), type(app.screen).__name__
        await pilot.press("end")  # … the last option is Ungroup; New group… is just above it
        await pilot.press("up")
        await pilot.press("enter")
        await settle(app)
        await pilot.press(*"web")
        await pilot.press("enter")
        await settle(app)
        return before, dragged, opened, marked, after_marks, _cards(app)

    before, dragged, opened, marked, after_marks, grouped = drive(go)
    assert before == ["group:tools", "prj_b", "prj_a", "prj_c"]
    assert dragged == ["group:tools", "prj_b", "prj_c", "prj_a"], "dropped on the header: last"
    assert opened == "project-prj_a", "a click on a drag handle is still a click"
    assert marked == ["prj_a", "prj_c"]
    assert after_marks == "project-prj_a", "a shift+click marks; it opens nothing"
    assert grouped == ["group:tools", "prj_b", "group:web", "prj_a", "prj_c"]
    with store_session() as store:
        names = {g.name for g in store.project_groups()}
    assert names == {"tools", "web"}


def test_a_drag_on_a_card_or_a_group_header_selects_no_text(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """Final review of #203, F1. The screen opens a text selection on the press BEFORE
    it forwards it, so the capture a handle takes in ``on_mouse_down`` is too late to
    stop one: every drag of a title or a group header also drag-selected, and a regroup
    left the rows from the pressed title to the header highlighted across the sidebar.
    A drag handle is not text, as the divider is not."""
    seed(tmp_path, ("prj_a", "api", None), ("prj_b", "cli", None), ("prj_c", "docs", None))
    with store_session() as store:
        groups_service.create_group(store, "tools", ["prj_b"])
        groups_service.create_group(store, "web", ["prj_a"])

    async def go(
        pilot: Pilot[None],
    ) -> tuple[list[str], list[Widget], str | None, list[str], list[Widget]]:
        app = fleet_app(pilot)
        tools = app.sidebar.query_one("#group-" + group_id("tools"), GroupHeader)
        web = app.sidebar.query_one("#group-" + group_id("web"), GroupHeader)
        title = card_for(app, "prj_c").query_one(ProjectTitle)
        await _drag_onto(pilot, title, tools, offset=(3, 0))
        carded = _cards(app), list(app.screen.selections), app.screen.get_selected_text()
        await _drag_onto(pilot, web, tools, offset=(3, 0))
        return (*carded, _cards(app), list(app.screen.selections))

    def group_id(name: str) -> str:
        with store_session() as store:
            return groups_service.resolve_group(store, name).id

    carded, card_selections, selected, headed, header_selections = drive(go)
    # The premise: both drags did what a drag does.
    assert carded == ["group:tools", "prj_b", "prj_c", "group:web", "prj_a"], carded
    assert headed == ["group:web", "prj_a", "group:tools", "prj_b", "prj_c"], headed
    assert card_selections == [] and selected is None, "a card's drag selects no text"
    assert header_selections == [], "nor does a group header's"


@_needs_tmux
def test_a_card_released_over_an_agents_pane_copies_nothing_and_ctrl_c_still_interrupts(
    tmp_path: Path,
    script: Script,
    no_real_tmux: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Final review of #203, F1, where it costs the most. The drag-select a card's press
    opened reached into the pane it was released over: the pane copied its rows to the
    clipboard with a toast, and the highlight stood, so the pane's next ctrl+c copied
    again instead of interrupting the agent. A card let go over a pane snaps back and
    touches nothing else."""
    seed(tmp_path, ("prj_a", "alpha", None))
    script["prj_a"] = [status("prj_a", "coder-auth", "coder", "working")]
    tmux = scripted_pane(no_real_tmux, ["red plain", "second row", "third row"])
    monkeypatch.setattr(tmux_core, "_tmux", tmux)

    async def go(pilot: Pilot[None]) -> tuple[str, int, bool, list[tuple[str, ...]]]:
        app = fleet_app(pilot)
        pane, _header = await _agent_pane(pilot)
        title = card_for(app, "prj_a").query_one(ProjectTitle)
        await press(pilot, title, (1, 0))
        await move(pilot, pane, (5, 1), button=1)
        await release(pilot, pane, (5, 1))
        await settle(app)  # "no highlight" read before a late one lands tests nothing
        standing = pane.has_standing_selection()
        pane.focus()
        await pilot.pause()
        await pilot.press("ctrl+c")
        await settle(app)  # read once the key has reached the pane, or "no copy" tests nothing
        return app.clipboard, len(app._notifications), standing, tmux.sent()

    clipboard, toasts, standing, sent = drive(go, notifications=True)
    assert clipboard == "" and toasts == 0, "a card let go over a pane copies nothing"
    assert not standing, "and leaves no highlight there"
    assert sent == [("C-c",)], "so ctrl+c in the pane is the agent's interrupt"


def test_a_mark_on_a_card_that_leaves_the_list_goes_with_it(
    tmp_path: Path, script: Script, isolated_home: Path, project_capture: None
) -> None:
    """Final review of #203, F5. A mark's highlight goes with its card, and the mark
    stayed: a project forgotten from a shell refused the next drag of the selection
    as a whole ("nothing to do: 'prj_c' is gone"), and a captured directory hidden
    again with `a` was moved into the group with the card the user dragged, without
    a word. The frame keeps only the marks on projects it lists."""
    seed(tmp_path, ("prj_a", "api", None), ("prj_b", "cli", None), ("prj_c", "docs", None))
    with store_session() as store:
        store.ensure_project(ProjectInfo(id="prj_scratch", root=tmp_path / "scratch"))  # a hook
        tools, _ = groups_service.create_group(store, "tools", ["prj_b"])

    async def go(pilot: Pilot[None]) -> tuple[list[str], list[str], list[str]]:
        app = fleet_app(pilot)
        app.sidebar.focus()
        await pilot.press("a")  # the captured directory is a card now
        await settle(app)
        for project_id in ("prj_a", "prj_c", "prj_scratch"):
            await _click(pilot, card_for(app, project_id).query_one(ProjectTitle), shift=True)
        marked = app.sidebar.marked_ids()
        with store_session() as store:
            store.forget_project("prj_c")
        app.sidebar.focus()
        await pilot.press("a")  # hidden again; the refresh also drops the forgotten docs
        await settle(app)
        kept = app.sidebar.marked_ids()
        header = app.sidebar.query_one(GroupHeader)
        await _drag_onto(pilot, card_for(app, "prj_a").query_one(ProjectTitle), header, (3, 0))
        await pilot.pause()
        toasts = [str(n.message) for n in app._notifications]
        return marked, kept, toasts

    marked, kept, toasts = drive(go, notifications=True)
    assert marked == ["prj_a", "prj_c", "prj_scratch"], "the premise: three marks"
    assert kept == ["prj_a"], "only the mark on a card still listed"
    assert not [t for t in toasts if "is gone" in t], toasts
    with store_session() as store:
        layout = {p.id: p.group_id for p in store.list_projects(all=True)}
    assert layout == {"prj_a": tools.id, "prj_b": tools.id, "prj_scratch": None}, (
        "the drag moved the card the user saw, and nothing hidden with it"
    )


@pytest.mark.parametrize(
    ("sequence", "arrives_as"),
    [("G", "G"), ("\x1b[103;2;71u", "G"), ("\x1b[103;2u", "shift+g")],
    ids=["legacy", "kitty-with-text", "kitty-without-text"],
)
def test_shift_g_groups_the_marked_cards_as_a_terminal_sends_it(
    tmp_path: Path, script: Script, isolated_home: Path, sequence: str, arrives_as: str
) -> None:
    """Final review of #203, F4. ``pilot.press("shift+g")`` sends a key name Textual's
    parser makes only of a kitty report that carries no text. A legacy terminal sends
    ``G``, and so does kitty at the flags Textual enables (the shift is dropped when
    the key carries text), and with ``shift+g`` alone bound the gesture the help screen
    names did nothing. So the key here is what the parser makes of each terminal's
    bytes, posted to the app as the driver posts it."""
    seed(tmp_path, ("prj_a", "api", None), ("prj_b", "cli", None))

    async def go(pilot: Pilot[None]) -> tuple[list[str], list[str], str]:
        app = fleet_app(pilot)
        for project_id in ("prj_a", "prj_b"):
            await _click(pilot, card_for(app, project_id).query_one(ProjectTitle), shift=True)
        marked = app.sidebar.marked_ids()
        app.sidebar.focus()
        await pilot.pause()
        keys = [token for token in XTermParser().feed(sequence) if isinstance(token, events.Key)]
        for key in keys:
            app.post_message(key)
        await _through_the_app(pilot)
        return marked, [key.key for key in keys], type(app.screen).__name__

    marked, keys, screen = drive(go)
    assert marked == ["prj_a", "prj_b"] and keys == [arrives_as], (marked, keys)
    assert screen == GroupPicker.__name__, "shift+g opens the picker for the marked cards"


def test_m_marks_the_card_under_the_cursor_so_a_selection_needs_no_shift_click(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """Final review of #203, F7. Shift+click was the only way to mark a card, and most
    terminals keep it for their own text selection while an app reports the mouse, so
    it never reached the card there: nothing could be marked, and shift+g and a drag of
    several cards had nothing to carry. `m` marks (and unmarks) the card under the
    cursor, an agent row's card included, and opens nothing."""
    seed(tmp_path, ("prj_a", "api", None), ("prj_b", "cli", None), ("prj_c", "docs", None))
    script["prj_c"] = [status("prj_c", "coder-1", "coder", "working")]

    async def go(pilot: Pilot[None]) -> tuple[list[str], list[str], str | None, list[str]]:
        app = fleet_app(pilot)
        app.sidebar.focus()
        for key in ("project:prj_a", "project:prj_b", "agent:agt_c_coder-1", "project:prj_b"):
            app.sidebar.select(key)  # the cursor's anchor
            await pilot.press("m")  # cli twice: marked, then unmarked
            # `m` is the sidebar's binding, not a priority one: it runs once the key has
            # bubbled back up to the sidebar, and the next select must not move the
            # cursor before it has.
            await settle(app)
        marked = app.sidebar.marked_ids()
        highlighted = sorted(c.project.id for c in app.query(ProjectCard) if c.has_class("marked"))
        view = app.current_view()
        await pilot.press("G")  # what a terminal sends for Shift+G
        await settle(app)
        assert isinstance(app.screen, GroupPicker), type(app.screen).__name__
        await pilot.press("end")  # … the last option is Ungroup; New group… is just above it
        await pilot.press("up")
        await pilot.press("enter")
        await settle(app)
        await pilot.press(*"web")
        await pilot.press("enter")
        # The new group's refresh starts the accounts read, a thread worker: its answer
        # is handled here, not while ``run_test`` tears the app down.
        await settle(app)
        return marked, highlighted, view.id if view is not None else None, _cards(app)

    marked, highlighted, opened, grouped = drive(go)
    assert marked == ["prj_a", "prj_c"] and highlighted == marked, (marked, highlighted)
    assert opened == "welcome", "a mark opens nothing"
    assert grouped == ["group:web", "prj_a", "prj_c", "prj_b"], grouped


def test_a_drop_the_store_refuses_part_way_lands_none_of_its_moves(
    tmp_path: Path, script: Script, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drag of several cards is one gesture, made of one move per card. When the store
    refused the second card's move (a FOREIGN KEY failure on a group deleted from a shell
    since the drag began), the first card's move stayed committed, and `u` had no entry
    for it because the gesture raised (review of #203). The drop is one transaction: none
    of it lands."""
    seed(tmp_path, ("prj_a", "api", None), ("prj_b", "cli", None), ("prj_c", "docs", None))
    with store_session() as store:
        tools, _ = groups_service.create_group(store, "tools")
    real = SqliteStore.update_project_layout

    def refused(self: SqliteStore, project_id: str, **fields: Any) -> ProjectInfo:
        if project_id == "prj_b" and fields.get("group_id") == tools.id:
            raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")
        return real(self, project_id, **fields)

    async def go(pilot: Pilot[None]) -> tuple[list[str], list[str], int]:
        app = fleet_app(pilot)
        before = _cards(app)
        with monkeypatch.context() as patched:  # scoped: the isolated home stays in place
            patched.setattr(SqliteStore, "update_project_layout", refused)
            app.on_drop_project(DropProject(["prj_a", "prj_b"], scope=tools.id, before=None))
        app.refresh_data()
        await settle(app)
        return before, _cards(app), len(app._undo)

    before, after, undo_depth = drive(go)
    assert after == before == ["group:tools", "prj_a", "prj_b", "prj_c"]
    assert undo_depth == 0
    with store_session() as store:
        assert [p.group_id for p in store.list_projects()] == [None, None, None]


def test_an_undo_the_store_refuses_keeps_its_entry_for_the_next_u(
    tmp_path: Path, script: Script, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``u`` popped its entry before applying it, so an undo the store refused lost the
    way back with nothing undone, and the next ``u`` said "nothing to undo" (review of
    #203). The undo is one transaction and changes nothing when refused; its entry goes
    back on the stack, and the next ``u`` applies it."""
    seed(tmp_path, ("prj_a", "api", None), ("prj_b", "cli", None))
    real = groups_service.undo
    refusals: list[str] = []

    def refused_once(store: ContextStore, entry: groups_service.UndoEntry) -> str:
        if not refusals:
            refusals.append(entry.description)
            raise sqlite3.OperationalError("database is locked")
        return real(store, entry)

    async def go(pilot: Pilot[None]) -> tuple[list[str], list[str], list[str]]:
        app = fleet_app(pilot)
        app.sidebar.focus()
        app.sidebar.select("project:prj_b")
        await pilot.press("shift+up")
        await settle(app)
        moved = _cards(app)
        with monkeypatch.context() as patched:  # scoped: the isolated home stays in place
            patched.setattr(groups_service, "undo", refused_once)
            await pilot.press("u")
            await settle(app)
            refused = _cards(app)
            await pilot.press("u")
            await settle(app)
        return moved, refused, _cards(app)

    moved, refused, undone = drive(go)
    assert moved == ["prj_b", "prj_a"]
    assert refused == moved and len(refusals) == 1
    assert undone == ["prj_a", "prj_b"], "the entry the refused undo kept was applied"


def test_a_group_named_like_markup_is_listed_as_typed_and_can_be_picked(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """A group name is the operator's text, never Textual markup. The picker handed
    ``📁 client [/api]`` to its OptionList as a markup string, so ``g`` raised MarkupError
    and took the TUI down for as long as such a group existed (review of #203)."""
    seed(tmp_path, ("prj_a", "api", None), ("prj_b", "cli", None))
    name = "client [/api] [bold]"
    with store_session() as store:
        groups_service.create_group(store, name, ["prj_b"])

    async def go(pilot: Pilot[None]) -> tuple[list[str], str, list[str]]:
        app = fleet_app(pilot)
        app.sidebar.focus()
        app.sidebar.select("project:prj_a")
        await pilot.press("g")
        await settle(app)
        assert isinstance(app.screen, GroupPicker), type(app.screen).__name__
        picker = app.screen.query_one("#grouplist", OptionList)
        listed = [
            str(picker.get_option_at_index(index).prompt) for index in range(picker.option_count)
        ]
        await pilot.press("enter")  # the group, first in the list
        await settle(app)
        return listed, shown(app.sidebar.query_one(GroupHeader)), _cards(app)

    listed, header, grouped = drive(go)
    assert listed[0] == f"📁 {name}", listed
    assert name in header
    assert grouped == [f"group:{name}", "prj_b", "prj_a"]


def test_a_click_on_a_group_header_folds_it_and_a_drag_back_onto_itself_opens_nothing(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """The header is a drag handle AND the fold. A drag that returns to the title it began
    on and is released there is a drag, not a click; the capture is let go either way."""
    seed(tmp_path, ("prj_a", "api", None), ("prj_b", "cli", None))
    with store_session() as store:
        groups_service.create_group(store, "tools", ["prj_b"])

    async def go(pilot: Pilot[None]) -> tuple[list[str], list[str], str | None, bool]:
        app = fleet_app(pilot)
        await _click(pilot, app.sidebar.query_one(GroupHeader))
        folded = _cards(app)
        await _click(pilot, app.sidebar.query_one(GroupHeader))
        unfolded = _cards(app)
        title = card_for(app, "prj_a").query_one(ProjectTitle)
        await _as_the_terminal_sends(pilot, events.MouseDown, title)
        await _as_the_terminal_sends(pilot, events.MouseMove, app.sidebar.query_one(GroupHeader))
        await _as_the_terminal_sends(pilot, events.MouseMove, title)
        await _as_the_terminal_sends(pilot, events.MouseUp, title)
        await pilot.pause()
        view = app.current_view()
        return folded, unfolded, view.id if view is not None else None, app.mouse_captured is None

    folded, unfolded, opened, released = drive(go)
    assert folded == ["group:tools", "prj_a"]
    assert unfolded == ["group:tools", "prj_b", "prj_a"]
    assert opened == "welcome", "the release of a drag is not an open"
    assert released


def test_a_drag_released_where_nothing_is_a_place_moves_nothing(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """Only a card, a group header and the list's own empty space are places. Read as
    "below the list", a drag abandoned over the main pane ungrouped its project and
    moved it last; one onto a pinned card took a pinned member out of its group; a
    group dropped on a card went to the end (review of #171, round 1)."""
    seed(
        tmp_path,
        ("prj_a", "api", None),
        ("prj_b", "cli", None),
        ("prj_c", "docs", None),
        ("prj_d", "web", None),
    )
    with store_session() as store:
        groups_service.create_group(store, "tools", ["prj_b"])
        groups_service.create_group(store, "site", ["prj_d"])
        groups_service.pin(store, "prj_c")

    async def go(pilot: Pilot[None]) -> tuple[list[list[str]], int, str | None]:
        app = fleet_app(pilot)
        seen = [_cards(app)]

        def title(project_id: str) -> ProjectTitle:
            return card_for(app, project_id).query_one(ProjectTitle)

        def header(name: str) -> GroupHeader:
            return next(h for h in app.sidebar.query(GroupHeader) if h.group.name == name)

        await _drag_onto(pilot, title("prj_b"), app.content, offset=(10, 12))  # over the main pane
        seen.append(_cards(app))
        await _drag_onto(pilot, title("prj_b"), card_for(app, "prj_c"))  # onto a pinned card
        seen.append(_cards(app))
        await _drag_onto(pilot, header("tools"), card_for(app, "prj_a"))  # a group onto a card
        seen.append(_cards(app))
        abandoned = len(app._undo)
        # The list's own empty space below the last row is still the end of the top level.
        holder = app.sidebar.query_one("#projects", VerticalScroll)
        await _drag_onto(pilot, title("prj_b"), holder, offset=(2, holder.region.height - 1))
        seen.append(_cards(app))
        with store_session() as store:
            moved = store.get_project("prj_b")
        return seen, abandoned, moved.group_id if moved is not None else "gone"

    seen, abandoned, group_id = drive(go)
    arranged = ["pinned", "prj_c", "group:tools", "prj_b", "group:site", "prj_d", "prj_a"]
    assert seen[:4] == [arranged] * 4, seen
    assert abandoned == 0, "an abandoned drag is no gesture to undo"
    assert seen[4] == ["pinned", "prj_c", "group:tools", "group:site", "prj_d", "prj_a", "prj_b"]
    assert group_id is None


def test_a_pinned_row_is_not_dragged_as_it_is_not_stepped(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """A pinned row's place is the pin order, which is ``p``'s: shift+↓ on it is "nothing
    to move", and a drag from it snaps back. Dragged, a pinned member dropped on the empty
    space or on a loose card left its group while its card stayed under Pinned — nothing on
    screen changed, and ``u`` had a step to undo (review of #171, round 1). A selection
    drags only its unpinned cards; a click on a pinned title still opens it."""
    seed(
        tmp_path,
        ("prj_a", "api", None),
        ("prj_b", "cli", None),
        ("prj_c", "docs", None),
        ("prj_d", "web", None),
    )
    with store_session() as store:
        tools, _ = groups_service.create_group(store, "tools", ["prj_b"])
        site, _ = groups_service.create_group(store, "site", ["prj_c"])
        groups_service.pin(store, "prj_b")  # a pinned member of tools
        groups_service.pin_group(store, site.id)

    async def go(
        pilot: Pilot[None],
    ) -> tuple[list[list[str]], bool, int, str | None, str | None]:
        app = fleet_app(pilot)
        seen = [_cards(app)]

        def title(project_id: str) -> ProjectTitle:
            return card_for(app, project_id).query_one(ProjectTitle)

        def header(name: str) -> GroupHeader:
            return next(h for h in app.sidebar.query(GroupHeader) if h.group.name == name)

        holder = app.sidebar.query_one("#projects", VerticalScroll)
        empty = (2, holder.region.height - 1)
        await _drag_onto(pilot, title("prj_b"), holder, offset=empty)  # onto the empty space
        seen.append(_cards(app))
        # Onto a loose card, looked at on the way: nothing lifts off, nothing is a place.
        api = card_for(app, "prj_a")
        await _as_the_terminal_sends(pilot, events.MouseDown, title("prj_b"))
        await _as_the_terminal_sends(pilot, events.MouseMove, api)
        lifted = card_for(app, "prj_b").has_class("-dragging") or api.has_class("-drop-before")
        await _as_the_terminal_sends(pilot, events.MouseUp, api)
        seen.append(_cards(app))
        await _drag_onto(pilot, header("site"), header("tools"), offset=(3, 0))  # a pinned group
        seen.append(_cards(app))
        depth = len(app._undo)
        # A selection of a loose and a pinned card, dragged by the loose one: it moves alone.
        await _click(pilot, title("prj_a"), shift=True)
        await _click(pilot, title("prj_b"), shift=True)
        await _drag_onto(pilot, title("prj_a"), holder, offset=empty)
        seen.append(_cards(app))
        await _click(pilot, title("prj_b"))
        view = app.current_view()
        with store_session() as store:
            cli = store.get_project("prj_b")
        group_id = cli.group_id if cli is not None else None
        return seen, lifted, depth, group_id, view.id if view is not None else None

    seen, lifted, depth, group_id, opened = drive(go)
    arranged = ["pinned", "prj_b", "group:site", "prj_c", "group:tools", "prj_a", "prj_d"]
    assert seen[:4] == [arranged] * 4, seen
    assert not lifted, "a press on a pinned card is no drag"
    assert depth == 0, "a drag from a pinned row is no gesture to undo"
    assert seen[4] == ["pinned", "prj_b", "group:site", "prj_c", "group:tools", "prj_d", "prj_a"]
    assert group_id == tools.id, "the pinned member is still the group's"
    assert opened == "project-prj_b", "a click on a pinned title opens it"


def test_a_drag_dims_the_card_it_moves_and_marks_only_a_place(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """The whole card dims while it moves, not its title line (which no rule styled). Its
    own card is no place — a release there snaps back — so it gets no drop mark, and
    neither does a group's own header under its drag; and a group header marked as the
    place keeps its name on screen: the accent line on a one-row header took the row,
    and the name went blank under the pointer."""
    seed(tmp_path, ("prj_a", "api", None), ("prj_b", "cli", None), ("prj_c", "docs", None))
    with store_session() as store:
        groups_service.create_group(store, "tools", ["prj_b"])

    async def go(pilot: Pilot[None]) -> tuple[list[bool], int, str, list[str]]:
        app = fleet_app(pilot)
        docs = card_for(app, "prj_c")
        header = app.sidebar.query_one(GroupHeader)
        await _as_the_terminal_sends(pilot, events.MouseDown, docs.query_one(ProjectTitle))
        await _as_the_terminal_sends(pilot, events.MouseMove, docs, (1, 1))  # its own spawn row
        marks = [docs.has_class("-dragging"), docs.has_class("-drop-before")]
        await _as_the_terminal_sends(pilot, events.MouseMove, header, (3, 0))
        await pilot.pause()
        marks.append(header.has_class("-drop-before"))
        rows = header.content_region.height
        box = Region(0, 0, header.outer_size.width, header.outer_size.height)
        name = "".join(strip.text for strip in header.render_lines(box))
        await _as_the_terminal_sends(pilot, events.MouseUp, header, (3, 0))
        await pilot.pause()
        marks.append(docs.has_class("-dragging"))
        # The group, off its header and back onto it.
        await _as_the_terminal_sends(pilot, events.MouseDown, header, (3, 0))
        await _as_the_terminal_sends(pilot, events.MouseMove, card_for(app, "prj_b"))
        await _as_the_terminal_sends(pilot, events.MouseMove, header, (3, 0))
        marks += [header.has_class("-dragging"), header.has_class("-drop-before")]
        await _as_the_terminal_sends(pilot, events.MouseUp, header, (3, 0))
        await pilot.pause()
        return marks, rows, name, _cards(app)

    marks, rows, name, cards = drive(go)
    dimmed, own_mark, header_marked, still_dimmed, group_dimmed, own_header_mark = marks
    assert dimmed, "the card it moves dims"
    assert not own_mark, "a card is no place to drop itself"
    assert header_marked and rows == 1, (header_marked, rows)
    assert "📁 tools" in name, name
    assert not still_dimmed
    assert group_dimmed and not own_header_mark, "a header is no place to drop its group"
    assert cards == ["group:tools", "prj_b", "prj_c", "prj_a"], "dropped on the header: last"


def test_another_button_let_go_mid_drag_does_not_end_it(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """The drag is button 1's. A right button pressed and let go while it is held neither
    drops the card there nor counts as a click on it; the left button's release does.
    SelectionHost's one-gesture rule drops the right button's press and release at the
    app, before the handle sees either, so this pins the whole path, not the handle's
    own check."""
    seed(tmp_path, ("prj_a", "api", None), ("prj_b", "cli", None), ("prj_c", "docs", None))
    with store_session() as store:
        groups_service.create_group(store, "tools", ["prj_b"])

    async def go(pilot: Pilot[None]) -> tuple[list[str], list[str], str | None, list[str]]:
        app = fleet_app(pilot)
        before = _cards(app)
        header = app.sidebar.query_one(GroupHeader)
        await _as_the_terminal_sends(
            pilot, events.MouseDown, card_for(app, "prj_c").query_one(ProjectTitle)
        )
        await _as_the_terminal_sends(pilot, events.MouseMove, header, (3, 0))
        await _as_the_terminal_sends(pilot, events.MouseDown, header, (3, 0), button=3)
        await _as_the_terminal_sends(pilot, events.MouseUp, header, (3, 0), button=3)
        await pilot.pause()
        midway = _cards(app)
        view = app.current_view()
        await _as_the_terminal_sends(pilot, events.MouseUp, header, (3, 0))
        await pilot.pause()
        return before, midway, view.id if view is not None else None, _cards(app)

    before, midway, opened, after = drive(go)
    assert midway == before == ["group:tools", "prj_b", "prj_a", "prj_c"], midway
    assert opened == "welcome", "the right button's click is no open"
    assert after == ["group:tools", "prj_b", "prj_c", "prj_a"], "the left release drops"


def test_a_drag_whose_release_was_lost_ends_at_the_first_move_with_no_button_held(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """Let go outside the terminal, the release never arrives; the first move reported with
    no button held says so. The drag ends there and snaps back — the release was nowhere a
    place is — and the handle lets the mouse go. Left held and armed, the card stayed dimmed,
    the card under the pointer kept its drop mark, and the next click anywhere went to the
    dragged title: a click on api opened docs (review of #171, round 1)."""
    seed(tmp_path, ("prj_a", "api", None), ("prj_b", "cli", None), ("prj_c", "docs", None))

    async def go(
        pilot: Pilot[None],
    ) -> tuple[list[str], bool, list[bool], object, bool, int, list[str], str | None]:
        app = fleet_app(pilot)
        before = _cards(app)
        docs = card_for(app, "prj_c")
        api = card_for(app, "prj_a")
        await _as_the_terminal_sends(pilot, events.MouseDown, docs.query_one(ProjectTitle))
        await _as_the_terminal_sends(pilot, events.MouseMove, api)
        running = docs.has_class("-dragging") and api.has_class("-drop-before")
        await _as_the_terminal_sends(pilot, events.MouseMove, api, button=0)
        await pilot.pause()
        marks = [docs.has_class("-dragging"), api.has_class("-drop-before")]
        captured = app.mouse_captured
        closed = app.sidebar._drag is None
        depth = len(app._undo)
        after = _cards(app)
        await _click(pilot, api.query_one(ProjectTitle))
        view = app.current_view()
        return before, running, marks, captured, closed, depth, after, view.id if view else None

    before, running, marks, captured, closed, depth, after, opened = drive(go)
    assert running, "the drag was over api's card when its release was lost"
    assert marks == [False, False], "nothing dimmed, nothing marked"
    assert captured is None and closed, "the handle let the mouse go and the drag is closed"
    assert after == before and depth == 0, "a lost release drops nowhere"
    assert opened == "project-prj_a", "the next click is the click it was"


def test_a_drag_whose_release_was_lost_ends_at_the_next_press_where_no_motion_reports_it(
    tmp_path: Path, script: Script, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal that reports no motion without a button never sends the move that ends a
    lost release. The next report is the next press: the same button, long past
    DUPLICATE_PRESS_WINDOW, which SelectionHost takes as a new gesture. The handle still
    held the mouse, so that press came to it and armed a drag again, and the Click of its
    release came to it too: a click on api opened docs (review of #171, round 2). The press
    ends the old drag instead — nothing dimmed or marked, nothing moved, nothing to undo,
    the mouse let go — and the click is api's."""
    seed(tmp_path, ("prj_a", "api", None), ("prj_b", "cli", None), ("prj_c", "docs", None))
    now = {"t": 100.0}
    monkeypatch.setattr("aisquare.cli.ui.terminal._monotonic", lambda: now["t"])

    async def go(
        pilot: Pilot[None],
    ) -> tuple[list[str], bool, list[bool], object, bool, int, list[str], str | None]:
        app = fleet_app(pilot)
        before = _cards(app)
        docs = card_for(app, "prj_c")
        api = card_for(app, "prj_a")
        await _as_the_terminal_sends(pilot, events.MouseDown, docs.query_one(ProjectTitle))
        await _as_the_terminal_sends(pilot, events.MouseMove, api)
        running = docs.has_class("-dragging") and api.has_class("-drop-before")
        now["t"] += DUPLICATE_PRESS_WINDOW + 1.0  # let go outside; nothing reported it
        await _click(pilot, api.query_one(ProjectTitle))
        marks = [docs.has_class("-dragging"), api.has_class("-drop-before")]
        captured = app.mouse_captured
        closed = app.sidebar._drag is None
        view = app.current_view()
        return (
            before,
            running,
            marks,
            captured,
            closed,
            len(app._undo),
            _cards(app),
            view.id if view else None,
        )

    before, running, marks, captured, closed, depth, after, opened = drive(go)
    assert running, "the drag was over api's card when its release was lost"
    assert marks == [False, False], "nothing dimmed, nothing marked"
    assert captured is None and closed, "the handle let the mouse go and the drag is closed"
    assert after == before and depth == 0, "a lost release drops nowhere"
    assert opened == "project-prj_a", "the click is the click it was"


def test_a_step_with_nowhere_to_go_leaves_nothing_to_undo(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """shift+↑ on the first row, or on a pinned one, moves nothing; it must not take the
    place of the last real gesture on the undo stack either."""
    seed(tmp_path, ("prj_a", "api", None), ("prj_b", "cli", None), ("prj_c", "docs", None))
    with store_session() as store:
        groups_service.pin(store, "prj_c")

    async def go(pilot: Pilot[None]) -> tuple[list[str], int, list[str]]:
        app = fleet_app(pilot)
        app.sidebar.focus()
        app.sidebar.select("project:prj_b")
        await pilot.press("shift+up")  # a real step: cli above api
        await settle(app)
        stepped = _cards(app)
        await pilot.press("shift+up")  # already first
        app.sidebar.select("project:prj_c")
        await pilot.press("shift+down")  # pinned: no place in a scope
        await settle(app)
        depth = len(app._undo)
        await pilot.press("u")
        await settle(app)
        return stepped, depth, _cards(app)

    stepped, depth, undone = drive(go)
    assert stepped == ["pinned", "prj_c", "prj_b", "prj_a"]
    assert depth == 1
    assert undone == ["pinned", "prj_c", "prj_a", "prj_b"], "u undid the real step"


def test_a_handle_removed_mid_press_lets_the_mouse_go(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """The handle holds the mouse from the press; a refresh that removes it (the project
    forgotten from a shell) must not leave the capture on a widget that is gone —
    Textual would deliver every later mouse event nowhere. The drag it began closes
    with it: left open, the card under the pointer kept its drop mark until the next
    press on a handle (review of #171, round 1)."""
    seed(tmp_path, ("prj_a", "api", None), ("prj_b", "cli", None))

    async def go(pilot: Pilot[None]) -> tuple[bool, bool, object, bool, bool]:
        app = fleet_app(pilot)
        title = card_for(app, "prj_b").query_one(ProjectTitle)
        api = card_for(app, "prj_a")
        await _as_the_terminal_sends(pilot, events.MouseDown, title)
        held = app.mouse_captured is title
        await _as_the_terminal_sends(pilot, events.MouseMove, api)
        marked = api.has_class("-drop-before")
        with store_session() as store:
            store.forget_project("prj_b")
        app.refresh_data()
        await settle(app)
        await pilot.pause()
        await _as_the_terminal_sends(pilot, events.MouseUp, api)
        return (
            held,
            marked,
            app.mouse_captured,
            api.has_class("-drop-before"),
            app.sidebar._drag is None,
        )

    held, marked, captured, still_marked, closed = drive(go)
    assert held, "the press is held by the handle"
    assert marked, "the drag was running over api's card"
    assert captured is None
    assert not still_marked and closed, "the drag went with its handle"


# --- the project's Settings tab follows a rename made elsewhere -----------------------------


def test_the_settings_tab_follows_a_rename_made_on_the_card_or_in_a_shell(
    tmp_path: Path, script: Script
) -> None:
    """The tab kept the project it was built with: after a card rename it still showed the
    folder name, and Save with a blank field was refused as "already that" against its stale
    copy — so the nickname could not be cleared from there. Each frame now hands the view
    the stored row; the field follows unless the operator is typing in it; Save always writes.
    """
    from aisquare.cli.ui.sidebar import RenameProject
    from aisquare.cli.ui.views.settings import SettingsView
    from aisquare.core.store import store_session
    from aisquare.services import project as project_service

    projects = seed(tmp_path, ("prj_a", "alpha", None))

    def stored() -> str | None:
        with store_session() as store:
            row = store.get_project("prj_a")
        assert row is not None
        return row.nickname

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        await settle(app)
        await pilot.click(card_for(app, "prj_a").query_one(ProjectTitle))
        await settle(app)
        view = app.current_view()
        assert isinstance(view, ProjectView)
        view.active = "tab-settings"
        await settle(app)
        tab = view.query_one(SettingsView)
        field = tab.query_one("#nickname", Input)

        def seen() -> tuple[str, str]:
            return field.value, shown(tab.query_one("#settings-title", Static))

        out: dict[str, object] = {"opened": seen()}
        app.post_message(RenameProject("prj_a", "Card name"))  # what the card's field posts
        await settle(app)
        out["card"] = seen()
        project_service.set_nickname(projects[0], "Shell name")  # `aisquare project rename`
        app.refresh_data()  # the next tick
        await settle(app)
        out["shell"] = seen()
        field.value = "half typed"
        project_service.set_nickname(projects[0], "Elsewhere again")
        app.refresh_data()
        await settle(app)
        out["typing"] = seen()
        field.value = ""
        tab.query_one("#save-nickname", Button).press()
        await settle(app)
        out["cleared"] = (seen(), stored())
        return out

    out = drive(go)
    assert out["opened"] == ("", "project settings — alpha")
    assert out["card"] == ("Card name", "project settings — Card name")
    assert out["shell"] == ("Shell name", "project settings — Shell name")
    assert out["typing"] == ("half typed", "project settings — Elsewhere again"), (
        "what the operator is typing stays; the title says the stored name"
    )
    assert out["cleared"] == (("", "project settings — alpha"), None), (
        "a blank Save clears a nickname set elsewhere"
    )


# --- a paste no focused widget took goes to the one live pane (#288) ------------------------


class PasteRecordingPane(TerminalPane):
    """A live pane on the test's socket whose ``_forward`` records instead of sending."""

    def __init__(self, id: str = "paste-pane") -> None:
        super().__init__("%9", server=TmuxServer(PRIVATE_SOCKET, binary=sys.executable), id=id)
        self.forwarded: list[tuple[str, str | bytes]] = []

    def _forward(self, kind: Any, value: str | bytes) -> bool:
        self.forwarded.append((kind, value))
        return True


def test_a_paste_the_sidebar_got_reaches_the_one_live_pane_and_focuses_it(
    tmp_path: Path, script: Script
) -> None:
    """#288: a terminal hands a dropped file over as a paste, and Textual gives a paste to
    the focused widget alone. With the sidebar focused, a screenshot dropped on `asq`
    reached no agent; it goes to the pane on screen now, which takes the keyboard."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> tuple[list[tuple[str, str | bytes]], bool, bool]:
        app = fleet_app(pilot)
        pane = PasteRecordingPane()
        await app.content.add_content(pane, set_current=True)
        app.sidebar.focus()
        await pilot.pause()
        was_sidebar = app.focused is app.sidebar
        app.post_message(events.Paste("/tmp/shot.png"))
        await settle(app)
        return pane.forwarded, was_sidebar, app.focused is pane

    forwarded, was_sidebar, focused = drive(go)
    assert was_sidebar, "the premise: the sidebar had the keyboard"
    assert forwarded == [("paste", "/tmp/shot.png")]
    assert focused, "the pane took the keyboard with it"


def test_a_paste_with_no_live_pane_on_screen_is_one_toast_and_no_tmux_call(
    tmp_path: Path, script: Script, no_real_tmux: list[tuple[str, ...]]
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    toasts: list[str] = []

    async def go(pilot: Pilot[None]) -> list[tuple[str, ...]]:
        app = fleet_app(pilot)
        real_notify = app.notify

        def notify(message: str, **kwargs: Any) -> None:
            toasts.append(message)
            real_notify(message, **kwargs)

        app.notify = notify  # type: ignore[method-assign]
        app.sidebar.focus()
        await pilot.pause()
        before = list(no_real_tmux)
        app.post_message(events.Paste("/tmp/shot.png"))
        await settle(app)
        return no_real_tmux[len(before) :]

    tmux_calls = drive(go)
    assert toasts == ["nothing to paste into — open an agent first"]
    assert not any("paste" in " ".join(argv) or "buffer" in " ".join(argv) for argv in tmux_calls)


def test_a_paste_with_two_live_panes_on_screen_says_to_pick_one(
    tmp_path: Path, script: Script
) -> None:
    """With two live panes on screen the router does not guess, and the toast said "nothing to
    paste into" (#340's review): it says what to do instead."""
    seed(tmp_path, ("prj_a", "alpha", None))
    toasts: list[str] = []

    async def go(pilot: Pilot[None]) -> list[tuple[str, str | bytes]]:
        app = fleet_app(pilot)
        real_notify = app.notify

        def notify(message: str, **kwargs: Any) -> None:
            toasts.append(message)
            real_notify(message, **kwargs)

        app.notify = notify  # type: ignore[method-assign]
        first = PasteRecordingPane()
        await app.content.add_content(first, set_current=True)
        second = PasteRecordingPane(id="paste-pane-2")
        await app.screen.mount(second)
        app.sidebar.focus()
        await pilot.pause()
        app.post_message(events.Paste("/tmp/shot.png"))
        await settle(app)
        return [*first.forwarded, *second.forwarded]

    forwarded = drive(go)
    assert forwarded == []
    pastes = [toast for toast in toasts if "paste" in toast]  # not the fake panes' own
    assert pastes == ["2 panes on screen — click the one to paste into, then paste again"]


def test_a_windows_path_pasted_under_wsl_is_converted_off_the_ui_thread(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`wslpath` may take its whole 2 s timeout, and the paste ran it on the UI thread, so
    a slow one froze asq (#340's review). It runs in a worker now, and the paste reaches
    the agent, converted, when it answers."""
    import subprocess
    from types import SimpleNamespace

    seed(tmp_path, ("prj_a", "alpha", None))
    shot = tmp_path / "c" / "shot.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"png")
    on_ui_thread: list[bool] = []

    run = subprocess.run  # patched on the module, so every other caller gets the real one

    def wslpath(argv: list[str], **kwargs: Any) -> Any:
        if argv[:1] != ["wslpath"]:
            return run(argv, **kwargs)
        on_ui_thread.append(threading.current_thread() is threading.main_thread())
        return SimpleNamespace(returncode=0, stdout=f"{shot}\n", stderr="")

    monkeypatch.setattr("aisquare.cli.ui.terminal.subprocess.run", wslpath)
    monkeypatch.setattr("aisquare.cli.ui.terminal.in_wsl", lambda environ: True)

    async def go(pilot: Pilot[None]) -> list[tuple[str, str | bytes]]:
        app = fleet_app(pilot)
        pane = PasteRecordingPane()
        await app.content.add_content(pane, set_current=True)
        pane.focus()
        await pilot.pause()
        app.post_message(events.Paste("C:\\shot.png"))
        await settle(app)
        app.post_message(events.Paste("/tmp/plain.png"))  # no worker for a path as sent
        await settle(app)
        return pane.forwarded

    forwarded = drive(go)
    assert on_ui_thread == [False], "asked once, off the UI thread"
    assert forwarded == [("paste", str(shot)), ("paste", "/tmp/plain.png")]


def test_a_paste_into_a_dialogs_input_never_reaches_a_pane_and_nothing_passes_a_dialog(
    tmp_path: Path, script: Script
) -> None:
    """The Spawn dialog's label keeps its own paste; and with the dialog open, a paste its
    focused widget did not take is not handed to the pane behind it."""
    from textual.widgets import Button, Input

    from aisquare.cli.ui.spawn import SpawnDialog

    [project] = seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> tuple[str, list[tuple[str, str | bytes]]]:
        app = fleet_app(pilot)
        pane = PasteRecordingPane()
        await app.content.add_content(pane, set_current=True)
        app.push_screen(SpawnDialog(project, accounts=None))
        await settle(app)
        label = app.screen.query_one("#spawn-label", Input)
        label.value = ""
        label.focus()
        await pilot.pause()
        app.post_message(events.Paste("coder-pasted"))
        await settle(app)
        app.screen.query_one("#spawn-cancel", Button).focus()
        await pilot.pause()
        app.post_message(events.Paste("/tmp/shot.png"))
        await settle(app)
        return label.value, pane.forwarded

    value, forwarded = drive(go)
    assert value == "coder-pasted"
    assert forwarded == []


def test_a_paste_a_text_area_took_is_not_sent_to_the_pane_as_well(
    tmp_path: Path, script: Script
) -> None:
    """Textual's ``TextArea`` inserts a paste without stopping it, so it bubbles on to the
    shell: a text area on the main screen must not have its paste typed into the agent too."""
    from textual.widgets import TextArea

    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> tuple[str, list[tuple[str, str | bytes]]]:
        app = fleet_app(pilot)
        pane = PasteRecordingPane()
        await app.content.add_content(pane, set_current=True)
        area = TextArea(id="paste-area")
        await app.screen.mount(area)
        area.focus()
        await pilot.pause()
        app.post_message(events.Paste("a note"))
        await settle(app)
        return area.text, pane.forwarded

    text, forwarded = drive(go)
    assert text == "a note"
    assert forwarded == []


def test_the_paste_log_records_each_paste_where_it_lands_only_when_asked(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#288's reproduction matrix is filled from what a terminal sends on a drop: with
    `AISQUARE_PASTE_LOG` naming a file, each paste adds its length, its head and the
    widget that had the keyboard. Without it, nothing is written."""
    seed(tmp_path, ("prj_a", "alpha", None))
    log = tmp_path / "paste.log"

    async def go(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        pane = PasteRecordingPane()
        await app.content.add_content(pane, set_current=True)
        app.sidebar.focus()
        await pilot.pause()
        app.post_message(events.Paste("/tmp/shot.png"))  # the router's: the sidebar had it
        await settle(app)
        app.post_message(events.Paste("again"))  # the pane's own: it has the keyboard now
        await settle(app)

    drive(go)
    unasked = log.exists()
    monkeypatch.setenv("AISQUARE_PASTE_LOG", str(log))
    drive(go)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert not unasked, "off by default: a paste may be a secret"
    assert len(lines) == 2, lines
    assert "focused=Sidebar" in lines[0] and "len=13 head='/tmp/shot.png'" in lines[0]
    assert "focused=PasteRecordingPane#paste-pane" in lines[1] and "len=5" in lines[1]
