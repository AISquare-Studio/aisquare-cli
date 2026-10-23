"""The captain from Settings (#33): the live *Captain* box, *Start captain*, the message box.

On a fresh install the owner could not find how to turn the captain on (it took ``aisquare
config set experimental.captain true`` and a restart) or how to talk to it. Settings' box
switches it at once, *▶ Start captain* in its section opens what the ``★`` opens when there
is no captain, and the captain's view has a one-line box that types by What's up's path.
Driven headless through ``tests.test_ui_shell``'s ``drive``; every claim reads what a widget
shows or what the one door typed.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Checkbox, Input, Static

from aisquare.cli.ui.receiver import ui_receiver
from aisquare.cli.ui.sidebar import (
    CAPTAIN_READING,
    AgentRow,
    CaptainButton,
    CaptainSection,
    SettingsRow,
    StartCaptain,
)
from aisquare.cli.ui.spawn import SpawnDialog
from aisquare.cli.ui.views.settings_captain import ABOUT, CaptainToggle
from aisquare.core import experimental
from aisquare.core.config import load_config
from aisquare.models import FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services.captain import state as captain_state
from tests import test_captain_say as say_suite
from tests import test_ui_shell as ui_suite
from tests.captain_screens import INPUT_BOX, REAL_TRUST
from tests.test_captain_say import Captain, Clock
from tests.test_captain_sidebar import until
from tests.test_captain_view import _captain, _open_view, drive
from tests.test_ui_shell import Script, fleet_app, refreshed, seed, shown
from tests.toast_log import record
from tests.ui_workers import settle_page

no_real_tmux = ui_suite.no_real_tmux
script = ui_suite.script
captain_door = say_suite.captain


async def _settings(pilot: Pilot[None]) -> CaptainToggle:
    """Open Settings, as a click on *⚙ Settings* does, and hand back its Captain section."""
    app = fleet_app(pilot)
    await pilot.click(app.query_one(SettingsRow))
    await settle_page(app)
    return app.query_one(CaptainToggle)


async def _tick(pilot: Pilot[None], box: Checkbox) -> None:
    """A press of the box, and everything it leads to landed: the save, the shell's switch,
    and the fleet read it asked for."""
    app = fleet_app(pilot)
    box.toggle()
    await settle_page(app)
    await refreshed(app)


# --- the live box ---------------------------------------------------------------------------


def test_ticking_the_captain_box_shows_the_captain_at_once_and_unticking_hides_it(
    tmp_path: Path, script: Script, captain_off: None
) -> None:
    """Off, as it ships: no ``★``, no Captain section, no ui receiver. A tick saves the setting
    and shows all of it with no restart, the captain's row read into its section; an untick
    takes it all away again, the setting saved off."""
    seed(tmp_path, ("prj_aaa", "alpha", "amber-otter"))
    home = captain_state.home_project()
    script[home.id] = [_captain("waiting")]
    Seen = tuple[bool, bool, list[str], bool, bool]

    async def body(pilot: Pilot[None]) -> tuple[Seen, Seen, Seen]:
        app = fleet_app(pilot)

        def seen() -> Seen:
            rows = [row.status.agent.label for row in app.query(AgentRow) if _in_section(row)]
            return (
                bool(app.query(CaptainButton)),
                bool(app.query(CaptainSection)),
                rows,
                ui_receiver(app) is not None,
                load_config().experimental.captain,
            )

        await refreshed(app)
        before = seen()
        toggle = await _settings(pilot)
        box = toggle.query_one("#captain-toggle", Checkbox)
        assert box.value is False and not box.disabled
        await _tick(pilot, box)
        ticked = seen()
        await _tick(pilot, box)
        return before, ticked, seen()

    before, ticked, unticked = drive(body)
    assert before == (False, False, [], False, False)
    assert ticked == (True, True, ["captain"], True, True), "★, section, its row, receiver, saved"
    assert unticked == (False, False, [], False, False)


def _in_section(row: AgentRow) -> bool:
    return any(isinstance(node, CaptainSection) for node in row.ancestors)


def test_a_read_begun_before_the_tick_is_not_taken_for_no_captain(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch, captain_off: None
) -> None:
    """A fleet read already on its way when the box is ticked began with the captain off and
    read nothing of it. Painted as it was, the section said *Start captain* over a captain
    that was running; it says the captain is being read until a read that asked answers."""
    seed(tmp_path, ("prj_aaa", "alpha", "amber-otter"))
    home = captain_state.home_project()
    script[home.id] = [_captain("waiting")]
    scripted = fleet_service.list_agents
    permits = threading.Semaphore(1000)

    def held(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        assert permits.acquire(timeout=10), "a fleet read held past the test"
        return scripted(project, live_only=live_only)

    monkeypatch.setattr(fleet_service, "list_agents", held)

    async def body(pilot: Pilot[None]) -> tuple[str, bool, str, bool, list[str]]:
        app = fleet_app(pilot)
        await refreshed(app)
        toggle = await _settings(pilot)
        while permits.acquire(blocking=False):  # from here on, one read at a time
            pass
        app.refresh_data()
        before = app._fleet_worker
        assert before is not None and not before.is_finished
        toggle.query_one("#captain-toggle", Checkbox).toggle()
        await until(pilot, lambda: bool(app.query(CaptainSection)), what="the section mounted")
        section = app.query_one(CaptainSection)
        notice, start = (
            section.query_one("#captain-notice", Static),
            section.query_one(StartCaptain),
        )
        mounted = shown(notice), start.display
        permits.release()  # the read begun with the captain off: alpha alone
        await until(pilot, lambda: app._fleet_worker is not before, what="its frame painted")
        landed = shown(notice), start.display
        permits.release(1000)  # the read the tick asked for, and every one after it
        await refreshed(app)
        rows = [row.status.agent.label for row in section.query(AgentRow)]
        return (*mounted, *landed, rows)

    said, start_shown, said_after, start_after, rows = drive(body)
    assert (said, start_shown) == (CAPTAIN_READING, False)
    assert (said_after, start_after) == (CAPTAIN_READING, False), "not Start captain"
    assert rows == ["captain"]


@pytest.mark.parametrize(("value", "forced"), [("1", True), ("0", False)])
def test_the_environment_variable_greys_the_box_in_the_state_it_forces_and_says_why(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch, value: str, forced: bool
) -> None:
    """``AISQUARE_EXPERIMENTAL_CAPTAIN`` wins over the config: a tick would save a setting it
    overrides, so the box shows what it forces, greyed, and the line under it says why."""
    monkeypatch.setenv(experimental.CAPTAIN_ENV, value)

    async def body(pilot: Pilot[None]) -> tuple[bool, bool, str, bool]:
        toggle = await _settings(pilot)
        box = toggle.query_one("#captain-toggle", Checkbox)
        about = shown(toggle.query_one("#captain-toggle-about", Static))
        return box.value, box.disabled, about, fleet_app(pilot).captain_on

    shows, greyed, about, on = drive(body)
    assert (shows, greyed, on) == (forced, True, forced)
    assert experimental.CAPTAIN_ENV in about and f"captain {'on' if forced else 'off'}" in about
    assert about != ABOUT


# --- Start captain --------------------------------------------------------------------------


@pytest.mark.needs_captain
def test_start_captain_opens_the_spawn_dialog_with_the_captain_chosen(
    tmp_path: Path, script: Script
) -> None:
    """The section's line where there is no captain is a *Start captain* that does what the dim
    ``★`` does: the Spawn dialog, on the home board, with the captain chosen."""

    async def body(pilot: Pilot[None]) -> tuple[str, str, bool]:
        app = fleet_app(pilot)
        await refreshed(app)
        start = app.query_one("#captain-empty", Static)
        assert start.display and shown(start) == "▶ Start captain"
        await pilot.click(start)
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, SpawnDialog), type(dialog).__name__
        label = dialog.query_one("#spawn-label", Input)
        return dialog.project.id, dialog._role, label.disabled and label.value == "captain"

    project, role, fixed = drive(body)
    assert (project, role, fixed) == (captain_state.home_project().id, "captain", True)


# --- the message box ------------------------------------------------------------------------


async def _say(pilot: Pilot[None], line: str) -> tuple[list[str], str]:
    """Type ``line`` into the captain view's box and press Enter; what was said, and what the
    box holds once the send has answered."""
    app = fleet_app(pilot)
    await refreshed(app)
    await _open_view(pilot)
    box = app.query_one("#captain-say", Input)
    told = record(app)
    box.focus()
    box.value = line
    await pilot.press("enter")
    await pilot.pause()
    await settle_page(app)
    await pilot.pause()
    return told, box.value


@pytest.mark.needs_captain
def test_the_message_box_types_its_line_by_what_s_ups_path_and_clears(
    tmp_path: Path, script: Script, captain_door: tuple[Captain, Clock]
) -> None:
    """Enter types the line through ``brain.send``, What's up's one guarded door (never
    ``fleet.tell``, whose Enter at a trust dialog picks "No, exit"), and the box clears."""
    fake, _ = captain_door
    fake.present()  # type: ignore[attr-defined]
    fake.screen = list(INPUT_BOX)
    script[captain_state.home_project().id] = [_captain("waiting")]
    told, left = drive(lambda pilot: _say(pilot, "unblock the coder on alpha"))
    assert fake.typed == [("paste", "unblock the coder on alpha"), ("keys", "Enter")]
    assert fake.told == [], "never fleet.tell"
    assert left == ""
    assert "sent to the captain: unblock the coder on alpha" in told, told


@pytest.mark.needs_captain
def test_a_send_the_door_refuses_is_said_and_the_line_comes_back_into_the_box(
    tmp_path: Path, script: Script, captain_door: tuple[Captain, Clock]
) -> None:
    """A captain at Claude Code's trust dialog: send refuses by name and types nothing. The
    toast says so in send's words, and the line is back in the box to send again."""
    fake, _ = captain_door
    fake.present()  # type: ignore[attr-defined]
    fake.screen = list(REAL_TRUST)
    script[captain_state.home_project().id] = [_captain("waiting")]
    told, left = drive(lambda pilot: _say(pilot, "status please"))
    assert fake.typed == [] and fake.told == []
    assert any("nothing typed" in line and "trust its folder" in line for line in told), told
    assert left == "status please"
