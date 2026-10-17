"""Project cards: the name a person gave the project, renamed in place, and the card's ⋯ menu.

The human's words: "if I rename from left pane no need to redirect to settings pane,
instead just allow me to type the new name and hit enter … or clicking outside to save";
"the codename is no longer rendered"; "an easy tui way to remove an onboarded project …
a drop down or settings menu … for showing things like rename and delete".

Driven as ``test_ui_shell.py`` drives the shell — the real store in the isolated home,
``list_agents`` scripted — and its helpers are reused. Every click goes through the app
as the terminal sends it (``_as_the_terminal_sends``): that is where a release becomes a
Click and where two at one spot become a double-click.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from rich.cells import cell_len
from textual import events
from textual.pilot import Pilot
from textual.widget import Widget
from textual.widgets import Button, OptionList, Static

from aisquare.cli.ui.app import FleetApp, HelpScreen
from aisquare.cli.ui.project_menu import MENU_WIDTH, ConfirmRemove, ProjectMenu
from aisquare.cli.ui.sidebar import (
    SETTINGS_ICON,
    CardChips,
    CardMenuButton,
    NicknameInput,
    ProjectCard,
    ProjectTitle,
    SettingsRow,
)
from aisquare.cli.ui.views.agent import AgentView
from aisquare.cli.ui.views.project import ProjectView
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo
from aisquare.services import install_route
from aisquare.services import project as project_service
from tests.test_ui_shell import (
    PRIVATE_SOCKET,
    Script,
    _as_the_terminal_sends,
    _click,
    _toasts,
    card_for,
    composited,
    drive,
    fleet_app,
    row_for,
    seed,
    settle,
    shown,
    status,
)
from tests.test_ui_shell import no_real_tmux as _no_real_tmux
from tests.test_ui_shell import script as _script

#: Re-exported so pytest collects them here (the guard is autouse there, and here).
no_real_tmux = _no_real_tmux
script = _script


@pytest.fixture
def wide_click_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two clicks at one cell chain within Textual's half-second window, and each event here
    is waited through the app before the next is sent: on a loaded machine the two halves
    of the rename's double-click outlasted it and read as two single clicks (once in a
    12-file UI run; every run with each mouse event held 300 ms). The window's length is not
    what these test, so it is wide enough for a loaded runner, as in ``test_ui_shell.py``:
    every two clicks at one cell chain, and the card's own rules alone decide a rename."""
    monkeypatch.setattr(FleetApp, "CLICK_CHAIN_TIME_THRESHOLD", 5.0)


def _stored(project_id: str) -> ProjectInfo | None:
    with store_session() as store:
        return store.get_project(project_id)


def _nickname(project_id: str, nickname: str) -> None:
    with store_session() as store:
        store.set_project_nickname(project_id, nickname)


def _title(app: FleetApp, project_id: str) -> str:
    return shown(card_for(app, project_id).query_one(ProjectTitle))


def _view_id(app: FleetApp) -> str | None:
    view = app.current_view()
    return view.id if view is not None else None


def _field(app: FleetApp, project_id: str) -> NicknameInput:
    return card_for(app, project_id).query_one(NicknameInput)


async def _double_click(pilot: Pilot[None], widget: ProjectTitle) -> None:
    """Two clicks at one spot, back to back, as the terminal reports them: Textual chains them."""
    for kind in (events.MouseDown, events.MouseUp, events.MouseDown, events.MouseUp):
        await _as_the_terminal_sends(pilot, kind, widget)
    await settle(fleet_app(pilot))


Gesture = Callable[[Pilot[None], ProjectTitle], Awaitable[None]]


async def _shift_click(pilot: Pilot[None], title: ProjectTitle) -> None:
    await _click(pilot, title, shift=True)


async def _right_click_and_esc(pilot: Pilot[None], title: ProjectTitle) -> None:
    app = fleet_app(pilot)
    for kind in (events.MouseDown, events.MouseUp):
        await _as_the_terminal_sends(pilot, kind, title, button=3)
    await settle(app)
    assert isinstance(app.screen, ProjectMenu)
    await pilot.press("escape")
    await settle(app)


async def _drag_away_and_back(pilot: Pilot[None], title: ProjectTitle) -> None:
    """Press, move onto the next card and back with the button held, release where it began."""
    beta = card_for(fleet_app(pilot), "prj_b").query_one(ProjectTitle)
    await _as_the_terminal_sends(pilot, events.MouseDown, title)
    await _as_the_terminal_sends(pilot, events.MouseMove, beta)
    await _as_the_terminal_sends(pilot, events.MouseMove, title)
    await _as_the_terminal_sends(pilot, events.MouseUp, title)
    await settle(fleet_app(pilot))


