"""The sidebar's actions block: one *Start captain* and one *Spawn agent*, no spawn row per project.

The owner found a spawn row under every project card annoying (tsk_01m5v6mvbqphwq8jtdw9sbc3he).
The block under the Fleet header holds the captain's slot — *▶ Start captain*, or the captain's
own row while one is live — and *Spawn agent*, whose dialog asks the project. Driven with the
UI suite's harness: the real store in the isolated home, ``list_agents`` scripted, every tmux call
held to a private socket. Each claim reads what a widget shows or what the dialog opened on.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.cells import cell_len
from textual.pilot import Pilot
from textual.widgets import Select, Static

from aisquare.cli.ui import sidebar as sidebar_module
from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.sidebar import (
    ActionsSection,
    ProjectCard,
    SpawnButton,
    StartCaptain,
)
from aisquare.cli.ui.spawn import SpawnDialog
from aisquare.cli.ui.views.captain import CaptainView
from aisquare.core import experimental
from aisquare.core.store import store_session
from aisquare.services import project_groups
from aisquare.services.captain import state as captain_state
from tests import test_ui_shell as ui_suite
from tests.test_captain_sidebar import agent_opened, quiet, until
from tests.test_captain_view import _captain
from tests.test_ui_shell import (
    Script,
    card_for,
    composited,
    drive,
    fleet_app,
    refreshed,
    row_for,
    seed,
    settle,
    shown,
    status,
)

# The UI suite's fixtures, bound here so pytest finds them for this module's tests
# (``no_real_tmux`` is autouse there: every tmux call held to a private socket).
no_real_tmux = ui_suite.no_real_tmux
script = ui_suite.script

SPAWN_AGENT = "\uff0b Spawn agent"
"""The button's label: the fullwidth plus of the plan's mockup, visibly not the header's +."""

THREE = (
    ("prj_a", "alpha", "amber-otter"),
    ("prj_b", "beta", "ruby-fox"),
    ("prj_c", "gamma", "jade-lynx"),
)


def walk(app: FleetApp) -> list[str]:
    """The keys the cursor visits, top to bottom: what ↑/↓ walk over, as the sidebar sees them."""
    return [row.selection_key for row in app.sidebar._rows()]


def top(app: FleetApp) -> list[str]:
    """The keys above the first project: the header's ``+`` and the actions block."""
    keys = walk(app)
    return keys[: next(i for i, key in enumerate(keys) if key.startswith("project:"))]


def actions_lines(app: FleetApp) -> list[str]:
    """What the eye gets of the actions block, one string per row, at the sidebar's width."""
    block = app.query_one(ActionsSection)
    rows = [row for row in block.query("Activatable, #captain-notice") if row.display]
    return [composited(row).rstrip() for row in rows]  # type: ignore[arg-type]


# --- no spawn row under any project ---------------------------------------------------------


def test_no_project_card_has_a_spawn_row_and_no_key_for_one_is_left(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, *THREE)
    script["prj_a"] = [status("prj_a", "manager", "manager", "waiting")]

    async def body(pilot: Pilot[None]) -> tuple[list[int], list[str]]:
        app = fleet_app(pilot)
        await refreshed(app)
        cards = list(app.query(ProjectCard))
        assert len(cards) == 3
        spawn_rows = [len(card.query(".spawn-row")) for card in cards]
        for card in cards:  # nothing in a card reads as an action of its own
            rows = [r for r in card.query("Activatable") if "spawn" in shown(r).lower()]  # type: ignore[arg-type]
            spawn_rows.append(len(rows))
        return spawn_rows, [key for key in walk(app) if key.startswith("spawn:")]

    rows, keys = drive(body)
    assert rows == [0] * 6 and keys == []
    assert not hasattr(sidebar_module, "SpawnRow")


# --- the block in its three states ----------------------------------------------------------


def test_captain_off_the_block_is_spawn_agent_alone(
    tmp_path: Path, script: Script, captain_off: None
) -> None:
    seed(tmp_path, *THREE)

    async def body(pilot: Pilot[None]) -> tuple[list[str | None], list[str], list[str]]:
        app = fleet_app(pilot)
        await refreshed(app)
        ids = [child.id for child in app.query_one(ActionsSection).children]
        return ids, top(app), actions_lines(app)

    ids, keys, lines = drive(body)
    assert ids == ["spawn-agent"] and keys == ["add", "spawn-agent"]
    assert lines == [SPAWN_AGENT]


