"""R17 (#150): Pause beside Stop and Restart, one box with *Don't ask again* for each, its
reset, an agent's ⋯ menu, and App settings ▸ Display — driven through the whole app.

The owner (2026-11-16): the agent view's header "should also show pause and all 3 of them
should have on click confirmation box, but an option to not ask again", reset from Settings
for every kind "even in future"; a ⋯ per agent in the left pane "for the same of pause stop
restart rename"; and simple switches "to hide things like all the decorations after the
agent for showing their usage". What each test pins is what the SERVICE was asked (the row
it is pinned to, never a label), what the file holds, and what the screen says.

The Stop box's own paths: tests/test_ui_stop_dont_ask.py. The bulk boxes' pick-list:
tests/test_ui_fleet_bulk.py and tests/test_ui_pause_resume.py. The store's keys:
tests/test_config.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from rich.text import Text
from textual.app import App, ComposeResult
from textual.geometry import Region
from textual.notifications import Notification, Notifications
from textual.pilot import Pilot
from textual.widget import Widget
from textual.widgets import Button, Checkbox, OptionList, Static

from aisquare.cli.ui.agent_menu import AgentMenu
from aisquare.cli.ui.app import FleetApp, Panes
from aisquare.cli.ui.board import BoardPanel
from aisquare.cli.ui.confirm_action import ASK_AGAIN_WHERE, ConfirmActionScreen, ask_first
from aisquare.cli.ui.sidebar import MENU_GLYPH, AgentRow
from aisquare.cli.ui.stop import StopAgentScreen
from aisquare.cli.ui.views.agent import AgentView, pause_question, restart_question
from aisquare.cli.ui.views.app_settings import UiSettingsChanged
from aisquare.cli.ui.views.doctor import COMMAND
from aisquare.cli.ui.views.fleet_bulk import BulkAsked
from aisquare.core.config import load_config
from aisquare.models import FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import settings as settings_service
from tests.test_ui_shell import (
    Script,
    composited,
    drive,
    ended,
    fleet_app,
    on_slot,
    row_for,
    seed,
    session_use,
    settle,
    status,
)
from tests.test_ui_shell import no_real_tmux as _no_real_tmux
from tests.test_ui_shell import script as _script

#: Re-exported so pytest collects them here (the guard is autouse there, and here).
no_real_tmux = _no_real_tmux
script = _script

CODER = "agt_a_coder-auth"
MANAGER = "agt_a_manager"

POSTED: list[Notification] = []
"""Every toast the app was given, as it was given: never the app's live collection, which
Textual reaps of expired toasts as it is read (review of #477, M3; the Windows habit of #517)."""


@pytest.fixture(autouse=True)
def posted(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Notification]]:
    real = Notifications.add

    def recording(self: Notifications, notification: Notification) -> Notifications:
        POSTED.append(notification)
        return real(self, notification)

    POSTED.clear()
    monkeypatch.setattr(Notifications, "add", recording)
    yield POSTED
    POSTED.clear()


def messages() -> list[str]:
    return [str(n.message) for n in POSTED]


def drawn_line(widget: Widget) -> str:
    """The rows ``widget`` composites, as the eye gets them, one after another: a button's
    label, cut or whole (a box's buttons are three rows, the label on the middle one)."""
    lines = widget.render_lines(Region(0, 0, widget.region.width, widget.region.height))
    return "\n".join(strip.text for strip in lines)


def drawn(widget: Static) -> str:
    """What a sidebar row or the title draws: the text it was built for its width with (its
    padding left out, which ``composited`` reads the strip at the content's width with)."""
    plain = getattr(widget.visual, "plain", None)
    assert isinstance(plain, str), type(widget.visual).__name__
    return plain


