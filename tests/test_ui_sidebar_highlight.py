"""The sidebar's ONE highlight: a click and the arrows move the same one (task #77).

There were two: a click set ``.selected`` and left the keyboard's ``.cursor`` on
another row, and the arrows moved ``.cursor`` while ``.selected`` stayed put — two
rows lit, and ↓ after a click went on from wherever the arrows had last been. Now a
click puts the highlight on its row and opens it, and ↑/↓ move that highlight and
open the row they land on, as a click on it would, the keyboard staying in the
sidebar. A row that acts rather than shows (``+``, *Spawn agent*, a group header) is
only highlighted by the arrows: Enter acts.

Every test pins the layout the UI ships (``captain_off``: no captain slot above
*Spawn agent*), so the rows' walk reads as the user sees it.

Driven through the shell suite's harness: a scripted fleet, every tmux call held to
a private socket.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.css.query import NoMatches
from textual.pilot import Pilot
from textual.widgets import Button

from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.sidebar import Disclosure, ProjectCard, ProjectTitle, StatusLine
from aisquare.cli.ui.spawn import SpawnDialog
from aisquare.cli.ui.stop import StopAgentScreen
from aisquare.core.store import store_session
from aisquare.services import project_groups as groups_service
from tests import test_ui_shell as ui_suite
from tests.test_ui_shell import (
    Script,
    card_for,
    drive,
    fleet_app,
    highlighted,
    refreshed,
    row_for,
    seed,
    settle,
    status,
    stopper,
)

# The UI suite's fixtures, bound here so pytest finds them for this module's tests
# (``no_real_tmux`` is autouse there: every tmux call held to a private socket).
no_real_tmux = ui_suite.no_real_tmux
script = ui_suite.script


def two_projects(tmp_path: Path, script: Script) -> None:
    """alpha and beta, a manager each. The rows, top to bottom: ``add``, ``spawn-agent``,
    alpha's title, its manager, beta's two, ``settings``."""
    seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None))
    script["prj_a"] = [status("prj_a", "manager", "manager", "waiting")]
    script["prj_b"] = [status("prj_b", "manager", "manager", "waiting")]


def test_a_click_takes_the_one_highlight_and_the_arrows_go_on_from_the_clicked_row(
    tmp_path: Path, script: Script, captain_off: None
) -> None:
    """Acceptance 1. The arrows leave the highlight on beta's title first, so a second,
    keyboard-only highlight would still sit there after the click on alpha — and ↓
    would go on from beta."""
    two_projects(tmp_path, script)

    async def go(pilot: Pilot[None]) -> list[tuple[list[str], str]]:
        app = fleet_app(pilot)
        seen: list[tuple[list[str], str]] = []

        def look() -> None:
            seen.append((highlighted(app), str(app.content.current)))

        app.sidebar.focus()
        await pilot.pause()
        for _ in range(5):  # add, Spawn agent, alpha, its manager, beta
            await pilot.press("down")
            await settle(app)
        look()
        await pilot.click(card_for(app, "prj_a").query_one(ProjectTitle))
        await settle(app)
        look()
        await pilot.press("down")
        await settle(app)
        look()
        await pilot.click(row_for(app, "agt_b_manager"))
        await settle(app)
        look()
        assert type(app.focused).__name__ == "TerminalPane", "a click on an agent's row: its pane"
        app.sidebar.focus()  # the escape key's hop back, without the key
        await pilot.pause()
        await pilot.press("up")
        await settle(app)
        look()
        return seen

    assert drive(go) == [
        (["project:prj_b"], "project-prj_b"),
        (["project:prj_a"], "project-prj_a"),  # the click's row, and no other
        (["agent:agt_a_manager"], "agent-agt_a_manager"),  # ↓ from alpha, not from beta
        (["agent:agt_b_manager"], "agent-agt_b_manager"),
        (["project:prj_b"], "project-prj_b"),  # ↑ from the clicked agent row
    ]


def test_the_arrows_open_an_agent_without_its_pane_taking_the_keys_and_x_stops_it(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch, captain_off: None
) -> None:
    """Acceptance 2: the agent's view opens as the arrows land on its row, the keyboard
    stays in the sidebar, and ``x`` acts on that row — the highlighted one."""
    seed(tmp_path, ("prj_a", "alpha", None))
    live = status("prj_a", "coder-auth", "coder", "working")
    script["prj_a"] = [live]
    stopper(monkeypatch, live.agent)

    async def go(pilot: Pilot[None]) -> tuple[list[str], str, str, str | None]:
        app = fleet_app(pilot)
        app.sidebar.focus()
        await pilot.pause()
        for _ in range(4):  # add, Spawn agent, alpha, its agent
            await pilot.press("down")
            await settle(app)
        lit, current, focused = highlighted(app), str(app.content.current), type(app.focused)
        await pilot.press("x")
        await settle(app)
        screen = app.screen
        label = screen.label if isinstance(screen, StopAgentScreen) else None
        return lit, current, focused.__name__, label

    lit, current, focused, stopping = drive(go)
    assert lit == ["agent:agt_a_coder-auth"]
    assert current == "agent-agt_a_coder-auth", "the row the arrows landed on opened"
    assert focused == "Sidebar", "its pane did not take the keyboard: the next ↓ is the sidebar's"
    assert stopping == "coder-auth", "x asked to stop the highlighted agent"


def test_passing_over_add_spawn_and_a_group_header_only_highlights_them(
    tmp_path: Path, script: Script, captain_off: None
) -> None:
    """Acceptance 4. ``+`` (onboarding), *Spawn agent* (its dialog) and a group header (a
    fold) act; an arrow passing over one highlights it and does nothing else — no view
    changes, no dialog, nothing folds, there and back. Enter still acts, and a click on
    ``+`` still highlights nothing (the Onboard view is no row's)."""
    seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None))
    script["prj_b"] = [status("prj_b", "manager", "manager", "waiting")]
    with store_session() as store:
        group, _ = groups_service.create_group(store, "tools", ["prj_b"])

    async def go(
        pilot: Pilot[None],
    ) -> tuple[list[tuple[str, str, bool]], bool, str, tuple[list[str], str]]:
        app = fleet_app(pilot)
        app.sidebar.focus()
        await pilot.pause()
        base = app.screen
        walk: list[tuple[str, str, bool]] = []
        for key in ["down"] * 6 + ["up"] * 4:  # down to alpha, back up to Spawn agent
            await pilot.press(key)
            await settle(app)
            (lit,) = highlighted(app)
            walk.append((lit, str(app.content.current), app.screen is not base))
        with store_session() as store:
            stored = store.get_project_group(group.id)
        folded = stored is None or stored.collapsed
        await pilot.press("enter")  # on Spawn agent: Enter is its click
        await settle(app)
        dialog = type(app.screen).__name__
        app.pop_screen()
        await settle(app)
        await pilot.click("#add-project")
        await settle(app)
        return walk, folded, dialog, (highlighted(app), str(app.content.current))

    walk, folded, dialog, after_add = drive(go)
    # (the row lit, the view shown, whether a dialog is up)
    assert walk == [
        ("add", "welcome", False),  # highlighted; the Onboard view did not open
        ("spawn-agent", "welcome", False),  # highlighted; no Spawn dialog
        (f"group:{group.id}", "welcome", False),  # highlighted; nothing folded
        ("project:prj_b", "project-prj_b", False),
        ("agent:agt_b_manager", "agent-agt_b_manager", False),
        ("project:prj_a", "project-prj_a", False),
        ("agent:agt_b_manager", "agent-agt_b_manager", False),
        ("project:prj_b", "project-prj_b", False),
        (f"group:{group.id}", "project-prj_b", False),  # the view stays beta's
        ("spawn-agent", "project-prj_b", False),
    ]
    assert not folded, "passing over the group's header folded it"
    assert dialog == SpawnDialog.__name__, "Enter on Spawn agent opens its dialog"
    assert after_add == ([], "onboard"), "a click on + onboards and highlights no row"


