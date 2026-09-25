"""Captured screens every pane reader is pinned on — ONE set, through both readers (13278).

The real ones are runner2's captures from a REAL Claude Code 2.1.282 (board 13265, PR #224):
on its permission chooser and on the trust dialog the letter y does nothing; the digit 1
(Yes), Enter with Yes highlighted and the arrows do. ``services.captain.screen`` reads them
for ``brain.say`` (may text be typed here?) and for ``actions.press`` (is there a prompt to
answer, and with which key?), and ``tests/test_captain_screen.py`` asserts the two views
agree on every screen here.
"""

from __future__ import annotations

MARK = "\u276f"
REAL_RULE = "─" * 100
RULE = "─" * 60

INPUT_BOX = [RULE, f"{MARK} ", RULE, "  ⏵⏵ accept edits on (shift+tab to cycle)"]
"""How a waiting Claude Code pane ends: the input line between two rules, the mode footer."""

REAL_IDLE = [
    "● Created probe2.txt in the working directory containing the word again.",
    "✻ Cooked for 5s · done 10:17 AM",
    REAL_RULE,
    f"{MARK} ",
    REAL_RULE,
    "  ⏸ manual mode on · ? for shortcuts · ← for agents",
]
REAL_CHOOSER = [
    f"{MARK} Create a file named probe2.txt in this folder containing the word again",
    "",
    " Do you want to create probe2.txt?",
    f" {MARK} 1. Yes",
    "   2. Yes, and switch to accept edits (auto-approve file edits and common file commands)"
    " for this session (shift+tab)",
    "   3. No",
    "",
    " Esc to cancel · Tab to amend",
]
REAL_TRUST = [
    " Quick safety check: Is this a project you created or one you trust? (Like your own"
    " code, a well-known open source",
    f" {MARK} No, exit",
    "   Yes, I trust this folder",
    "",
    " Enter to confirm · Esc to cancel",
]
YES_NO = ["Installing 3 packages.", "Proceed? [y/N] "]
REAL_QUOTED = [
    "● coder-2 is stuck at this prompt:",
    "  Do you want to create probe2.txt?",
    f"  {MARK} 1. Yes",
    "    2. No",
    "  Esc to cancel · Tab to amend",
    REAL_RULE,
    f"{MARK} ",
    REAL_RULE,
    "  ⏸ manual mode on · ? for shortcuts",
]
BOX_WITH_ESC_FOOTER = [
    *REAL_QUOTED[:5],
    REAL_RULE,
    f"{MARK} ",
    REAL_RULE,
    "  ⏸ manual mode on · Esc to cancel a draft",
]
MID_TURN_LIST = [
    "Here are the options:",
    f" {MARK} 1. Yes",
    "   2. No",
    "✻ Thinking… (esc to interrupt)",
]
CHOOSER_NO_FIRST = [" Allow this?", f" {MARK} 1. No", "   2. Yes", " Esc to cancel"]
RATING_ABOVE_BOX = [
    "How is Claude doing this session? (optional)",
    "1: Bad  2: Fine  3: Good  0: Dismiss",
    *INPUT_BOX,
]
MODEL_PICKER = ["Select a model", "Enter to confirm · Esc to cancel"]
TRUST_QUOTED_MID_TURN = [
    "The owner must choose Yes, I trust this folder in the captain's own window.",
    "✻ Thinking… (esc to interrupt)",
]
WORKING_QUOTING = [f"Reading coder-1's pane: {MARK} 1. Yes / Esc to cancel", "· Thinking…"]


def pane(*transcript: str, box: bool = True) -> list[str]:
    """A pane: the conversation, then (unless a dialog replaced it) the input box."""
    return [*transcript, *(INPUT_BOX if box else [])]