class Calls:
    """Stands in for ``pause_agent`` and ``restart``: records each call by the row it is
    pinned to, and answers the receipt the service would."""

    def __init__(self, script: Script, project_id: str = "prj_a") -> None:
        self.script = script
        self.project_id = project_id
        self.paused: list[tuple[str, str, str | None, str | None]] = []
        self.restarted: list[tuple[str, str, str | None, tuple[int, int] | None]] = []

    def pause_agent(
        self,
        project: ProjectInfo,
        label: str,
        *,
        agent_id: str | None = None,
        actor: str | None = None,
        grace: float = 5.0,
    ) -> fleet_service.StopReceipt:
        self.paused.append((project.id, label, agent_id, actor))
        rows = self.script.get(project.id, [])
        row = next(r for r in rows if r.agent.id == agent_id)
        self.script[project.id] = [
            r.model_copy(update={"state": "paused"}) if r is row else r for r in rows
        ]
        return fleet_service.StopReceipt(agent=ended(row.agent), released=[])

    def restart(
        self,
        project: ProjectInfo,
        label: str,
        *,
        size: tuple[int, int] | None = None,
        agent_id: str | None = None,
        **_: object,
    ) -> fleet_service.RestartReceipt:
        self.restarted.append((project.id, label, agent_id, size))
        rows = self.script.get(project.id, [])
        row = next(r for r in rows if r.agent.id == agent_id)
        started = row.agent.model_copy(update={"id": f"{row.agent.id}-2"})
        self.script[project.id] = [
            FleetAgentStatus(agent=started, state="waiting") if r is row else r for r in rows
        ]
        return fleet_service.RestartReceipt(
            replaced=row.agent,
            started=started,
            resumed=True,
            was_running=row.state in fleet_service.PROCESS_STATES,
            tmux_session="asq-x",
        )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch, script: Script) -> Calls:
    recorder = Calls(script)
    monkeypatch.setattr(fleet_service, "pause_agent", recorder.pause_agent)
    monkeypatch.setattr(fleet_service, "restart", recorder.restart)
    monkeypatch.setattr(fleet_service, "project_of", lambda agent: _project(agent.project_id))
    return recorder


def _project(project_id: str) -> ProjectInfo:
    from aisquare.core.store import store_session

    with store_session() as store:
        project = store.get_project(project_id)
    assert project is not None
    return project


def one_coder(tmp_path: Path, script: Script, state: str = "working") -> FleetAgentStatus:
    seed(tmp_path, ("prj_a", "alpha", None))
    row = status("prj_a", "coder-auth", "coder", state)
    manager = status("prj_a", "manager", "manager", "waiting")
    script["prj_a"] = [manager, row]
    return row


async def open_view(pilot: Pilot[None], app: FleetApp, agent_id: str) -> AgentView:
    await pilot.click(row_for(app, agent_id))
    await pilot.pause()
    await settle(app)
    view = app.current_view()
    assert isinstance(view, AgentView) and view.status.agent.id == agent_id
    return view


def shown_buttons(view: AgentView) -> list[str]:
    """The header's buttons, left to right, as drawn: the ones with ``display`` on."""
    return [
        str(button.label)
        for button in view.query("#agent-bar Button").results(Button)
        if button.display
    ]


# --- A. the header ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "buttons"),
    [
        ("working", ["Pause", "Stop", "Restart"]),
        ("attention", ["Pause", "Stop", "Restart"]),
        ("paused", ["Resume", "Stop"]),
        ("exited", ["Stop", "Restart (resume)"]),
        ("lost", ["Restart (resume)"]),
    ],
)
def test_the_header_offers_pause_before_stop_and_resume_in_its_place_on_a_paused_row(
    tmp_path: Path, script: Script, state: str, buttons: list[str]
) -> None:
    """Pause while there is a process; a paused row's Resume sits in its place, and its
    Restart is hidden (restarting a paused row IS its resume, Q2)."""
    one_coder(tmp_path, script, state)

    async def go(pilot: Pilot[None]) -> list[str]:
        return shown_buttons(await open_view(pilot, fleet_app(pilot), CODER))

    assert drive(go) == buttons


