"""Textual key events → tmux ``send-keys`` arguments. Pure, and unit-tested.

The fleet UI forwards every key the embedded pane receives to the agent
running inside tmux. Printable characters travel as literal text
(``send-keys -l``); everything else must be spelled in tmux's own key
vocabulary (``Enter``, ``BSpace``, ``C-c``, ``M-x``, ``S-Enter``…). A key this
table does not know is dropped and the caller says so once — silently sending
the wrong thing to a running agent is worse than sending nothing.

Modifier chords beyond ctrl/alt on letters depend on the OUTER terminal
speaking the kitty keyboard protocol (Textual 8.2.7+ does); where it does not,
``shift+enter`` simply arrives as ``enter`` and there is nothing to translate.
See docs/plans/fleet-tui.md §6.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Kind = Literal["literal", "key"]


@dataclass(frozen=True)
class Translation:
    """What to hand tmux: literal text, or a named key."""

    kind: Kind
    value: str

    @property
    def argv(self) -> list[str]:
        """The ``send-keys`` arguments (after ``-t <pane>``)."""
        if self.kind == "literal":
            return ["-l", "--", self.value]
        return [self.value]


#: Textual's name for a key → tmux's name for the same key.
SPECIAL: dict[str, str] = {
    "enter": "Enter",
    "escape": "Escape",
    "tab": "Tab",
    "backspace": "BSpace",
    "delete": "DC",
    "insert": "IC",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "pageup": "PPage",
    "pagedown": "NPage",
    "space": "Space",
}

#: Whole chords tmux names differently from "modifier + key".
CHORDS: dict[str, str] = {"shift+tab": "BTab"}

#: Textual modifier prefix → tmux modifier prefix.
MODIFIERS: dict[str, str] = {"ctrl": "C-", "alt": "M-", "meta": "M-", "shift": "S-"}

#: Textual's spelled-out names for punctuation that arrives without a character.
PUNCTUATION: dict[str, str] = {
    "minus": "-",
    "plus": "+",
    "equals_sign": "=",
    "comma": ",",
    "full_stop": ".",
    "slash": "/",
    "backslash": "\\",
    "semicolon": ";",
    "apostrophe": "'",
    "quotation_mark": '"',
    "grave_accent": "`",
    "tilde": "~",
    "left_square_bracket": "[",
    "right_square_bracket": "]",
    "left_curly_bracket": "{",
    "right_curly_bracket": "}",
    "underscore": "_",
    "vertical_line": "|",
    "circumflex_accent": "^",
    "ampersand": "&",
    "asterisk": "*",
    "percent_sign": "%",
    "dollar_sign": "$",
    "number_sign": "#",
    "commercial_at": "@",
    "exclamation_mark": "!",
    "question_mark": "?",
    "less_than_sign": "<",
    "greater_than_sign": ">",
    "left_parenthesis": "(",
    "right_parenthesis": ")",
    "colon": ":",
}


def translate(key: str, character: str | None, *, printable: bool) -> Translation | None:
    """Translate one Textual key event; ``None`` when tmux has no name for it.

    ``key`` is Textual's ``Key.key`` (``"ctrl+c"``, ``"shift+tab"``, ``"f5"``,
    ``"a"``), ``character`` its ``Key.character`` and ``printable`` its
    ``Key.is_printable``. Printable input is always literal, so a pasted ``é``
    or a typed ``[`` never goes through the name table at all.
    """
    if printable and character:
        return Translation("literal", character)
    if key in CHORDS:
        return Translation("key", CHORDS[key])
    parts = key.split("+")
    base = parts[-1]
    modifiers = parts[:-1]
    if "shift" in modifiers and len(base) == 1 and base.isalpha() and len(modifiers) == 1:
        return Translation("literal", base.upper())
    prefix = "".join(MODIFIERS[m] for m in modifiers if m in MODIFIERS)
    if base in SPECIAL:
        name = SPECIAL[base]
    elif len(base) == 1 and (base.isalnum() or base in "-=[]\\;',./`"):
        name = base
    elif base in PUNCTUATION:
        name = PUNCTUATION[base]
    elif len(base) >= 2 and base[0] == "f" and base[1:].isdigit() and 1 <= int(base[1:]) <= 12:
        name = "F" + base[1:]
    else:
        return None
    if not prefix and name == "Space":
        return Translation("literal", " ")
    if prefix and len(name) == 1 and name.isalpha() and "S-" in prefix:
        # tmux spells ctrl+shift+a as C-S-a only with extended keys; plain
        # ctrl+A (uppercase) is the portable form of the same chord.
        return Translation("key", prefix.replace("S-", "") + name.upper())
    return Translation("key", prefix + name)
