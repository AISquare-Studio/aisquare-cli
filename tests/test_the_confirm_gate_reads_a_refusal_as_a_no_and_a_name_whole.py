"""The captain's confirm gate: a refusal that names the agent is still a refusal, and a name
is read whole (review of #240, finding 1).

``stop``, ``restart`` and ``spawn`` take ``confirm=true`` only on the owner's own words, and
the bundled persona passes every owner answer with ``confirm=true``, a no included: the server
is the gate. It took "No, leave coder-1 running" as the confirmation, because those words name
coder-1 and only a bare "no" counted as a no. The owner's rule (2026-09-30) is CLEAR refusals
only, not any negation anywhere: words that begin with a no, a negation right before the
tool's own verb, "leave" or "keep" right before the target's name. "Stop coder-1, no need for
it anymore" is still an order.

Names, the other half: a hyphen is part of a label or a project name ("coder-1-2" is never
coder-1), and another name is looked for on EVERY row of the board, ended agents included,
before the call's own name is set aside.

The fleet is the recorder of ``tests/test_captain_actions.py``; the board and the state file
are real.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aisquare.core.store import store_session
from aisquare.core.workspace import project_id_for
from aisquare.models import FleetAgent, ProjectInfo
from aisquare.services.captain import actions, words
from tests import test_captain_actions as actions_suite
from tests.test_captain_actions import Fleet, audit, ok, refused

# The suite's fixtures, bound here so pytest finds them for this module's tests.
projects = actions_suite.projects
alpha = actions_suite.alpha
fleet_rec = actions_suite.fleet_rec
agents = actions_suite.agents


def _said_no(utterance: str, action: str) -> str:
    """What a whole-utterance "No." is answered, word for word (``actions._confirmation``)."""
    return (
        f"refused: the owner said no ({utterance!r}) — nothing done, and the question "
        f'"{action}?" is closed; ask again only if they raise it again'
    )


def _stop(utterance: str, label: str = "coder-1", *, force: bool = False) -> str:
    return actions.stop("alpha", label, force=force, confirm=True, utterance=utterance)


def _onboard(tmp_path: Path, name: str) -> ProjectInfo:
    root = (tmp_path / name).resolve()
    root.mkdir()
    with store_session() as store:
        return store.onboard_project(ProjectInfo(id=project_id_for(root), root=root))


# --- A. a refusal is a no, even when it names the target ---------------------------------------


def test_the_refusal_vocabulary_is_the_owners_list() -> None:
    """The owner chose these words on 2026-09-30 (clear refusals only): change them with the
    owner, not in passing. A no begins the words; a negation stands right before the tool's
    own verb; leave or keep stands at most two words before the target's name."""
    assert words.NO_WORDS == ("no", "nope", "nah", "never", "negative", "don't", "dont", "do not")
    assert words.NEGATIONS == (
        "don't",
        "dont",
        "do not",
        "not",
        "never",
        "shouldn't",
        "shouldnt",
        "mustn't",
        "mustnt",
    )
    assert words.ACTION_VERBS == {
        "stop": ("stop", "force stop"),
        "restart": ("restart",),
        "spawn": ("spawn",),
    }
    assert (words.KEEPS, words.KEEP_REACH) == (("leave", "keep"), 2)
    assert set(words.NO_WORDS) < set(words.NEGATIVES), "every no that begins is a no alone"


@pytest.mark.parametrize(
    "answer",
    ["No, leave coder-1 running", "Leave coder-1 alone", "Don't stop coder-1, it's almost done"],
)
def test_the_owners_refusal_of_the_captains_question_stops_nothing_and_closes_it(
    alpha: ProjectInfo,
    agents: dict[str, FleetAgent],
    fleet_rec: Fleet,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
) -> None:
    """The finding's repro: "Stop it.", the server's question, then a refusal that names the
    agent. The persona passes it with confirm=true as it passes every answer, and coder-1
    was stopped. Now it is the no it is: nothing stops, and a later yes revives nothing."""
    actions_suite._at_wall(monkeypatch, 1000.0)
    assert refused(lambda: _stop("Stop it.")) == "Stop coder-1 in alpha?"
    message = refused(lambda: _stop(answer))
    assert message.startswith(_said_no(answer, "stop coder-1 in alpha")), message
    assert refused(lambda: _stop("Yes.")) == "Stop coder-1 in alpha?", "closed: asked afresh"
    assert "no question was pending" in audit(alpha.id)[-1]["said"]
    assert fleet_rec.calls == [], "nothing stopped"


