"""What an agent's pane SHOWS, read by structure: Claude Code's input box, its dialogs, a prompt.

One reader for the two things that type into a pane (board 13278): ``brain.say``, which
must never type into a dialog (13227), and ``actions.press``, which answers one. Two
parsers of the same screen could disagree on one pane — ``say`` refusing while ``press``
answers, or the reverse — so both ask here, and one set of captured screens
(``tests/captain_screens.py``) is pinned through both.

The rule (the manager's, 13264, from runner2's real captures at 13265): a WAITING Claude
Code pane ends with its **input box** — a rule, the ``❯`` input line (a dim suggestion may
sit on it), a rule, then footer lines. A dialog **replaces** that box. So with the box drawn
nothing below the transcript is a dialog, whatever the conversation above it quotes — a
reply that reports a stuck prompt quotes chooser lines and footers, and that is the
captain's job — and with no box drawn, only the lines that replaced it are read:

- a **numbered chooser**: options ``1.``, ``2.``… with exactly one highlighted by the mark,
  and a footer naming Esc or Enter on the last two lines; *yes* is the digit of the first
  option that says Yes (``1`` on the real permission chooser, never a blind ``1``), *no* is
  Esc;
- the **trust dialog** (``Quick safety check …``): its own shape, no keys — trusting a
  folder is the owner's to answer;
- a ``[y/N]``-style **last line**: ``y`` and ``n``.

:func:`modal_showing` is ``say``'s view of the same screen: those shapes, named, plus two
that are not prompts to ANSWER but are still not places to TYPE text into — the
session-rating survey Claude Code draws in the one or two lines just above its box, and
any other dialog by an Enter/Esc footer on the last line with no box drawn.

This module imports nothing of aisquare: ``brain`` and ``actions`` both import it.
"""  # noqa: RUF002

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

PANE_ESCAPES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
"""Every escape a captured pane row can carry — CSI with any parameters (colon ones
included), OSC (hyperlinks, ended by BEL or by ST, which is ESC and ONE backslash) and the
bare two-byte sequences. T1's pattern, character for character (coderp's S1 on #219)."""

PROMPT_MARK = "\u276f"
"""Claude Code's prompt mark, U+276F: its input line and a chooser's highlighted option."""

RULE = re.compile(r"^\s*[─━]{8,}\s*$")
"""A horizontal rule: the two that frame the input line."""
INPUT_LINE = re.compile(r"^\s*" + PROMPT_MARK)
OPTION = re.compile(r"^\s*(" + PROMPT_MARK + r"\s*)?(\d)[.)]\s+(\S.*)$")
MODAL_FOOTER = re.compile(r"Esc to cancel|Enter to confirm")
YES_NO = re.compile(r"[\[(]\s*[yY](?:es)?\s*/\s*[nN](?:o)?\s*[\])]\s*[:?]?\s*$")
TRUST = re.compile(r"Quick safety check|Yes, I trust this folder")
RATING = re.compile(r"How is Claude doing this session")

TAIL_LINES = 14
"""How many non-blank lines at the bottom are read: the box takes about five, a dialog about
the same, and the transcript above them is never searched."""
FOOTER_LINES = 3
"""How many footer lines may sit under the input box (the mode line, a hint)."""
BOX_BODY_LINES = 8
"""How tall the input box may be: a long draft wraps onto more input lines."""


@dataclass(frozen=True)
class Prompt:
    """A prompt showing at the bottom of a pane, and the keys that answer it."""

    shape: str
    """``chooser`` (Claude Code's numbered menu), ``yn`` (a ``[y/N]`` line), ``trust``."""
    question: str
    yes_key: str | None
    """tmux's key for yes — the digit of the first option that says Yes, or ``y``."""
    no_key: str | None


def strip_escapes(lines: Sequence[str]) -> list[str]:
    """The rows as the owner sees them: escapes gone, blank tail dropped."""
    rows = [PANE_ESCAPES.sub("", line).rstrip() for line in lines]
    while rows and not rows[-1].strip():
        rows.pop()
    return rows


