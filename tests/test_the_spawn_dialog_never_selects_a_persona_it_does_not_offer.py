"""A role that joins the Spawn dialog's list brings its default persona as an option.

Review of #240, "also confirmed": the Spawn dialog's role picker (``spawn.py`` L713). The
Persona select is built at compose from the catalogue and from the default persona of every
role the list holds then, so the field can show a default this project lacks ("ghost — not
one of this project's personas", and the spawn refuses it with the reason). *Pick…* adds a
role afterwards: a seat bound once the dialog was open (the picker's own *+ New bind*), or
the role of a picked agent. That role's default was no option, the persona followed the
role to it, and Textual raised ``InvalidSelectValueError`` in a message handler: the end of
the TUI, with whatever the form held.

Now the role's default is an option before the role is selected, shown the way a missing
default always was, and the persona still follows the role: nothing the owner picked.
"""

from __future__ import annotations

from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Input, OptionList

from aisquare.cli.ui.attach import AttachTargetScreen
from aisquare.cli.ui.spawn import SpawnDialog
from aisquare.models import ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import settings as settings_service
from tests import test_ui_spawn as spawn_suite
from tests.test_ui_spawn import (
    Host,
    SpawnRecorder,
    configure_role,
    drive,
    note,
    option_prompts,
    picker_ids,
    select,
    settle,
    wait_for_screen,
)

# The Spawn dialog suite's fixtures, bound here so pytest finds them for this module's tests
# (``no_real_tmux``, ``spawns`` and ``teammates`` are autouse there).
no_real_tmux = spawn_suite.no_real_tmux
spawns = spawn_suite.spawns
teammates = spawn_suite.teammates
git_project = spawn_suite.git_project

MISSING = "ghost — not one of this project's personas"
"""How the Persona select shows a default the catalogue lacks (``_persona_options``)."""


async def pick_the_bind(pilot: Pilot[None], seat: str) -> None:
    """*Pick…*, then the bound seat in the picker: the choice fills the dialog's Role."""
    await pilot.click("#spawn-pick")
    picker = await wait_for_screen(pilot, AttachTargetScreen)
    await settle(pilot)
    listing = picker.query_one("#picker-list", OptionList)
    listing.highlighted = picker_ids(picker).index(f"bind:{seat}")
    await pilot.press("enter")
    await settle(pilot)


def test_a_role_pick_brings_in_shows_its_missing_default_persona_and_the_app_keeps_running(
    git_project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, spawns: SpawnRecorder
) -> None:
    """``[fleet.roles.coder2].persona`` names a persona this project does not have, and the
    seat is bound after the dialog opened, so the role list (and the Persona select built
    from it) never knew the role."""
    configure_role("coder2", persona="ghost")
    monkeypatch.setattr(fleet_service, "list_agents", lambda project, *, live_only=True: [])

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        seen: list[Any] = [
            "coder2" in option_prompts(dialog, "role"),
            MISSING in option_prompts(dialog, "persona"),
        ]
        settings_service.bind_role("coder2", agent_bin="claude2")  # as + New bind saves it
        await pick_the_bind(pilot, "coder2")
        seen += [
            host.is_running and host.screen is dialog,
            select(dialog, "role").value,
            dialog.query_one("#spawn-binary", Input).value,
            select(dialog, "persona").value,
            MISSING in option_prompts(dialog, "persona"),
            note(dialog, "#spawn-persona-description"),
            dialog.spawn_kwargs()["persona"],
        ]
        select(dialog, "role").value = "tester"
        await settle(pilot)
        seen.append(select(dialog, "persona").value)
        return seen

    listed, offered, *after = drive(git_project, scenario)
    assert (listed, offered) == (False, False)  # the control: the open form never knew coder2
    running, role, binary, persona, now_offered, said, sent, followed = after
    assert running is True
    assert (role, binary) == ("coder2", "claude2")
    assert (persona, now_offered) == ("ghost", True)  # a value the select offers, shown as missing
    assert said == "ghost is not one of this project's personas — spawn refuses it"
    assert sent is None  # still the role's default: the service resolves it, and refuses it
    assert followed == ""  # and it still follows the role: nothing here was the owner's pick


def test_the_missing_default_is_shown_from_a_form_that_was_showing_another_roles_default(
    git_project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, spawns: SpawnRecorder
) -> None:
    """The same pick from a form whose field holds a persona, the coder's own default: the
    field ends on the new role's default all the same, a value it offers."""
    configure_role("coder", persona="minimalist")
    configure_role("coder2", persona="ghost")
    monkeypatch.setattr(fleet_service, "list_agents", lambda project, *, live_only=True: [])

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        seen: list[Any] = [select(dialog, "persona").value]
        settings_service.bind_role("coder2", agent_bin="claude2")
        await pick_the_bind(pilot, "coder2")
        seen += [
            host.is_running and host.screen is dialog,
            select(dialog, "role").value,
            select(dialog, "persona").value,
            note(dialog, "#spawn-persona-description"),
        ]
        return seen

    assert drive(git_project, scenario) == [
        "minimalist",  # the control: the coder's own default, before the pick
        True,
        "coder2",
        "ghost",
        "ghost is not one of this project's personas — spawn refuses it",
    ]


def test_a_role_pick_brings_in_does_not_stop_the_persona_following_the_role(
    git_project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, spawns: SpawnRecorder
) -> None:
    """What the fix must not cost. Rebuilding what the field offers passes through *(none)*
    and back, and as a ``Select.Changed`` that read as the owner's pick: the persona was
    sent as chosen and no longer followed the role. Every role in play has one default here
    (the role list's own rebuild passes through its first role, the manager, as it always
    did), so nothing but the persona rebuild could have touched the field."""
    for role in ("coder", "manager", "coder2"):
        configure_role(role, persona="minimalist")
    monkeypatch.setattr(fleet_service, "list_agents", lambda project, *, live_only=True: [])

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        settings_service.bind_role("coder2", agent_bin="claude2")
        await pick_the_bind(pilot, "coder2")
        seen: list[Any] = [
            select(dialog, "role").value,
            select(dialog, "persona").value,
            dialog.spawn_kwargs()["persona"],
        ]
        select(dialog, "role").value = "tester"
        await settle(pilot)
        return [*seen, select(dialog, "persona").value]

    # The role's default, sent as None, and gone with the role: a tester has none.
    assert drive(git_project, scenario) == ["coder2", "minimalist", None, ""]


def test_a_persona_the_owner_picked_stays_picked_when_pick_brings_in_a_role(
    git_project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, spawns: SpawnRecorder
) -> None:
    """The other side of the rebuild: it keeps the field's value, so a pick is not lost to
    it, and a picked persona never followed a role anyway."""
    configure_role("coder2", persona="ghost")
    monkeypatch.setattr(fleet_service, "list_agents", lambda project, *, live_only=True: [])

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        select(dialog, "persona").value = "skeptic"
        await settle(pilot)
        settings_service.bind_role("coder2", agent_bin="claude2")
        await pick_the_bind(pilot, "coder2")
        return [
            host.is_running and host.screen is dialog,
            select(dialog, "role").value,
            select(dialog, "persona").value,
            dialog.spawn_kwargs()["persona"],
        ]

    assert drive(git_project, scenario) == [True, "coder2", "skeptic", "skeptic"]
