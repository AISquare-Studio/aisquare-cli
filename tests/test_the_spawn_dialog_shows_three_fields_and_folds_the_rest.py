"""The Spawn dialog shows three fields and folds the rest into *Advanced* (tsk_01m5mm2sz23b).

The owner's words: "spawn agent, select role (should allow no role, normal general purpose
claude), handoff, account". The dialog showed a dozen rows at once, and *Pick…* beside Role
opened the target picker, which picks an account too, beside the Account select. Now Role,
*Hand off from* and Account are on show; everything else is in one *Advanced* section,
folded until it is opened, or until it holds the reason *Spawn* is disabled. Role's first
option is *(none) — general-purpose agent*: ``fleet.PLAIN_ROLE``, a plain session of its
agent on the board with no standing cycle and no mission.

What these assert is what the owner meets: what ``Tab`` reaches, what a click on the title
or ``Enter`` does, how tall the box is, and the keywords the spawn received.
"""

from __future__ import annotations

from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Input, Select, Static
from textual.widgets._collapsible import CollapsibleTitle

from aisquare.cli.ui.spawn import LABEL_RULE, NO_ROLE_CHOICE, NO_ROLE_NOTE, SpawnDialog
from aisquare.models import ProjectInfo
from aisquare.services import fleet as fleet_service
from tests import test_ui_spawn as spawn_suite
from tests.test_ui_spawn import (
    SIZE,
    UNTOUCHED,
    Host,
    SpawnRecorder,
    advanced,
    drive,
    label_input,
    note,
    open_advanced,
    option_prompts,
    select,
    settle,
    submit,
    unfolded,
)

# The Spawn dialog suite's fixtures, bound here so pytest finds them for this module's tests
# (``no_real_tmux``, ``spawns`` and ``teammates`` are autouse there).
no_real_tmux = spawn_suite.no_real_tmux
spawns = spawn_suite.spawns
teammates = spawn_suite.teammates
git_project = spawn_suite.git_project

FOLDED = (
    "spawn-pick",
    "spawn-binary",
    "spawn-persona",
    "spawn-import",
    "spawn-label",
    "spawn-dice",
    "spawn-task",
    "spawn-worktree",
    "spawn-permission",
    "spawn-args",
    "spawn-prompt",
)
"""Every field *Advanced* holds, by id."""


def tab_order(dialog: SpawnDialog) -> list[str]:
    """What ``Tab`` reaches, by id; *Advanced*'s own title as ``advanced``. Not the scroll
    container the fields are in, which takes focus to scroll and is no field."""
    return [
        "advanced" if isinstance(widget, CollapsibleTitle) else widget.id or "?"
        for widget in dialog.focus_chain
        if widget.id != "spawn-fields"
    ]


def test_advanced_opens_folded_and_tab_reaches_the_three_fields_its_title_and_the_buttons(
    git_project: ProjectInfo,
) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        box = dialog.query_one("#spawn-box")
        return [
            advanced(dialog).collapsed,
            str(advanced(dialog).title),
            tab_order(dialog),
            [advanced(dialog) in dialog.query_one(f"#{name}").ancestors for name in FOLDED],
            box.outer_size.height,
        ]

    collapsed, title, order, inside, height = drive(git_project, scenario)
    assert collapsed is True and title == "Advanced"
    # No teammate to hand off from: that select is disabled, so Tab skips it.
    assert order == ["spawn-role", "spawn-account", "advanced", "spawn-submit", "spawn-cancel"]
    assert all(inside), dict(zip(FOLDED, inside, strict=True))  # Pick… is in it, not by Role
    # Sized to what it shows, where it was 92% of the terminal (46 rows here) whatever it
    # held: folded, it fits whole in the 22 rows a 24-row terminal gives it.
    assert height <= 22, f"the folded dialog is {height} rows tall on a {SIZE[1]}-row terminal"


def test_enter_on_the_title_opens_advanced_and_a_click_folds_it_again(
    git_project: ProjectInfo,
) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        dialog.query_one("#spawn-advanced CollapsibleTitle").focus()
        await pilot.press("enter")
        await unfolded(pilot)
        opened = [advanced(dialog).collapsed, set(FOLDED) <= set(tab_order(dialog))]
        await pilot.click("#spawn-advanced CollapsibleTitle")
        await unfolded(pilot)
        return [*opened, advanced(dialog).collapsed, set(FOLDED) & set(tab_order(dialog))]

    collapsed, reachable, folded_again, still_reached = drive(git_project, scenario)
    assert collapsed is False and reachable is True
    assert folded_again is True and still_reached == set()


