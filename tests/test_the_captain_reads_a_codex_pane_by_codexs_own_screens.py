"""The captain reads a Codex agent's pane by Codex's own screens, not Claude Code's.

SCOPE #113 0.8.2, A1c (a safety defect after the merge): the captain's ``press`` and
``paste`` read every pane with Claude Code's shapes (its U+276F highlight, its "Esc to
cancel" / "Enter to confirm" footers, its "Quick safety check"). Codex's screens differ
(measured on 0.162.1: the highlight is U+203A, its footers "enter continue · esc quit" and
"Press enter to confirm or esc to cancel"), so no Codex dialog read as a prompt: ``press
yes`` at its approval found none, and ``paste … submit=True`` at its folder-trust dialog was
not refused, its Enter picking "1. Trust and continue", the owner's decision made by the
captain. They ask the pane's own agent now (``services.captain.screen``, by the placed
row's agent): Codex's trust is refused as Claude Code's is, its approval is a chooser on
``1`` and Esc, and every other dialog of its own is the owner's.

The screens are Codex's own, measured on 0.162.1 (``tests/fixtures/agent_screens/
codex.json``), on ``tests/test_captain_actions.py``'s recorders.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo
from aisquare.services.captain import actions
from tests import test_captain_actions as suite
from tests.test_captain_actions import Fleet, Pane, ok, refused

projects = suite.projects
alpha = suite.alpha
fleet_rec = suite.fleet_rec

SCREENS: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "agent_screens" / "codex.json").read_text("utf-8")
)


def _screen(what: str) -> list[str]:
    """Codex's measured screen ``what`` (fixture 4b)."""
    shown = [*SCREENS["dialogs"], *SCREENS["not_dialogs"]]
    rows: list[str] = next(item["rows"] for item in shown if item["what"].startswith(what))
    return rows


TRUST = _screen("folder trust")
APPROVAL = _screen("an approval view")
SWITCH = _screen("Switch to gpt-6-luna")
IDLE = _screen("its empty prompt after a turn")


@pytest.fixture
def codex(alpha: ProjectInfo, fleet_rec: Fleet) -> Pane:
    """coder-1, a Codex agent at its empty prompt, asking (its approval view reads
    ``attention``) unless a test says otherwise."""
    row = suite._row(alpha, "coder-1", "coder", "%1").model_copy(update={"agent": "codex"})
    with store_session() as store:
        store.upsert_fleet_agent(row)
    pane = Pane(command="codex", screen=list(IDLE))
    fleet_rec.panes["%1"] = pane
    fleet_rec.states["coder-1"] = "attention"
    return pane


def _live(alpha: ProjectInfo) -> FleetAgent:
    with store_session() as store:
        row = store.fleet_agent_by_label(alpha.id, "coder-1", live_only=True)
    assert row is not None and row.agent == "codex"
    return row


@pytest.mark.parametrize("submit", [True, False])
def test_a_paste_at_a_codex_agents_folder_trust_dialog_is_refused(
    alpha: ProjectInfo, codex: Pane, fleet_rec: Fleet, submit: bool
) -> None:
    """The defect: Claude Code's shapes saw no prompt here, the paste went in, and its Enter
    picked "1. Trust and continue". Refused, saying to trust the folder first; nothing
    typed."""
    _live(alpha)
    codex.screen = list(TRUST)
    message = refused(lambda: actions.paste("alpha", "coder-1", "carry on", submit=submit))
    assert message.startswith("refused: the trust dialog is showing on coder-1"), message
    assert "trust this folder first" in message
    assert codex.typed == []


@pytest.mark.parametrize("key", ["yes", "no", "enter", "1", "esc"])
def test_a_press_at_a_codex_agents_folder_trust_dialog_is_refused(
    alpha: ProjectInfo, codex: Pane, key: str
) -> None:
    codex.screen = list(TRUST)
    message = refused(lambda: actions.press("alpha", "coder-1", key))
    assert "trust this folder first" in message, message
    assert codex.typed == []


def test_press_yes_at_a_codex_approval_sends_its_1_and_reads_the_prompt_gone(
    alpha: ProjectInfo, codex: Pane
) -> None:
    """Its approval view is a chooser: ``1. Yes, proceed (y)`` takes its digit (the first
    option that says Yes), as Remote's quick answers send it."""
    codex.screen = list(APPROVAL)
    codex.answers = {"1": list(IDLE)}
    result = ok(actions.press("alpha", "coder-1", "yes"))
    assert codex.keys == [("1",)]
    assert (result["sent"], result["answered"]) == ("1", True)
    assert result["prompt"].startswith("Would you like to run the following command?")


