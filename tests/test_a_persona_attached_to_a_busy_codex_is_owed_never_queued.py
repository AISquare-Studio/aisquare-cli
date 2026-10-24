"""A persona attached to a busy Codex agent is owed to its next prompt, never queued.

SCOPE #113 0.8.2, A4's rule. ``attach_persona`` types the briefing with ``tell(note=False)``.
The PR's ``tell`` queues a busy Codex's message with ``codex queue``, which runs it as the
agent's next turn in the human's name: the persona's body would be a prompt the owner
never typed, in the transcript as theirs. A message that may not reach the board is no turn
of the human's either, so a busy Codex is queued nothing; the attachment owes the briefing
(``team.owe_persona_briefing``), and Codex's own next ``UserPromptSubmit`` hands it over as
context, to that agent alone.

On the fleet's fake tmux, with Codex's hooks as 0.162.1 sends them.
"""

from __future__ import annotations

import subprocess

import pytest

from aisquare.core import personas
from aisquare.core.store import store_session
from aisquare.models import TeamTask
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from tests import test_codex_account_switch as switch_suite
from tests import test_codex_fleet_parity as codex_suite
from tests import test_fleet_service as fleet_suite
from tests.test_codex_fleet_parity import THREAD_ONE, Codex

# The suites' fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project
codex_on_path = switch_suite.codex_on_path
codex_agent = codex_suite.codex_agent


def test_a_persona_attached_to_a_busy_codex_queues_nothing_and_is_owed_its_next_prompt(
    codex_agent: tuple[Codex, TeamTask], monkeypatch: pytest.MonkeyPatch
) -> None:
    codex, _ = codex_agent
    codex.start(THREAD_ONE)
    codex.hook("UserPromptSubmit", THREAD_ONE, prompt="work", turn_id="turn-1")  # busy
    queued: list[list[str]] = []

    def run(argv: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        queued.append(argv)
        return subprocess.CompletedProcess(argv, 0, "Queued message m for thread t.\n", "")

    monkeypatch.setattr(fleet_service, "_run_native", run)

    receipt = fleet_service.attach_persona(codex.project, codex.agent.label, "skeptic")

    assert queued == [], "no `codex queue` turn in the human's name"
    assert receipt.delivered == "noted"
    assert "reaches it with its next prompt" in receipt.how
    with store_session() as store:
        assert store.get_meta(team_service._persona_owed_key(codex.agent.id)), "owed"
    block = personas.briefing(personas.resolve("skeptic", codex.project.root))[0]

    briefed = codex.hook("UserPromptSubmit", THREAD_ONE, prompt="next", turn_id="turn-2")

    assert block in briefed, "its own next prompt hands it the briefing"
    with store_session() as store:
        assert not store.get_meta(team_service._persona_owed_key(codex.agent.id)), "settled"
    assert block not in codex.hook("UserPromptSubmit", THREAD_ONE, prompt="again"), "once"