def test_there_is_never_a_second_highlight_through_a_refresh_a_fold_and_put_cursor(
    tmp_path: Path, script: Script, captain_off: None
) -> None:
    """Acceptance 3 (and the card ⋯ of acceptance 5): one highlighted row at most —
    hidden in a folded card included — after a refresh, a fold and an unfold, and
    ``put_cursor`` (the ui receiver's ``focus_project``); the card whose title holds it
    is the one whose menu shows."""
    two_projects(tmp_path, script)

    async def go(pilot: Pilot[None]) -> list[tuple[list[str], list[str]]]:
        app = fleet_app(pilot)
        seen: list[tuple[list[str], list[str]]] = []

        def look() -> None:
            active = [
                card.project.id for card in app.query(ProjectCard) if card.has_class("-active")
            ]
            seen.append((highlighted(app), active))

        await pilot.click(card_for(app, "prj_a").query_one(ProjectTitle))
        await settle(app)
        look()
        await pilot.press("down")  # alpha's manager
        await settle(app)
        look()
        await refreshed(app)  # a frame: cards and rows reconciled in place
        look()
        await pilot.press("space")  # folds alpha's card, its manager's row with it
        await settle(app)
        look()
        await pilot.press("down")  # beta: the nearest row on screen
        await settle(app)
        look()
        await pilot.click(card_for(app, "prj_a").query_one(Disclosure))  # alpha unfolds
        await pilot.pause()
        look()
        assert app.sidebar.put_cursor("agent:agt_a_manager")
        await pilot.pause()
        look()
        await refreshed(app)
        look()
        return seen

    assert drive(go) == [
        (["project:prj_a"], ["prj_a"]),
        (["agent:agt_a_manager"], []),
        (["agent:agt_a_manager"], []),
        (["agent:agt_a_manager"], []),  # held by the hidden row, shown nowhere
        (["project:prj_b"], ["prj_b"]),
        (["project:prj_b"], ["prj_b"]),  # the unfolded row did not come back lit
        (["agent:agt_a_manager"], []),
        (["agent:agt_a_manager"], []),
    ]