def test_press_no_at_a_codex_approval_sends_escape(alpha: ProjectInfo, codex: Pane) -> None:
    """``3. No, and tell Codex what to do differently (esc)``: Esc answers No."""
    codex.screen = list(APPROVAL)
    codex.answers = {"Escape": list(IDLE)}
    result = ok(actions.press("alpha", "coder-1", "no"))
    assert codex.keys == [("Escape",)] and result["answered"] is True


def test_press_yes_at_a_dialog_of_codexs_own_is_the_owners_and_sends_nothing(
    alpha: ProjectInfo, codex: Pane
) -> None:
    """``Switch to gpt-6-luna for lower credit usage?``: its keys mean something new each
    time, so it offers no yes for the captain to send, and every other key is refused as
    well (review r17 of #113, finding 5)."""
    codex.screen = list(SWITCH)
    message = refused(lambda: actions.press("alpha", "coder-1", "yes"))
    assert message.startswith("refused: a dialog of its own is showing on coder-1"), message
    assert "its answer is the owner's" in message
    assert codex.typed == []


def test_a_tell_into_a_codex_agent_at_its_approval_is_refused_before_the_fleet_types(
    alpha: ProjectInfo, codex: Pane, fleet_rec: Fleet
) -> None:
    """``fleet.tell`` pastes and presses Enter without reading the pane: at Codex's approval
    the Enter would pick "1. Yes, proceed". The captain's tell door reads it first."""
    codex.screen = list(APPROVAL)
    fleet_rec.states["coder-1"] = "waiting"
    message = refused(lambda: actions.tell("alpha", "coder-1", "rebase first"))
    assert message.startswith("refused: a prompt is showing on coder-1"), message
    assert "tell" not in fleet_rec.names() and codex.typed == []


def test_a_codex_agent_the_fleet_reads_working_is_typed_into_at_its_empty_prompt(
    alpha: ProjectInfo, codex: Pane, fleet_rec: Fleet
) -> None:
    """13313's rule on Codex: a working read is overridden by its screen at its empty
    composer (``ready_on_screen``), never while a turn runs on it."""
    fleet_rec.states["coder-1"] = "working"
    ok(actions.paste("alpha", "coder-1", "carry on", submit=True))
    assert codex.typed == [("paste", "carry on"), ("keys", "Enter")]
    codex.typed.clear()
    codex.screen = list(_screen("a turn running"))
    message = refused(lambda: actions.paste("alpha", "coder-1", "again"))
    assert message.startswith("refused: coder-1 is working"), message
    assert codex.typed == []


# The captain's full control on Codex (#274, 17626009's pins): its approval view answered,
# and a paste submitted there, with no words of the owner's, as at Claude Code's chooser.


@pytest.mark.parametrize("call", ["press yes", "approve_prompt", "unblock"])
def test_the_captain_approves_a_codex_approval_with_no_owner_words(
    alpha: ProjectInfo, codex: Pane, call: str
) -> None:
    codex.screen = list(APPROVAL)
    codex.answers = {"1": list(IDLE)}
    target = {"project": "alpha", "label": "coder-1"}
    if call == "press yes":
        ok(actions.press("alpha", "coder-1", "yes", utterance=""))
    else:
        ok(actions.act(call, target, utterance=""))
    assert codex.keys == [("1",)], "its 1. Yes, proceed, pressed"
    assert suite.audit(alpha.id)[-1]["ok"] is True


@pytest.mark.parametrize("key", ["enter", "1", "2", "esc"])
def test_the_captain_presses_any_answering_key_at_a_codex_approval_with_no_owner_words(
    alpha: ProjectInfo, codex: Pane, key: str
) -> None:
    codex.screen = list(APPROVAL)
    codex.answers = {name: list(IDLE) for name in ("Enter", "1", "2", "Escape")}
    ok(actions.press("alpha", "coder-1", key, utterance=""))
    assert codex.keys == [(actions.KEYS[key],)]


def test_the_captain_submits_a_paste_into_a_codex_approval_with_no_owner_words(
    alpha: ProjectInfo, codex: Pane
) -> None:
    codex.screen = list(APPROVAL)
    codex.answers = {"Enter": list(IDLE)}
    result = ok(actions.paste("alpha", "coder-1", "go on", submit=True, utterance=""))
    assert codex.typed == [("paste", "go on"), ("keys", "Enter")]
    assert result["submitted"] is True
