"""What the Spawn dialog takes from a teammate goes when that teammate is no longer chosen.

Review of #240, "also confirmed": the Spawn dialog's account (``spawn.py`` L792). Choosing
a teammate under *Hand off from* fills Role, Account and Persona from it. The Account was
filled only from a teammate that HAS a slot, so after coder-1 (slot 2) a teammate on this
shell's account kept showing "2", and its fork or take-over was sent ``account="2"``: the
previous teammate's slot, which nobody chose. Back on *(none)* all three stayed too, and the
plain spawn went out with the teammate's role, persona and slot.

Now every teammate fills all three, its own account whether a slot or this shell's, and
*(none)* gives the form back what it held before a teammate filled it: the plain spawn,
field for field. Driven on the bare host of ``tests/test_ui_spawn.py``, with its recorders
in front of ``fleet_service.spawn`` and ``fleet_service.hand_off``.
"""

from __future__ import annotations

from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import RadioButton

from aisquare.cli.ui.spawn import SpawnDialog
from aisquare.models import FleetAgentStatus, ProjectInfo
from tests import test_ui_spawn as spawn_suite
from tests.test_ui_spawn import (
    UNTOUCHED,
    HandOffRecorder,
    Host,
    SpawnRecorder,
    configure_role,
    drive,
    option_prompts,
    overview,
    select,
    settle,
    teammate,
)

# The Spawn dialog suite's fixtures, bound here so pytest finds them for this module's tests
# (``no_real_tmux``, ``spawns`` and ``teammates`` are autouse there).
no_real_tmux = spawn_suite.no_real_tmux
spawns = spawn_suite.spawns
teammates = spawn_suite.teammates
hand_offs = spawn_suite.hand_offs
git_project = spawn_suite.git_project


async def choose(pilot: Pilot[None], dialog: SpawnDialog, label: str | None) -> None:
    """Choose the teammate whose row starts with ``label`` under *Hand off from*; ``None``
    is *(none)*. By what the row shows, as a click does, not by the select's own value."""
    field = select(dialog, "from")
    if label is None:
        field.value = ""
    else:
        [value] = [v for prompt, v in field._options if str(prompt).startswith(f"{label} · ")]
        field.value = value
    await settle(pilot)


def shows(dialog: SpawnDialog) -> tuple[Any, Any, Any]:
    """What the three fields a teammate fills hold: Role, Account, Persona."""
    return (
        select(dialog, "role").value,
        select(dialog, "account").value,
        select(dialog, "persona").value,
    )


async def press(pilot: Pilot[None], *, take_over: bool) -> None:
    """Press the button; a take-over asks its one question first."""
    await pilot.click("#spawn-submit")
    await settle(pilot)
    if take_over:
        await pilot.click("#take-over-yes")
        await settle(pilot)


@pytest.mark.parametrize("take_over", [False, True], ids=["fork", "take-over"])
def test_a_teammate_on_this_shells_account_is_not_sent_the_slot_of_the_one_chosen_before_it(
    git_project: ProjectInfo,
    hand_offs: HandOffRecorder,
    teammates: list[FleetAgentStatus],
    take_over: bool,
) -> None:
    """coder-1 runs on slot 2, coder-3 on this shell's account. Chosen after coder-1, the
    Account still showed "2", which is not coder-3's own, so it was sent as a choice."""
    teammates.extend([teammate(git_project), teammate(git_project, label="coder-3", slot=None)])

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        await choose(pilot, dialog, "coder-1")
        seen: list[Any] = [shows(dialog), option_prompts(dialog, "account")]
        await choose(pilot, dialog, "coder-3")
        if take_over:
            dialog.query_one("#spawn-take-over", RadioButton).value = True
            await settle(pilot)
        seen += [shows(dialog), option_prompts(dialog, "account")]
        await press(pilot, take_over=take_over)
        return seen

    first, offered, second, now_offered = drive(git_project, scenario)
    [(_, source, sent)] = hand_offs.calls
    assert (source, sent["mode"]) == ("coder-3", "take_over" if take_over else "fork")
    assert sent["account"] is None  # the teammate's own: the service keeps the row's
    # The control: coder-1 filled its own slot in, offered as a preset no account read lists.
    assert first == ("coder", "2", "careful") and offered == ["(this shell's)", "2 (preset)"]
    assert second == ("coder", "", "careful")  # coder-3's own: (this shell's)
    assert now_offered == ["(this shell's)"]  # and coder-1's slot is no longer on offer