@pytest.mark.needs_captain
def test_captain_on_and_none_live_the_block_is_start_captain_over_spawn_agent(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, *THREE)

    async def body(pilot: Pilot[None]) -> tuple[list[str | None], list[str], list[str]]:
        app = fleet_app(pilot)
        await refreshed(app)
        ids = [child.id for child in app.query_one(ActionsSection).children]
        return ids, top(app), actions_lines(app)

    ids, keys, lines = drive(body)
    assert ids == ["captain-section", "spawn-agent"]
    assert keys == ["add", "start-captain", "spawn-agent"]
    assert lines == ["▶  Start captain", SPAWN_AGENT]


@pytest.mark.needs_captain
def test_captain_live_its_own_row_takes_the_place_of_start_captain(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, *THREE)
    captain = _captain("working")
    script[captain_state.home_project().id] = [captain]

    async def body(pilot: Pilot[None]) -> tuple[list[str], list[str]]:
        app = fleet_app(pilot)
        await refreshed(app)
        await pilot.pause()
        return top(app), actions_lines(app)

    keys, lines = drive(body)
    assert keys == ["add", f"agent:{captain.agent.id}", "spawn-agent"]
    assert len(lines) == 2
    assert lines[0].startswith("🤖 captain ") and "▶" in lines[0]  # the captain, plainly
    assert lines[1] == SPAWN_AGENT


@pytest.mark.needs_captain
def test_an_exited_captains_row_stays_with_its_exit_status_and_a_start_under_it(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, *THREE)
    captain = _captain("exited")
    script[captain_state.home_project().id] = [captain]

    async def body(pilot: Pilot[None]) -> tuple[list[str], list[str]]:
        app = fleet_app(pilot)
        await refreshed(app)
        await pilot.pause()
        return top(app), actions_lines(app)

    keys, lines = drive(body)
    assert keys == ["add", f"agent:{captain.agent.id}", "start-captain", "spawn-agent"]
    assert lines[0].startswith("🤖 captain") and "exited" in lines[0]
    assert lines[1:] == ["▶  Start captain", SPAWN_AGENT]


@pytest.mark.needs_captain
def test_the_header_has_no_star_and_nothing_is_headed_captain(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, *THREE)
    script[captain_state.home_project().id] = [_captain("working")]

    async def body(pilot: Pilot[None]) -> tuple[list[str | None], list[str]]:
        app = fleet_app(pilot)
        await refreshed(app)
        header = app.query_one("#fleet-header")
        statics = [shown(s) for s in app.sidebar.query(Static)]
        return [child.id for child in header.children], statics

    header, statics = drive(body)
    assert header == ["fleet-title", "add-project"]
    assert "Captain" not in statics and not any("★" in text for text in statics)


@pytest.mark.parametrize("width", [30, 24])
@pytest.mark.needs_captain
def test_the_actions_fit_the_sidebar_at_its_default_and_minimum_widths(
    tmp_path: Path, script: Script, width: int
) -> None:
    """Stacked, one line each: side by side the two labels did not fit in 30 columns."""
    seed(tmp_path, *THREE)

    async def body(pilot: Pilot[None]) -> list[str]:
        app = fleet_app(pilot)
        app.query_one("#sidebar").styles.width = width
        await refreshed(app)
        await pilot.pause()
        return actions_lines(app)

    assert drive(body) == ["▶  Start captain", SPAWN_AGENT]


@pytest.mark.parametrize("width", [30, 24])
@pytest.mark.parametrize(
    "state",
    [
        "off",
        pytest.param("none", marks=pytest.mark.needs_captain),
        pytest.param("live", marks=pytest.mark.needs_captain),
        pytest.param("exited", marks=pytest.mark.needs_captain),
    ],
)
def test_every_label_in_the_block_starts_in_one_column_by_measured_cell_width(
    tmp_path: Path,
    script: Script,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    width: int,
) -> None:
    """``▶`` is one cell, the fullwidth plus and the captain row's emoji two (rich ``cell_len``);
    ``▶`` is East-Asian-ambiguous, so it is padded by its measured width, not assumed to be one.
    Whatever the state and the width, *Start captain*, *Spawn agent* and the captain's own row
    put their label at the same cell, and show it whole."""
    seed(tmp_path, *THREE)
    if state == "off":
        monkeypatch.delenv(experimental.CAPTAIN_ENV, raising=False)
    elif state != "none":
        script[captain_state.home_project().id] = [
            _captain("working" if state == "live" else state)
        ]
    labels = {"live": ["captain", "Spawn agent"], "none": ["Start captain", "Spawn agent"]}
    expected = labels.get(
        state,
        ["Spawn agent"] if state == "off" else ["captain", "Start captain", "Spawn agent"],
    )

    async def body(pilot: Pilot[None]) -> tuple[int, list[tuple[str, int]]]:
        app = fleet_app(pilot)
        app.query_one("#sidebar").styles.width = width
        await refreshed(app)
        await pilot.pause()
        await pilot.pause()
        columns: list[tuple[str, int]] = []
        for line in actions_lines(app):
            label = max(
                (name for name in expected if name in line), key=len
            )  # not 'captain' in 'Start captain'
            columns.append((label, cell_len(line[: line.index(label)])))
        return app.sidebar.size.width, columns

    got_width, columns = drive(body)
    assert got_width == width
    assert [label for label, _ in columns] == expected  # each row shows its label whole
    assert {column for _, column in columns} == {3}, columns  # icon (2 cells) + a space