@pytest.mark.parametrize(
    "utterance",
    [
        # the words begin with a no
        "No, leave coder-1 running",
        "Nope, coder-1 stays",
        "Nah, coder-1 is fine",
        "Never mind coder-1",
        "Negative, coder-1 carries on",
        "Don't stop coder-1, it's almost done",
        "Dont, coder-1 is busy",
        "Do not touch coder-1",
        # a negation right before the tool's own verb, anywhere in the words
        "Please don't stop coder-1, it's almost done",
        "please dont stop coder-1",
        "I said do not stop coder-1",
        "I would not stop coder-1",
        "You must never stop coder-1",
        "You shouldn't stop coder-1, it is mid-merge",
        "you mustnt stop coder-1",
        # a curly apostrophe, as a phone or the voice page types it
        "Don\u2019t stop coder-1",
        "Please don\u2019t stop coder-1",
        # leave or keep, then the target within two words: its label, its role, its project
        "Leave coder-1 alone",
        "Leave coder 1 alone",
        "keep coder-1 running",
        "Just leave the coder alone",
        "Better keep alpha's coder going",
        "Leave Dave's coder alone",
        # case and punctuation are ignored
        "LEAVE CODER-1 ALONE!!",
    ],
)
def test_a_refusal_that_names_the_agent_is_still_a_refusal(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, utterance: str
) -> None:
    """Each of these names coder-1, its role or its project, so each was a confirmation.
    A refusal is answered exactly as a bare "No." is: the same words, the same audit."""
    message = refused(lambda: _stop(utterance))
    assert message.startswith(_said_no(utterance, "stop coder-1 in alpha")), message
    assert fleet_rec.calls == [], "nothing stopped"
    last = audit(alpha.id)[-1]
    assert (last["tool"], last["ok"], last["utterance"]) == ("stop", False, utterance)
    assert last["said"] == _said_no(utterance, "stop coder-1 in alpha"), last["said"]