@pytest.mark.parametrize(("size", "tight"), [((45, 30), True), ((120, 40), False)])
@pytest.mark.parametrize("state", ["working", "exited", "paused"])
def test_every_header_button_is_whole_in_a_45_column_terminal(
    tmp_path: Path, script: Script, size: tuple[int, int], tight: bool, state: str
) -> None:
    """Beside a 45-column terminal's sidebar the bar has 21 cells: three padded buttons took 22
    and Restart read ``Rest``. Tight there, each label whole inside the bar; padded at 120."""
    one_coder(tmp_path, script, state)

    async def go(pilot: Pilot[None]) -> tuple[bool, list[tuple[str, bool]]]:
        app = fleet_app(pilot)
        view = await open_view(pilot, app, CODER)
        await settle(app)
        bar = view.query_one("#agent-bar")
        right = bar.region.x + bar.region.width
        whole = [
            (str(b.label), b.region.right <= right and str(b.label) in drawn_line(b))
            for b in bar.query(Button)
            if b.display
        ]
        return bar.has_class("-tight"), whole

    is_tight, buttons = drive(go, size=size)
    assert is_tight is tight
    assert buttons and all(inside for _, inside in buttons), buttons


@pytest.mark.parametrize("box", ["stop", "pause", "bulk"])
def test_every_box_shows_all_its_buttons_whole_in_a_45_column_terminal(
    tmp_path: Path, script: Script, box: str
) -> None:
    """At 45 columns the Stop box's Stop, Force and Cancel (16 cells each) put Cancel past the
    box, and the bulk box stayed 72 wide past the screen: every button of every box is on
    screen, whole, and so is the box."""
    one_coder(tmp_path, script)

    async def go(pilot: Pilot[None]) -> list[tuple[str, bool]]:
        app = fleet_app(pilot)
        view = await open_view(pilot, app, CODER)
        if box == "bulk":
            app.post_message(BulkAsked("pause", "prj_a"))
        else:
            view.query_one(f"#agent-{box}", Button).press()
        await pilot.pause()
        await settle(app)
        screen = app.screen
        assert type(screen).__name__ != "PaneScreen", "a box is up"
        width = app.size.width
        return [
            (str(b.label), b.region.right <= width and str(b.label) in drawn_line(b))
            for b in screen.query(Button)
            if b.display and b.region.width
        ]

    buttons = drive(go, size=(45, 30))
    assert len(buttons) >= 2 and all(whole for _, whole in buttons), buttons


