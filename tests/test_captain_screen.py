"""One reader, two questions (13278): ``say`` asks "may I type text here?", ``press`` asks
"is there a prompt to answer, and with which key?" — and on every captured screen the two
answers agree about what is on the screen.

The captain asks them of the pane's OWN agent (``services.captain.screen``, by the placed
row's ``agent``): Claude Code's shapes (``core.agent_adapters.claude_screen``) for a Claude
Code row, Codex's own dialogs for a Codex row (``tests/fixtures/agent_screens/codex.json``,
measured on 0.162.1), and nothing for an agent this install does not know."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aisquare.core.agent_adapters import claude_screen
from aisquare.models import FleetAgent
from aisquare.services.captain import screen
from tests import captain_screens as shots

MARK = shots.MARK
CODEX_SCREENS: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "agent_screens" / "codex.json").read_text("utf-8")
)


def _row(agent: str | None) -> FleetAgent:
    """A placed row of ``agent``'s (``None``: one from before adapters, Claude Code's)."""
    return FleetAgent(
        id="agt_1", project_id="prj_1", label="coder-1", role="coder", pane_id="%1",
        cwd=Path("/w"), created_at=datetime(2026, 10, 23, tzinfo=UTC), agent=agent,
    )  # fmt: skip


CLAUDE = _row("claude-code")
CODEX = _row("codex")


def _codex(what: str) -> list[str]:
    """Codex's measured screen ``what`` (fixture 4b)."""
    shown = [*CODEX_SCREENS["dialogs"], *CODEX_SCREENS["not_dialogs"]]
    rows: list[str] = next(item["rows"] for item in shown if item["what"].startswith(what))
    return rows


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
        ("a real box drawn mid-turn, its top rule named", shots.REAL_WORKING, None, None),
        ("a real idle box after the first Stop, named", shots.REAL_IDLE_AFTER_STOP, None, None),
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
    for row in (CLAUDE, _row(None)):
        got = screen.prompt_showing(row, lines)
        shape = (got.shape, got.yes_key, got.no_key) if got is not None else None
        assert shape == prompt, f"{name}: prompt_showing"
        assert screen.modal_showing(row, lines) == modal, f"{name}: modal_showing"
    # The agreement: a prompt to ANSWER is never a screen say may type into, and the box drawn
    # is the same fact for both.
    if got is not None and got.shape != "yn":
        assert modal is not None, f"{name}: press sees a prompt that say would type over"
    if modal in ("a numbered choice", "the trust dialog"):
        assert got is not None, f"{name}: say refuses a prompt that press cannot answer"


def _with_footer(words: str) -> list[str]:
    """The real mid-turn capture with its footer's 'esc to interrupt' swapped for ``words``."""
    return [line.replace("esc to interrupt", words) for line in shots.REAL_WORKING]


def _without_spinner() -> list[str]:
    """The real mid-turn capture without its spinner line: only the footer says it."""
    return [line for line in shots.REAL_WORKING if "Sautéing…" not in line]


def test_the_box_is_found_on_both_real_captures_whose_top_rule_carries_the_name() -> None:
    """T2b (13383): the fleet launches every agent with --name, and Claude Code draws it in
    the box's TOP rule. A reader that took only bare rules found no box on any real pane,
    so say and send never typed."""
    for lines in (shots.REAL_WORKING, shots.REAL_IDLE_AFTER_STOP):
        rows = claude_screen.tail(lines)
        top = claude_screen.input_box_at(rows)
        assert top is not None and "screen-coder" in rows[top]
    assert claude_screen.RULE.match(shots.REAL_TOP_RULE)
    assert claude_screen.RULE.match(shots.TOP_RULE)


@pytest.mark.parametrize(
    "line",
    [
        "the verdict ─── is in",
        f"{'─' * 20} two words ─",
        f"{'─' * 20} coder-1 ─ and more",
        f"{'─' * 20} coder-1 {'─' * 20}",
        "─── x ─",
    ],
)
def test_a_rule_carries_one_name_and_a_short_close_nothing_else(line: str) -> None:
    """The card's shape (T2b): 8 or more rule characters, then optionally one space, the name,
    one space and a short closing run — nothing else on the line."""
    assert claude_screen.RULE.match(line) is None


@pytest.mark.parametrize(
    ("name", "lines", "idle"),
    [
        ("the real idle pane, its finished turn's line above the box", shots.REAL_IDLE, True),
        ("the real idle box after the first Stop", shots.REAL_IDLE_AFTER_STOP, True),
        ("the plain input box", shots.INPUT_BOX, True),
        ("the real box mid-turn: a live spinner above, esc to interrupt below",
         shots.REAL_WORKING, False),
        ("the live spinner alone says it", _with_footer("? for shortcuts"), False),
        ("the footer alone says it", _without_spinner(), False),
        ("a chooser: no box at all", shots.REAL_CHOOSER, False),
        ("a spinner and no box", shots.MID_TURN_LIST, False),
        ("an empty pane", [], False),
    ],
)  # fmt: skip
def test_an_idle_box_is_read_by_its_structure(name: str, lines: list[str], idle: bool) -> None:
    """13313: real Claude Code keeps its input box drawn DURING a turn. Idle means the box is
    drawn, no live spinner sits just above it, and its footer does not say 'esc to interrupt'.
    The finished turn's line ('✻ Cooked for 5s · done') is no spinner."""
    assert claude_screen.box_idle(lines) is idle, name
    assert screen.ready(CLAUDE, lines) is idle, f"{name}: the captain's reading of its row"


