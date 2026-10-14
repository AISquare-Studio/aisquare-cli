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

from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual import events
from textual.pilot import Pilot
from textual.widgets import Button, OptionList, Static

from aisquare.cli.ui.app import FleetApp, HelpScreen
from aisquare.cli.ui.project_menu import ConfirmRemove, ProjectMenu
from aisquare.cli.ui.sidebar import (
    CardChips,
    CardMenuButton,
    NicknameInput,
    ProjectCard,
    ProjectTitle,
)
from aisquare.cli.ui.views.agent import AgentView
from aisquare.cli.ui.views.project import ProjectView
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo
from aisquare.services import project as project_service
from tests.test_ui_shell import (
    PRIVATE_SOCKET,
    Script,
    _as_the_terminal_sends,
    _click,
    _toasts,
    card_for,
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
            "view": _view_id(app),
        }

    seen = drive(go)
    assert seen["hidden_before"] and seen["shown_on_selected"] and seen["still_hidden"]
    assert seen["opened"] == ["button:prj_a", "dot:prj_b", "right:prj_a"]
    assert seen["labels"] == ["✎ Rename", "📌 Pin", "⚙ Open settings", "✗ Remove from fleet…"]
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
