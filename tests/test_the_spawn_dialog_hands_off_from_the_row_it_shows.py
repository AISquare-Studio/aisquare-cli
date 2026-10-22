"""The Spawn dialog hands off from the row it shows, not from whoever carries its label.

Review of #240, "also confirmed": hand-off by label (``spawn.py`` and ``fleet.py``). Two
rows of a project can carry one label over time, an exited coder-1 and the coder-1 started
since, and *Hand off from* lists the rows it read when the form opened: either, or both.
The picker's value was the label, so the form took the FIRST row under it for its prefill,
and Fork or Take over sent the label alone, which the service resolves to the NEWEST row:
the owner chose one teammate and the hand-off acted on another.

The contract, the dialog's half (the service's is
``tests/test_a_hand_off_acts_on_the_row_it_was_given.py``): each teammate in the picker is
its row, the prefill is that row's, and the hand-off sends that row's ``agent_id`` beside
its label, on Fork and on Take over.

Driven as ``tests/test_ui_spawn.py`` drives the dialog, with its fixtures: the hand-off is
that suite's recorder, so nothing here starts tmux, git or an agent.
"""

from __future__ import annotations

import inspect
from typing import Any

from textual.pilot import Pilot
from textual.widgets import RadioButton

from aisquare.cli.ui.spawn import SpawnDialog
from aisquare.models import FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from tests import test_ui_spawn as ui_suite
from tests.test_ui_spawn import (
    NO_CHANGE,
    HandOffRecorder,
    Host,
    drive,
    select,
    settle,
    teammate,
)

# The UI suite's fixtures, bound here so pytest finds them for this module's tests: the
# autouse ones keep tmux, the spawn and the picker's listing off the real fleet.
no_real_tmux = ui_suite.no_real_tmux
spawns = ui_suite.spawns
teammates = ui_suite.teammates
hand_offs = ui_suite.hand_offs
git_project = ui_suite.git_project

REAL_HAND_OFF = fleet_service.hand_off
"""The service's own ``hand_off``, read before the recorder replaces it: what the dialog
sends must be keywords it takes."""


def _two_rows_under_one_label(project: ProjectInfo) -> tuple[FleetAgentStatus, FleetAgentStatus]:
    """The exited coder-1 and the coder-1 started since, listed oldest first as the fleet
    lists them, each with its own account and persona so whose prefill it is shows."""
    older = teammate(project, state="exited", slot=None, persona=None)
    newer = teammate(project, slot=2, persona="careful")
    older = older.model_copy(update={"agent": older.agent.model_copy(update={"id": "agt_older"})})
    newer = newer.model_copy(update={"agent": newer.agent.model_copy(update={"id": "agt_newer"})})
    return older, newer


async def _choose(pilot: Pilot[None], dialog: SpawnDialog, shown: str) -> None:
    """Pick the teammate whose line in the picker reads ``shown``, as the owner does."""
    field = select(dialog, "from")
    [value] = [value for prompt, value in field._options if str(prompt) == shown]
    field.value = value
    await settle(pilot)


def test_a_fork_of_the_exited_row_sends_that_rows_id_with_its_label(
    git_project: ProjectInfo, hand_offs: HandOffRecorder, teammates: list[FleetAgentStatus]
) -> None:
    """The pin: the owner chose the EXITED coder-1. Sent as the label alone, the service
    forked the live coder-1, the newest row under it. The row's id goes with the label, and
    every keyword sent is one the service takes."""
    older, newer = _two_rows_under_one_label(git_project)
    teammates.extend([older, newer])

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> None:
        await _choose(pilot, dialog, "coder-1 · exited · coder · no task · this shell's")
        await pilot.click("#spawn-submit")
        await settle(pilot)

    drive(git_project, scenario)
    [(_, source, sent)] = hand_offs.calls
    assert (source, sent.get("agent_id")) == ("coder-1", older.agent.id)
    assert sent == {**NO_CHANGE, "agent_id": older.agent.id}
    inspect.signature(REAL_HAND_OFF).bind(git_project, source, **sent)


def test_the_newer_row_listed_second_is_the_one_prefilled_and_sent(
    git_project: ProjectInfo, hand_offs: HandOffRecorder, teammates: list[FleetAgentStatus]
) -> None:
    """The other way round: the owner chose the LIVE coder-1, listed after the exited one.
    By label the form took the first row under it, so the prefill was the exited row's
    (no slot, no persona) and the fork ran on this shell's account with no persona. It is
    the chosen row's: slot 2 and its persona, sent as the teammate's own, with its id."""
    older, newer = _two_rows_under_one_label(git_project)
    teammates.extend([older, newer])

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        await _choose(pilot, dialog, "coder-1 · working · coder · no task · slot 2")
        prefilled = [select(dialog, "account").value, select(dialog, "persona").value]
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return prefilled

    prefilled = drive(git_project, scenario)
    assert prefilled == ["2", "careful"], "the chosen row's account and persona"
    [(_, source, sent)] = hand_offs.calls
    assert (source, sent.get("agent_id")) == ("coder-1", newer.agent.id)
    assert sent == {**NO_CHANGE, "agent_id": newer.agent.id}


def test_a_take_over_of_the_exited_row_sends_that_rows_id_after_the_question(
    git_project: ProjectInfo, hand_offs: HandOffRecorder, teammates: list[FleetAgentStatus]
) -> None:
    """*Take over* stops its source. By label it sent the live coder-1 ``/exit``; with the
    row's id the service takes over the exited row, or refuses when a newer one carries its
    label, and never stops the other."""
    older, newer = _two_rows_under_one_label(git_project)
    teammates.extend([older, newer])

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> None:
        await _choose(pilot, dialog, "coder-1 · exited · coder · no task · this shell's")
        dialog.query_one("#spawn-take-over", RadioButton).value = True
        await settle(pilot)
        await pilot.click("#spawn-submit")
        await settle(pilot)
        await pilot.click("#take-over-yes")
        await settle(pilot)

    drive(git_project, scenario)
    [(_, source, sent)] = hand_offs.calls
    assert (source, sent.get("agent_id")) == ("coder-1", older.agent.id)
    assert sent == {**NO_CHANGE, "mode": "take_over", "agent_id": older.agent.id}