def test_the_box_is_read_with_a_wrapped_draft_and_a_dim_suggestion() -> None:
    long_draft = [
        shots.RULE,
        f"{MARK} a draft that wraps",
        "  onto a second line",
        shots.RULE,
        "  ⏸ manual mode on",
    ]
    assert claude_screen.input_box_at(long_draft) == 0
    with_suggestion = [shots.RULE, f'{MARK} \x1b[2mTry "what is up"\x1b[0m', shots.RULE]
    assert claude_screen.input_box_at(screen.strip_escapes(with_suggestion)) == 0
    assert claude_screen.input_box_at([shots.RULE, "not the mark", shots.RULE]) is None


def test_a_hyperlink_ended_by_st_strips_clean_as_t1_stripped_it() -> None:
    """coderp's S1 on #219: the shared pattern wanted ESC and TWO backslashes to end an OSC,
    so a hyperlink ended by ST (ESC and one backslash, what tmux passes through) leaked its
    target and broke the anchored option match on its line."""
    linked = f"\x1b]8;;https://example.com\x1b\\ {MARK} 1. Yes\x1b]8;;\x1b\\"
    assert screen.strip_escapes([linked]) == [f" {MARK} 1. Yes"]
    raw = ["Do you want to proceed?", linked, "   2. No", " Esc to cancel"]
    assert screen.modal_showing(CLAUDE, raw) == "a numbered choice"
    got = screen.prompt_showing(CLAUDE, raw)
    assert got is not None and (got.shape, got.yes_key) == ("chooser", "1")


def test_escapes_are_stripped_before_anything_is_read() -> None:
    raw = [
        "\x1b[1;32mDo you want to proceed?\x1b[0m",
        f"\x1b]8;;http://x\x07 {MARK} 1. Yes\x1b]8;;\x07",
        "   2. No",
        " Esc to cancel",
    ]
    got = screen.prompt_showing(CLAUDE, raw)
    assert got is not None and (got.shape, got.yes_key) == ("chooser", "1")
    assert screen.modal_showing(CLAUDE, raw) == "a numbered choice"


# --- a Codex row: Codex's own reading of its pane (fixture 4b, 0.162.1) ----------------------


@pytest.mark.parametrize(
    ("what", "prompt"),
    [
        ("folder trust", ("trust", None, None)),
        ("an approval view", ("chooser", "1", "Escape")),
        ("an approval view in a 60x8 pane", ("chooser", "1", "Escape")),
        ("the hook review", ("dialog", None, None)),
        ("Switch to gpt-6-luna", ("dialog", None, None)),
        ("Implement this plan?", ("dialog", None, None)),
        ("a question view", ("dialog", None, None)),
    ],
)
def test_a_codex_rows_dialogs_are_read_by_codex_with_their_own_keys(
    what: str, prompt: tuple[str, str | None, str | None]
) -> None:
    """Its folder trust is the owner's (no keys), its approval view a chooser on ``1`` and Esc,
    every other dialog of its own the owner's. Claude Code's reader saw none of them: the
    highlight is U+203A and the footers are Codex's, so a paste's Enter at its trust dialog
    picked "1. Trust and continue"."""
    rows = _codex(what)
    got = screen.prompt_showing(CODEX, rows)
    assert got is not None and (got.shape, got.yes_key, got.no_key) == prompt
    assert screen.modal_showing(CODEX, rows) and not screen.ready(CODEX, rows)
    assert claude_screen.prompt_showing(rows) is None, "the defect: Claude Code's shapes"


@pytest.mark.parametrize(
    ("what", "ready"),
    [
        ("its empty prompt after a turn", True),
        ("its empty prompt after one Esc", True),
        ("a turn running", False),
        ("a draft", False),
        ("its transcript after a second Esc", False),
    ],
)
def test_a_codex_row_is_ready_at_its_empty_composer_alone(what: str, ready: bool) -> None:
    rows = _codex(what)
    assert screen.prompt_showing(CODEX, rows) is None
    assert screen.modal_showing(CODEX, rows) is None
    assert screen.ready(CODEX, rows) is ready


def test_a_codex_pane_is_read_with_its_escapes_stripped_first() -> None:
    """The captain's capture keeps SGR escapes; Codex's readers take plain rows."""
    raw = [f"\x1b[1m{row}\x1b[0m" if row.strip() else row for row in _codex("folder trust")]
    got = screen.prompt_showing(CODEX, raw)
    assert got is not None and got.shape == "trust"


def test_a_row_of_an_agent_this_install_does_not_know_shows_nothing_and_is_never_ready() -> None:
    unknown = _row("not-an-agent")
    for rows in (shots.REAL_TRUST, _codex("folder trust"), shots.REAL_IDLE):
        assert screen.prompt_showing(unknown, rows) is None
        assert screen.modal_showing(unknown, rows) is None
        assert not screen.ready(unknown, rows)
    assert not screen.reads_prompts(unknown)
    assert screen.reads_prompts(CLAUDE) and screen.reads_prompts(CODEX)