def test_an_answer_the_arrows_have_moved_past_opens_nothing_and_takes_nothing_back(
    tmp_path: Path, script: Script, captain_off: None
) -> None:
    """Keys outrun the shell: by the time a row's message is read, the highlight may be
    two rows on. That message must neither pull the highlight back nor build its view —
    an agent view attaches its pane. Driven without a pause between the moves, as a
    held key or a click and a quick ↓ deliver them."""
    seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None))
    script["prj_a"] = [
        status("prj_a", "coder-1", "coder", "working"),
        status("prj_a", "coder-2", "coder", "working", minute=1),
    ]
    script["prj_b"] = [status("prj_b", "manager", "manager", "waiting")]

    def built(app: FleetApp, view_id: str) -> bool:
        try:
            app.content.get_child_by_id(view_id)
        except NoMatches:
            return False
        return True

    async def go(
        pilot: Pilot[None],
    ) -> tuple[list[str], tuple[list[str], str, str], list[bool], list[str], str]:
        app = fleet_app(pilot)
        rows = [row.selection_key for row in app.sidebar._rows()]
        await pilot.click(card_for(app, "prj_a").query_one(ProjectTitle))
        await settle(app)
        app.sidebar.action_cursor_down()  # alpha's first agent…
        app.sidebar.action_cursor_down()  # …and on to the next before the shell reads either
        await settle(app)
        held = (highlighted(app), str(app.content.current), type(app.focused).__name__)
        passed = [built(app, view) for view in ("agent-agt_a_coder-1", "agent-agt_a_coder-2")]
        # A click, and an arrow before its message is read: ↓ goes on from the clicked row.
        card_for(app, "prj_b").query_one(ProjectTitle).activate()
        app.sidebar.action_cursor_down()
        await settle(app)
        return rows, held, passed, highlighted(app), str(app.content.current)

    rows, held, passed, lit, current = drive(go)
    assert rows[2:5] == ["project:prj_a", "agent:agt_a_coder-1", "agent:agt_a_coder-2"], rows
    assert held == (["agent:agt_a_coder-2"], "agent-agt_a_coder-2", "Sidebar")
    assert passed == [False, True], "the row passed over built no view"
    assert (lit, current) == (["agent:agt_b_manager"], "agent-agt_b_manager")


