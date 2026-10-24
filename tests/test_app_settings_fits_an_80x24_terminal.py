"""App settings in an 80x24 terminal, beside the sidebar: the tabs keep the room main had.

Tester's report on PR #302: at 80x24 the tab body shrank to two to four rows, because Save,
Reload, Reset all (three lines) and an eleven-line note were pinned under the tabs. The
User preferences switch was off screen behind a two-row viewport, where main's single
scroll had shown four whole role rows. The buttons are one compact line and the note one
line now, and the tab body takes the rest.

Folded (#41), the Agent settings tab opens on the Captain box and the first of how
launches pick an account, whole, with no scrolling; its *Advanced*, opened and scrolled to
the roles, still shows as many whole role rows as main's single scroll did.

And every row ends inside its tab (#44, m4): a label of 24 columns and a field of 26 ran
to column 82 of a tab that ends at 79, so each select's arrow was cut off, and a role's
four fields on one line ran to 146. The label column gives way now, and a role takes one
line per field.
"""

from __future__ import annotations

from textual.containers import Horizontal, Vertical
from textual.geometry import Region
from textual.pilot import Pilot
from textual.widget import Widget
from textual.widgets import Button, Collapsible, Static, Switch, TabbedContent

from aisquare.cli.ui.sidebar import AppSettingsSelected
from aisquare.cli.ui.views.app_settings import (
    AGENT_ADVANCED,
    AGENT_TAB,
    PREFERENCES_TAB,
    REMOTE_TAB,
    AppSettingsView,
)
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
            # Rows, as main's measure is; how far right each reaches is the test below.
            "in view": [
                _whole(app.query_one(wid), agent.region)
                for wid in ("#captain-toggle", "#captain-toggle-about")
            ]
            + [_whole(app.query_one("#accounts-pick").parent, agent.region)],  # type: ignore[arg-type]
            "buttons": [
                _whole(app.query_one(wid, Button), view.region)
                for wid in ("#save-settings", "#reload-settings", "#reset-settings")
            ],
            "note lines": app.query_one("#settings-note", Static).region.height,
        }
        app.query_one(f"#{AGENT_ADVANCED}", Collapsible).collapsed = False
        await settle(app)
        app.query_one("#role-perm-manager").scroll_visible(animate=False, top=True)
        await settle(app)
        seen["agent rows"] = sum(_whole(row, agent.region) for row in agent.query(".row"))
        app.query_one("#app-settings-tabs", TabbedContent).active = PREFERENCES_TAB
        await settle(app)
        prefs = app.query_one("#user-preferences")
        switch = app.query_one("#ui-always-show-path", Switch)
        seen["switch"] = _whole(switch, prefs.region)
        seen["hint"] = _whole(app.query_one("#ui-always-show-path-about", Static), prefs.region)
        # The release check's switch, below Sidebar and shown as the tab opens (the owner's
        # choice of three, for PR #301).
        seen["updates"] = [
            _whole(app.query_one(wid), prefs.region)
            for wid in ("#updates-check", "#updates-check-about")
        ]
        seen["scrolled"] = prefs.scroll_y
        await pilot.click("#ui-always-show-path")  # a click lands only on what is on screen
        await pilot.click("#updates-check")
        await pilot.click("#save-settings")
        await settle(app)
        return seen

    seen = drive(go, size=(80, 24))
    assert seen["in view"] == [True, True, True], "the Captain box, its line, launches pick"
    assert isinstance(seen["agent rows"], int)
    assert seen["agent rows"] >= MAIN_WHOLE_ROWS, seen
    assert seen["buttons"] == [True, True, True], "Save, Reload and Reset all, whole"
    assert seen["note lines"] == 1
    assert seen["switch"] is True and seen["hint"] is True, seen
    assert seen["updates"] == [True, True], "the release check's switch and its line, whole"
    assert seen["scrolled"] == 0
    assert load_config().ui.always_show_project_path is True, "Save was reached and saved"
    assert load_config().updates.check is False, "the release check's switch saved off"


def _shown(widget: Widget, body: Widget) -> bool:
    """Whether ``widget`` is laid out in ``body``: neither it nor anything between them is
    hidden, as a closed fold's contents are."""
    node: Widget | None = widget
    while node is not None and node is not body:
        if not node.display:
            return False
        node = node.parent if isinstance(node.parent, Widget) else None
    return node is body


TABS = {
    AGENT_TAB: "#agent-settings",
    PREFERENCES_TAB: "#user-preferences",
    REMOTE_TAB: "#remote-settings",
}


def test_at_80x24_every_row_of_every_tab_ends_inside_the_tab_folded_and_unfolded() -> None:
    """Each part of each tab, with its folds closed as it opens and then all opened, is
    scrolled to and is inside the tab's body, left to right: every child of a row
    (a ``Horizontal`` or a ``Vertical``) and every ``Static`` (``Label`` included). No select
    arrow, field, switch or line of text is cut off at the edge (#44, m4). Rows were
    ``Horizontal``s alone, and Remote's write hint, a ``Static`` in a ``Vertical`` row, ran
    to column 151 (final delta review, m3)."""

    async def go(pilot: Pilot[None]) -> tuple[dict[str, int], list[str]]:
        app = fleet_app(pilot)
        app.post_message(AppSettingsSelected())
        await settle(app)
        rows: dict[str, int] = {}
        cut: list[str] = []
        for tab, body_id in TABS.items():
            app.query_one("#app-settings-tabs", TabbedContent).active = tab
            await settle(app)
            body = app.query_one(body_id)
            for folded in (True, False):
                if not folded:
                    for fold in body.query(Collapsible):
                        fold.collapsed = False
                    await settle(app)
                shown = [row for row in body.query(Horizontal) if _shown(row, body)] + [
                    row for row in body.query(Vertical) if _shown(row, body)
                ]
                rows[f"{tab} {'folded' if folded else 'unfolded'}"] = len(shown)
                parts: dict[int, Widget] = {}
                for row in shown:
                    parts.update({id(part): part for part in row.children})
                parts.update({id(text): text for text in body.query(Static) if _shown(text, body)})
                for part in parts.values():
                    if not _shown(part, body):
                        continue
                    part.scroll_visible(animate=False, top=True)
                    await settle(app)
                    if part.region.area == 0:
                        continue
                    port = body.scrollable_content_region
                    if part.region.x < port.x or part.region.right > port.right:
                        name = part.id or f"{type(part).__name__} in {part.parent}"
                        cut.append(
                            f"{tab}: {name} spans {part.region.x}..{part.region.right},"
                            f" the tab {port.x}..{port.right}"
                        )
        return rows, cut

    rows, cut = drive(go, size=(80, 24))
    assert cut == []
    # Measured, not vacuous: the folds hide most rows, and opened they are all there.
    assert rows[f"{AGENT_TAB} unfolded"] > rows[f"{AGENT_TAB} folded"] >= 3, rows
    assert rows[f"{PREFERENCES_TAB} folded"] >= 1 and rows[f"{REMOTE_TAB} unfolded"] >= 1, rows
