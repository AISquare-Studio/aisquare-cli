"""Claude Code 2.1.326's question about an ``--add-dir`` folder, drawn about any folder.

The captures (``tests/fixtures/claude_code/added_folder_trust_2.1.326*.txt``) name one
masked macOS folder, which is a directory on no machine and, as a ``Path``, not even the
same text on Windows (review of #520's Windows shards, brief 217). So a test that reads the
folder off the disk draws a real one of its own (under ``tmp_path``, as that platform spells
it) into the capture in its place, wrapped as Claude Code wraps it: :func:`wrapped` is the
wrap its renderer (Ink) asks of ``wrap-ansi`` (``{trim: false, hard: true}``), and
:func:`drawn` finds the capture's folder by drawing the masked one the same way, so a capture
it does not reproduce is an error, not a silent pass (``tests/test_claude_sharing.py`` pins
it against every capture).
"""

from __future__ import annotations

import re

MASKED = "/private/var/tmp/asq-native"
"""What each capture's temp folder was masked to."""

_ESCAPES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


def wrapped(text: str, width: int) -> list[str]:
    """``text`` wrapped at ``width`` columns as ``wrap-ansi`` wraps it with ``{trim: false,
    hard: true}``: at a space, kept at the end of the row it breaks (a trailing space, which
    the terminal does not show); a word wider than ``width`` broken inside it, started on a
    row of its own where that breaks it fewer times. Plain text, one column a character."""
    rows = [""]
    for index, word in enumerate(text.split(" ")):
        length = len(rows[-1])
        if index:
            if length >= width:
                rows.append("")
                length = 0
            rows[-1] += " "
            length += 1
        if len(word) > width:
            this_row = 1 + (len(word) - (width - length) - 1) // width
            next_row = (len(word) - 1) // width
            if next_row < this_row:
                rows.append("")
            _wrap_word(rows, word, width)
            continue
        if length + len(word) > width and length > 0 and word:
            rows.append("")
        rows[-1] += word
    return rows


def _wrap_word(rows: list[str], word: str, width: int) -> None:
    """``wrap-ansi``'s ``wrapWord``: ``word`` onto the last row and on, ``width`` at most each."""
    visible = len(rows[-1])
    for index, character in enumerate(word):
        if visible + 1 <= width:
            rows[-1] += character
        else:
            rows.append(character)
            visible = 0
        visible += 1
        if visible == width and index < len(word) - 1:
            rows.append("")
            visible = 0
    if not visible and rows[-1] and len(rows) > 1:
        rows[-2] += rows.pop()


def plain(screen: list[str]) -> list[str]:
    """``screen``'s rows as the terminal shows them: no escapes, no trailing blanks."""
    return [_ESCAPES.sub("", row).rstrip() for row in screen]


def drawn(screen: list[str], masked: str, folder: str, columns: int) -> list[str]:
    """``screen``, a capture ``columns`` wide asking about ``masked``, asking about ``folder``
    instead: under the title, inside the dialog's one column of padding each side; or, cut
    off, after `` Adding: `` under the key hint, from the pane's first column. Its rows are
    plain text (the readers strip the escapes either way). Raises where the capture does not
    show ``masked`` as :func:`wrapped` draws it."""
    shown = plain(screen)
    forms = (
        (wrapped(masked, columns - 2), wrapped(folder, columns - 2), " "),
        (wrapped(f" Adding: {masked}", columns), wrapped(f" Adding: {folder}", columns), ""),
    )
    for old, new, pad in forms:
        rows = [f"{pad}{row}".rstrip() for row in old]
        for at in range(len(shown) - len(rows) + 1):
            if shown[at : at + len(rows)] == rows:
                return [*screen[:at], *(f"{pad}{row}" for row in new), *screen[at + len(rows) :]]
    raise AssertionError(f"{masked!r} is not drawn on the screen as Claude Code draws it")