def test_advanced_scrolls_on_a_short_terminal_and_the_buttons_stay_in_sight(
    git_project: ProjectInfo,
) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        await open_advanced(pilot)
        fields = dialog.query_one("#spawn-fields")
        return [
            fields.max_scroll_y > 0,
            dialog.query_one("#spawn-submit").region.bottom <= pilot.app.size.height,
            dialog.query_one("#spawn-box").outer_size.height <= pilot.app.size.height,
        ]

    async def short(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        await pilot.resize_terminal(SIZE[0], 24)
        await settle(pilot)
        return await scenario(pilot, host, dialog)

    assert drive(git_project, short) == [True, True, True]


@pytest.mark.parametrize(
    ("presets", "opened"),
    [
        ({"persona": "skeptic"}, True),
        ({"binary": "claude2"}, True),
        ({"persona": "skeptic", "role": "tester", "binary": "claude2", "account": "2"}, True),
        ({"role": "tester", "account": "2"}, False),  # both are basic fields
    ],
    ids=["persona", "binary", "the-picker-flow", "basic-only"],
)
def test_a_preset_that_fills_an_advanced_field_opens_it_with_the_form(
    git_project: ProjectInfo, presets: dict[str, str], opened: bool
) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> bool:
        return not advanced(dialog).collapsed

    assert drive(git_project, scenario, presets=presets) is opened


def test_a_label_that_blocks_spawn_opens_advanced_and_folding_it_opens_it_again(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    """A role too long for a label prefills one the rule refuses: *Spawn* is disabled for a
    field in *Advanced*, so *Advanced* is open — and folding it does not hide the reason."""

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        seen: list[Any] = [
            label_input(dialog).value,
            submit(dialog).disabled,
            advanced(dialog).collapsed,
            note(dialog, "#spawn-label-rule"),
        ]
        await pilot.click("#spawn-advanced CollapsibleTitle")
        await unfolded(pilot)
        seen.append(advanced(dialog).collapsed)
        return seen

    label, disabled, collapsed, rule, refolded = drive(
        git_project, scenario, presets={"role": "a-very-long-custom-role"}
    )
    assert len(label) > 24 and disabled is True
    assert collapsed is False and rule == LABEL_RULE
    assert refolded is False


def test_extra_args_that_break_quoting_open_advanced_and_a_basic_refusal_does_not(
    git_project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control is a refusal in the basic tier — a second manager — whose reason is
    under Role: *Advanced* has nothing to show for it and stays folded."""
    monkeypatch.setattr(fleet_service, "manager_of", lambda project: object())

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        select(dialog, "role").value = "manager"
        await pilot.pause()
        seen: list[Any] = [submit(dialog).disabled, advanced(dialog).collapsed]
        select(dialog, "role").value = "coder"
        await pilot.pause()
        seen.append(submit(dialog).disabled)
        dialog.query_one("#spawn-args", Input).value = '--append "unclosed'
        await pilot.pause()
        seen += [submit(dialog).disabled, advanced(dialog).collapsed]
        return seen

    manager_blocked, folded, coder_free, args_blocked, collapsed = drive(git_project, scenario)
    assert manager_blocked is True and folded is True
    assert coder_free is False
    assert args_blocked is True and collapsed is False


def test_no_role_is_the_first_option_and_spawns_a_plain_claude_with_nothing_else_sent(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        seen: list[Any] = [option_prompts(dialog, "role")[0], select(dialog, "role").value]
        select(dialog, "role").value = fleet_service.PLAIN_ROLE
        await pilot.pause()
        seen += [
            label_input(dialog).value,
            note(dialog, "#spawn-role-note"),
            advanced(dialog).collapsed,
        ]
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return seen

    first, default, label, role_note, collapsed = drive(git_project, scenario)
    assert first == NO_ROLE_CHOICE and default == "coder"  # coder stays the default
    assert label == "agent-1" and role_note == NO_ROLE_NOTE and collapsed is True
    # Every field the owner did not touch goes as the role's default — the plain role's
    # own: no worktree, where coder's was one, and the session's own permission mode.
    assert spawns.calls == [(git_project.id, fleet_service.PLAIN_ROLE, UNTOUCHED)]


@pytest.mark.needs_captain
def test_the_captains_dialog_opens_folded_with_its_fixed_fields_fixed(
    git_project: ProjectInfo,
) -> None:
    """Its persona preset is the captain's own, not a choice: it does not open *Advanced*."""

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        return [
            advanced(dialog).collapsed,
            dialog.query_one("#spawn-role", Select).disabled,
            label_input(dialog).disabled,
            dialog.query_one("#spawn-args", Input).disabled,
            dialog.query_one("#spawn-pick").disabled,
            submit(dialog).disabled,
            "Start the captain" in str(dialog.query_one("#spawn-header", Static).render()),
        ]

    seen = drive(
        git_project,
        scenario,
        presets={"role": fleet_service.CAPTAIN_ROLE, "persona": "captain"},
    )
    assert seen == [True, True, True, True, True, False, True]