def test_a_slot_chosen_for_the_second_teammate_is_still_sent_as_itself(
    git_project: ProjectInfo, hand_offs: HandOffRecorder, teammates: list[FleetAgentStatus]
) -> None:
    """The control for the other side: what the owner sets is theirs, and a number is sent
    as itself. The Account starts from the teammate's own each time one is chosen."""
    teammates.extend([teammate(git_project), teammate(git_project, label="coder-3", slot=None)])

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> None:
        await choose(pilot, dialog, "coder-1")
        await choose(pilot, dialog, "coder-3")
        select(dialog, "account").value = "2"  # the owner's own pick, for coder-3's fork
        await settle(pilot)
        await press(pilot, take_over=False)

    drive(
        git_project,
        scenario,
        accounts=lambda: overview((1, "me@example.com"), (2, "two@example.com")),
    )
    [(_, source, sent)] = hand_offs.calls
    assert (source, sent["account"]) == ("coder-3", "2")


def test_unpicking_the_teammate_gives_back_the_plain_spawn_field_for_field(
    git_project: ProjectInfo,
    spawns: SpawnRecorder,
    hand_offs: HandOffRecorder,
    teammates: list[FleetAgentStatus],
) -> None:
    """A tester on slot 2 running as ``careful`` is chosen, then *(none)* again. The plain
    spawn went out as a tester, on slot 2, as ``careful``: none of it the owner's choice.
    The coder's own default persona shows that the field is back to following the role."""
    configure_role("coder", persona="minimalist")
    teammates.append(teammate(git_project, label="tester-1", role="tester"))

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        seen: list[Any] = [shows(dialog)]
        await choose(pilot, dialog, "tester-1")
        seen.append(shows(dialog))
        await choose(pilot, dialog, None)
        seen += [shows(dialog), option_prompts(dialog, "account")]
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return seen

    opened, filled, back, offered = drive(git_project, scenario)
    assert spawns.calls == [(git_project.id, "coder", UNTOUCHED)] and hand_offs.calls == []
    assert opened == ("coder", "", "minimalist")
    assert filled == ("tester", "2", "careful")  # the control: the teammate's own, all three
    assert back == opened and offered == ["(this shell's)"]


def test_unpicking_the_teammate_gives_back_what_the_form_was_opened_with(
    git_project: ProjectInfo,
    spawns: SpawnRecorder,
    hand_offs: HandOffRecorder,
    teammates: list[FleetAgentStatus],
) -> None:
    """ "Back" is what the form held, not the defaults: a dialog opened for a persona, a seat
    and an account (the Personas tab's *Attach to new*) has them again, and sends them."""
    teammates.append(teammate(git_project, label="tester-1", role="tester"))
    presets = {"persona": "skeptic", "role": "reviewer", "account": "7"}

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        seen: list[Any] = [shows(dialog)]
        await choose(pilot, dialog, "tester-1")
        seen.append(shows(dialog))
        await choose(pilot, dialog, None)
        seen += [shows(dialog), option_prompts(dialog, "account")]
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return seen

    opened, filled, back, offered = drive(git_project, scenario, presets=presets)
    [(_, role, sent)] = spawns.calls
    assert (role, sent["account"], sent["persona"]) == ("reviewer", "7", "skeptic")
    assert hand_offs.calls == []
    assert opened == ("reviewer", "7", "skeptic")
    assert filled == ("tester", "2", "careful")
    assert back == opened and offered == ["(this shell's)", "7 (preset)"]
