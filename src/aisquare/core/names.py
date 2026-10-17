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
rainbow flag, the slightly smiling face + U+200D + left-right arrow + U+FE0F is the head
shaking), the emoji and text variation selectors after a character that has the two
presentations (a symbol; ‼, ⁉, U+2139 information, 〰 and 〽; a keycap's digit), skin
tones, and the tag characters of a subdivision flag. Any other variation selector is kept
where it picks a form too: a standardized or ideographic one after a Han ideograph (the
glyph a person's or a place's name is registered in), one of Mongolian's after a Mongolian
letter. Anywhere else a variation selector draws nothing — ``work`` U+FE0F and ``work``
U+FE00 print as ``work`` — and is refused; any variation selector is left out when two
names are compared (:func:`name_key`). A joiner is kept, too, where a script draws with
it: after a virama (Sinhala's "Sri") and between the letters of a cursive script (the
non-joiner in Persian). Between two letters that draw the same either way a joiner is an
invisible look-alike, and refused.
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
"""The text and emoji presentation selectors (VS15, VS16)."""
_EMOJI_BESIDE_SYMBOLS = frozenset({"\u203c", "\u2049", "\u2139", "\u3030", "\u303d"})
"""‼ ⁉ U+2139 〰 〽: punctuation and a letter, not symbols, each with an emoji presentation
(Unicode's emoji-variation-sequences.txt; every other base there but a keycap's is So/Sm)."""
_KEYCAP_BASES = frozenset("#*0123456789")
_KEYCAP = "\u20e3"
_MONGOLIAN_SELECTORS = range(0x180B, 0x1810)
_ANY_VARIATION_SELECTOR = (range(0xFE00, 0xFE10), range(0xE0100, 0xE01F0), _MONGOLIAN_SELECTORS)
"""Every variation selector: the standardized ones, the ideographic ones, Mongolian's."""
_HAN = ("CJK UNIFIED IDEOGRAPH", "CJK COMPATIBILITY IDEOGRAPH")
"""How the names of the Han ideographs begin, a selector after which picks a glyph."""
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


def name_key(text: str) -> str:
    """``text`` as two names are compared: without case, and without any variation selector,
    which changes how a character is drawn and never which one it is."""
    return "".join(char for char in text if not _is_variation_selector(char)).casefold()


def _is_variation_selector(char: str) -> bool:
    return any(ord(char) in selectors for selectors in _ANY_VARIATION_SELECTOR)


def _hidden(text: str, at: int) -> bool:
    """Whether the character at ``at`` prints nothing or turns the text round, where it is."""
    char = text[at]
    if char in _BLANK_LETTERS:
        return True
    if _is_variation_selector(char):
        return not _selects_a_form(text, at)
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
    if _ends_an_emoji_part(before) and _starts_an_emoji_part(text, at + 1):
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


def _starts_an_emoji_part(text: str, at: int) -> bool:
    """A pictograph (So), or a symbol (Sm) drawn as an emoji by the U+FE0F after it: the
    arrow in the head shaking (U+2194 U+FE0F)."""
    category = unicodedata.category(text[at])
    if category == "So":
        return True
    return category == "Sm" and text[at + 1 : at + 2] == "\ufe0f"


def _selects_a_form(text: str, at: int) -> bool:
    """Whether the variation selector at ``at`` picks a form of the character before it:
    VS15 or VS16 after one with two presentations (:func:`_selects_a_presentation`), one of
    Mongolian's after a Mongolian letter, any other after a Han ideograph. Only VS15 and
    VS16 were asked: VS1 to VS14, the ideographic selectors and Mongolian's are category
    Mn, not Cf, and ``work`` U+FE00 was a name that printed as ``work`` (review of #259,
    round 33, 10/10)."""
    if text[at] in _VARIATION_SELECTORS:
        return _selects_a_presentation(text, at)
    if at == 0:
        return False
    base = unicodedata.name(text[at - 1], "")
    if ord(text[at]) in _MONGOLIAN_SELECTORS:
        return base.startswith("MONGOLIAN LETTER")
    return base.startswith(_HAN)


def _selects_a_presentation(text: str, at: int) -> bool:
    """Whether the VS15 or VS16 at ``at`` follows a character with a text and an emoji
    presentation to choose between: a symbol, one of ‼ ⁉ U+2139 〰 〽, or a keycap's ``#``,
    ``*`` or digit (then U+20E3). After anything else it draws nothing."""
    if at == 0:
        return False
    base = text[at - 1]
    if unicodedata.category(base) in ("So", "Sm") or base in _EMOJI_BESIDE_SYMBOLS:
        return True
    return base in _KEYCAP_BASES and text[at + 1 : at + 2] == _KEYCAP


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
