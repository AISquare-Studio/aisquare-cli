"""A late ``Select.Changed`` in the Spawn dialog is nobody's pick (reopen of PR #303).

On Windows CI the role went coder → tester → coder with the persona untouched, and the
spawn sent coder's own persona, ``minimalist``, as the owner's choice. The tester switch
put ``""`` in Persona and that field's ``Select.Changed`` reached the dialog after the
coder switch had put ``minimalist`` back: a value the form had not *last* put there, so
it read as a pick. Persona is inside *Advanced* now, two widgets deeper than Role, and a
message climbs one widget per hop; Linux was fast enough that it never showed.

The order a slow event loop gives cannot be waited for, so these hold a message back and
deliver it late themselves, after the change that overtakes it.
"""

from __future__ import annotations

from typing import Any

from textual.pilot import Pilot
from textual.widgets import Select

from aisquare.cli.ui.spawn import SpawnDialog
from aisquare.models import ProjectInfo
from tests import test_ui_spawn as spawn_suite
from tests.test_ui_spawn import (
    UNTOUCHED,
    Host,
    SpawnRecorder,
    configure_role,
    drive,
    label_input,
    select,
    settle,
)

# The Spawn dialog suite's fixtures, bound here so pytest finds them for this module's tests
# (``no_real_tmux``, ``spawns`` and ``teammates`` are autouse there).
no_real_tmux = spawn_suite.no_real_tmux
spawns = spawn_suite.spawns
teammates = spawn_suite.teammates
git_project = spawn_suite.git_project


async def there_and_back(pilot: Pilot[None], dialog: SpawnDialog) -> None:
    """The role to tester and back to coder, each switch handled before the next."""
    for role in ("tester", "coder"):
        select(dialog, "role").value = role
        await settle(pilot)


def test_a_persona_changed_from_a_role_switch_that_arrives_late_is_not_a_pick(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    configure_role("coder", persona="minimalist")

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        await there_and_back(pilot, dialog)
        persona = select(dialog, "persona")
        # What the tester switch raised for its "" — delivered after the switch back.
        persona.post_message(Select.Changed(persona, ""))
        await settle(pilot)
        seen = [persona.value, dialog.spawn_kwargs()["persona"]]
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return seen

    # Still coder's own default, so sent as None and resolved by the spawn.
    assert drive(git_project, scenario) == ["minimalist", None]
    assert spawns.calls == [(spawns.calls[0][0], "coder", UNTOUCHED)]


def test_a_role_changed_that_arrives_late_does_not_switch_the_form_under_the_field(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    """Read from the message, the form became a tester's — its label, its persona, and
    the spawn — while the Role field showed coder."""
    configure_role("coder", persona="minimalist")

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        await there_and_back(pilot, dialog)
        role = select(dialog, "role")
        role.post_message(Select.Changed(role, "tester"))
        await settle(pilot)
        seen = [role.value, label_input(dialog).value, select(dialog, "persona").value]
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return seen

    assert drive(git_project, scenario) == ["coder", "coder-1", "minimalist"]
    assert [call[1:] for call in spawns.calls] == [("coder", UNTOUCHED)]


def test_an_owners_persona_pick_still_on_its_way_survives_a_role_switch(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    """The other side of the same race: the field holds the owner's pick, its Changed not
    yet handled, when a role switch is. The pick is not the form's to replace."""
    configure_role("coder", persona="minimalist")

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        persona = select(dialog, "persona")
        with dialog.prevent(Select.Changed):  # held back, as a slow loop would
            persona.value = "mentor"
        select(dialog, "role").value = "tester"
        await settle(pilot)
        persona.post_message(Select.Changed(persona, "mentor"))
        await settle(pilot)
        seen = [persona.value, dialog.spawn_kwargs()["persona"]]
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return seen

    assert drive(git_project, scenario) == ["mentor", "mentor"]
    assert [(call[1], call[2]["persona"]) for call in spawns.calls] == [("tester", "mentor")]
