"""What a name a person gives may hold: any text and any emoji, and nothing that makes it lie.

A project's nickname (``services.project.set_nickname``) and a Claude account's alias
(``core.claude_accounts.normalise_alias``) are free text — the human: "just a string, no
convention", "rename them in place into anything I want" — and one rule says what both
refuse, so the two never drift apart:

- a character that breaks the line (a newline, a tab, an escape; a line or paragraph
  separator): a name is one line on every surface that shows it;
- half of a character (a lone surrogate): what Python makes of bytes the terminal's
  encoding could not decode, which the store cannot write;
- a character that prints nothing, or turns the text round, so a name reads as another
  (``a`` U+200B ``b`` beside ``ab``) or as what it is not (``evil`` U+202E ``gnp.exe``
  reads ``evilexe.png``): Unicode's format characters (category Cf: the bidi embeddings,
  overrides, isolates and marks, the zero-width space, the byte-order mark, the soft
  hyphen, the word joiner) and the default-ignorable letters that draw as blanks.

What an emoji is made of is kept: the zero-width joiner between its parts (woman +
U+200D + laptop is the technologist, the white flag + U+FE0F + U+200D + rainbow is the
rainbow flag), variation selectors, skin tones, and the tag characters of a subdivision
flag. So is a joiner where a script draws with it: after a virama (Sinhala's "Sri") and
between the letters of a cursive script (the non-joiner in Persian). Between two letters
that draw the same either way a joiner is an invisible look-alike, and refused.
"""

from __future__ import annotations

import unicodedata
from typing import Literal

NameFlaw = Literal["control", "undecodable", "invisible"]
"""Why a name is refused: it breaks the line, holds half a character, or hides something."""

_LINE_BREAKING = frozenset({"Cc", "Zl", "Zp"})
_JOINERS = frozenset({"\u200c", "\u200d"})
"""The zero-width non-joiner and joiner: kept where they change what is drawn."""
_SKIN_TONES = range(0x1F3FB, 0x1F400)
_VARIATION_SELECTORS = frozenset({"\ufe0e", "\ufe0f"})
_TAGS = range(0xE0020, 0xE0080)
_BLACK_FLAG = "\U0001f3f4"
_CANCEL_TAG = "\U000e007f"
_BLANK_LETTERS = frozenset({"\u034f", "\u115f", "\u1160", "\u17b4", "\u17b5", "\u3164", "\uffa0"})
"""Default-ignorable characters outside Cf that draw nothing: the combining grapheme joiner,
the Hangul fillers (a known blank "name"), the Khmer inherent vowels."""
_CURSIVE_SCRIPTS = ("ARABIC", "SYRIAC", "NKO", "MONGOLIAN", "MANDAIC", "ADLAM", "HANIFI")
"""Scripts whose letters join, so a joiner or non-joiner between two of them is drawn."""
_VIRAMA = 9
"""The canonical combining class of a virama: a joiner after one picks a conjunct's form."""


def name_flaw(text: str) -> NameFlaw | None:
    """Why ``text`` may not be a name (the module docstring's rule), or ``None`` when it may.

    A line break wins over half a character, which wins over a hidden one, whatever
    their order in the text.
    """
    categories = [unicodedata.category(char) for char in text]
    if any(category in _LINE_BREAKING for category in categories):
        return "control"
    if "Cs" in categories:
        return "undecodable"
    if any(_hidden(text, at) for at in range(len(text))):
        return "invisible"
    return None


def _hidden(text: str, at: int) -> bool:
    """Whether the character at ``at`` prints nothing or turns the text round, where it is."""
    char = text[at]
    if char in _BLANK_LETTERS:
        return True
    if unicodedata.category(char) != "Cf":
        return False
    if char in _JOINERS:
        return not _joins(text, at)
    if ord(char) in _TAGS:
        return not _in_a_flag(text, at)
    return True


def _joins(text: str, at: int) -> bool:
    """Whether the joiner at ``at`` changes what is drawn: between two parts of an emoji,
    after a virama, or between two letters of a cursive script. Never at either end."""
    if at == 0 or at == len(text) - 1:
        return False
    before, after = text[at - 1], text[at + 1]
    if _ends_an_emoji_part(before) and unicodedata.category(after) == "So":
        return True
    if unicodedata.combining(before) == _VIRAMA:
        return unicodedata.category(after).startswith("L")
    return _cursive(before) and _cursive(after)


def _ends_an_emoji_part(char: str) -> bool:
    """A pictograph, or what follows one inside its part: a skin tone, a variation selector."""
    return (
        unicodedata.category(char) == "So"
        or ord(char) in _SKIN_TONES
        or char in _VARIATION_SELECTORS
    )


def _cursive(char: str) -> bool:
    return unicodedata.name(char, "").startswith(_CURSIVE_SCRIPTS)


def _in_a_flag(text: str, at: int) -> bool:
    """Whether the tag character at ``at`` is in a subdivision flag: the black flag, then a
    run of tags that the cancel tag ends (England, Scotland, Wales)."""
    start = end = at
    while start > 0 and ord(text[start - 1]) in _TAGS:
        start -= 1
    while end + 1 < len(text) and ord(text[end + 1]) in _TAGS:
        end += 1
    return start > 0 and text[start - 1] == _BLACK_FLAG and end > start and text[end] == _CANCEL_TAG
