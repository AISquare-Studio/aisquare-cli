"""One reader, two questions (13278): ``say`` asks "may I type text here?", ``press`` asks
"is there a prompt to answer, and with which key?" — and on every captured screen the two
answers agree about what is on the screen."""

from __future__ import annotations

import pytest

from aisquare.services.captain import screen
from tests import captain_screens as shots

MARK = shots.MARK


@pytest.mark.parametrize(
    ("name", "lines", "prompt", "modal"),
    [
        ("a real idle pane", shots.REAL_IDLE, None, None),
        (
            "the real permission chooser",
            shots.REAL_CHOOSER,
            ("chooser", "1", "Escape"),
            "a numbered choice",
        ),
        ("the real trust dialog", shots.REAL_TRUST, ("trust", None, None), "the trust dialog"),
        ("a [y/N] line", shots.YES_NO, ("yn", "y", "n"), None),
        ("a chooser quoted above the box", shots.REAL_QUOTED, None, None),
        ("a box whose footer names Esc", shots.BOX_WITH_ESC_FOOTER, None, None),
        ("a highlighted list mid-turn, no footer", shots.MID_TURN_LIST, None, None),
        (
            "a chooser whose first option is No",
            shots.CHOOSER_NO_FIRST,
            ("chooser", "2", "Escape"),
            "a numbered choice",
        ),
        ("a working pane quoting dialog words", shots.WORKING_QUOTING, None, None),
        ("the trust sentence quoted mid-turn, no footer", shots.TRUST_QUOTED_MID_TURN, None, None),
        (
            "the rating survey above the box",
            shots.RATING_ABOVE_BOX,
            None,
            "the session-rating prompt",
        ),
        ("a model picker", shots.MODEL_PICKER, None, "a dialog waiting for Enter or Esc"),
        ("an empty pane", [], None, None),
        ("a shell", ["$ ", "ready"], None, None),
    ],
)  # fmt: skip
def test_both_views_of_the_one_reader_agree_on_every_captured_screen(
    name: str,
    lines: list[str],
    prompt: tuple[str, str | None, str | None] | None,
    modal: str | None,
) -> None:
    got = screen.prompt_showing(lines)
    shape = (got.shape, got.yes_key, got.no_key) if got is not None else None
    assert shape == prompt, f"{name}: prompt_showing"
    assert screen.modal_showing(lines) == modal, f"{name}: modal_showing"
    # The agreement: a prompt to ANSWER is never a screen say may type into, and the box drawn
    # is the same fact for both.
    if got is not None and got.shape != "yn":
        assert modal is not None, f"{name}: press sees a prompt that say would type over"
    if modal in ("a numbered choice", "the trust dialog"):
        assert got is not None, f"{name}: say refuses a prompt that press cannot answer"


def test_the_box_is_read_with_a_wrapped_draft_and_a_dim_suggestion() -> None:
    long_draft = [
        shots.RULE,
        f"{MARK} a draft that wraps",
        "  onto a second line",
        shots.RULE,
        "  ⏸ manual mode on",
    ]
    assert screen.input_box_at(long_draft) == 0
    with_suggestion = [shots.RULE, f'{MARK} \x1b[2mTry "what is up"\x1b[0m', shots.RULE]
    assert screen.input_box_at(screen.strip_escapes(with_suggestion)) == 0
    assert screen.input_box_at([shots.RULE, "not the mark", shots.RULE]) is None


def test_a_hyperlink_ended_by_st_strips_clean_as_t1_stripped_it() -> None:
    """coderp's S1 on #219: the shared pattern wanted ESC and TWO backslashes to end an OSC,
    so a hyperlink ended by ST (ESC and one backslash, what tmux passes through) leaked its
    target and broke the anchored option match on its line."""
    linked = f"\x1b]8;;https://example.com\x1b\\ {MARK} 1. Yes\x1b]8;;\x1b\\"
    assert screen.strip_escapes([linked]) == [f" {MARK} 1. Yes"]
    raw = ["Do you want to proceed?", linked, "   2. No", " Esc to cancel"]
    assert screen.modal_showing(raw) == "a numbered choice"
    got = screen.prompt_showing(raw)
    assert got is not None and (got.shape, got.yes_key) == ("chooser", "1")


def test_escapes_are_stripped_before_anything_is_read() -> None:
    raw = [
        "\x1b[1;32mDo you want to proceed?\x1b[0m",
        f"\x1b]8;;http://x\x07 {MARK} 1. Yes\x1b]8;;\x07",
        "   2. No",
        " Esc to cancel",
    ]
    got = screen.prompt_showing(raw)
    assert got is not None and (got.shape, got.yes_key) == ("chooser", "1")
    assert screen.modal_showing(raw) == "a numbered choice"