async def _menu(pilot: Pilot[None], choice: str) -> None:
    """Pick ``choice`` (its option id) in the menu on screen, by the keyboard."""
    app = fleet_app(pilot)
    assert isinstance(app.screen, ProjectMenu)
    options = app.screen.query_one(OptionList)
    options.highlighted = options.get_option_index(choice)
    await pilot.press("enter")
    await settle(app)


# --- the name on the card --------------------------------------------------------------------


def test_the_card_shows_the_nickname_and_never_the_codename(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, ("prj_a", "alpha", "amber-otter"), ("prj_b", "beta", "ruby-fox"))
    _nickname("prj_a", "Auth rewrite [wip] — día 2")
    script["prj_a"] = [status("prj_a", "manager", "manager", "working")]

    async def go(pilot: Pilot[None]) -> tuple[str, str, str]:
        app = fleet_app(pilot)
        chips = shown(card_for(app, "prj_a").query_one(CardChips))
        return _title(app, "prj_a"), chips, _title(app, "prj_b")

    named, chips, plain = drive(go)
    assert named == "🗂 Auth rewrite [wip] — día 2", "free text, brackets kept, not markup"
    assert plain == "🗂 beta", "no nickname: the folder's name"
    assert chips.strip() == "1"
    assert "amber-otter" not in named + chips and "ruby-fox" not in plain


def test_an_emoji_nickname_shows_whole_and_keeps_the_cards_columns(
    tmp_path: Path, script: Script
) -> None:
    """A compound emoji (joined by U+200D) is one character on screen, two cells wide, and
    Rich measures it so: the name is drawn whole and the chips and the menu stay in the
    column a plain card has them in."""
    seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None))
    name = "\U0001f469\u200d\U0001f4bb ops \U0001f3f3\ufe0f\u200d\U0001f308"
    _nickname("prj_a", name)
    for project_id in ("prj_a", "prj_b"):
        script[project_id] = [status(project_id, "manager", "manager", "working")]

    async def go(pilot: Pilot[None]) -> tuple[str, int, tuple[int, int], tuple[int, int]]:
        app = fleet_app(pilot)
        a, b = card_for(app, "prj_a"), card_for(app, "prj_b")
        title = a.query_one(ProjectTitle)
        return (
            composited(title),
            title.size.width,
            (a.query_one(CardChips).region.x, b.query_one(CardChips).region.x),
            (a.query_one(CardMenuButton).region.x, b.query_one(CardMenuButton).region.x),
        )

    drawn, width, chips, menu = drive(go)
    assert drawn.rstrip() == f"\U0001f5c2 {name}", "whole: no joiner dropped, nothing cut"
    assert cell_len(drawn) == width, "the row is as wide as the title, no wider"
    assert chips[0] == chips[1] and menu[0] == menu[1], "in the plain card's columns"


@pytest.mark.usefixtures("wide_click_chain")
def test_a_double_click_renames_in_place_and_enter_saves_at_once(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, ("prj_a", "alpha", "amber-otter"), ("prj_b", "beta", None))

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        title = card_for(app, "prj_a").query_one(ProjectTitle)
        order = [card.project.id for card in app.sidebar.query(ProjectCard)]
        await _double_click(pilot, title)
        field = _field(app, "prj_a")
        opened = {
            "open": field.display and not title.display,
            "value": field.value,
            "selected": field.selected_text,
            "focused": app.focused is field,
        }
        await pilot.press(*"Payments", "space", *"v2")
        await pilot.press("enter")
        await settle(app)  # no refresh tick runs in these tests (refresh_seconds=3600)
        return {
            **opened,
            "title": _title(app, "prj_a"),
            "closed": not field.display and title.display,
            "sidebar_focused": app.focused is app.sidebar,
            "undo": len(app._undo),
            "order": [card.project.id for card in app.sidebar.query(ProjectCard)] == order,
            "view": _view_id(app),
        }

    seen = drive(go)
    assert seen["open"] and seen["focused"]
    assert seen["value"] == seen["selected"] == "alpha", "prefilled with the name, all selected"
    assert seen["title"] == "🗂 Payments v2" and seen["closed"] and seen["sidebar_focused"]
    stored = _stored("prj_a")
    assert stored is not None and stored.nickname == "Payments v2"
    assert stored.codename == "amber-otter", "the codename is not touched"
    assert seen["undo"] == 0 and seen["order"], "a double-click is no drag: nothing moved"
    assert seen["view"] == "project-prj_a", "the first click of the two opened it"


