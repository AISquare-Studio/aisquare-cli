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
import os
import shutil
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar

import pytest
from textual import Logger, events
from textual.app import App, ComposeResult, ScreenStackError
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.geometry import Region
from textual.pilot import Pilot
from textual.widgets import Button, Static, Switch
from textual.widgets._toast import Toast
from textual.worker import Worker, WorkerState

from aisquare.cli.ui import app as app_mod
from aisquare.cli.ui.app import SIDEBAR_WIDTH_KEY, FleetApp, HelpScreen, Panes
from aisquare.cli.ui.autosave import Autosave
from aisquare.cli.ui.divider import WIDEST_ASK, Divider, cells
from aisquare.cli.ui.sidebar import (
    RESIZE_STEP,
    Activatable,
    AgentRow,
    Disclosure,
    DoctorSection,
    DoctorTitle,
    ProjectCard,
    ProjectTitle,
    ordered_agents,
    short_path,
)
from aisquare.cli.ui.terminal import (
    EscapeToSidebar,
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
from aisquare.core import tmux as tmux_core
from aisquare.core.state_file import update_state
from aisquare.core.store import ContextStore, store_session
from aisquare.core.tmux import Completed
from aisquare.models import CheckStatus, DoctorCheck, FleetAgent, FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service

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
            store.ensure_project(project)
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
    """A scripted agent; the id is derived so a test can address its row."""
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
    )
    return FleetAgentStatus(agent=agent, state=state)


