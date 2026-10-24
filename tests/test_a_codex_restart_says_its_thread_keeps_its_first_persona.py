"""A restart that resumes a Codex thread says its system seam keeps its first persona.

SCOPE #113 0.8.2, A3's replay rule (``capabilities.resume_keeps_system_prompt``): measured on
0.162.1, ``codex resume <id>`` keeps the ``developer_instructions`` of the thread's first
turn and ignores a new one. So a restart of a Codex row whose persona changed since its
launch (``persona attach``) passes the new persona to a seam that drops it: it reaches the
agent through the session-start briefing alone, and the receipt says so
(``services.personas.resume_seam_note``). One whose persona is the one its launch passed
says nothing.

On the fleet's fake tmux, with Codex's hooks as 0.162.1 sends them.
"""

from __future__ import annotations

from aisquare.models import TeamTask
from aisquare.services import fleet as fleet_service
from tests import test_codex_account_switch as switch_suite
from tests import test_codex_fleet_parity as codex_suite
from tests import test_fleet_service as fleet_suite
from tests.test_codex_fleet_parity import THREAD_ONE, Codex, _rollout

# The suites' fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project
codex_on_path = switch_suite.codex_on_path
codex_agent = codex_suite.codex_agent

SEAM = "Codex resumes the thread with the system prompt it began with"


def test_a_codex_restart_after_a_persona_attach_says_the_briefing_carries_it(
    codex_agent: tuple[Codex, TeamTask],
) -> None:
    codex, _ = codex_agent
    _rollout(codex, THREAD_ONE)
    codex.start(THREAD_ONE)
    fleet_service.attach_persona(codex.project, codex.agent.label, "skeptic")

    receipt = fleet_service.restart(codex.project, codex.agent.label)

    assert receipt.resumed is True
    said = [note for note in receipt.notes if SEAM in note]
    assert said == [
        f"persona skeptic: {SEAM}, so its system seam keeps its first persona — skeptic "
        "reaches it through the session-start briefing"
    ], receipt.notes

    again = fleet_service.restart(codex.project, codex.agent.label)

    assert again.resumed is True
    assert not [note for note in again.notes if SEAM in note], "its launch passed skeptic"


def test_a_codex_restart_with_the_persona_its_launch_passed_says_nothing_of_its_seam(
    codex_agent: tuple[Codex, TeamTask],
) -> None:
    codex, _ = codex_agent
    _rollout(codex, THREAD_ONE)
    codex.start(THREAD_ONE)

    receipt = fleet_service.restart(codex.project, codex.agent.label)

    assert receipt.resumed is True
    assert not [note for note in receipt.notes if SEAM in note], receipt.notes