def test_a_click_while_another_rows_view_is_being_built_is_not_lost(
    tmp_path: Path, script: Script, captain_off: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Building a view awaits its mount, and a row handles its click meanwhile: it takes the
    highlight and queues its own message. The first row's answer, resuming after the wait,
    must not select its row again — that pulled the highlight back, and the clicked row's
    message then found it gone and opened nothing. The click is made from inside the build,
    so the interleaving is the same on every run."""
    seed(tmp_path, ("prj_a", "alpha", None), ("prj_b", "beta", None))
    script["prj_a"] = [status("prj_a", "coder-1", "coder", "working")]
    real_show = FleetApp._show

    async def show_then_click(app: FleetApp, view_id: str, factory: Any = None) -> None:
        await real_show(app, view_id, factory)
        if view_id == "agent-agt_a_coder-1":
            card_for(app, "prj_b").query_one(ProjectTitle).activate()  # beta clicked meanwhile

    monkeypatch.setattr(FleetApp, "_show", show_then_click)

    async def go(pilot: Pilot[None]) -> tuple[list[str], str, str, str | None]:
        app = fleet_app(pilot)
        row_for(app, "agt_a_coder-1").activate()  # a click on alpha's agent
        await settle(app)
        with store_session() as store:
            remembered = store.ui_state("fleet.selected")
        return highlighted(app), str(app.content.current), type(app.focused).__name__, remembered

    lit, current, focused, remembered = drive(go)
    assert (lit, current) == (["project:prj_b"], "project-prj_b"), "the later click won"
    assert remembered == "project:prj_b"
    assert focused != "TerminalPane", "the agent's pane did not take the keys after the click"


def test_the_arrows_open_settings_without_handing_the_hub_the_keys_and_enter_does(
    tmp_path: Path, script: Script, captain_off: None
) -> None:
    """*⚙ Settings* is a view row: the arrows open its hub and keep the keyboard (the hub
    takes it on Enter, or a click, as before). Pressed again at the bottom of the list
    the highlight stays and nothing reopens — the Accounts page under *⚙ Settings* is
    not swapped back for the hub."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def go(pilot: Pilot[None]) -> list[tuple[list[str], str, str]]:
        app = fleet_app(pilot)
        seen: list[tuple[list[str], str, str]] = []

        def look() -> None:
            seen.append((highlighted(app), str(app.content.current), type(app.focused).__name__))

        app.sidebar.focus()
        await pilot.pause()
        for _ in range(4):  # add, Spawn agent, alpha, Settings
            await pilot.press("down")
            await settle(app)
        look()
        await pilot.click(app.query_one(StatusLine))  # the Accounts page, Settings still lit
        await settle(app)
        app.sidebar.focus()
        await pilot.pause()
        await pilot.press("down")  # the last row already
        await settle(app)
        look()
        await pilot.press("enter")
        await settle(app)
        look()
        return seen

    assert drive(go) == [
        (["settings"], "settings-hub", "Sidebar"),
        (["settings"], "accounts", "Sidebar"),
        (["settings"], "settings-hub", Button.__name__),
    ]