@pytest.mark.usefixtures("wide_click_chain")
def test_a_hand_that_drifts_along_the_name_while_pressed_still_double_clicks(
    tmp_path: Path, script: Script
) -> None:
    """A press on the name is a drag only once the pointer leaves its row
    (``Sidebar.drag_over``): a hand that drifts a cell along the name with the button down
    still clicks, and two such clicks released at one cell are the double-click."""
    seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None))

    async def go(pilot: Pilot[None]) -> tuple[str | None, bool]:
        app = fleet_app(pilot)
        title = card_for(app, "prj_a").query_one(ProjectTitle)
        for pressed in ((1, 0), (3, 0)):
            await _as_the_terminal_sends(pilot, events.MouseDown, title, pressed)
            await _as_the_terminal_sends(pilot, events.MouseMove, title, (2, 0))
            await _as_the_terminal_sends(pilot, events.MouseUp, title, (2, 0))
        await settle(app)
        field = _field(app, "prj_a")
        return _view_id(app), field.display and app.focused is field

    view, renaming = drive(go)
    assert view == "project-prj_a", "the first click opened it"
    assert renaming, "the second, released at the same cell, renames"


@pytest.mark.usefixtures("wide_click_chain")
@pytest.mark.parametrize(
    "between",
    [_shift_click, _right_click_and_esc, _drag_away_and_back],
    ids=["shift+click", "right-click", "drag away and back"],
)
def test_only_a_click_after_a_plain_open_of_the_name_renames(
    tmp_path: Path, script: Script, between: Gesture
) -> None:
    """Textual chains any two clicks at one cell, whatever the button and whether the press
    was dragged: a mark, the menu or a drag dropped where it began, then a click, is a
    double-click to it. Only the click after a plain click that opened the project renames
    (``ProjectTitle._opened``). The window is wide, so the chain never breaks here: the
    click after that one renames, and it is the rule that held the one before."""
    seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None))

    async def go(pilot: Pilot[None]) -> tuple[bool, str | None, bool]:
        app = fleet_app(pilot)
        title = card_for(app, "prj_a").query_one(ProjectTitle)
        await _click(pilot, title)  # a plain open
        await between(pilot, title)  # chained to it, and no plain open
        await _click(pilot, title)  # chained again: an open, not a rename
        early = bool(_field(app, "prj_a").display)
        view = _view_id(app)
        await _click(pilot, title)  # after a plain open: the rename
        field = _field(app, "prj_a")
        return early, view, field.display and app.focused is field

    early, view, renaming = drive(go)
    assert not early, "the click after the gesture opened the project and renamed nothing"
    assert view == "project-prj_a"
    assert renaming, "the click after that plain open renames: the chain held throughout"


def test_leaving_the_field_saves_and_esc_cancels(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None))

    async def go(pilot: Pilot[None]) -> tuple[str, str | None, str, bool]:
        app = fleet_app(pilot)
        await _click(pilot, card_for(app, "prj_a").query_one(ProjectTitle))
        app.sidebar.focus()
        await pilot.press("f2")  # the card under the cursor (the one just selected)
        await pilot.press("backspace", *"docs")
        await _click(pilot, card_for(app, "prj_b").query_one(ProjectTitle))  # a click away
        left = _title(app, "prj_a")
        view = app.current_view()
        # Esc: the field closes and nothing is written.
        app.sidebar.focus()
        await pilot.press("f2")
        cancelled_field = _field(app, "prj_b")
        await pilot.press("backspace", *"nope", "escape")
        await settle(app)
        return (
            left,
            view.id if view is not None else None,
            _title(app, "prj_b"),
            (cancelled_field.display),
        )

    left, view, kept, still_open = drive(go)
    assert left == "🗂 docs" and view == "project-prj_b", "the click away saved, and opened beta"
    assert (_stored("prj_a") or ProjectInfo(id="x", root=Path("/"))).nickname == "docs"
    assert kept == "🗂 beta" and not still_open
    assert (_stored("prj_b") or ProjectInfo(id="x", root=Path("/"))).nickname is None


async def _pilot_click(pilot: Pilot[None], widget: Widget) -> None:
    """``Pilot``'s click: straight to the screen, past ``App.on_event`` (``_click`` is the
    terminal's) — the way the validator's probe pressed."""
    await pilot.click(widget, offset=(1, 0))
    await settle(fleet_app(pilot))