def tail(lines: Sequence[str]) -> list[str]:
    """The last :data:`TAIL_LINES` non-blank rows, escapes stripped."""
    return [row for row in strip_escapes(lines) if row.strip()][-TAIL_LINES:]


def input_box_at(rows: Sequence[str]) -> int | None:
    """Where Claude Code's input box starts in ``rows``, or ``None`` when none is drawn.

    A rule, one to :data:`BOX_BODY_LINES` input lines the first of which carries the
    mark, a rule, then up to :data:`FOOTER_LINES` footer lines. Leans towards "a box":
    a box read as a dialog only refuses, while a dialog read as a box would type into it.
    """
    end = len(rows)
    while end and not rows[end - 1].strip():
        end -= 1
    for footer in range(FOOTER_LINES + 1):
        below = end - 1 - footer
        if below < 2:
            break
        if not RULE.match(rows[below]):
            continue
        above = below - 1
        while above >= 0 and not RULE.match(rows[above]) and below - above <= BOX_BODY_LINES:
            above -= 1
        if above < 0 or not RULE.match(rows[above]):
            continue
        body = rows[above + 1 : below]
        if body and INPUT_LINE.match(body[0]):
            return above
    return None


def dialog_region(rows: Sequence[str]) -> list[str]:
    """The lines a dialog would occupy: what follows the last rule (or all of ``rows``)."""
    rules = [index for index, row in enumerate(rows) if RULE.match(row)]
    region = list(rows[rules[-1] + 1 :]) if rules else list(rows)
    return [row for row in region if row.strip()]


def prompt_showing(lines: Sequence[str]) -> Prompt | None:
    """The prompt at the bottom of a pane, read by its structure, or ``None`` (T1b's question)."""
    rows = tail(lines)
    if not rows or input_box_at(rows) is not None:
        return None
    region = dialog_region(rows)
    if not region:
        return None
    footer = any(MODAL_FOOTER.search(row) for row in region[-2:])
    if footer and any(TRUST.search(row) for row in region):
        question = next((row.strip() for row in region if "Quick safety check" in row), "")
        return Prompt("trust", question or "the trust dialog", None, None)
    numbered = [(index, match) for index, match in enumerate(map(OPTION.match, region)) if match]
    if footer and len(numbered) >= 2:
        digits = [match.group(2) for _, match in numbered]
        highlighted = [match for _, match in numbered if match.group(1)]
        if digits == [str(n) for n in range(1, len(digits) + 1)] and len(highlighted) == 1:
            first = numbered[0][0]
            question = next(
                (row.strip() for row in reversed(region[:first]) if row.strip()),
                "a numbered choice",
            )
            yes = next(
                (m.group(2) for _, m in numbered if m.group(3).lower().startswith("yes")), None
            )
            return Prompt("chooser", question, yes, "Escape")
    if YES_NO.search(region[-1]):
        return Prompt("yn", region[-1].strip(), "y", "n")
    return None


def modal_showing(lines: Sequence[str]) -> str | None:
    """What the pane shows that text must not be typed into, in the owner's words (``say``).

    :func:`prompt_showing`'s shapes, named — plus the session-rating survey in the one or
    two lines just above a drawn box, and any other dialog by an Enter/Esc footer on the
    last line with no box drawn. A ``[y/N]`` line is a prompt to answer, not a dialog: the
    captain's own pane is Claude Code, which never shows one, and ``say`` types text.
    """
    rows = tail(lines)
    if not rows:
        return None
    box = input_box_at(rows)
    if box is not None:
        above = "\n".join(rows[max(0, box - 2) : box])
        return "the session-rating prompt" if RATING.search(above) else None
    prompt = prompt_showing(rows)
    if prompt is not None:
        if prompt.shape == "trust":
            return "the trust dialog"
        if prompt.shape == "chooser":
            return "a numbered choice"
        return None
    region = dialog_region(rows)
    if region and RATING.search("\n".join(region)):
        return "the session-rating prompt"
    if region and MODAL_FOOTER.search(region[-1]):
        return "a dialog waiting for Enter or Esc"
    return None