@pytest.mark.needs_captain
def test_the_switch_in_settings_mounts_and_removes_the_captains_slot_first_in_the_block(
    tmp_path: Path, script: Script, captain_off: None
) -> None:
    seed(tmp_path, *THREE)

    async def body(pilot: Pilot[None]) -> list[list[str | None]]:
        app = fleet_app(pilot)
        block = app.query_one(ActionsSection)
        seen = [[child.id for child in block.children]]
        await app.sidebar.set_captain(True)
        seen.append([child.id for child in block.children])
        await app.sidebar.set_captain(False)
        seen.append([child.id for child in block.children])
        return seen

    assert drive(body) == [
        ["spawn-agent"],
        ["captain-section", "spawn-agent"],
        ["spawn-agent"],
    ]


# --- they act ---------------------------------------------------------------------------------


@pytest.mark.needs_captain
def test_both_actions_work_from_the_keyboard_and_the_mouse(tmp_path: Path, script: Script) -> None:
    seed(tmp_path, *THREE)

    async def body(pilot: Pilot[None]) -> list[str]:
        app = fleet_app(pilot)
        await refreshed(app)
        opened: list[str] = []
        app.sidebar.focus()
        await pilot.pause()
        for key in ("down", "down", "enter"):  # add → start-captain → Enter
            await pilot.press(key)
            await settle(app)
        assert isinstance(app.screen, SpawnDialog)
        opened.append(f"{app.screen._role} {bool(app.screen.query('#spawn-project'))}")
        await pilot.press("escape")
        await settle(app)
        for key in ("down", "enter"):  # spawn-agent → Enter
            await pilot.press(key)
            await settle(app)
        assert isinstance(app.screen, SpawnDialog)
        opened.append(f"{app.screen._role} {bool(app.screen.query('#spawn-project'))}")
        await pilot.press("escape")
        await settle(app)
        await pilot.click(app.query_one(StartCaptain))
        await pilot.pause()
        assert isinstance(app.screen, SpawnDialog)
        opened.append(f"{app.screen._role} {bool(app.screen.query('#spawn-project'))}")
        await pilot.press("escape")
        await settle(app)
        await pilot.click(app.query_one(SpawnButton))
        await pilot.pause()
        assert isinstance(app.screen, SpawnDialog)
        opened.append(f"{app.screen._role} {bool(app.screen.query('#spawn-project'))}")
        return opened

    assert drive(body) == ["captain False", "coder True", "captain False", "coder True"]


@pytest.mark.needs_captain
def test_the_live_captains_row_opens_the_captain_view_from_the_keyboard(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, *THREE)
    captain = _captain("working")
    script[captain_state.home_project().id] = [captain]

    async def body(pilot: Pilot[None]) -> bool:
        app = fleet_app(pilot)
        await refreshed(app)
        await pilot.pause()
        app.sidebar.focus()
        await pilot.pause()
        for key in ("down", "down", "enter"):  # add → the captain's row → Enter
            await pilot.press(key)
            await settle(app)
        await until(pilot, agent_opened(pilot, captain.agent.id), what="the captain view open")
        opened = isinstance(app.current_view(), CaptainView)
        await quiet(pilot)
        return opened

    assert drive(body) is True


# --- Spawn agent opens on the sidebar's selection ----------------------------------------------


def dialog_project(app: FleetApp) -> tuple[str, str, list[str]]:
    """The project the open dialog is on, its Project field's value and offers."""
    dialog = app.screen
    assert isinstance(dialog, SpawnDialog), type(dialog).__name__
    field = dialog.query_one("#spawn-project", Select)
    offered = [str(value) for _, value in field._options if isinstance(value, str)]
    return dialog.project.id, str(field.value), offered