def test_a_refused_force_stop_is_a_refusal_too(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    """The gate's own question says "Force-stop coder-1 in alpha?", so that is its verb too."""
    utterance = "Please don't force-stop coder-1"
    message = refused(lambda: _stop(utterance, force=True))
    assert message.startswith(_said_no(utterance, "force-stop coder-1 in alpha")), message
    assert fleet_rec.calls == []


@pytest.mark.parametrize(
    "utterance",
    ["don't restart coder-1", "I would not restart coder-1", "Leave coder-1 as it is"],
)
def test_a_refused_restart_is_never_a_restart(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, utterance: str
) -> None:
    message = refused(
        lambda: actions.restart("alpha", "coder-1", confirm=True, utterance=utterance)
    )
    assert message.startswith(_said_no(utterance, "restart coder-1 in alpha")), message
    assert fleet_rec.calls == [], "nothing restarted"


@pytest.mark.parametrize(
    "utterance",
    ["Don't spawn a coder", "Please do not spawn a coder", "Let's not spawn a coder yet"],
)
def test_a_refused_spawn_is_never_a_spawn(
    alpha: ProjectInfo, fleet_rec: Fleet, utterance: str
) -> None:
    message = refused(lambda: actions.spawn("alpha", "coder", confirm=True, utterance=utterance))
    assert message.startswith(_said_no(utterance, "spawn a coder in alpha")), message
    assert fleet_rec.calls == [], "nothing spawned"


@pytest.mark.parametrize(
    "utterance",
    [
        "Stop coder-1",
        "Stop coder-1, no need for it anymore",  # a no further in is no refusal
        "Stop coder-1 and keep its worktree",  # keep, but not the agent
        "Stop coder-1 and keep the worktree of coder-1",  # the name is past keep's two words
        "Stop coder-1, it is not responding",  # a negation, but not of the stop
        "Why don't you stop coder-1?",  # nor right before it
        "Stop coder-1 and do not restart it",  # another tool's verb
        "coder-1 won't stop, so stop coder-1",  # won't says what the agent does
    ],
)
def test_an_order_with_a_no_or_a_keep_in_it_is_still_an_order(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, utterance: str
) -> None:
    """The owner chose clear refusals over any negation anywhere: these are orders, and the
    captain must not ask twice for them."""
    ok(_stop(utterance))
    assert fleet_rec.names() == ["stop"]


def test_a_restart_that_says_why_is_still_a_restart(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    ok(
        actions.restart(
            "alpha", "coder-1", confirm=True, utterance="Restart coder-1, it is not responding"
        )
    )
    assert fleet_rec.names() == ["restart"]


def test_a_bare_yes_still_answers_the_captains_question(
    alpha: ProjectInfo,
    agents: dict[str, FleetAgent],
    fleet_rec: Fleet,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions_suite._at_wall(monkeypatch, 1000.0)
    assert refused(lambda: _stop("Stop it.")) == "Stop coder-1 in alpha?"
    ok(_stop("yes"))
    assert fleet_rec.names() == ["stop"]


def test_leave_before_another_agents_name_is_no_refusal_for_this_one(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    """The words "Leave coder-1 alone and stop coder-2" are a no for coder-1. For coder-2 the
    "coder" after leave is part of coder-1's label, not coder-2's role, so they are never "the
    owner said no": they name two agents, and the gate asks first, as it did (13570)."""
    utterance = "Leave coder-1 alone and stop coder-2"
    message = refused(lambda: _stop(utterance, "coder-1"))
    assert message.startswith(_said_no(utterance, "stop coder-1 in alpha")), message
    message = refused(lambda: _stop(utterance, "coder-2"))
    assert "name coder-1, not coder-2" in message, message
    assert 'ask first: "stop coder-2 in alpha?"' in message, message
    assert fleet_rec.calls == []


def test_leave_before_a_longer_label_is_no_refusal_for_the_shorter_one(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    """The words "Leave coder-1-2 alone and stop coder-1" are an order for coder-1: the label
    after leave is coder-1-2's, whole. They name two agents, so the gate asks first. They
    were the confirmation: the call's own "coder-1" was taken out of both names."""
    with store_session() as store:
        store.upsert_fleet_agent(actions_suite._row(alpha, "coder-1-2", "coder", "%6"))
    message = refused(lambda: _stop("Leave coder-1-2 alone and stop coder-1"))
    assert "name coder-1-2, not coder-1" in message, message
    assert 'ask first: "stop coder-1 in alpha?"' in message, message
    assert fleet_rec.calls == [], "nothing stopped"


# --- B. a name is read whole, on every row of the board ----------------------------------------


@pytest.mark.parametrize(
    "utterance", ["stop coder-1 in alpha-omega", "stop coder 1 in alpha omega"]
)
def test_a_longer_project_name_is_another_project_never_this_one(
    alpha: ProjectInfo,
    agents: dict[str, FleetAgent],
    fleet_rec: Fleet,
    tmp_path: Path,
    utterance: str,
) -> None:
    """The call's own "alpha" was set aside first, so "alpha-omega" was never looked at: the
    words for alpha-omega's coder stopped alpha's."""
    _onboard(tmp_path, "alpha-omega")
    message = refused(lambda: _stop(utterance))
    assert "name alpha-omega, not alpha" in message, message
    assert 'ask first: "stop coder-1 in alpha?"' in message, message
    assert fleet_rec.calls == [], "nothing stopped"


@pytest.mark.parametrize(
    "utterance", ["stop coder-1 in alpha-omega", "stop coder 1 in alpha omega"]
)
def test_a_shorter_project_name_is_never_read_into_the_longer_one(
    alpha: ProjectInfo, fleet_rec: Fleet, tmp_path: Path, utterance: str
) -> None:
    """The other way round stays an order: alpha is not what "alpha-omega" names."""
    omega = _onboard(tmp_path, "alpha-omega")
    ok(actions.stop("alpha-omega", "coder-1", confirm=True, utterance=utterance))
    assert fleet_rec.calls == [("stop", {"project": omega.id, "label": "coder-1", "force": False})]


def test_a_hyphenated_name_no_project_carries_is_not_this_project_either(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    """A hyphen is part of a name: with no alpha-omega on the board at all, the words still
    name something that is not alpha, and the captain guessed."""
    message = refused(lambda: _stop("stop coder-1 in alpha-omega"))
    assert "name alpha-omega, not alpha" in message, message
    assert fleet_rec.calls == []


def test_a_longer_label_is_another_agent_never_this_one(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    """coder-1-2 is the label a second "coder-1" gets (``fleet.next_label``). Its words
    stopped coder-1: the call's own "coder-1" was set aside first."""
    with store_session() as store:
        store.upsert_fleet_agent(actions_suite._row(alpha, "coder-1-2", "coder", "%6"))
    message = refused(lambda: _stop("stop coder-1-2"))
    assert "name coder-1-2, not coder-1" in message, message
    assert fleet_rec.calls == [], "nothing stopped"
    ok(_stop("stop coder-1-2", "coder-1-2"))
    assert fleet_rec.calls == [
        ("stop", {"project": alpha.id, "label": "coder-1-2", "force": False})
    ]


def test_a_longer_label_no_row_carries_is_not_this_agent_either(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    message = refused(lambda: _stop("stop coder-1-2"))
    assert "name coder-1-2, not coder-1" in message, message
    assert fleet_rec.calls == []


def test_an_ended_agents_label_is_still_another_agents_name(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    """Only live rows were looked at: once coder-1 had ended, "Stop coder-1" named nothing
    else, the role "coder" in it named coder-2, and coder-2 was stopped."""
    with store_session() as store:
        store.end_fleet_agent(agents["coder-1"].id)
    message = refused(lambda: _stop("Stop coder-1", "coder-2"))
    assert "name coder-1, not coder-2" in message, message
    assert 'ask first: "stop coder-2 in alpha?"' in message, message
    assert fleet_rec.calls == [], "nothing stopped"


def test_an_ended_agents_own_label_still_names_it(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    """A restart acts on an ended agent, and a label spawned again has an ended row and a
    live one: neither row of the call's own label is "another agent"."""
    with store_session() as store:
        store.end_fleet_agent(agents["coder-1"].id)
    ok(actions.restart("alpha", "coder-1", confirm=True, utterance="Restart coder-1"))
    with store_session() as store:
        store.upsert_fleet_agent(actions_suite._row(alpha, "coder-1", "coder", "%7"))
    ok(_stop("Stop coder-1"))
    assert fleet_rec.names() == ["restart", "stop"]


def test_the_agent_named_with_its_project_is_confirmed_as_before(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    ok(_stop("Stop coder-1 in alpha"))
    assert fleet_rec.calls == [("stop", {"project": alpha.id, "label": "coder-1", "force": False})]


def test_a_label_said_with_a_space_is_still_that_label(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    """The voice page hears "atlas 2" for atlas-2: a hyphen is part of a name, and a name may
    still be said without it."""
    with store_session() as store:
        store.upsert_fleet_agent(actions_suite._row(alpha, "atlas-2", "tester", "%6"))
    ok(_stop("stop atlas 2", "atlas-2"))
    assert fleet_rec.names() == ["stop"]


def test_the_calls_own_longer_name_is_never_read_as_the_shorter_project(
    alpha: ProjectInfo, fleet_rec: Fleet, tmp_path: Path
) -> None:
    """What setting the call's own names aside is for (13570): "aisquare cli", said with a
    space, names aisquare-cli, not the project called aisquare."""
    cli = _onboard(tmp_path, "aisquare-cli")
    _onboard(tmp_path, "aisquare")
    ok(actions.spawn("aisquare-cli", "coder", confirm=True, utterance="spawn one in aisquare cli"))
    assert [(name, call["project"]) for name, call in fleet_rec.calls] == [("spawn", cli.id)]


def test_the_projects_name_inside_the_agents_own_label_is_no_other_name(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    """The label "alpha-coder" holds the project's "alpha", and is the call's own: a longer
    name is another name only when it is not one of the call's own."""
    with store_session() as store:
        store.upsert_fleet_agent(actions_suite._row(alpha, "alpha-coder", "tester", "%6"))
    ok(_stop("stop alpha-coder", "alpha-coder"))
    assert fleet_rec.names() == ["stop"]


def test_a_dash_between_words_is_punctuation_not_part_of_a_name(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    """Only a hyphen that joins two words is part of a name."""
    ok(_stop("Stop coder-1 - it is done"))
    assert fleet_rec.names() == ["stop"]


@pytest.mark.parametrize(
    ("utterance", "why"),
    [
        ("stop coder-2-b", "name coder-2, not coder-1"),
        ("stop the coder in beta-2", "name beta, not alpha"),
    ],
)
def test_another_name_inside_a_longer_one_is_still_another_name(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, utterance: str, why: str
) -> None:
    """Reading the call's own names whole takes no refusal away: whatever "coder-2-b" and
    "beta-2" are, they hold another agent's and another project's name, and the role "coder"
    in the words is no confirmation for coder-1 beside them (13570)."""
    message = refused(lambda: _stop(utterance))
    assert why in message, message
    assert fleet_rec.calls == []
