"""``press`` tells the prompt it answered from the next one in line (review of #240, finding 10).

A key that answers is read back, and a prompt still showing is a said failure: the captain
never reports a press the prompt ignored (T1b). The read-back compared the QUESTION line
alone, and Claude Code asks the same one of every Bash command ("Do you want to proceed?").
With two calls queued, the first prompt answered and the second drawn before the first
read-back looked like a key that did nothing. ``Failed`` is retryable, and the retried yes
approved the second command, which nobody had read.

The screens are fakes in the shape of the real chooser (a rule, what is asked about, the
question, the options, the footer: the capture in ``docs/runbooks/captain-acceptance.md``),
because the second prompt has to draw AT ONCE: the fake pane swaps its screen as the key
lands, which no real capture can do.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from aisquare.models import FleetAgent, ProjectInfo
from aisquare.services.captain import actions
from aisquare.services.captain import screen as screen_reader
from tests import captain_screens as shots
from tests import test_captain_actions as actions_suite
from tests.test_captain_actions import Clock, Fleet, Pane, audit, ok, refused

# The Actions suite's fixtures, bound here so pytest finds them for this module's tests.
projects = actions_suite.projects
alpha = actions_suite.alpha
fleet_rec = actions_suite.fleet_rec
agents = actions_suite.agents
clock = actions_suite.clock

MARK = shots.MARK
ASKS = "Do you want to proceed?"
"""What Claude Code asks of every Bash command: the line two queued prompts share."""


def _chooser(*about: str, options: Sequence[str] = ("Yes", "No"), marked: int = 1) -> list[str]:
    """Claude Code's permission chooser where the input box was: a rule, what the question
    is about (``about``), the question, the numbered options with one highlighted, the
    footer. Above the rule, the transcript."""
    return [
        "● Two commands to run.",
        shots.REAL_RULE,
        " Bash command",
        "",
        *(f"   {line}" for line in about),
        "",
        f" {ASKS}",
        *(
            f" {MARK} {number}. {text}" if number == marked else f"   {number}. {text}"
            for number, text in enumerate(options, start=1)
        ),
        "",
        " Esc to cancel · Tab to amend",
    ]


GIT_STATUS_ABOUT = ("git status", "Show the working tree status")
GIT_STATUS = _chooser(*GIT_STATUS_ABOUT)
RM_BUILD = _chooser("rm -rf build", "Remove the build directory")


def _asking(fleet_rec: Fleet, screen: list[str], **answers: list[str]) -> Pane:
    """coder-1 at ``screen``, asking; ``answers`` is what the screen becomes as a key lands."""
    pane = fleet_rec.panes["%1"]
    pane.screen = list(screen)
    pane.answers = dict(answers)
    fleet_rec.states["coder-1"] = "attention"
    return pane


# --- the pin: prompt A is answered, and prompt B draws at once --------------------------------


def test_a_press_that_answered_is_not_a_failure_when_the_next_prompt_asks_the_same_question(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    """The finding's scenario: parallel Bash calls queue a prompt for ``git status``, then one
    for ``rm -rf build``. Yes answers the first; the second draws with the identical question
    before the first read-back. That was "the key did not answer it", a retryable error, and
    the retry's 1 approved ``rm -rf build``. It is the first prompt answered: said as that,
    with what shows now, and the second prompt is left for someone to read."""
    pane = _asking(fleet_rec, GIT_STATUS, **{"1": RM_BUILD})
    result = ok(actions.press("alpha", "coder-1", "yes"))
    assert (result["sent"], result["answered"], result["prompt"]) == ("1", True, ASKS)
    assert pane.keys == [("1",)], "one key, for the prompt that was read"
    assert pane.screen == RM_BUILD, "the next prompt is still there, unanswered"
    last = audit(alpha.id)[-1]
    assert last["ok"] is True
    assert last["said"] == (
        f"pressed yes (1) in coder-1: the prompt is answered; another shows now: {ASKS}"
    )


def test_the_same_question_with_other_options_is_another_prompt_too(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    """The whole prompt is compared, its options included: the real chooser's second option
    names the command it would stop asking about."""
    first = _chooser("npm test", options=("Yes", "Yes, and don't ask again for npm test", "No"))
    second = _chooser("npm test", options=("Yes", "Yes, and don't ask again for npm", "No"))
    pane = _asking(fleet_rec, first, **{"1": second})
    assert ok(actions.press("alpha", "coder-1", "yes"))["answered"] is True
    assert pane.keys == [("1",)]


def test_a_y_n_line_asked_again_about_something_else_is_another_prompt(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    again = [
        "Installing 3 packages.",
        "Proceed? [y/N] y",
        "Removing 5 packages.",
        "Proceed? [y/N] ",
    ]
    pane = _asking(fleet_rec, shots.YES_NO, y=again)
    assert ok(actions.press("alpha", "coder-1", "yes"))["answered"] is True
    assert pane.keys == [("y",)]


# --- the control: the identical prompt still showing is still a said failure ------------------


@pytest.mark.parametrize(("key", "sent"), [("y", "y"), ("yes", "1"), ("9", "9")])
def test_the_identical_prompt_still_showing_after_the_key_is_still_a_failure(
    key: str,
    sent: str,
    alpha: ProjectInfo,
    agents: dict[str, FleetAgent],
    fleet_rec: Fleet,
    clock: Clock,
) -> None:
    """As today (13265): a key the prompt ignored is an error, never a success."""
    pane = _asking(fleet_rec, GIT_STATUS)
    message = refused(lambda: actions.press("alpha", "coder-1", key))
    assert message.startswith("error: pressed ")
    assert f"in coder-1 but the prompt is still showing: {ASKS}" in message
    assert "the key did not answer it" in message
    assert pane.keys == [(sent,)]
    assert audit(alpha.id)[-1]["ok"] is False
    assert sum(clock.slept) == pytest.approx(actions.READBACK_POLLS * actions.READBACK_POLL_S)


def test_the_same_prompt_with_its_highlight_moved_is_not_another_prompt(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    """A key that only moved the mark answered nothing: comparing the whole prompt must not
    turn that into a success."""
    pane = _asking(fleet_rec, GIT_STATUS, **{"2": _chooser(*GIT_STATUS_ABOUT, marked=2)})
    message = refused(lambda: actions.press("alpha", "coder-1", "2"))
    assert f"the prompt is still showing: {ASKS}" in message
    assert pane.keys == [("2",)]


def test_the_same_prompt_redrawn_at_another_width_is_not_another_prompt(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    """The fleet UI resizes a window when it attaches, and Claude Code redraws: the same
    words on other rows are the same prompt."""
    wide = _chooser("git log --oneline --decorate --graph", "Show the history")
    narrow = _chooser("git log --oneline", "  --decorate --graph", "Show the", "  history")
    pane = _asking(fleet_rec, wide, y=narrow)
    message = refused(lambda: actions.press("alpha", "coder-1", "y"))
    assert f"the prompt is still showing: {ASKS}" in message
    assert pane.keys == [("y",)]


def test_a_prompt_that_is_gone_is_said_gone_as_before(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    _asking(fleet_rec, GIT_STATUS, **{"1": shots.REAL_IDLE})
    assert ok(actions.press("alpha", "coder-1", "yes"))["answered"] is True
    assert audit(alpha.id)[-1]["said"] == "pressed yes (1) in coder-1: the prompt is gone"


# --- the reader: the prompt, whole ------------------------------------------------------------


def test_the_reader_keeps_the_whole_prompt_not_its_question_alone() -> None:
    first = screen_reader.prompt_showing(GIT_STATUS)
    second = screen_reader.prompt_showing(RM_BUILD)
    assert first is not None and second is not None
    assert first.question == second.question == ASKS
    assert first.whole != second.whole
    assert first.whole == (
        "Bash command git status Show the working tree status Do you want to proceed? "
        "1. Yes 2. No Esc to cancel · Tab to amend"
    ), "what follows the rule, as one line: never the transcript above it, never the mark"
    moved = screen_reader.prompt_showing(_chooser(*GIT_STATUS_ABOUT, marked=2))
    assert moved is not None and moved.whole == first.whole


def test_the_reader_keeps_the_whole_of_every_real_capture_it_reads_as_a_prompt() -> None:
    chooser = screen_reader.prompt_showing(shots.REAL_CHOOSER)
    assert chooser is not None
    assert "Do you want to create probe2.txt? 1. Yes 2. Yes, and switch to" in chooser.whole
    assert chooser.whole.endswith("3. No Esc to cancel · Tab to amend")
    yes_no = screen_reader.prompt_showing(shots.YES_NO)
    assert yes_no is not None and yes_no.whole == "Installing 3 packages. Proceed? [y/N]"
    trust = screen_reader.prompt_showing(shots.REAL_TRUST)
    assert trust is not None and "No, exit Yes, I trust this folder" in trust.whole
