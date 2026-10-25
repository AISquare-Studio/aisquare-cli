"""The Personas tab's toast after an attach says what the CLI's two lines say.

Review of #240, finding 7. Attaching to a busy agent no longer files the briefing on the
board: the persona is recorded, and the agent's own next prompt hands it the briefing,
for it alone (``tests/test_a_busy_agent_is_briefed_privately_at_its_next_prompt.py``).
The toast read ``✓ attached pair to coder-auth (noted)`` and stopped there, which told
the owner the briefing was waiting on the board. For ``noted`` — and since #201's R1-F9,
``unsent`` and ``maybe`` too — it now carries the receipt's own sentence, as ``persona
attach`` prints it on its second line; a ``typed`` attach, which is done when the toast
shows, reads as it did.

Driven like ``tests/test_ui_personas.py``: the picker and the confirmation are real, and
``fleet_service.attach_persona`` is a recorder that answers with the receipt under test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from textual.pilot import Pilot

from aisquare.cli.ui.attach import AttachTargetScreen, ConfirmAttachScreen
from aisquare.models import FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from tests import test_ui_personas as ui_suite
from tests.test_ui_personas import Host, drive, press, select_row, settle, wait_for

# The persona UI suite's fixtures, bound here so pytest finds them for this module's tests.
no_real_tmux = ui_suite.no_real_tmux
repo = ui_suite.repo
project = ui_suite.project
catalogue = ui_suite.catalogue
targets = ui_suite.targets

LATER = (
    "it is working, so the briefing was not typed: pair is recorded and reaches it with its "
    "next prompt, for it alone; the board carries the name only"
)
"""``AttachReceipt.how`` for a busy agent, as ``fleet.attach_persona`` words it."""


def _toasts_after_attaching(
    project: ProjectInfo,
    targets: list[FleetAgentStatus],
    monkeypatch: pytest.MonkeyPatch,
    *,
    delivered: Literal["typed", "noted", "unsent", "maybe"],
    how: str,
) -> list[tuple[str, str]]:
    """Attach ``pair`` to the picker's one agent, confirmed; every toast the tab raised."""

    def attach(target: ProjectInfo, label: str, name: str) -> fleet_service.AttachReceipt:
        agent = targets[0].agent.model_copy(update={"persona": name})
        return fleet_service.AttachReceipt(
            agent=agent, persona=name, replaced="mentor", delivered=delivered, how=how
        )

    monkeypatch.setattr(fleet_service, "attach_persona", attach)

    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, str]]:
        await select_row(pilot, host, "user:pair")
        await press(pilot, "#persona-attach-existing")
        await wait_for(pilot, AttachTargetScreen)
        await settle(pilot)
        await pilot.press("enter")  # the highlighted agent
        await wait_for(pilot, ConfirmAttachScreen)
        await press(pilot, "#attach-confirm")
        await settle(pilot)
        return list(host.notices)

    return drive(scenario, project=project)


def test_the_toast_for_a_busy_agent_says_the_persona_reaches_it_with_its_next_prompt(
    project: ProjectInfo,
    catalogue: dict[str, Path],
    targets: list[FleetAgentStatus],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notices = _toasts_after_attaching(project, targets, monkeypatch, delivered="noted", how=LATER)

    assert (f"✓ attached pair to coder-auth (noted) — {LATER}", "information") in notices


@pytest.mark.parametrize(
    ("delivered", "how"),
    [
        ("unsent", "typed it at its prompt, but its Enter was refused — not told again"),
        ("maybe", "typed it at its prompt, but tmux did not answer in time — not told again"),
    ],
)
def test_the_toast_for_an_unsent_or_maybe_briefing_says_how_too(
    project: ProjectInfo,
    catalogue: dict[str, Path],
    targets: list[FleetAgentStatus],
    monkeypatch: pytest.MonkeyPatch,
    delivered: Literal["unsent", "maybe"],
    how: str,
) -> None:
    """Only ``noted`` carried the receipt's sentence, though an ``unsent`` or ``maybe``
    briefing may not have reached the agent either, and the CLI prints it for every kind
    (review of #201, R1-F9)."""
    notices = _toasts_after_attaching(project, targets, monkeypatch, delivered=delivered, how=how)

    assert (f"✓ attached pair to coder-auth ({delivered}) — {how}", "information") in notices


def test_the_toast_for_a_waiting_agent_reads_as_it_did(
    project: ProjectInfo,
    catalogue: dict[str, Path],
    targets: list[FleetAgentStatus],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: the briefing was typed, and there is nothing to add to that."""
    notices = _toasts_after_attaching(
        project, targets, monkeypatch, delivered="typed", how="typed into its pane (it was waiting)"
    )

    assert ("✓ attached pair to coder-auth (typed)", "information") in notices
