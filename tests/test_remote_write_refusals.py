"""What a phone's write may hold, and how each refusal reads (review of #243, round 5).

Text that reaches a pane, a board or a terminal is held to one rule per path: what it
would press, reorder or run there is refused before anything is sent or written. A ref
that names nothing says which field named it, and a path the operating system refuses
is a refusal of the request, never ``write_failed`` or an ambiguity.
"""

from __future__ import annotations

import pytest

from aisquare.services.remote_server import RequestError, check_note_text, check_remote_text


def test_a_quick_answer_in_words_refuses_a_tab_as_typed_text_does() -> None:
    """A quick answer's words are typed into the pane as send-keys' text is, and a tab
    there is the Tab key (review of #243, round 5)."""
    from aisquare.services.remote_needs import _needs_answer_body

    with pytest.raises(RequestError) as refused:
        _needs_answer_body({"id": "nd_1", "text": "a\tb"})
    assert (refused.value.status, refused.value.error) == (400, "invalid")
    assert refused.value.message == (
        "'text' holds the control character U+0009 — send the pad's Tab key instead"
    )


@pytest.mark.parametrize(
    ("text", "said"),
    [
        (
            "hi \x9d52;c;cHduZWQ=\x9c end",
            "the control character U+009D — no key of the pad sends it",
        ),
        ("hi \x9b2J end", "the control character U+009B — no key of the pad sends it"),
        ("approve \u202edeleted\u202c", "the bidi control U+202E — it reorders how the text"),
        ("ok \u2066x\u2069", "the bidi control U+2066 — it reorders how the text"),
    ],
    ids=["c1-osc", "c1-csi", "rlo", "lri"],
)
def test_a_tell_holding_a_c1_or_bidi_control_is_refused(text: str, said: str) -> None:
    """A tell in mode ``auto`` may be filed as a board note, which ``aisquare board`` prints
    as it came, past Rich: its C1 controls and overrides were let through where the
    note's ``to`` refused them (sweep 3 of #243). One rule for every tell."""
    from aisquare.services.remote_actions import action_tell_text

    with pytest.raises(RequestError) as refused:
        action_tell_text({"text": text})
    assert (refused.value.status, refused.value.error) == (400, "invalid")
    assert refused.value.message.startswith(f"'text' holds {said}"), refused.value.message


def test_typed_text_holding_a_c1_control_is_refused() -> None:
    """A C1 control is a control character as an ASCII one is: nothing a phone types needs
    one, and the audit line's ``text=Nch`` could not show it (sweep 3 of #243)."""
    with pytest.raises(RequestError) as refused:
        check_remote_text("yes\x9bA")
    assert refused.value.message == (
        "'text' holds the control character U+009B — no key of the pad sends it"
    )


@pytest.mark.parametrize("char", ["\u202e", "\u2068"], ids=["rlo", "fsi"])
def test_a_switch_reason_holding_a_bidi_control_is_refused(char: str) -> None:
    """The board's ``switched`` event repeats the reason, and ``aisquare board`` prints it as
    it came: an override let through made the line read in another order (sweep 3 of
    #243)."""
    from aisquare.services.remote_actions import action_switch_reason

    with pytest.raises(RequestError) as refused:
        action_switch_reason({"reason": f"limit {char}hit"})
    assert refused.value.message == (
        f"'reason' holds U+{ord(char):04X}, a bidi control — a reason is one line of text"
    )


RIGHT_TO_LEFT_AND_JOINED = (
    "\u05e9\u05dc\u05d5\u05dd \u200f\u05e2\u05d5\u05dc\u05dd, \u0645\u0631\u062d\u0628\u0627, "
    "a\u00a0b, \U0001f469\u200d\U0001f4bb and e\u0301"
)
"""Text in two right-to-left scripts with a right-to-left mark, a no-break space, the joiner
inside an emoji and a combining accent: none of it is a control, and all of it is kept."""


def test_a_note_a_tell_and_a_reason_keep_right_to_left_text_and_what_joins_a_line() -> None:
    from aisquare.services.remote_actions import action_switch_reason, action_tell_text

    check_note_text(RIGHT_TO_LEFT_AND_JOINED, "text")
    check_remote_text(RIGHT_TO_LEFT_AND_JOINED)
    assert action_tell_text({"text": RIGHT_TO_LEFT_AND_JOINED}) == RIGHT_TO_LEFT_AND_JOINED
    assert action_switch_reason({"reason": RIGHT_TO_LEFT_AND_JOINED}) == RIGHT_TO_LEFT_AND_JOINED