def test_spawn_agent_opens_on_the_selected_card_the_selected_agents_project_else_the_first(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, *THREE)
    script["prj_b"] = [status("prj_b", "coder-1", "coder", "working")]

    async def open_spawn(pilot: Pilot[None]) -> tuple[str, str, list[str]]:
        app = fleet_app(pilot)
        await pilot.click(app.query_one(SpawnButton))
        await pilot.pause()
        seen = dialog_project(app)
        await pilot.press("escape")
        await settle(app)
        return seen

    async def body(pilot: Pilot[None]) -> list[tuple[str, str, list[str]]]:
        app = fleet_app(pilot)
        await refreshed(app)
        seen = [await open_spawn(pilot)]  # nothing selected: the first
        await pilot.click(card_for(app, "prj_c").query_one(sidebar_module.ProjectTitle))
        await settle(app)
        seen.append(await open_spawn(pilot))  # a card selected: its project
        row_for(app, "agt_b_coder-1").activate()
        await until(pilot, agent_opened(pilot, "agt_b_coder-1"), what="the coder opened")
        seen.append(await open_spawn(pilot))  # an agent's row: its project
        await quiet(pilot)
        return seen

    first, card, agent = drive(body)
    order = ["prj_a", "prj_b", "prj_c"]
    assert first == ("prj_a", "prj_a", order)
    assert card == ("prj_c", "prj_c", order)
    assert agent == ("prj_b", "prj_b", order)


@pytest.mark.needs_captain
def test_with_the_captain_selected_spawn_agent_opens_on_the_first_project(
    tmp_path: Path, script: Script
) -> None:
    """Its home board takes no spawns, and is not in the Project field either."""
    seed(tmp_path, *THREE)
    captain = _captain("working")
    home = captain_state.home_project()
    script[home.id] = [captain]

    async def body(pilot: Pilot[None]) -> tuple[str, str, list[str]]:
        app = fleet_app(pilot)
        await refreshed(app)
        await pilot.pause()
        row_for(app, captain.agent.id).activate()
        await until(pilot, agent_opened(pilot, captain.agent.id), what="the captain selected")
        await pilot.click(app.query_one(SpawnButton))
        await pilot.pause()
        seen = dialog_project(app)
        await pilot.press("escape")
        await quiet(pilot)
        return seen

    project, field, offered = drive(body)
    assert (project, field) == ("prj_a", "prj_a")
    assert offered == ["prj_a", "prj_b", "prj_c"] and home.id not in offered


def test_the_project_field_lists_the_projects_in_the_order_the_sidebar_shows_them(
    tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, *THREE)
    with store_session() as store:
        project_groups.pin(store, "prj_c")  # Pinned comes first in the sidebar

    async def body(pilot: Pilot[None]) -> tuple[list[str], list[str], str]:
        app = fleet_app(pilot)
        await refreshed(app)
        cards = [card.project.id for card in app.query(ProjectCard)]
        await pilot.click(app.query_one(SpawnButton))
        await pilot.pause()
        project, _field, offered = dialog_project(app)
        await pilot.press("escape")
        await settle(app)
        return cards, offered, project

    cards, offered, project = drive(body)
    assert cards[0] == "prj_c" and offered == cards  # the cards' order is the field's
    assert project == "prj_c"  # nothing selected: the first listed, which is the pinned one


def test_with_no_projects_spawn_agent_stays_and_says_so_instead_of_opening_a_dialog(
    tmp_path: Path, script: Script
) -> None:
    async def body(pilot: Pilot[None]) -> tuple[bool, str, list[str]]:
        app = fleet_app(pilot)
        await refreshed(app)
        await pilot.click(app.query_one(SpawnButton))
        await pilot.pause()
        return (
            bool(app.query(SpawnButton)),
            type(app.screen).__name__,
            [str(note.message) for note in app._notifications],
        )

    present, screen, toasts = drive(body, notifications=True)
    assert present and screen != "SpawnDialog"
    assert toasts == ["No projects yet — press + to onboard one"]


@pytest.mark.needs_captain
def test_the_cursor_target_of_an_action_row_is_no_project(tmp_path: Path, script: Script) -> None:
    """``p``, ``m``, ``g`` and the rest act on a card; on the actions they do nothing."""
    seed(tmp_path, *THREE)

    async def body(pilot: Pilot[None]) -> list[tuple[str | None, object]]:
        app = fleet_app(pilot)
        await refreshed(app)
        app.sidebar.focus()
        seen: list[tuple[str | None, object]] = []
        for key in ("down", "down", "down"):  # add → start-captain → spawn-agent
            await pilot.press(key)
            await settle(app)
            seen.append((app.sidebar._cursor_key, app.sidebar._cursor_target()))
        return seen

    assert drive(body) == [("add", None), ("start-captain", None), ("spawn-agent", None)]
