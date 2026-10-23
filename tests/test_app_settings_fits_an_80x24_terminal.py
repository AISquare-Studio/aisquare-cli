"""App settings in an 80x24 terminal, beside the sidebar: the tabs keep the room main had.

Tester's report on PR #302: at 80x24 the tab body shrank to two to four rows, because Save,
Reload, Reset all (three lines) and an eleven-line note were pinned under the tabs. The
User preferences switch was off screen behind a two-row viewport, where main's single
scroll had shown four whole role rows. The buttons are one compact line and the note one
line now, and the tab body takes the rest.
"""

from __future__ import annotations

from textual.geometry import Region
from textual.pilot import Pilot
from textual.widget import Widget
from textual.widgets import Button, Static, Switch, TabbedContent

from aisquare.cli.ui.sidebar import AppSettingsSelected
from aisquare.cli.ui.views.app_settings import PREFERENCES_TAB, AppSettingsView
from aisquare.core.config import load_config
from tests.test_ui_shell import drive, fleet_app, settle
from tests.test_ui_shell import no_real_tmux as _no_real_tmux
from tests.test_ui_shell import script as _script

#: Re-exported so pytest collects them here (the guard is autouse there, and here).
no_real_tmux = _no_real_tmux
script = _script

MAIN_WHOLE_ROWS = 4
"""Field rows main (09f48563) showed whole at 80x24 in the same app: manager, coder,
tester, reviewer — measured as below, against its single scroll."""


def _whole(widget: Widget, port: Region) -> bool:
    """Whether all of ``widget`` is inside ``port``: on screen, no scrolling."""
    return widget.region.area > 0 and port.contains_region(widget.region)


def test_at_80x24_the_tabs_keep_mains_rows_and_the_preference_is_on_screen() -> None:
    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        app.post_message(AppSettingsSelected())
        await settle(app)
        view = app.query_one(AppSettingsView)
        agent = app.query_one("#agent-settings")
        seen: dict[str, object] = {
            "agent rows": sum(_whole(row, agent.region) for row in agent.query(".row")),
            "buttons": [
                _whole(app.query_one(wid, Button), view.region)
                for wid in ("#save-settings", "#reload-settings", "#reset-settings")
            ],
            "note lines": app.query_one("#settings-note", Static).region.height,
        }
        app.query_one("#app-settings-tabs", TabbedContent).active = PREFERENCES_TAB
        await settle(app)
        prefs = app.query_one("#user-preferences")
        switch = app.query_one("#ui-always-show-path", Switch)
        seen["switch"] = _whole(switch, prefs.region)
        seen["hint"] = _whole(app.query_one("#ui-always-show-path-about", Static), prefs.region)
        seen["scrolled"] = prefs.scroll_y
        await pilot.click("#ui-always-show-path")  # a click lands only on what is on screen
        await pilot.click("#save-settings")
        await settle(app)
        return seen

    seen = drive(go, size=(80, 24))
    assert isinstance(seen["agent rows"], int)
    assert seen["agent rows"] >= MAIN_WHOLE_ROWS, seen
    assert seen["buttons"] == [True, True, True], "Save, Reload and Reset all, whole"
    assert seen["note lines"] == 1
    assert seen["switch"] is True and seen["hint"] is True, seen
    assert seen["scrolled"] == 0
    assert load_config().ui.always_show_project_path is True, "Save was reached and saved"
