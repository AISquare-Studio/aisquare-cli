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
from typing import Any, TypeVar

import pytest
from textual import Logger, events
from textual.app import ScreenStackError
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.geometry import Region
from textual.pilot import Pilot
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Input, Static, Switch
from textual.widgets._toast import Toast
from textual.worker import Worker, WorkerState

from aisquare.cli.ui import app as app_mod
from aisquare.cli.ui.app import FleetApp, HelpScreen
from aisquare.cli.ui.groups import GroupPicker
from aisquare.cli.ui.sidebar import (
    Activatable,
    AgentRow,
    Disclosure,
    DoctorSection,
    DoctorTitle,
    GroupHeader,
    ProjectCard,
    ProjectTitle,
    SectionLabel,
    ordered_agents,
    short_path,
)
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
from aisquare.core import tmux as tmux_core
from aisquare.core.config import load_config, save_config
from aisquare.core.store import ContextStore, store_session
from aisquare.core.tmux import Completed
from aisquare.models import CheckStatus, DoctorCheck, FleetAgent, FleetAgentStatus, ProjectInfo
from aisquare.services import explainability as explainability_service
from aisquare.services import fleet as fleet_service
from aisquare.services import project_groups as groups_service
from tests.pane_harness import FakePane, FakeTmux, asks_a_server, move, press, release, socket_of

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

    monkeypatch.setattr(fleet_service, "list_agents", fake)
    return agents


def drive(
    fn: Callable[[Pilot[None]], Awaitable[T]],
    *,
    doctor: Callable[[], list[DoctorCheck]] | None = None,
    notifications: bool = False,
) -> T:
    """Run ``fn`` against a mounted ``FleetApp`` (no timer refresh; a stub doctor).

    ``notifications`` opts the screen's ``ToastRack`` in — ``run_test`` leaves it
    out by default, and without it a ``notify`` goes nowhere to be read.
    """

    async def run() -> T:
        app = FleetApp(refresh_seconds=3600, doctor=doctor or (lambda: []))
        async with app.run_test(size=SIZE, notifications=notifications) as pilot:
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
    assert tester.startswith("🧪") and tester.rstrip().endswith("💤 exited(3)")  # #138: the word
    assert scout.startswith("🤖") and "🔔" in scout  # unknown role: the custom icon
    assert "(3)" not in coder and "💤" not in coder and "exited" not in coder  # only on that row
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
        await pilot.pause()
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
        await pilot.pause()
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
        return app.screen.query_one(Toast).render().plain

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
        return app.screen.query_one(Toast).render().plain

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
        return _toasts(app)

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
        await pilot.pause()
        return app.screen.query_one(Toast).render().plain

    assert "nothing to save" in drive(go, notifications=True)
    assert load_config().explainability.targets == {}


def _toasts(app: Any) -> str:
    """Every toast on screen, oldest first — one save can raise more than one."""
    return " | ".join(toast.render().plain for toast in app.screen.query(Toast))


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
        return _toasts(app)

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
        return _toasts(app)

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
        return _toasts(app)

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
        return _toasts(app)

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
        await pilot.pause()
        return _toasts(app)

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
        return _toasts(app)

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
        return app.screen.query_one("#explainability-key", Input).value, _toasts(app)

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
    assert screens == ["PaneScreen", "PaneScreen", "HelpScreen", "HelpScreen"]
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

    async def go(pilot: Pilot[None]) -> tuple[bool, bool, bool, str]:
        app = fleet_app(pilot)
        closed_before = not isinstance(app.screen, HelpScreen)
        await pilot.press("question_mark")
        await pilot.pause()
        opened = isinstance(app.screen, HelpScreen)
        keys = shown(app.screen.query_one("#helpbox Static", Static))
        await pilot.press("escape")
        await pilot.pause()
        return closed_before, opened, isinstance(app.screen, HelpScreen), keys

    closed_before, opened, still_open, keys = drive(go)
    assert (closed_before, opened, still_open) == (True, True, False)
    # The sidebar's arranging keys (#140) are all show=False in the footer: here is
    # the one place they are found (review of #171, round 1).
    for key in ("shift+↑ ↓", "g p space", "shift+click", "drag title"):
        assert key in keys, key


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