def _socket_of(argv: Sequence[str]) -> str | None:
    """The ``-L <socket>`` a tmux argv addresses, or ``None`` when it names none."""
    args = list(argv)
    return args[args.index("-L") + 1] if "-L" in args else None


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
    wrong = [argv for argv in ran if _socket_of(argv) != PRIVATE_SOCKET]
    assert not wrong, f"a UI test addressed a tmux socket that is not the test's: {wrong[:2]}"


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> Script:
    """What ``fleet_service.list_agents`` answers, per project id — mutable mid-test."""
    agents: Script = {}

    def fake(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        return list(agents.get(project.id, []))

    monkeypatch.setattr(fleet_service, "list_agents", fake)
    return agents


def drive(
    fn: Callable[[Pilot[None]], Awaitable[T]],
    *,
    doctor: Callable[[], list[DoctorCheck]] | None = None,
    notifications: bool = False,
    size: tuple[int, int] = SIZE,
) -> T:
    """Run ``fn`` against a mounted ``FleetApp`` (no timer refresh; a stub doctor).

    ``notifications`` opts the screen's ``ToastRack`` in — ``run_test`` leaves it
    out by default, and without it a ``notify`` goes nowhere to be read.
    ``size`` is the terminal's; a test about a laptop passes a narrow one.
    """

    async def run() -> T:
        app = FleetApp(refresh_seconds=3600, doctor=doctor or (lambda: []))
        async with app.run_test(size=size, notifications=notifications) as pilot:
            await pilot.pause()
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
    """Wait for the app's own workers — not Textual's ``_loader``.

    ``DirectoryTree`` (the Onboard view) keeps a ``_loader`` worker running for
    its whole life, so ``workers.wait_for_complete()`` would never return once
    that view exists. The doctor worker and any fix/spawn worker are what a test
    actually waits for.
    """
    ours = [worker for worker in app.workers if worker.group != "_loader"]
    if ours:
        await app.workers.wait_for_complete(ours)


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
        await pilot.pause()

    drive(go)

    assert no_real_tmux, "no tmux call recorded — the guard inspects nothing here"
    assert {_socket_of(argv) for argv in no_real_tmux} == {PRIVATE_SOCKET}


def test_the_no_tmux_guard_rejects_the_real_fleets_socket() -> None:
    """The negative half, on the rule itself — and it must still SEE a good argv."""
    assert _socket_of(("tmux", "-L", "asq", "capture-pane")) != PRIVATE_SOCKET
    assert _socket_of(("tmux", "-L", PRIVATE_SOCKET, "capture-pane")) == PRIVATE_SOCKET
    assert _socket_of(("tmux", "-V")) is None  # an argv naming no socket is not the test's
    assert FleetAgent.model_fields["tmux_socket"].default == "asq" != PRIVATE_SOCKET


def card_for(app: FleetApp, project_id: str) -> ProjectCard:
    return app.query_one(f"#card-{project_id}", ProjectCard)


def row_for(app: FleetApp, agent_id: str) -> AgentRow:
    return app.query_one(f"#agent-row-{agent_id}", AgentRow)


# --- pure helpers ------------------------------------------------------------------


def test_short_path_collapses_the_home_directory_only() -> None:
    home = Path("/home/me")
    assert short_path(home / "work" / "api", home) == "~/work/api"
    assert short_path(Path("/srv/api"), home) == "/srv/api"  # not under home: untouched


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
        return rows, shown(card_for(app, "prj_a").query_one(ProjectTitle))

    rows, title = drive(go)
    assert [r.split()[1] for r in rows] == ["manager", "coder-auth", "tester-1", "scout-1"]
    manager, coder, tester, scout = rows
    assert manager.startswith("🧭") and manager.rstrip().endswith("⏸")
    assert coder.startswith("🔨") and coder.rstrip().endswith("▶")
    assert tester.startswith("🧪") and tester.rstrip().endswith("💤(3)")
    assert scout.startswith("🤖") and "🔔" in scout  # unknown role: the custom icon
    assert "(3)" not in coder and "💤" not in coder  # exit status only on the exited row
    assert "🔔" not in manager and "▶" not in manager
    # The card's chips: three alive (the exited one is not), one needing the user.
    assert title.rstrip().endswith("3 · 🔔1")


def test_codename_badge_and_duplicate_basename_subtitle(tmp_path: Path, script: Script) -> None:
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
    assert "amber-otter" in cards["prj_w"][0] and "ruby-fox" in cards["prj_o"][0]
    assert "amber-otter" not in cards["prj_o"][0]  # each card its own badge
    assert "-" not in cards["prj_u"][0].replace("🗂 unique", "")  # no badge without a codename
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

    async def go(pilot: Pilot[None]) -> tuple[str, str, str, bool, str]:
        app = fleet_app(pilot)
        title = card_for(app, "prj_l").query_one(ProjectTitle)
        await pilot.click(title)  # the selected, highlighted row the report was about
        await pilot.pause()
        return (
            shown(title),
            composited(title),
            composited(card_for(app, "prj_s").query_one(ProjectTitle)),
            title.has_class("selected"),
            composited(row_for(app, "agt_l_manager")),
        )

    held, long_row, short_row, selected, manager_row = drive(go)
    assert selected
    assert held.startswith("🗂 AISquare-Explainability-SDK  cosmic-narwhal")  # the data, intact
    assert long_row.startswith("🗂 AISquare-Explainabilit")  # the NAME reaches the row
    assert long_row.rstrip().endswith("…")  # and the cut is declared, not silent
    # Controls: a name that fits is shown whole and uncut; the agent row still renders.
    assert short_row.rstrip() == "🗂 api  amber-otter"
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
        await pilot.pause()
        shown_ids = [app.content.current or ""]
        view = app.current_view()
        assert isinstance(view, ProjectView) and view.project.id == "prj_b"
        await pilot.click(card_for(app, "prj_a").query_one(ProjectTitle))
        await pilot.pause()
        shown_ids.append(app.content.current or "")
        await pilot.click(card_for(app, "prj_b").query_one(ProjectTitle))  # back again
        await pilot.pause()
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


class PaneScript:
    """A tmux runner that answers a pane's frames, so a real ``AgentView`` in a
    real ``FleetApp`` shows real rows.

    ``no_real_tmux`` stubs every command into a failure, which is right for
    tests about routing and wrong for one about SELECTING text — there is
    nothing on screen to select. This answers the one call the pane makes per
    frame (``capture-pane`` + ``display-message`` in a single process) and reads
    the pane's size back out of the ``resize-window`` that precedes it, exactly
    as the real server would.
    """

    def __init__(self, ran: list[tuple[str, ...]], rows: list[str]) -> None:
        self.ran = ran
        self.rows = rows
        self.width, self.height = 40, len(rows)

    def __call__(self, argv: Sequence[str], stdin: bytes | None) -> Completed:
        args = list(argv)
        self.ran.append(tuple(args))
        if args[1:] == ["-V"]:
            return Completed(0, "tmux 3.5a\n", "")
        command = args[5:]
        if command and command[0] == "resize-window":
            self.width = int(command[command.index("-x") + 1])
            self.height = int(command[command.index("-y") + 1])
            return Completed(0, "", "")
        if command and command[0] == "capture-pane":
            body = [*self.rows, *([""] * (self.height - len(self.rows)))][: self.height]
            facts = tmux_core._SEP.join(
                ["%1", str(self.width), str(self.height), "0", "0", "1", "0", "0",
                 "0", "", "0", "bash", "0", "0", ""]
            )  # fmt: skip
            return Completed(0, "\n".join([*body, facts]) + "\n", "")
        return Completed(0, "", "")


async def _agent_pane(pilot: Pilot[None]) -> tuple[TerminalPane, Static]:
    """Open the scripted agent and wait until its pane has painted a frame."""
    app = fleet_app(pilot)
    await pilot.click(row_for(app, "agt_a_coder-auth"))
    await pilot.pause()
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


def test_a_drag_from_the_agent_header_into_the_pane_copies_through_the_app(
    tmp_path: Path,
    script: Script,
    no_real_tmux: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The app is what turns the end of a selection gesture into a copy.

    ``FleetApp.on_text_selected`` is the only thing that makes a drag crossing
    the pane's edge copy, and nothing exercised it: replacing its body with
    ``return`` left the whole suite green, because every test of that gesture
    re-implemented the handler on its own test ``Host`` (review of #120, round
    7). This drives the real app, the real ``AgentView``, and the real header.
    """
    seed(tmp_path, ("prj_a", "alpha", None))
    script["prj_a"] = [status("prj_a", "coder-auth", "coder", "working")]
    monkeypatch.setattr(
        tmux_core, "_tmux", PaneScript(no_real_tmux, ["red plain", "second row", "third row"])
    )

    async def go(pilot: Pilot[None]) -> tuple[str, int, str, int]:
        app = fleet_app(pilot)
        pane, header = await _agent_pane(pilot)
        await pilot.mouse_down(header, offset=(1, 0))
        await pilot.hover(pane, offset=(5, 1))
        await pilot.mouse_up(pane, offset=(5, 1))
        await pilot.pause()
        crossed, toasts = app.clipboard, len(app._notifications)
        # The negative half: a gesture that touches no row of the pane must
        # leave both the clipboard and the toast count exactly as they were.
        app.screen.clear_selection()
        await pilot.pause()
        await pilot.mouse_down(header, offset=(1, 0))
        await pilot.hover(header, offset=(6, 0))
        await pilot.mouse_up(header, offset=(6, 0))
        await pilot.pause()
        return crossed, toasts, app.clipboard, len(app._notifications)

    crossed, toasts, after, toasts_after = drive(go, notifications=True)
    assert crossed == "red plain\nsecon", crossed
    assert toasts == 1, "one copy, one toast"
    assert after == crossed and toasts_after == toasts, (
        "a gesture over no pane row must not re-copy a standing selection"
    )


def test_a_right_button_drag_from_the_agent_header_copies_nothing_through_the_app(
    tmp_path: Path,
    script: Script,
    no_real_tmux: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``FleetApp.on_mouse_down`` is the whole of the button fix: a pane only
    sees a press that lands ON it, so without the app a right-button drag begun
    on the header reads as a left one and copies. Nothing reached that handler —
    replacing its body left the suite green (review of #120, round 8)."""
    seed(tmp_path, ("prj_a", "alpha", None))
    script["prj_a"] = [status("prj_a", "coder-auth", "coder", "working")]
    monkeypatch.setattr(
        tmux_core, "_tmux", PaneScript(no_real_tmux, ["red plain", "second row", "third row"])
    )

    async def go(pilot: Pilot[None]) -> tuple[str, int, bool]:
        app = fleet_app(pilot)
        pane, header = await _agent_pane(pilot)
        await pilot.mouse_down(header, offset=(1, 0), button=3)
        await pilot.hover(pane, offset=(5, 1))
        await pilot.mouse_up(pane, offset=(5, 1))
        await pilot.pause()
        return app.clipboard, len(app._notifications), pane.text_selection is not None

    clipboard, toasts, highlighted = drive(go, notifications=True)
    assert highlighted, "the premise: the gesture did select text in the pane"
    assert clipboard == "" and toasts == 0, "a right-button drag is not a copy request"


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
        tmux_core, "_tmux", PaneScript(no_real_tmux, ["red plain", "second row", "third row"])
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
        await pilot.pause()
        pane, header = await _agent_pane(pilot)  # opens coder-auth, leaves both mounted
        panes = list(app.screen.query(TerminalPane))
        assert len(panes) >= 2, "the app keeps a view per opened agent mounted"

        async def cross() -> None:
            await pilot.mouse_down(header, offset=(1, 0))
            await pilot.hover(pane, offset=(5, 1))
            await pilot.mouse_up(pane, offset=(5, 1))
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
        tmux_core, "_tmux", PaneScript(no_real_tmux, ["red plain", "second row", "third row"])
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
        await pilot.pause()
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
        await pilot.pause()
        return before, app.content.current

    assert drive(go) == ("welcome", "onboard")


def test_doctor_section_opens_the_doctor_view_and_paints_the_summary(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    calls: list[int] = []

    def doctor() -> list[DoctorCheck]:
        calls.append(1)
        return [
            DoctorCheck(name="python", status=CheckStatus.ok, detail="Python 3.14"),
            DoctorCheck(
                name="repomix",
                status=CheckStatus.warn,
                detail="repomix not found",
                fix="npm install -g repomix",
            ),
            DoctorCheck(
                name="home", status=CheckStatus.fail, detail="~/.aisquare is missing", fix="init"
            ),
        ]

    async def go(pilot: Pilot[None]) -> tuple[int, str | None, str, str, list[str], int]:
        app = fleet_app(pilot)
        await settle(app)
        await pilot.pause()
        calls_after_mount = len(calls)
        before = app.content.current
        await pilot.click(app.query_one(DoctorSection))
        await pilot.pause()
        await settle(app)
        await pilot.pause()
        report = shown(app.query_one("#doctor", DoctorView).query_one("#doctor-report", Static))
        summary = shown(app.query_one(DoctorTitle))
        lines = [
            shown(line)
            for line in app.query_one(DoctorSection).query(".doctor-line").results(Static)
            if line.display
        ]
        return calls_after_mount, before, report, summary, lines, len(calls)

    at_mount, before, report, summary, lines, total = drive(go, doctor=doctor)
    assert at_mount == 1  # the counts are painted at start-up…
    assert before == "welcome"
    assert total == 2  # …and the click runs the checks again
    assert "repomix not found" in report and "→ npm install -g repomix" in report
    assert "Python 3.14" in report
    assert summary.startswith("Doctor") and "✓ 1" in summary and "⚠ 1" in summary
    assert "✗ 1" in summary
    assert len(lines) == 2 and lines[0].startswith("✗ home") and lines[1].startswith("⚠ repomix")
    assert not any("python" in line for line in lines)  # only the findings, never the ✓ rows


def test_a_crashing_doctor_is_reported_not_raised(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))

    def broken() -> list[DoctorCheck]:
        raise RuntimeError("boom")

    async def go(pilot: Pilot[None]) -> tuple[bool, str, str, int | None]:
        app = fleet_app(pilot)
        await settle(app)
        await pilot.pause()
        notice = app.query_one(DoctorSection).query_one(".doctor-notice", Static)
        report = shown(app.query_one("#doctor", DoctorView).query_one("#doctor-report", Static))
        return notice.display, shown(notice), report, app.return_code

    displayed, notice, report, return_code = drive(go, doctor=broken)
    assert displayed and "doctor could not run" in notice and "boom" in notice
    assert "the checks crashed: RuntimeError: boom" in report
    assert return_code is None  # the app kept running

    async def healthy(pilot: Pilot[None]) -> bool:
        app = fleet_app(pilot)
        await settle(app)
        await pilot.pause()
        return app.query_one(DoctorSection).query_one(".doctor-notice", Static).display

    assert drive(healthy) is False  # a working doctor leaves no such notice


SNAPSHOT_WARN = DoctorCheck(
    name="snapshot",
    status=CheckStatus.warn,
    detail="no codebase snapshot",
    fix="Pack one: aisquare project onboard",
)
"""A finding whose fix is one of ours and is PROJECT-scoped — the button needs a cwd."""
CONNECT_WARN = DoctorCheck(
    name="claude-code",
    status=CheckStatus.warn,
    detail="hooks are missing",
    fix="(Re)connect it: aisquare agents connect claude-code",
)


def _fix_buttons(view: DoctorView) -> list[tuple[str, bool]]:
    return [(str(b.label), b.disabled) for b in view.query("#doctor-fixes Button").results(Button)]


def test_the_selected_projects_root_arms_its_project_scoped_fixes(
    tmp_path: Path, script: Script
) -> None:
    """§0 item 4: a project fix is one click. Without a cwd every one renders disabled.

    ``DoctorView`` disables ``scope == "project"`` fixes when ``cwd is None``,
    so the shell must hand it the scoped project's root — and the project's own
    Doctor tab the same report and the same root.
    """
    projects = seed(tmp_path, ("prj_a", "alpha", None))

    async def go(
        pilot: Pilot[None],
    ) -> tuple[list[tuple[str, bool]], Path | None, list[tuple[str, bool]], Path | None, str]:
        app = fleet_app(pilot)
        await settle(app)
        await pilot.pause()
        shell_doctor = app.query_one("#doctor", DoctorView)
        globally = (_fix_buttons(shell_doctor), shell_doctor.cwd)  # control: nothing selected yet
        await pilot.click(card_for(app, "prj_a").query_one(ProjectTitle))
        await pilot.pause()
        await settle(app)
        await pilot.pause()
        view = app.current_view()
        assert isinstance(view, ProjectView)
        tab = view.query_one(DoctorView)
        return (
            *globally,
            _fix_buttons(shell_doctor),
            shell_doctor.cwd,
            shown(tab.query_one("#doctor-report", Static)),
        )

    checks = [SNAPSHOT_WARN, CONNECT_WARN]
    global_buttons, global_cwd, scoped_buttons, scoped_cwd, tab_report = drive(
        go, doctor=lambda: list(checks)
    )
    assert global_cwd is None
    assert global_buttons == [
        ("aisquare project onboard --refresh", True),  # no project: correctly refused
        ("aisquare agents connect claude-code", False),
    ]
    assert scoped_cwd == projects[0].root
    assert scoped_buttons == [
        ("aisquare project onboard --refresh", False),  # a project IS selected: armed
        ("aisquare agents connect claude-code", False),
    ]
    assert "⚠ snapshot: no codebase snapshot" in tab_report  # the project's own tab is fed


def test_a_doctor_refreshed_message_updates_the_sidebar_counts(
    tmp_path: Path, script: Script
) -> None:
    """A one-click fix re-runs the doctor inside the view; the shell must follow it."""
    seed(tmp_path, ("prj_a", "alpha", None))
    calls: list[int] = []

    def doctor() -> list[DoctorCheck]:
        calls.append(1)
        return [SNAPSHOT_WARN]

    async def go(pilot: Pilot[None]) -> tuple[str, int, str, int, str, int]:
        app = fleet_app(pilot)
        await settle(app)
        await pilot.pause()
        summary = app.query_one(DoctorTitle)
        before = (shown(summary), len(calls))
        # The report the view was handed is about the scope on screen: adopt it.
        app.post_message(
            DoctorRefreshed(
                [
                    DoctorCheck(
                        name="snapshot", status=CheckStatus.ok, detail="packed 2 minutes ago"
                    )
                ],
                None,
            )
        )
        await pilot.pause()
        await settle(app)
        await pilot.pause()
        adopted = (shown(summary), len(calls))
        # A report about another root (the Onboard view's) says ours is stale.
        app.post_message(DoctorRefreshed([], tmp_path / "elsewhere"))
        await pilot.pause()
        await settle(app)
        await pilot.pause()
        return (*before, *adopted, shown(summary), len(calls))

    before, calls_before, adopted, calls_adopted, rerun, calls_rerun = drive(go, doctor=doctor)
    assert "⚠ 1" in before and "✓ 0" in before and calls_before == 1
    assert "✓ 1" in adopted and "⚠ 0" in adopted, "the fixed check is reflected in the counts"
    assert calls_adopted == 1, "the matching report was adopted — the checks did not run again"
    assert calls_rerun == 2, "a report about another root re-runs ours instead"
    assert "⚠ 1" in rerun  # …and shows what OUR doctor says, not the foreign report's ✓ 0


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

    ``show_doctor`` re-reads the scope at PAINT time and the worker filter was
    the name alone, so a SUCCESS queued across a selection change (reachable:
    ``on_project_selected`` awaits ``add_content`` before ``_set_doctor_scope``,
    and a worker past RUNNING is past cancelling) handed the old project's
    findings to the new project's ``DoctorView.cwd`` — a one-click project fix
    would then have run in the wrong root, the hazard ``show_doctor``'s own
    comment warns about. Replaying that queued message IS the race, without
    racing.
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
            await pilot.click(card_for(app, "prj_a").query_one(ProjectTitle))
            await settle(app)
            await pilot.pause()
            stale = app.doctor_runs[-1]  # the run whose scope is alpha
            await pilot.click(card_for(app, "prj_b").query_one(ProjectTitle))
            await settle(app)
            await pilot.pause()
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
    assert runs == ["run-1", "run-2", "run-3"]  # mount (global), alpha, beta
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
        await pilot.pause()
        await pilot.pause()
        return app.screen.query_one(Toast).render().plain

    rendered = drive(go, notifications=True)
    assert rendered == f"{path}: init failed: store_unopenable"
    assert "[archive]" in rendered
    # Control: the same string rendered AS MARKUP loses the bracketed segment —
    # the failure this assertion exists to catch, measured here.
    assert Content.from_markup(rendered).plain == rendered.replace("[archive]", "")


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
        await pilot.pause()
        await pilot.pause()
        return app.screen.query_one(Toast).render().plain

    rendered = drive(go, notifications=True)
    assert str(refused) in rendered and "[work]" in rendered
    assert rendered.startswith("could not write the config:")
    # Control: the same string parsed AS MARKUP loses the bracketed directory —
    # the failure this assertion exists to catch, measured here.
    assert "[work]" not in Content.from_markup(rendered).plain


def test_project_onboarded_refreshes_and_selects_the_project(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> tuple[int, int, str | None]:
        app = fleet_app(pilot)
        cards_before = len(app.query(ProjectCard))
        seed(tmp_path, ("prj_n", "newcomer", None))  # what the onboard worker's `init` did
        app.post_message(ProjectOnboarded("prj_n", tmp_path / "newcomer"))
        await pilot.pause()
        await pilot.pause()
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
        await pilot.pause()
        keys = list(pane.keys)
        alive = app.return_code
        screen_with_pane = type(app.screen).__name__
        # Positive control: the same keys are live once the sidebar has focus.
        app.sidebar.focus()
        await pilot.pause()
        await pilot.press("f1")
        await pilot.pause()
        screen_from_sidebar = type(app.screen).__name__
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("q")
        return keys, alive, screen_with_pane, screen_from_sidebar, app.return_code

    keys, alive, with_pane, from_sidebar, after_q = drive(go)
    assert keys == list(probes)  # every probe reached the pane, none was eaten
    assert alive is None  # …and q / ctrl+q did not quit
    assert with_pane == "Screen"  # no palette (f1), theme picker (t) or help (?) opened
    assert from_sidebar == "CommandPalette"
    assert after_q == 0


def test_the_app_keys_are_refused_while_focus_is_in_a_view(tmp_path: Path, script: Script) -> None:
    """A form widget is not a ``TerminalPane`` and does not consume a letter either.

    Of everything the views mount only ``Input`` implements
    ``check_consume_key``, so with a ``Button`` or a ``Switch`` focused ``q``
    reached the app's binding and exited the fleet UI — taking the unsaved form
    with it (measured against the running app: ``q`` with the Settings tab's
    permission-mode ``Select`` focused set ``_exit``). The sidebar is the
    control: there the very same keys still quit, theme and help.
    """
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> tuple[list[str], list[int | None], list[str], int | None]:
        app = fleet_app(pilot)
        button, switch = Button("save", id="probe-button"), Switch(id="probe-switch")
        await app.content.add_content(Vertical(button, switch, id="probe-form"), set_current=True)
        await pilot.pause()
        focused: list[str] = []
        codes: list[int | None] = []
        screens: list[str] = []
        for widget in (button, switch):
            widget.focus()
            await pilot.pause()
            focused.append(type(app.focused).__name__)
            await pilot.press("q", "t", "question_mark", "f1")
            await pilot.pause()
            codes.append(app.return_code)
            screens.append(type(app.screen).__name__)
        # The rule is "focus is IN the sidebar", not "focus IS the sidebar": a
        # focusable row would keep the keys live. The sidebar has none today, so
        # the probe is synthetic on purpose — it exercises the rule, not a widget.
        inside = Button("in the sidebar", id="probe-inside")
        await app.sidebar.mount(inside)
        inside.focus()
        await pilot.pause()
        focused.append(type(app.focused).__name__)
        await pilot.press("question_mark")
        await pilot.pause()
        screens.append(type(app.screen).__name__)
        await pilot.press("escape")
        await pilot.pause()
        # The control: the same keys from the sidebar itself do what they always did.
        app.sidebar.focus()
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        screens.append(type(app.screen).__name__)
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("q")
        return focused, codes, screens, app.return_code

    focused, codes, screens, quit_code = drive(go)
    # The claim first: a quit here also stops the rest of the probe, and the
    # reader should read "q quit from a form", not "the second probe lost focus".
    assert codes == [None, None], "q in a form must not quit the fleet UI"
    assert focused == ["Button", "Switch", "Button"]  # the probes really had focus
    assert screens == ["Screen", "Screen", "HelpScreen", "HelpScreen"]
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
        await pilot.pause()
        return before, type(app.focused).__name__

    assert drive(go) == ("RecordingPane", "Sidebar")


def test_help_opens_from_the_sidebar_and_closes(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> tuple[bool, bool, bool]:
        app = fleet_app(pilot)
        closed_before = not isinstance(app.screen, HelpScreen)
        await pilot.press("question_mark")
        await pilot.pause()
        opened = isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        await pilot.pause()
        return closed_before, opened, isinstance(app.screen, HelpScreen)

    assert drive(go) == (True, True, False)


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
        walk.append(type(app.query_one(".cursor")).__name__)
        await pilot.press("down")
        walk.append(type(app.query_one(".cursor")).__name__)
        await pilot.press("down")
        walk.append(type(app.query_one(".cursor")).__name__)
        await pilot.press("up")
        walk.append(type(app.query_one(".cursor")).__name__)
        await pilot.press("enter")
        await pilot.pause()
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
        await pilot.pause()
        focused = type(app.focused).__name__
        walk: list[str] = []
        scrolls: list[float] = []
        for _ in range(2):
            await pilot.press("down")
            await pilot.pause()
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
            await pilot.pause()
        walked = cursors()
        await pilot.click(card_for(app, "prj_a").query_one(Disclosure))  # hides the cursor's row
        await pilot.pause()
        collapsed = cursors()
        await pilot.press("down")
        await pilot.pause()
        moved = cursors()
        # Control: the walk goes on from there, one cursor at a time.
        await pilot.press("down")
        await pilot.pause()
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
                await pilot.pause()
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
        await pilot.pause()
        counts.append(len(rings))
        app.refresh_data()  # still attention: no second ring
        await pilot.pause()
        counts.append(len(rings))
        script["prj_a"] = [status("prj_a", "manager", "manager", "working")]
        app.refresh_data()
        await pilot.pause()
        counts.append(len(rings))
        script["prj_a"] = [status("prj_a", "manager", "manager", "attention")]
        app.refresh_data()  # a fresh transition rings again
        await pilot.pause()
        counts.append(len(rings))
        return counts

    assert drive(go) == [0, 1, 1, 1, 2]

    # Negative control: an agent that is ALREADY in attention on the first frame
    # is news the user opened the UI to see, not a transition — no bell.
    rings.clear()
    script["prj_a"] = [status("prj_a", "manager", "manager", "attention")]

    async def first_frame(pilot: Pilot[None]) -> int:
        fleet_app(pilot).refresh_data()
        await pilot.pause()
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
        await pilot.pause()
        row = row_for(app, "agt_a_coder-auth")
        assert row.has_class("selected")
        # Tick 1: the coder changes state; the manager leaves; a tester arrives.
        script["prj_a"] = [
            status("prj_a", "coder-auth", "coder", "waiting", minute=1),
            status("prj_a", "tester-1", "tester", "working", minute=2),
        ]
        app.refresh_data()
        await pilot.pause()
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
        await pilot.pause()
        assert app.snapshot is not None
        kept = [p.id for p in app.snapshot.projects]
        cards = [c.project.id for c in app.query(ProjectCard)]
        stale = app.snapshot.stale_since is not None and app.snapshot.taken_at == frame.taken_at
        text = shown(notice) if notice.display else ""
        monkeypatch.setattr(app_mod, "store_session", store_session)
        app.refresh_data()
        await pilot.pause()
        recovered = not notice.display and app.snapshot.stale_since is None
        return kept, cards, text, stale, quiet_before, recovered

    kept, cards, text, stale, quiet_before, recovered = drive(go)
    assert kept == ["prj_a", "prj_b"] and cards == kept  # the frame survived the outage
    assert stale
    assert quiet_before and "store unreadable" in text  # …and was labelled as such
    assert recovered


def test_agent_errors_fail_open_with_the_cost_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None))
    healthy = [status("prj_b", "manager", "manager", "waiting")]

    def fake(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        if project.id == "prj_a":
            raise fleet_service.FleetUnavailable("tmux 3.1 is too old (need 3.2)")
        return list(healthy)

    monkeypatch.setattr(fleet_service, "list_agents", fake)

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

    monkeypatch.setattr(fleet_service, "list_agents", fake)
    State = tuple[str | None, bool, bool, str]

    async def go(pilot: Pilot[None]) -> tuple[State, State, str, State]:
        app = fleet_app(pilot)
        await pilot.click(card_for(app, "prj_a").query_one(ProjectTitle))
        await pilot.pause()
        view = app.current_view()
        assert isinstance(view, ProjectView)
        tab = view.query_one(ManagerTab)
        header = tab.query_one("#manager-header", Static)
        pane = tab.query_one(TerminalPane)
        button = tab.query_one("#start-manager", Button)

        def state() -> State:
            return pane.pane_id, pane.display, button.display, shown(header)

        app.refresh_data()  # the first frame the freshly opened view is handed
        await pilot.pause()
        attached = state()
        failure.append(fleet_service.FleetUnavailable("tmux 3.1 is too old (need 3.2)"))
        app.refresh_data()
        await pilot.pause()
        failed_open = state()
        card_notice = shown(card_for(app, "prj_a").query_one(".card-notice", Static))
        # The control: the fleet answers, and its answer is that there is none.
        failure.clear()
        live.clear()
        app.refresh_data()
        await pilot.pause()
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
        await pilot.pause()
        view = app.current_view()
        assert isinstance(view, AgentView)
        opened_with = view.status.state
        # The agent flips to attention: the open view must see it on the next frame.
        script["prj_a"] = [
            status("prj_a", "manager", "manager", "waiting"),
            status("prj_a", "coder-auth", "coder", "attention", minute=1),
        ]
        app.refresh_data()
        await pilot.pause()
        after_change = view.status.state
        # Negative control: the manager's row changed too, but no view is open for it —
        # and the project's codename arrives, which the project view (once opened) shows.
        await pilot.click(card_for(app, "prj_a").query_one(ProjectTitle))
        await pilot.pause()
        project_view = app.current_view()
        assert isinstance(project_view, ProjectView)
        codename_before = project_view.project.codename
        with store_session() as store:
            store.set_codename("prj_a", "amber-otter")
        # The agent leaves the frame entirely: the view keeps its last row, nothing raises.
        script["prj_a"] = [status("prj_a", "manager", "manager", "waiting")]
        app.refresh_data()
        await pilot.pause()
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
        await pilot.pause()
        from_sidebar = shown(row)
        # The same key with a pane focused reaches the pane and refreshes nothing.
        pane = RecordingPane()
        await app.content.add_content(pane, set_current=True)
        pane.focus()
        await pilot.pause()
        script["prj_a"] = [status("prj_a", "manager", "manager", "attention")]
        await pilot.press("r")
        await pilot.pause()
        return first, from_sidebar, shown(row), list(pane.keys)

    first, from_sidebar, from_pane, keys = drive(go)
    assert first.rstrip().endswith("⏸")
    assert from_sidebar.rstrip().endswith("▶")  # r re-read the fleet
    assert from_pane.rstrip().endswith("▶") and "🔔" not in from_pane  # …and did not, here
    assert keys == ["r"]


# --- theme -------------------------------------------------------------------------


def test_theme_picker_applies_live_and_autosaves(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))

    async def browse(pilot: Pilot[None]) -> tuple[bool, str, str, str]:
        app = fleet_app(pilot)
        initial = str(app.theme)
        await pilot.press("t")
        await pilot.pause()
        assert isinstance(app.screen, ThemePicker)
        await pilot.press("down")  # browsing applies instantly…
        await pilot.press("down")
        await pilot.pause()
        still_open = isinstance(app.screen, ThemePicker)
        applied = str(app.theme)
        await pilot.press("escape")  # …until the explicit close
        await pilot.pause()
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


# --- the partition (#137) -----------------------------------------------------------------


def _state(isolated_home: Path) -> dict[str, object]:
    path = isolated_home / "state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _write_state(isolated_home: Path, data: object) -> Path:
    isolated_home.mkdir(parents=True, exist_ok=True)
    path = isolated_home / "state.json"
    path.write_text(json.dumps(data) + "\n")
    return path


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
    await pilot.pause()


async def _drag(pilot: Pilot[None], from_x: int, to_x: int, y: int = 5) -> None:
    await _mouse(pilot, events.MouseDown, from_x, y)
    await _mouse(pilot, events.MouseMove, to_x, y)
    await _mouse(pilot, events.MouseUp, to_x, y)


async def _settled(pilot: Pilot[None]) -> None:
    """Let a debounced save land: the debounce, then the worker it hands the write to — and
    stay well inside Textual's half-second click chain, which some tests need to span."""
    await pilot.pause(Autosave.DEBOUNCE + 0.05)
    await settle(fleet_app(pilot))
    await pilot.pause()


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
        await pilot.pause()
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
        await pilot.pause()
        seen["shrunk"] = (
            app.sidebar.outer_size.width,
            app.content.outer_size.width,
            app.query_one(Divider).region.x,
        )
        await pilot.resize_terminal(*SIZE)
        await pilot.pause()
        seen["back"] = app.sidebar.outer_size.width
        # A key at the small size steps from what is ON SCREEN, not from the wide screen's number.
        await pilot.resize_terminal(80, SIZE[1])
        await pilot.pause()
        app.sidebar.focus()
        await pilot.press("less_than_sign")
        await pilot.pause()
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


def test_a_tap_after_a_drag_keeps_the_drag_and_two_clicks_reset_and_forget_the_width(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """Textual counts a drag's release as a click (the handle follows the pointer, so the
    release lands on the widget the press did): a tap on the handle within half a second
    read as `chain == 2`, threw the drag away and saved the reset. `pilot.click(times=2)`
    could not see it — Pilot builds `Click(chain=2)` itself, past `App.on_event`."""
    seed(tmp_path, ("prj_a", "alpha", None))

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
            await pilot.pause()
            widths.append(app.sidebar.outer_size.width)
        seen["widths"] = widths
        await _settled(pilot)
        seen["state_after_reset"] = _state(isolated_home)
        # Autorepeat: five presses queued back to back, no layout pass between them.
        before = len(writes)
        for _ in range(5):
            app.post_message(events.Key("greater_than_sign", ">"))
        await pilot.pause()
        seen["burst"] = app.sidebar.outer_size.width
        await _settled(pilot)
        seen["burst_writes"] = writes[before:]
        # With a pane focused the same keys are text for the agent, not a resize.
        pane = RecordingPane()
        await app.content.add_content(pane, set_current=True)
        pane.focus()
        await pilot.pause()
        await pilot.press("greater_than_sign")
        await pilot.pause()
        seen["with_pane"] = (app.sidebar.outer_size.width, list(pane.keys))
        # The fallback exists for terminals without mouse reporting; ? is where they look.
        app.sidebar.focus()
        await pilot.press("question_mark")
        await pilot.pause()
        seen["help"] = shown(app.screen.query_one("#helpbox Static", Static))
        await pilot.press("escape")
        await pilot.pause()
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
            "toasts": [toast.render().plain for toast in app.screen.query(Toast)],
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
    assert "state.json" in toasts[0] and "not be remembered" in toasts[0]
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
        await pilot.pause()
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
        await pilot.pause()
        seen: dict[str, object] = {"floor": _floor(app), "shown": app.sidebar.outer_size.width}
        app.sidebar.focus()

        def now() -> tuple[int, object]:
            return app.sidebar.outer_size.width, _state(isolated_home).get(SIDEBAR_WIDTH_KEY)

        # Rule 1 on its own: a step the ceiling swallows does not settle — with the
        # ceiling rule stubbed out, so it cannot be the one catching this.
        with monkeypatch.context() as stubbed:
            stubbed.setattr(Divider, "_ceiling_under_remembered", lambda self, width: False)
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
        await pilot.pause()
        seen["after_room"] = now()
        await pilot.resize_terminal(80, SIZE[1])
        await pilot.pause()
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
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """`_moved` was set by any motion event: on a tall one-column handle a one-row hand drift
    between press and release is routine, moved nothing, and made the reset take three clicks."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> tuple[int, int, int]:
        app = fleet_app(pilot)
        declared = _declared(app)
        app.sidebar.focus()
        await pilot.press("greater_than_sign")
        await pilot.pause()
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
        return [toast.render().plain for toast in app.screen.query(Toast)], path.read_text()

    toasts, file = drive(go, notifications=True)
    assert len(toasts) == 1, "said once, not once per pick"
    assert "state.json" in toasts[0] and "theme will not be remembered" in toasts[0]
    assert file == body


def test_another_button_breaks_the_double_click_chain(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    """Textual's chain counter is button-agnostic: a middle click between two left clicks fifteen
    rows apart, or a right click between two at one cell, made the second left click
    `chain == 2` — and a time check on the previous left click let both through."""
    seed(tmp_path, ("prj_a", "alpha", None))

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
    writes: list[tuple[str, object]] = []

    def counting(key: str, value: object) -> None:
        writes.append((key, value))
        update_state(key, value)

    monkeypatch.setattr("aisquare.cli.ui.autosave.update_state", counting)

    async def go(pilot: Pilot[None]) -> int:
        app = fleet_app(pilot)
        declared = _declared(app)
        app.sidebar.focus()
        await pilot.press("greater_than_sign")
        await _settled(pilot)  # one write: 34
        await pilot.press("less_than_sign")  # 30 queued...
        await pilot.press(
            "greater_than_sign"
        )  # ...and back to what the file says: nothing to write
        await _settled(pilot)
        await pilot.press("greater_than_sign")  # 38 queued — and the app quits inside the debounce
        return declared

    declared = drive(go)
    step = RESIZE_STEP
    assert writes == [
        (SIDEBAR_WIDTH_KEY, declared + step),
        (SIDEBAR_WIDTH_KEY, declared + 2 * step),
    ]
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
        await pilot.pause()
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
        await pilot.pause()
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