@pytest.mark.usefixtures("wide_click_chain")
@pytest.mark.parametrize("deliver", [_click, _pilot_click], ids=["terminal", "pilot"])
@pytest.mark.parametrize("page", ["manager tab", "settings hub"])
def test_a_press_on_a_page_body_that_takes_no_focus_saves_the_name(
    tmp_path: Path,
    script: Script,
    page: str,
    deliver: Callable[[Pilot[None], Widget], Awaitable[None]],
) -> None:
    """Leaving the field saves — its blur — but Textual moves the focus on a press only to
    a FOCUSABLE widget under it. A project with no manager yet shows a header and a button,
    and the double-click that renames opens that page first: a press on its header left
    the field open, unsaved and focused, and so did one on the Settings hub (validator of
    PR #261, MINOR 1). The press hands the focus to the sidebar, as Enter does, and saves."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> tuple[bool, str, bool, str, bool, bool, str | None]:
        app = fleet_app(pilot)
        if page == "settings hub":
            await _click(pilot, app.query_one(SettingsRow))
            app.sidebar.open_rename("prj_a")  # F2 on its card: the page stays the hub
            await settle(app)
            body: Static = app.query_one("#settings-title", Static)
        else:
            await _double_click(pilot, card_for(app, "prj_a").query_one(ProjectTitle))
            body = app.query_one("#manager-header", Static)
        field = _field(app, "prj_a")
        opened = bool(field.display) and app.focused is field
        view = _view_id(app)
        await pilot.press(*"docs")  # over the name, all of it selected
        x, y = body.region.x + 1, body.region.y  # where the press lands
        nothing_focusable = app.screen.get_focusable_widget_at(x, y) is None
        await deliver(pilot, body)
        return (
            opened,
            shown(body),
            nothing_focusable,
            _title(app, "prj_a"),
            bool(field.display),
            app.focused is app.sidebar,
            view if view == _view_id(app) else f"{view} -> {_view_id(app)}",
        )

    opened, said, nothing_focusable, title, still_open, on_sidebar, view = drive(go)
    assert opened, "the premise: the field is open and has the keys"
    assert ("has no manager yet" if page == "manager tab" else "Settings") in said
    assert nothing_focusable, "the premise: the press lands on nothing Textual would focus"
    assert title == "🗂 docs" and not still_open
    assert (_stored("prj_a") or ProjectInfo(id="x", root=Path("/"))).nickname == "docs"
    assert on_sidebar, "the keys go back to the sidebar, as after Enter"
    assert view == ("settings-hub" if page == "settings hub" else "project-prj_a"), view


def test_an_empty_name_is_the_folders_and_the_field_holds_255_characters(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    _nickname("prj_a", "Old name")

    async def go(pilot: Pilot[None]) -> tuple[str, int, int]:
        app = fleet_app(pilot)
        app.sidebar.open_rename("prj_a")
        await settle(app)
        await pilot.press("backspace", "enter")  # everything was selected: now empty
        await settle(app)
        cleared = _title(app, "prj_a")
        app.sidebar.open_rename("prj_a")
        await settle(app)
        field = _field(app, "prj_a")
        field.post_message(events.Paste("y" * 300))
        await settle(app)
        held = len(field.value)
        await pilot.press("enter")
        await settle(app)
        return cleared, held, field.max_length or 0

    cleared, held, limit = drive(go)
    assert cleared == "🗂 alpha"
    assert held == limit == project_service.NICKNAME_MAX == 255
    stored = _stored("prj_a")
    assert stored is not None and stored.nickname == "y" * 255


def test_a_name_the_store_refuses_reopens_the_field_with_what_was_typed(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))

    def refuse(project: ProjectInfo, nickname: str | None) -> ProjectInfo:
        raise ValueError("a nickname is one line: no newlines, tabs or other control characters")

    monkeypatch.setattr(project_service, "set_nickname", refuse)

    async def go(pilot: Pilot[None]) -> tuple[str, bool, str, str]:
        app = fleet_app(pilot)
        app.sidebar.open_rename("prj_a")
        await settle(app)
        await pilot.press(*"bad", "enter")
        toasts = await _toasts(app)
        field = _field(app, "prj_a")
        return toasts, field.display, field.value, _title(app, "prj_a")

    toasts, reopened, typed, title = drive(go, notifications=True)
    assert "not renamed: a nickname is one line" in toasts
    assert reopened and typed == "bad", "the field is back with what was typed, to correct"
    assert title == "🗂 alpha", "the card says the stored name, not the refused one"


# --- the menu --------------------------------------------------------------------------------


def test_the_menu_opens_from_its_button_the_dot_key_and_a_right_click(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None))

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        a, b = card_for(app, "prj_a"), card_for(app, "prj_b")
        hidden_before = not a.query_one(CardMenuButton).visible
        await _click(pilot, a.query_one(ProjectTitle))
        shown_on_selected = a.query_one(CardMenuButton).visible
        still_hidden = not b.query_one(CardMenuButton).visible
        opened: list[str] = []
        await pilot.click(a.query_one(CardMenuButton))
        await settle(app)
        screen = app.screen
        if isinstance(screen, ProjectMenu):
            opened.append(f"button:{screen.project.id}")
            options = screen.query_one(OptionList)
            labels = [
                str(options.get_option_at_index(i).prompt) for i in range(options.option_count)
            ]
            width = screen.query_one("#project-menu").outer_size.width
        await pilot.press("escape")
        app.sidebar.focus()
        await pilot.press("down", "down")  # past alpha's spawn row, onto beta's name
        await pilot.press("full_stop")
        await settle(app)
        if isinstance(app.screen, ProjectMenu):
            opened.append(f"dot:{app.screen.project.id}")
        await pilot.press("escape")
        await _as_the_terminal_sends(pilot, events.MouseDown, a.query_one(ProjectTitle), button=3)
        await _as_the_terminal_sends(pilot, events.MouseUp, a.query_one(ProjectTitle), button=3)
        await settle(app)
        if isinstance(app.screen, ProjectMenu):
            opened.append(f"right:{app.screen.project.id}")
        await pilot.press("escape")
        await settle(app)
        return {
            "hidden_before": hidden_before,
            "shown_on_selected": shown_on_selected,
            "still_hidden": still_hidden,
            "opened": opened,
            "labels": labels,
            "width": width,
            "view": _view_id(app),
        }

    seen = drive(go)
    assert seen["hidden_before"] and seen["shown_on_selected"] and seen["still_hidden"]
    assert seen["opened"] == ["button:prj_a", "dot:prj_b", "right:prj_a"]
    assert seen["labels"] == [
        "✎ Rename",
        "📌 Pin",
        f"{SETTINGS_ICON} Open settings",  # the sidebar's monotone gear, not the emoji
        "✗ Remove from fleet…",
    ]
    assert seen["width"] == MENU_WIDTH
    assert seen["view"] == "project-prj_a", "a right-click opens the menu, not the project"


def test_the_menu_renames_pins_and_opens_the_settings_tab(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None))

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        app.sidebar.open_menu("prj_b")
        await settle(app)
        await _menu(pilot, "rename")
        field = _field(app, "prj_b")
        renaming = field.display and app.focused is field
        await pilot.press(*"web", "enter")
        await settle(app)
        app.sidebar.open_menu("prj_b")
        await settle(app)
        await _menu(pilot, "pin")
        app.sidebar.open_menu("prj_b")
        await settle(app)
        assert isinstance(app.screen, ProjectMenu)
        unpin = str(app.screen.query_one(OptionList).get_option("pin").prompt)
        await _menu(pilot, "settings")
        view = app.current_view()
        tab = view.active if isinstance(view, ProjectView) else None
        return {"renaming": renaming, "unpin": unpin, "view": view and view.id, "tab": tab}

    seen = drive(go)
    assert seen["renaming"]
    stored = _stored("prj_b")
    assert stored is not None and stored.nickname == "web" and stored.pinned_at is not None
    assert seen["unpin"] == "📌 Unpin"
    assert (seen["view"], seen["tab"]) == ("project-prj_b", "tab-settings")


def test_remove_asks_first_then_forgets_the_project_and_its_views_go_with_it(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None), ("prj_c", "gamma", None))
    script["prj_b"] = [status("prj_b", "manager", "manager", "exited", exit_status=0)]

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        await _click(pilot, card_for(app, "prj_b").query_one(ProjectTitle))
        await _click(pilot, row_for(app, "agt_b_manager"))  # its agent's pane is open
        app.sidebar.open_menu("prj_b")
        await settle(app)
        await _menu(pilot, "remove")
        asked = isinstance(app.screen, ConfirmRemove)
        question = shown(app.screen.query_one("#confirm-remove-text", Static)) if asked else ""
        await pilot.press("enter")  # Cancel has the focus: an Enter too fast removes nothing
        await settle(app)
        kept = _stored("prj_b") is not None
        app.sidebar.open_menu("prj_b")
        await settle(app)
        await _menu(pilot, "remove")
        await pilot.click("#confirm-remove-yes")
        toasts = await _toasts(app)
        views = [child.id for child in app.content.children]
        return {
            "question": question,
            "kept": kept,
            "toasts": toasts,
            "cards": [card.project.id for card in app.sidebar.query(ProjectCard)],
            "views": views,
            "agent_views": [v for v in app.content.children if isinstance(v, AgentView)],
            "current": _view_id(app),
            "selected": app.sidebar.selected_key,
        }

    seen = drive(go, notifications=True)
    assert "Remove beta from the fleet?" in str(seen["question"])
    assert "Its folder and history are kept" in str(seen["question"])
    assert seen["kept"], "Cancel removed nothing"
    assert _stored("prj_b") is None, "forgotten — the registration hidden, history kept"
    with store_session() as store:
        tombstoned = store.list_projects(include_forgotten=True)
    assert "prj_b" in [p.id for p in tombstoned], "a forget, not a purge"
    assert "beta removed from the fleet" in str(seen["toasts"])
    assert seen["cards"] == ["prj_a", "prj_c"]
    assert "project-prj_b" not in str(seen["views"]) and "project-prj_c" in str(seen["views"])
    assert seen["agent_views"] == []
    assert (seen["current"], seen["selected"]) == ("project-prj_c", "project:prj_c"), (
        "what was open on it moves to the next project in the list"
    )


def test_remove_names_the_command_that_brings_it_back_quoted(
    tmp_path: Path, script: Script
) -> None:
    """The way back is a command to copy: a root with a space is one argument in it, as in
    the toast after the removal (``install_route.command_line``: this shell's quotes)."""
    (project,) = seed(tmp_path, ("prj_a", "my app", None))

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        app.sidebar.open_menu("prj_a")
        await settle(app)
        await _menu(pilot, "remove")
        assert isinstance(app.screen, ConfirmRemove)
        return shown(app.screen.query_one("#confirm-remove-text", Static))

    question = drive(go)
    onboard = install_route.command_line(["aisquare", "project", "onboard", str(project.root)])
    assert f"{onboard} brings it back" in question
    assert "my app'" in question, "quoted: the space is inside the argument"


def test_removing_the_last_project_shows_welcome(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> tuple[str | None, str | None]:
        app = fleet_app(pilot)
        await _click(pilot, card_for(app, "prj_a").query_one(ProjectTitle))
        app.sidebar.open_menu("prj_a")
        await settle(app)
        await _menu(pilot, "remove")
        await pilot.click("#confirm-remove-yes")
        await settle(app)
        view = app.current_view()
        return (view.id if view is not None else None), app.sidebar.selected_key

    assert drive(go) == ("welcome", None)


def test_remove_is_refused_while_the_project_has_live_agents(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, ("prj_a", "alpha", "amber-otter"))
    with store_session() as store:
        store.upsert_fleet_agent(
            FleetAgent(
                id="agt_live",
                project_id="prj_a",
                label="coder-auth",
                role="coder",
                pane_id="%9",
                tmux_socket=PRIVATE_SOCKET,
                cwd=tmp_path / "alpha",
                created_at=datetime(2026, 10, 1, tzinfo=UTC),
            )
        )

    async def go(pilot: Pilot[None]) -> tuple[str, list[str]]:
        app = fleet_app(pilot)
        app.sidebar.open_menu("prj_a")
        await settle(app)
        await _menu(pilot, "remove")
        assert isinstance(app.screen, ConfirmRemove)
        app.screen.query_one("#confirm-remove-yes", Button).press()
        toasts = await _toasts(app)
        return toasts, [card.project.id for card in app.sidebar.query(ProjectCard)]

    toasts, cards = drive(go, notifications=True)
    assert "coder-auth" in toasts and "fleet shutdown" in toasts, toasts
    assert cards == ["prj_a"] and _stored("prj_a") is not None


def test_the_help_screen_names_the_rename_and_the_menu(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        app.sidebar.focus()
        await pilot.press("question_mark")
        await settle(app)
        assert isinstance(app.screen, HelpScreen)
        return shown(app.screen.query_one(Static))

    keys = drive(go)
    assert "F2 dblclick" in keys and "rename the card in place" in keys
    assert ". ⋯ r-click" in keys and "remove from the fleet" in keys