def test_r_re_runs_the_doctor_and_a_only_re_reads_the_list(tmp_path: Path, script: Script) -> None:
    """`r` is "refresh now" — the fleet AND the doctor. #139's `a` action was first
    inserted between the two calls and took the doctor run with it, so `r` re-read
    the store only and `a`, which changes nothing the doctor looks at, re-ran it."""
    seed(tmp_path, ("prj_a", "alpha", None))
    calls: list[int] = []

    def doctor() -> list[DoctorCheck]:
        calls.append(1)
        return []

    async def go(pilot: Pilot[None]) -> tuple[int, int, int]:
        app = fleet_app(pilot)
        await settle(app)
        app.sidebar.focus()
        at_mount = len(calls)
        await pilot.press("r")
        await pilot.pause()
        await settle(app)
        after_r = len(calls)
        await pilot.press("a")
        await pilot.pause()
        await settle(app)
        return at_mount, after_r, len(calls)

    at_mount, after_r, after_a = drive(go, doctor=doctor)
    assert at_mount == 1
    assert after_r == 2, "r re-runs the doctor"
    assert after_a == 2, "a changes which cards are shown, not what the doctor finds"


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


def test_restart_from_the_agent_view_selects_the_new_row_in_the_shell(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#138 end to end through the real app: the exited row's view offers Restart,
    the service is called with the row's project and the pane's size, the frame is
    re-read, and the NEW row is what the shell shows and highlights."""
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

    async def go(pilot: Pilot[None]) -> tuple[str | None, str | None, list[str], list[str]]:
        app = fleet_app(pilot)
        await pilot.click(row_for(app, "agt_a_manager"))
        await pilot.pause()
        view = app.current_view()
        assert isinstance(view, AgentView)
        stop_shown = view.query_one("#agent-stop", Button).display
        await pilot.click("#agent-restart")
        await settle(app)
        await pilot.pause()
        await pilot.pause()
        current = app.current_view()
        # The pane's own "(pane gone)" toast is there too: read them all.
        toasts = [toast.render().plain for toast in app.screen.query(Toast)]
        rows = [shown(row) for row in card_for(app, "prj_a").query(AgentRow)]
        assert stop_shown is True  # the 💤 row's Stop removes its dead window
        return (current.id if current else None), app.sidebar.selected_key, toasts, rows

    current, selected, toasts, rows = drive(go, notifications=True)
    assert calls and calls[0][:2] == ("prj_a", "manager")
    assert current == "agent-agt_a_new" and selected == "agent:agt_a_new"
    assert any("✓ restarted manager — resumed its session" in toast for toast in toasts), toasts
    assert len(rows) == 1 and rows[0].rstrip().endswith("⏸")  # the old 💤 exited row is gone


def test_the_sidebar_hides_captured_directories_until_a_shows_them(
    tmp_path: Path, script: Script
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
        await pilot.pause()
        with_captured = [card.project.id for card in app.query(ProjectCard)]
        title = shown_text(app, "prj_scratch")
        await pilot.press("a")
        await pilot.pause()
        after = [card.project.id for card in app.query(ProjectCard)]
        await pilot.press("question_mark")
        await pilot.pause()
        return before, with_captured, title, after, shown(app.screen.query_one(Static))

    def shown_text(app: FleetApp, project_id: str) -> str:
        return shown(card_for(app, project_id).query_one(ProjectTitle))

    before, with_captured, title, after, keys = drive(go)
    assert before == ["prj_a"], "a captured directory is not a card"
    assert with_captured == ["prj_a", "prj_scratch"]
    assert "captured" in title
    assert after == ["prj_a"], "a hides them again"
    assert "captured directories" in keys, "the key is on the ? screen (it has no footer label)"


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
        await pilot.pause()
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
    tmp_path: Path, script: Script
) -> None:
    """Review of #169: what the shell remembers beyond a row came back untested —
    the captured directories shown with `a`, the Accounts page, the Doctor."""
    seed(tmp_path, ("prj_a", "alpha", None))
    with store_session() as store:
        store.ensure_project(ProjectInfo(id="prj_scratch", root=tmp_path / "scratch"))  # a hook

    async def press_a(pilot: Pilot[None]) -> None:
        fleet_app(pilot).sidebar.focus()
        await pilot.press("a")
        await pilot.pause()

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

    with store_session() as store:
        store.set_ui_state("fleet.selected", "accounts")
    assert drive(relaunch)[1:] == ("accounts", "accounts")
    with store_session() as store:
        store.set_ui_state("fleet.selected", "doctor:")
    assert drive(relaunch)[1:] == ("doctor", "doctor")


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
        await pilot.pause()
        moved = _cards(app)
        await pilot.press("p")
        await pilot.pause()
        pinned = _cards(app)
        await pilot.press("u")
        await pilot.pause()
        undone = _cards(app)
        app.sidebar.select("group:" + store_group_id("tools"))
        await pilot.press("space")
        await pilot.pause()
        folded = _cards(app)
        header = app.sidebar.query_one(GroupHeader)
        rollup = shown(header)
        await pilot.press("space")
        await pilot.pause()
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
    """
    x, y = widget.region.offset + offset
    app = pilot.app
    app.post_message(kind(None, x, y, 0, 0, button, shift, False, False, screen_x=x, screen_y=y))
    await pilot.pause()


async def _click(pilot: Pilot[None], widget: Widget, *, shift: bool = False) -> None:
    await _as_the_terminal_sends(pilot, events.MouseDown, widget, shift=shift)
    await _as_the_terminal_sends(pilot, events.MouseUp, widget, shift=shift)
    await pilot.pause()


async def _drag(
    pilot: Pilot[None], source: Widget, target: Widget, offset: tuple[int, int] = (1, 0)
) -> None:
    """Press on ``source``, move onto ``target`` with the button held, release there."""
    await _as_the_terminal_sends(pilot, events.MouseDown, source)
    await _as_the_terminal_sends(pilot, events.MouseMove, target, offset)
    await _as_the_terminal_sends(pilot, events.MouseUp, target, offset)
    await pilot.pause()


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
        await _drag(pilot, title, app.sidebar.query_one(GroupHeader), offset=(3, 0))
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
        await pilot.pause()
        assert isinstance(app.screen, GroupPicker), type(app.screen).__name__
        await pilot.press("end")  # … the last option is Ungroup; New group… is just above it
        await pilot.press("up")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press(*"web")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
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

        await _drag(pilot, title("prj_b"), app.content, offset=(10, 12))  # over the main pane
        seen.append(_cards(app))
        await _drag(pilot, title("prj_b"), card_for(app, "prj_c"))  # onto a pinned card
        seen.append(_cards(app))
        await _drag(pilot, header("tools"), card_for(app, "prj_a"))  # a group onto a card
        seen.append(_cards(app))
        abandoned = len(app._undo)
        # The list's own empty space below the last row is still the end of the top level.
        holder = app.sidebar.query_one("#projects", VerticalScroll)
        await _drag(pilot, title("prj_b"), holder, offset=(2, holder.region.height - 1))
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
        await _drag(pilot, title("prj_b"), holder, offset=empty)  # onto the empty space
        seen.append(_cards(app))
        # Onto a loose card, looked at on the way: nothing lifts off, nothing is a place.
        api = card_for(app, "prj_a")
        await _as_the_terminal_sends(pilot, events.MouseDown, title("prj_b"))
        await _as_the_terminal_sends(pilot, events.MouseMove, api)
        lifted = card_for(app, "prj_b").has_class("-dragging") or api.has_class("-drop-before")
        await _as_the_terminal_sends(pilot, events.MouseUp, api)
        seen.append(_cards(app))
        await _drag(pilot, header("site"), header("tools"), offset=(3, 0))  # a pinned group
        seen.append(_cards(app))
        depth = len(app._undo)
        # A selection of a loose and a pinned card, dragged by the loose one: it moves alone.
        await _click(pilot, title("prj_a"), shift=True)
        await _click(pilot, title("prj_b"), shift=True)
        await _drag(pilot, title("prj_a"), holder, offset=empty)
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
        await pilot.pause()
        stepped = _cards(app)
        await pilot.press("shift+up")  # already first
        app.sidebar.select("project:prj_c")
        await pilot.press("shift+down")  # pinned: no place in a scope
        await pilot.pause()
        depth = len(app._undo)
        await pilot.press("u")
        await pilot.pause()
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
        await pilot.pause()
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
