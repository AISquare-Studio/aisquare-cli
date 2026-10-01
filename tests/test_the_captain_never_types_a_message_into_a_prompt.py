"""No captain door types a message into a prompt nobody has read (review of #240, finding 2).

``tell``, the manager ask, ``wololo`` and ``attach_persona`` all end in ``fleet.tell``: one
paste and one Enter into an agent the fleet reads ``waiting``. The fleet reads a pane parked
at a permission prompt as waiting once its ``attention`` row has gone stale, and a quiet
agent without hooks as waiting always; there the Enter picks the highlighted "1. Yes". T1c
(13505) put a refusal of Claude Code's trust dialog in front of three of those doors. This is
the same refusal for every prompt the one reader sees, and for ``attach_persona``, which read
no screen at all.

The Actions suite replaces ``fleet.tell`` with a recorder. Here the REAL ``fleet.tell`` and
``fleet.attach_persona`` run over that suite's fake tmux, so "nothing typed" is read off the
pane itself, and on the code before the fix the Enter is seen landing in the chooser.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

from aisquare.core.store import store_session
from aisquare.core.tmux import Capture, TmuxError
from aisquare.models import FleetAgent, FleetAgentState, ProjectInfo, TeamEvent
from aisquare.services import fleet
from aisquare.services import team as team_service
from aisquare.services.captain import actions
from aisquare.services.captain import state as captain_state
from aisquare.services.captain.errors import Failed, Refused
from tests import captain_screens as shots
from tests import test_a_captains_first_prompt_is_never_typed_into_a_dialog as first_prompt_suite
from tests import test_captain_actions as actions_suite
from tests import test_fleet_service as fleet_suite
from tests.test_a_captains_first_prompt_is_never_typed_into_a_dialog import ScreenTmux
from tests.test_captain_actions import (
    Clock,
    FakeServer,
    Fleet,
    Pane,
    add_task,
    audit,
    ok,
    task_now,
    write_config,
)

# The Actions suite's fixtures, bound here so pytest finds them for this module's tests.
projects = actions_suite.projects
alpha = actions_suite.alpha
agents = actions_suite.agents
clock = actions_suite.clock
# And for the one test that uses no recorder at all: the fleet suite's fake tmux with a
# screen, its fake ``claude``, and its project that is no git checkout.
tmux = first_prompt_suite.tmux
claude_on_path = fleet_suite.claude_on_path
plain_project = fleet_suite.plain_project


@pytest.fixture
def fleet_rec(monkeypatch: pytest.MonkeyPatch) -> Fleet:
    """The Actions suite's fleet with the REAL ``tell`` and ``attach_persona`` put back over
    its fake tmux: what a door types lands in the pane, where a test reads it."""
    real_tell, real_attach = fleet.tell, fleet.attach_persona
    rec = Fleet(monkeypatch)
    monkeypatch.setattr(fleet, "tell", real_tell)
    monkeypatch.setattr(fleet, "attach_persona", real_attach)
    return rec


@dataclass(frozen=True)
class Door:
    """One captain tool that ends in ``fleet.tell``: its call (given the card ``wololo``
    converts to), the pane it types into, and the last words of its refusal."""

    call: Callable[[str], str]
    pane: str
    label: str
    verb: str
    text: str
    """What its paste carries when it does type."""


TOLD = "use a feature branch"
ASKED = "what is blocking the deploy?"

DOORS: dict[str, Door] = {
    "tell": Door(
        lambda card: actions.tell("alpha", "coder-1", TOLD), "%1", "coder-1", "told", TOLD
    ),
    "ask_manager": Door(
        lambda card: actions.ask_manager("alpha", ASKED, timeout=3),
        "%0",
        "manager",
        "asked",
        ASKED,
    ),
    "wololo": Door(
        lambda card: actions.wololo("alpha", "coder-1", card),
        "%1",
        "coder-1",
        "converted",
        "the captain reassigned you",
    ),
    "attach_persona": Door(
        lambda card: actions.attach_persona("alpha", "coder-1", "skeptic"),
        "%1",
        "coder-1",
        "attached",
        "the operator attached persona skeptic to you",
    ),
}

CHOOSER_ASKS = "Do you want to create probe2.txt?"
"""The question on runner2's real capture of Claude Code's permission chooser."""


def _parked(fleet_rec: Fleet, lines: list[str], pane_id: str = "%1") -> Pane:
    """An agent's pane showing ``lines`` while the fleet reads it WAITING — the recorder's
    default, and what ``fleet._derive`` says of a prompt parked past its attention row's
    freshness (the finding's coder-1, thirty minutes at "1. Yes")."""
    pane = fleet_rec.panes[pane_id]
    pane.screen = list(lines)
    return pane


def _said(call: Callable[[], str]) -> str:
    """What the door answered: its result, or the words of its refusal. Read AFTER the pane
    in each test, so a door that typed is shown by what it typed."""
    try:
        return call()
    except (Refused, Failed) as exc:
        return str(exc)


def _notes_to(project: ProjectInfo, label: str) -> list[TeamEvent]:
    with store_session() as store:
        events = store.filtered_events(project.id, since_seq=0, limit=500)
    return [event for event in events if event.to_role == label]


def _persona_of(project: ProjectInfo, label: str) -> str | None:
    with store_session() as store:
        row = store.fleet_agent_by_label(project.id, label, live_only=True)
    assert row is not None
    return row.persona


# --- the pin: a parked permission chooser, the fleet reading the agent waiting ----------------


@pytest.mark.parametrize("name", sorted(DOORS))
def test_no_door_types_a_message_into_a_parked_permission_prompt(
    name: str, alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    """The finding's repro, on the real chooser: the text was pasted, its Enter picked the
    highlighted "1. Yes", and the tool said "typed into its pane (it was waiting)". Each door
    now reads the pane first, refuses naming the prompt's question, and types nothing."""
    door = DOORS[name]
    card = add_task(alpha, "the new job")
    pane = _parked(fleet_rec, shots.REAL_CHOOSER, pane_id=door.pane)
    said = _said(lambda: door.call(card.id))
    assert pane.typed == [], "nothing reached the prompt: no paste, and no Enter to answer it"
    assert said.startswith(f"refused: a prompt is showing on {door.label}: {CHOOSER_ASKS}")
    assert f"nothing {door.verb}" in said
    last = audit(alpha.id)[-1]
    assert (last["tool"], last["ok"]) == (name, False)
    assert CHOOSER_ASKS in last["said"]


def test_the_repro_holds_with_the_real_fleet_reading_a_long_parked_prompt_as_waiting(
    tmux: ScreenTmux, claude_on_path: Path, plain_project: ProjectInfo
) -> None:
    """No recorder here: the real ``fleet.status_of`` and ``fleet.tell`` over the fleet
    suite's fake tmux. coder-1 asked for permission and has sat at the chooser past its
    attention row's freshness (``team._STALE_AFTER``), so the fleet stops believing the row,
    sees a quiet pane and reads it waiting — which is where the told text was typed and its
    Enter approved the command."""
    agent = fleet.spawn(plain_project, "coder", label="coder-1", worktree=False).agent
    fleet_suite._stale_board_session(
        agent, "attention", seen_ago=team_service._STALE_AFTER + timedelta(minutes=1)
    )
    tmux.screen = list(shots.REAL_CHOOSER)
    assert fleet.status_of(agent).state == "waiting", "the stale attention row is not believed"
    said = _said(lambda: actions.tell(agent.project_id, "coder-1", TOLD))
    assert tmux.reached() == [], "nothing reached the chooser"
    assert said.startswith(f"refused: a prompt is showing on coder-1: {CHOOSER_ASKS}")


@pytest.mark.parametrize(
    ("prompt", "asks"),
    [
        pytest.param(shots.REAL_CHOOSER, CHOOSER_ASKS, id="the real permission chooser"),
        pytest.param(
            shots.CHOOSER_NO_FIRST, "Allow this?", id="a chooser whose first option is No"
        ),
        pytest.param(shots.YES_NO, "Proceed? [y/N]", id="a [y/N] line"),
    ],
)
def test_tell_refuses_every_prompt_the_reader_sees_and_files_no_note_either(
    prompt: list[str],
    asks: str,
    alpha: ProjectInfo,
    agents: dict[str, FleetAgent],
    fleet_rec: Fleet,
) -> None:
    """Not only the trust dialog (T1c) and not only the chooser: any prompt. It is a refusal,
    said, and never a board note in the message's place."""
    pane = _parked(fleet_rec, prompt)
    said = _said(lambda: actions.tell("alpha", "coder-1", TOLD))
    assert pane.typed == []
    assert said.startswith(f"refused: a prompt is showing on coder-1: {asks}")
    assert "ask the owner and answer it first (press)" in said and "nothing told" in said
    assert _notes_to(alpha, "coder-1") == []


@pytest.mark.parametrize("state", ["waiting", "attention", "working"])
def test_tell_refuses_a_prompt_whatever_the_fleet_reads_as_it_refuses_the_trust_dialog(
    state: FleetAgentState, alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    """The trust dialog's rule (13505) for every prompt: the screen decides, never the fleet's
    word. ``fleet.tell`` reads the state again after this guard, and an attention row goes
    stale between two reads; a refusal that waited for ``waiting`` would lose that race."""
    pane = _parked(fleet_rec, shots.REAL_CHOOSER)
    fleet_rec.states["coder-1"] = state
    said = _said(lambda: actions.tell("alpha", "coder-1", TOLD))
    assert pane.typed == []
    assert said.startswith(f"refused: a prompt is showing on coder-1: {CHOOSER_ASKS}")


def test_a_tell_step_of_an_owner_action_is_refused_at_a_prompt_too(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    """``act`` runs the same effect: there is no second way to tell."""
    write_config('[captain.actions.nudge]\nsteps = ["tell {text}"]\n')
    pane = _parked(fleet_rec, shots.REAL_CHOOSER)
    said = _said(
        lambda: actions.act("nudge", {"project": "alpha", "label": "coder-1", "text": TOLD})
    )
    assert pane.typed == []
    assert said.startswith("refused: action nudge stopped at step 1 (tell use a feature branch)")
    assert f"a prompt is showing on coder-1: {CHOOSER_ASKS}" in said


def test_the_manager_ask_is_refused_at_once_and_waits_for_no_answer(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    """Typed into the manager's chooser, the question approved a command and then waited its
    whole timeout for an answer nobody was asked for."""
    pane = _parked(fleet_rec, shots.REAL_CHOOSER, pane_id="%0")
    said = _said(lambda: actions.ask_manager("alpha", ASKED, timeout=3))
    assert pane.typed == []
    assert said.startswith(f"refused: a prompt is showing on manager: {CHOOSER_ASKS}")
    assert clock.slept == [], "refused before the wait began"
    assert captain_state.waiting_on() is None


def test_wololo_moves_no_claim_when_the_agent_sits_at_a_prompt(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    """Checked before any claim moves, as the trust dialog is (13503): nothing to undo, and
    the reassignment's Enter never reaches the chooser."""
    old = add_task(alpha, "the old job")
    new = add_task(alpha, "the new job")
    team_service.claim_task(old.id, session_ref="sess-coder-1")
    pane = _parked(fleet_rec, shots.REAL_CHOOSER)
    said = _said(lambda: actions.wololo("alpha", "coder-1", new.id))
    assert pane.typed == []
    assert (task_now(new.id).status, task_now(new.id).claimed_by) == ("todo", None)
    assert (task_now(old.id).status, task_now(old.id).claimed_by) == ("doing", "sess-coder-1")
    assert said.startswith(f"refused: a prompt is showing on coder-1: {CHOOSER_ASKS}")
    assert captain_state.pop_undo() is None, "nothing moved, so bt has nothing to undo"


def test_attach_persona_attaches_nothing_when_the_agent_sits_at_a_prompt(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    """``fleet.attach_persona`` types the briefing through ``fleet.tell`` and read no screen.
    Refused before the row, the board event or the briefing: nothing half-attached."""
    pane = _parked(fleet_rec, shots.REAL_CHOOSER)
    said = _said(lambda: actions.attach_persona("alpha", "coder-1", "skeptic"))
    assert pane.typed == []
    assert said.startswith(f"refused: a prompt is showing on coder-1: {CHOOSER_ASKS}")
    assert _persona_of(alpha, "coder-1") is None
    assert _notes_to(alpha, "coder-1") == [], "no persona_attached event, no briefing note"


@pytest.mark.parametrize("state", ["waiting", "working"])
def test_attach_persona_never_types_into_the_trust_dialog(
    state: FleetAgentState, alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    """The finding's second repro: a freshly spawned agent still on Claude Code's trust
    dialog. The briefing's Enter picked the highlighted "No, exit", the agent exited, and
    the result said ``delivered='typed'``. Refused by name, as T1c refuses every other door
    (a fresh agent reads working: refused there too, never a note it will not read)."""
    pane = _parked(fleet_rec, shots.REAL_TRUST)
    fleet_rec.states["coder-1"] = state
    said = _said(lambda: actions.attach_persona("alpha", "coder-1", "skeptic"))
    assert pane.typed == [], "the dialog is untouched, so the agent lives"
    assert "the trust dialog is showing on coder-1: trust this folder first" in said
    assert "nothing attached" in said
    assert _persona_of(alpha, "coder-1") is None


@pytest.mark.parametrize("name", sorted(DOORS))
def test_a_pane_that_cannot_be_read_refuses_every_door(
    name: str,
    alpha: ProjectInfo,
    agents: dict[str, FleetAgent],
    fleet_rec: Fleet,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prompt can be ruled out only by reading the pane, so nothing is typed blind (T1c's
    rule for tell, the manager ask and wololo; new for attach_persona)."""
    door = DOORS[name]
    card = add_task(alpha, "the new job")
    pane = fleet_rec.panes[door.pane]

    def unreadable(*args: object, **kwargs: object) -> Capture:
        raise TmuxError("tmux capture-pane failed: no such pane")

    monkeypatch.setattr(FakeServer, "capture", unreadable)
    said = _said(lambda: door.call(card.id))
    assert pane.typed == []
    assert f"{door.label}'s pane could not be read" in said
    assert f"nothing {door.verb}" in said


# --- the control: an idle input box still gets the text ---------------------------------------


@pytest.mark.parametrize("name", sorted(DOORS))
@pytest.mark.parametrize(
    "idle",
    [
        pytest.param(shots.REAL_IDLE, id="the real idle pane"),
        pytest.param(shots.REAL_IDLE_AFTER_STOP, id="the real idle box after the first Stop"),
        pytest.param(shots.REAL_QUOTED, id="a chooser quoted above the box"),
    ],
)
def test_every_door_still_types_into_an_idle_input_box(
    idle: list[str],
    name: str,
    alpha: ProjectInfo,
    agents: dict[str, FleetAgent],
    fleet_rec: Fleet,
    clock: Clock,
) -> None:
    """With the box drawn nothing below the transcript is a prompt (13264), whatever a reply
    above it quotes: one paste carrying the message, then exactly one Enter."""
    door = DOORS[name]
    card = add_task(alpha, "the new job")
    pane = _parked(fleet_rec, idle, pane_id=door.pane)
    said = _said(lambda: door.call(card.id))
    assert [kind for kind, _ in pane.typed] == ["paste", "keys"], said
    assert door.text in pane.typed[0][1]
    assert pane.typed[1] == ("keys", "Enter")
    assert "a prompt is showing" not in said


def test_a_tell_to_an_idle_agent_says_it_was_typed(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    pane = _parked(fleet_rec, shots.REAL_IDLE)
    result = ok(actions.tell("alpha", "coder-1", TOLD))
    assert (result["delivered"], result["how"]) == (True, "typed into its pane (it was waiting)")
    assert pane.typed == [("paste", TOLD), ("keys", "Enter")]


def test_a_tell_to_a_busy_agent_with_no_prompt_showing_is_still_a_board_note(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    """What ``fleet.tell`` does for an agent mid-turn is its own, and unchanged: the box is
    drawn, so no prompt shows, and the message waits on the board."""
    pane = _parked(fleet_rec, shots.REAL_WORKING)
    fleet_rec.states["coder-1"] = "working"
    result = ok(actions.tell("alpha", "coder-1", TOLD))
    assert result["delivered"] is False and "it is working" in result["how"]
    assert pane.typed == []
    assert [event.text for event in _notes_to(alpha, "coder-1")] == [TOLD]


def test_an_idle_agent_is_converted_and_given_its_persona_as_before(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    new = add_task(alpha, "the new job")
    _parked(fleet_rec, shots.REAL_IDLE)
    converted = ok(actions.wololo("alpha", "coder-1", new.id))
    assert converted["claimed"] == new.id
    assert (task_now(new.id).status, task_now(new.id).claimed_by) == ("doing", "sess-coder-1")
    attached = ok(actions.attach_persona("alpha", "coder-1", "skeptic"))
    assert (attached["persona"], attached["delivered"]) == ("skeptic", "typed")
    assert _persona_of(alpha, "coder-1") == "skeptic"