def test_the_captains_row_offers_no_pause(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    one_coder(tmp_path, script)
    monkeypatch.setattr(fleet_service, "is_the_captains_row", lambda agent: True)

    async def go(pilot: Pilot[None]) -> list[str]:
        return shown_buttons(await open_view(pilot, fleet_app(pilot), CODER))

    assert drive(go) == ["Stop", "Restart"]


# --- B. Pause and Restart ask first, and Don't ask again -------------------------------------


async def press(pilot: Pilot[None], view: AgentView, button: str) -> None:
    view.query_one(f"#agent-{button}", Button).press()
    await pilot.pause()
    await settle(fleet_app(pilot))


@pytest.mark.parametrize("kind", ["pause", "restart"])
@pytest.mark.parametrize("answer", ["cancel", "escape"])
def test_a_box_cancelled_acts_on_nothing_and_saves_nothing(
    tmp_path: Path, script: Script, calls: Calls, kind: str, answer: str
) -> None:
    one_coder(tmp_path, script)

    async def go(pilot: Pilot[None]) -> tuple[str, str]:
        app = fleet_app(pilot)
        view = await open_view(pilot, app, CODER)
        await press(pilot, view, kind)
        box = app.screen
        assert isinstance(box, ConfirmActionScreen) and box.kind == kind
        question = str(box.query_one(f"#{kind}-question", Static).render())
        box.query_one(f"#{kind}-dont-ask", Checkbox).value = True  # ticked, then not confirmed
        if answer == "cancel":
            box.query_one(f"#{kind}-cancel", Button).press()
        else:
            await pilot.press("escape")
        await settle(app)
        return type(app.screen).__name__, question

    screen, question = drive(go)
    assert screen != "ConfirmActionScreen"
    assert (calls.paused, calls.restarted) == ([], [])
    assert load_config().ui.dont_ask == [], "Cancel and Escape save nothing"
    assert question.startswith(f"{kind.capitalize()} coder-auth?"), question


def test_pause_confirmed_pauses_this_row_and_says_what_is_kept(
    tmp_path: Path, script: Script, calls: Calls
) -> None:
    one_coder(tmp_path, script)

    async def go(pilot: Pilot[None]) -> list[str]:
        app = fleet_app(pilot)
        view = await open_view(pilot, app, CODER)
        await press(pilot, view, "pause")
        assert app.focused is not None and app.focused.id == "pause-confirm", "Enter acts"
        await pilot.press("enter")
        await settle(app)
        return shown_buttons(view)

    assert drive(go) is not None
    assert calls.paused == [("prj_a", "coder-auth", CODER, "user")], "pinned to the row"
    assert "⏸ coder-auth paused: its task, worktree and session are kept." in messages()
    assert load_config().ui.dont_ask == []


@pytest.mark.parametrize("kind", ["pause", "restart"])
def test_ticked_dont_ask_again_saves_the_kind_and_the_next_press_acts_at_once(
    tmp_path: Path, script: Script, calls: Calls, kind: str
) -> None:
    one_coder(tmp_path, script)
    verb = kind.capitalize()

    async def go(pilot: Pilot[None]) -> tuple[bool, list[str]]:
        app = fleet_app(pilot)
        view = await open_view(pilot, app, CODER)
        await press(pilot, view, kind)
        box = app.screen
        assert isinstance(box, ConfirmActionScreen)
        box.query_one(f"#{kind}-dont-ask", Checkbox).value = True
        box.query_one(f"#{kind}-confirm", Button).press()
        await settle(app)
        script["prj_a"] = [status("prj_a", "coder-auth", "coder", "working")]  # running again
        await settle(app)
        app.refresh_data()
        await settle(app)
        view = await open_view(pilot, app, CODER)
        await press(pilot, view, kind)
        return isinstance(app.screen, ConfirmActionScreen), messages()

    asked_again, said = drive(go)
    assert asked_again is False, "one click acts"
    assert load_config().ui.dont_ask == [kind]
    assert load_config().ui.confirm_stop is True, "Stop still asks: the tick is per kind"
    assert f"{verb} will not ask again. {ASK_AGAIN_WHERE} turns it back on." in said
    done = calls.paused if kind == "pause" else calls.restarted
    assert len(done) == 2, done


def test_resume_never_asks(tmp_path: Path, script: Script, calls: Calls) -> None:
    """Q1: Resume undoes a pause and loses nothing; it acts on its one click, box or no box."""
    one_coder(tmp_path, script, "paused")

    async def go(pilot: Pilot[None]) -> bool:
        app = fleet_app(pilot)
        view = await open_view(pilot, app, CODER)
        await press(pilot, view, "resume")
        return isinstance(app.screen, ConfirmActionScreen)

    assert drive(go) is False
    assert [(c[1], c[2]) for c in calls.restarted] == [("coder-auth", CODER)]


def test_a_tick_another_asq_saved_holds_at_the_next_press(
    tmp_path: Path, script: Script, calls: Calls
) -> None:
    """Read from the file at each press: another asq on this home saved it, and nothing
    told this one."""
    one_coder(tmp_path, script)

    async def go(pilot: Pilot[None]) -> bool:
        app = fleet_app(pilot)
        view = await open_view(pilot, app, CODER)
        settings_service.stop_asking_first("pause")  # the other asq's tick
        await press(pilot, view, "pause")
        return isinstance(app.screen, ConfirmActionScreen)

    assert drive(go) is False
    assert len(calls.paused) == 1


def test_a_tick_over_an_unreadable_file_is_not_saved_and_the_action_goes_on(
    tmp_path: Path, script: Script, calls: Calls
) -> None:
    one_coder(tmp_path, script)
    from aisquare.core import paths

    async def go(pilot: Pilot[None]) -> None:
        app = fleet_app(pilot)
        view = await open_view(pilot, app, CODER)
        paths.config_path().parent.mkdir(parents=True, exist_ok=True)
        paths.config_path().write_text("[ui\nnot toml", encoding="utf-8")
        await press(pilot, view, "pause")
        box = app.screen
        assert isinstance(box, ConfirmActionScreen), "an unreadable file asks"
        box.query_one("#pause-dont-ask", Checkbox).value = True
        box.query_one("#pause-confirm", Button).press()
        await settle(app)

    drive(go)
    assert len(calls.paused) == 1, "the pause went on"
    assert any(
        said.startswith("Don't ask again was not saved, so Pause will ask again: ")
        for said in messages()
    ), messages()
    assert paths_text() == "[ui\nnot toml"


def paths_text() -> str:
    from aisquare.core import paths

    return paths.config_path().read_text(encoding="utf-8")


def test_the_boxes_say_what_happens_in_words_and_never_a_command() -> None:
    """R13: no command in any box; the label is data, brackets and all."""
    for state in ("working", "exited"):
        row = status("prj_a", "[wip] coder", "coder", state)
        for question in (pause_question(row), restart_question(row)):
            assert isinstance(question, Text)
            assert "[wip] coder" in question.plain
            assert not COMMAND.search(question.plain), question.plain
    manager = status("prj_a", "manager", "manager", "working")
    assert "the agents it started keep running" in pause_question(manager).plain
    exited = restart_question(status("prj_a", "c", "coder", "exited")).plain
    assert exited.startswith("Start c again?\nIt starts again with the same role")


# --- C. the reset covers every kind, one added later too -------------------------------------


class BoxHost(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.acted: list[str] = []
        self.told: list[list[str]] = []

    def compose(self) -> ComposeResult:
        yield Static("host")

    def on_ui_settings_changed(self, event: UiSettingsChanged) -> None:
        self.told.append(list(event.ui.dont_ask))


def test_a_kind_added_later_asks_until_ticked_and_the_reset_brings_it_back() -> None:
    """The store is a list of kinds: a kind no release knew yet asks by default, is saved by
    its own box's tick, and *Ask again before every action* clears it with the rest."""
    settings_service.stop_asking_first("stop")

    async def run() -> tuple[bool, bool, list[list[str]]]:
        host = BoxHost()
        async with host.run_test() as pilot:
            box: ConfirmActionScreen[bool] = ConfirmActionScreen("archive", Text("Archive it?"))
            shown = ask_first(host, box, lambda: host.acted.append("archive"))
            await pilot.pause()
            box.query_one("#archive-dont-ask", Checkbox).value = True
            box.query_one("#archive-confirm", Button).press()
            await pilot.pause()
            again: ConfirmActionScreen[bool] = ConfirmActionScreen("archive", Text("Archive?"))
            shown_again = ask_first(host, again, lambda: host.acted.append("archive"))
            await pilot.pause()
            return shown, shown_again, host.told

    shown, shown_again, told = asyncio.run(run())
    assert (shown, shown_again) == (True, False)
    assert told == [["stop", "archive"]]
    assert load_config().ui.dont_ask == ["stop", "archive"]
    assert settings_service.asks_first("archive") is False

    settings_service.ask_first_again()
    assert load_config().ui.dont_ask == [] and load_config().ui.confirm_stop is True
    assert all(settings_service.asks_first(kind) for kind in ("stop", "pause", "archive"))


# --- D. an agent's ⋯ menu ---------------------------------------------------------------------


def entries(menu: AgentMenu) -> list[tuple[str, bool]]:
    options = menu.query_one(OptionList)
    return [
        (str(options.get_option_at_index(i).prompt), options.get_option_at_index(i).disabled)
        for i in range(options.option_count)
    ]


async def right_click(pilot: Pilot[None], app: FleetApp, agent_id: str) -> AgentMenu:
    await pilot.click(row_for(app, agent_id), button=3)
    await pilot.pause()
    await settle(app)
    menu = app.screen
    assert isinstance(menu, AgentMenu), type(menu).__name__
    return menu


@pytest.mark.parametrize(
    ("state", "listed"),
    [
        ("working", ["⏸ Pause", "■ Stop", "↺ Restart", "✎ Rename"]),
        ("paused", ["▶ Resume", "■ Stop", "✎ Rename"]),
        ("exited", ["■ Stop", "↺ Restart", "✎ Rename"]),
    ],
)
def test_a_right_click_opens_the_agents_menu_with_the_headers_entries(
    tmp_path: Path, script: Script, state: str, listed: list[str]
) -> None:
    one_coder(tmp_path, script, state)

    async def go(pilot: Pilot[None]) -> list[tuple[str, bool]]:
        app = fleet_app(pilot)
        await settle(app)
        return entries(await right_click(pilot, app, CODER))

    got = drive(go)
    assert [prompt for prompt, _ in got] == listed
    assert dict(got)["✎ Rename"] is False, "a coder's row renames"


def test_the_managers_menu_greys_rename_and_says_why(tmp_path: Path, script: Script) -> None:
    one_coder(tmp_path, script)

    async def go(pilot: Pilot[None]) -> tuple[list[tuple[str, bool]], str]:
        app = fleet_app(pilot)
        await settle(app)
        menu = await right_click(pilot, app, MANAGER)
        return entries(menu), str(menu.query_one("#agent-menu-note", Static).render())

    got, note = drive(go)
    assert dict(got)["✎ Rename"] is True
    refusal = fleet_service.relabel_refusal(status("prj_a", "manager", "manager", "waiting").agent)
    assert refusal is not None and note == f"Rename: {refusal}"


def test_a_paused_agents_menu_says_why_rename_waits_and_names_no_command(
    tmp_path: Path, script: Script
) -> None:
    """R13 in the menu's note: the service's refusal for a paused agent names the command that
    resumes it, which asq never shows; asq says it in words."""
    seed(tmp_path, ("prj_a", "alpha", None))
    row = status("prj_a", "coder-auth", "coder", "paused")
    now = datetime.now(tz=UTC)
    paused = row.model_copy(
        update={"agent": row.agent.model_copy(update={"ended_at": now, "paused_at": now})}
    )
    script["prj_a"] = [paused]
    assert "fleet resume" in (fleet_service.relabel_refusal(paused.agent) or ""), "the CLI's"

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        await settle(app)
        menu = await right_click(pilot, app, CODER)
        return str(menu.query_one("#agent-menu-note", Static).render())

    note = drive(go)
    assert note == "Rename: coder-auth is paused; once it is resumed it can be renamed"
    assert not COMMAND.search(note), note


def test_dot_on_a_highlighted_agent_opens_its_menu_and_on_a_card_the_cards(
    tmp_path: Path, script: Script
) -> None:
    """Q-F1: ``.`` on an agent's row is its menu; on its card's title, the card's."""
    one_coder(tmp_path, script)

    async def go(pilot: Pilot[None]) -> tuple[str, str]:
        app = fleet_app(pilot)
        await open_view(pilot, app, CODER)
        app.sidebar.focus()
        await pilot.press("full_stop")
        await settle(app)
        on_agent = type(app.screen).__name__
        await pilot.press("escape")
        await settle(app)
        app.sidebar.select("project:prj_a")
        await pilot.press("full_stop")
        await settle(app)
        return on_agent, type(app.screen).__name__

    assert drive(go) == ("AgentMenu", "ProjectMenu")


def test_the_dots_show_on_the_highlighted_and_the_hovered_row_alone(
    tmp_path: Path, script: Script
) -> None:
    """Q-F2: no other row gives up a cell; a click on the ⋯ opens the menu."""
    one_coder(tmp_path, script)
    glyph = MENU_GLYPH.strip()

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        await settle(app)
        seen: dict[str, object] = {
            "none highlighted": [glyph in drawn(r) for r in app.query(AgentRow)]
        }
        await open_view(pilot, app, CODER)
        coder, manager = drawn(row_for(app, CODER)), drawn(row_for(app, MANAGER))
        seen["coder highlighted"] = (coder.endswith(MENU_GLYPH), glyph in manager)
        seen["fits"] = Text(coder).cell_len == row_for(app, CODER).content_size.width
        await pilot.hover(row_for(app, MANAGER))
        await pilot.pause()
        seen["manager hovered"] = drawn(row_for(app, MANAGER)).endswith(MENU_GLYPH)
        row = row_for(app, CODER)
        await pilot.click(row, offset=(row.region.width - 1, 0))  # its last cell: the ⋯
        await settle(app)
        seen["clicked"] = type(app.screen).__name__
        return seen

    seen = drive(go)
    assert seen["none highlighted"] == [False, False]
    assert seen["coder highlighted"] == (True, False)
    assert seen["fits"] is True, "the label gave way for it: the ⋯ ends the row, inside it"
    assert seen["manager hovered"] is True
    assert seen["clicked"] == "AgentMenu"


async def choose(pilot: Pilot[None], app: FleetApp, menu: AgentMenu, entry: str) -> None:
    options = menu.query_one(OptionList)
    index = next(
        i
        for i in range(options.option_count)
        if str(options.get_option_at_index(i).prompt).endswith(entry)
    )
    options.highlighted = index
    options.action_select()
    await pilot.pause()
    await settle(app)


def test_the_menus_pause_with_no_view_open_asks_then_pauses_the_row(
    tmp_path: Path, script: Script, calls: Calls
) -> None:
    one_coder(tmp_path, script)

    async def go(pilot: Pilot[None]) -> tuple[str, list[AgentView]]:
        app = fleet_app(pilot)
        await settle(app)
        await choose(pilot, app, await right_click(pilot, app, CODER), "Pause")
        asked = type(app.screen).__name__
        app.screen.query_one("#pause-confirm", Button).press()
        await settle(app)
        return asked, list(app.query(AgentView))

    asked, views = drive(go)
    assert asked == "ConfirmActionScreen"
    assert views == [], "the menu opened no view"
    assert calls.paused == [("prj_a", "coder-auth", CODER, "user")]
    assert "⏸ coder-auth paused: its task, worktree and session are kept." in messages()


def test_the_menus_restart_with_the_view_open_is_the_views_own_press(
    tmp_path: Path, script: Script, calls: Calls
) -> None:
    """The open view runs it, so its buttons grey while it runs and its pane's size goes."""
    one_coder(tmp_path, script)
    settings_service.stop_asking_first("restart")

    async def go(pilot: Pilot[None]) -> bool:
        app = fleet_app(pilot)
        await open_view(pilot, app, CODER)
        await choose(pilot, app, await right_click(pilot, app, CODER), "Restart")
        return isinstance(app.screen, ConfirmActionScreen)

    assert drive(go) is False, "Don't ask again holds in the menu too"
    [(_, label, agent_id, size)] = calls.restarted
    assert (label, agent_id) == ("coder-auth", CODER)
    assert size is not None, "the view's own press, with its pane's size"


def test_the_menus_stop_is_the_stop_box_and_rename_the_rows_field(
    tmp_path: Path, script: Script
) -> None:
    one_coder(tmp_path, script)

    async def go(pilot: Pilot[None]) -> tuple[str, bool]:
        app = fleet_app(pilot)
        await settle(app)
        await choose(pilot, app, await right_click(pilot, app, CODER), "Stop")
        stop_box = type(app.screen).__name__
        app.screen.query_one("#stop-cancel", Button).press()
        await settle(app)
        await choose(pilot, app, await right_click(pilot, app, CODER), "Rename")
        await pilot.pause()
        return stop_box, row_for(app, CODER).renaming

    assert drive(go) == (StopAgentScreen.__name__, True)


# --- E. Display --------------------------------------------------------------------------------


def _display(app: FleetApp, key: str, on: bool) -> None:
    """What App settings ▸ Display does at a tick: the key saved, the shell told."""
    app.post_message(UiSettingsChanged(settings_service.set_display_choice(key, on)))


def test_account_usage_and_the_live_count_go_and_come_back_live(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    script["prj_a"] = [on_slot(status("prj_a", "coder-auth", "coder", "working"), 1)]

    async def go(pilot: Pilot[None]) -> list[tuple[str, str, str]]:
        app = fleet_app(pilot)
        view = await open_view(pilot, app, CODER)
        app.sidebar.show_live_total(1)  # what the frame's capacity read hands it
        await settle(app)
        header = view.query_one("#agent-header", Static)
        title = app.query_one("#fleet-title", Static)

        def now() -> tuple[str, str, str]:
            return drawn(row_for(app, CODER)), composited(header), drawn(title)

        seen = [now()]
        _display(app, "show_usage", False)
        _display(app, "show_live_count", False)
        await settle(app)
        seen.append(now())
        _display(app, "show_usage", True)
        _display(app, "show_live_count", True)
        await settle(app)
        seen.append(now())
        return seen

    on, off, back = drive(go, headroom=session_use((1, 42.0)))
    assert "#1·42%" in on[0] and "· 42%" in on[1] and "1 live" in on[2], on
    assert "#1" not in off[0] and "42%" not in off[1] and "live" not in off[2], off
    assert back == on


def test_display_is_read_as_asq_starts(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    script["prj_a"] = [on_slot(status("prj_a", "coder-auth", "coder", "working"), 1)]
    for key in ("show_usage", "show_live_count", "fade_unfocused", "bell"):
        settings_service.set_display_choice(key, False)

    async def go(pilot: Pilot[None]) -> tuple[str, str, bool]:
        app = fleet_app(pilot)
        await settle(app)
        app.sidebar.show_live_total(1)
        await settle(app)
        return (
            drawn(row_for(app, CODER)),
            drawn(app.query_one("#fleet-title", Static)),
            app.ui_shown.bell,
        )

    row, title, bell = drive(go, headroom=session_use((1, 42.0)))
    assert "#1" not in row and title.strip() == "rlx" and bell is False


def test_the_fade_and_the_bell_follow_their_switches(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    one_coder(tmp_path, script)
    rang: list[int] = []
    monkeypatch.setattr(App, "bell", lambda self: rang.append(1))

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        await open_view(pilot, app, CODER)  # the pane has the keyboard: the sidebar fades
        panes = app.query_one(Panes)

        def faded() -> bool:
            return app.sidebar.has_class("-unfocused")

        seen: dict[str, object] = {"faded": faded()}
        app.bell()
        _display(app, "fade_unfocused", False)
        _display(app, "bell", False)
        await settle(app)
        panes.mark_focus()
        seen["off"] = faded()
        app.bell()
        seen["rang"] = len(rang)
        _display(app, "fade_unfocused", True)
        await settle(app)
        seen["back"] = faded()
        return seen

    seen = drive(go)
    assert seen == {"faded": True, "off": False, "rang": 1, "back": True}


def test_the_board_opens_following_its_feed_as_display_says() -> None:
    assert BoardPanel(None).autoscroll is True
    settings_service.set_display_choice("board_autoscroll", False)
    assert BoardPanel(None).autoscroll is False


def test_agent_marks_off_draws_a_mixed_fleets_rows_without_them(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    claude = status("prj_a", "coder-auth", "coder", "working")
    codex_row = status("prj_a", "coder-codex", "coder", "working")
    codex = codex_row.model_copy(
        update={"agent": codex_row.agent.model_copy(update={"agent": "codex"})}
    )
    script["prj_a"] = [claude, codex]

    async def go(pilot: Pilot[None]) -> tuple[bool, bool, str, str]:
        app = fleet_app(pilot)
        await settle(app)
        marked, row = app.sidebar.agents_marked, drawn(row_for(app, CODER))
        _display(app, "show_agent_marks", False)
        await settle(app)
        return marked, app.sidebar.agents_marked, row, drawn(row_for(app, CODER))

    marked, after, with_mark, without = drive(go)
    assert (marked, after) == (True, False)
    assert "✻" in with_mark and "✻" not in without
