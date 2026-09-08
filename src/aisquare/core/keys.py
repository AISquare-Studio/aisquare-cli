"""Textual key events → tmux ``send-keys`` arguments. Pure, and unit-tested.

The fleet UI forwards every key the embedded pane receives to the agent
running inside tmux. Printable characters travel as literal text
(``send-keys -l``); everything else must be spelled in tmux's own key
vocabulary (``Enter``, ``BSpace``, ``C-c``, ``M-x``, ``S-Enter``…). A key this
table does not know is dropped and the caller says so once — silently sending
the wrong thing to a running agent is worse than sending nothing.

Why the table is conservative: tmux TYPES AN UNKNOWN KEY NAME LITERALLY.
Measured against tmux 3.7c on 2026-08-28 with a raw-mode ``cat -v`` pane:
``send-keys C-BSpace`` put the eight characters ``C-BSpace`` into the agent's
input; so did ``C-Escape``, ``F13`` and ``Bogus``. ``S-a`` arrived as a
lowercase ``a`` and ``C-1`` as a bare ``1`` — the modifier silently dropped —
and any argument ending in ``;`` is tmux's command separator, so ``M-;`` sends
nothing at all. Every ``None`` below is one of those cases; every name this
module can emit was sent to that pane and came back as the right bytes.

Modifier chords beyond ctrl/alt on letters depend on the OUTER terminal
speaking the kitty keyboard protocol (Textual 8.2.7+ does); where it does not,
``shift+enter`` simply arrives as ``enter`` and there is nothing to translate.
Textual names chords with the modifiers sorted alphabetically
(``alt+ctrl+x``, ``ctrl+shift+a``); the order is irrelevant here.
See docs/plans/fleet-tui.md §6.
"""

from __future__ import annotations

import re
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

#: Whole chords tmux names differently from "modifier + key". ``S-Tab`` exists
#: but tmux 3.7c sends a plain Tab for it; ``BTab`` is the back-tab (``CSI Z``)
#: Claude Code cycles modes with.
CHORDS: dict[str, str] = {
    "shift+tab": "BTab",
    "ctrl+shift+tab": "C-BTab",
    "alt+shift+tab": "M-BTab",
}

#: Textual modifier → tmux modifier prefix. ``super`` and ``hyper`` have no
#: tmux spelling and make the whole chord untranslatable.
MODIFIERS: dict[str, str] = {"ctrl": "C-", "alt": "M-", "meta": "M-", "shift": "S-"}

#: Named keys tmux 3.7c refuses to combine with ctrl: ``C-Escape`` and
#: ``C-BSpace`` come out as those literal strings.
NO_CTRL: frozenset[str] = frozenset({"Escape", "BSpace"})

#: Punctuation tmux can put a control modifier on (the classic C0 mappings:
#: ``C-@`` → NUL … ``C-_`` → US, ``C-?`` → DEL; ``C-/`` and ``C--`` are ``C-_``).
#: Everything else — ``C-,``, ``C-.``, ``C-=`` … — tmux sends as the bare
#: character, so those chords are dropped rather than mistyped.
CTRL_PUNCTUATION: frozenset[str] = frozenset("@[\\]^_?/-")

#: Textual's spelled-out names for punctuation that arrives without a character
#: (a kitty-protocol chord such as ``alt+left_square_bracket``). Textual applies
#: ``KEY_NAME_REPLACEMENTS`` (``commercial_at`` → ``at``…) before an event is
#: built; both spellings are kept so a binding written either way translates.
PUNCTUATION: dict[str, str] = {
    "minus": "-",
    "hyphen_minus": "-",
    "plus": "+",
    "plus_sign": "+",
    "equals_sign": "=",
    "comma": ",",
    "full_stop": ".",
    "slash": "/",
    "solidus": "/",
    "backslash": "\\",
    "reverse_solidus": "\\",
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
    "low_line": "_",
    "vertical_line": "|",
    "circumflex_accent": "^",
    "ampersand": "&",
    "asterisk": "*",
    "percent_sign": "%",
    "dollar_sign": "$",
    "number_sign": "#",
    "at": "@",
    "commercial_at": "@",
    "exclamation_mark": "!",
    "question_mark": "?",
    "less_than_sign": "<",
    "greater_than_sign": ">",
    "left_parenthesis": "(",
    "right_parenthesis": ")",
    "colon": ":",
}

#: tmux knows F1-F12 only; Textual can report up to F24.
MAX_FUNCTION_KEY = 12
_FUNCTION = re.compile(r"f([1-9]\d?)")

#: tmux treats an argument that ends in ``;`` as the end of one command and the
#: start of the next, so no key NAME may end in it (``M-;`` sends nothing).
ARGV_SEPARATOR = ";"


def _base_character(base: str) -> str | None:
    """The single character a Textual base name stands for, or ``None``."""
    if len(base) == 1:
        return base
    return PUNCTUATION.get(base)


EXTENDED_MINIMUM: tuple[int, int] = (3, 5)
"""The tmux version from which every chord this module emits arrives as a key.

Measured 2026-08-29, raw-mode ``cat -v`` panes under the bundled conf, in
containers: tmux 3.3a and 3.4 TYPE the extended-only chords LITERALLY into the
pane — ``S-Enter``, ``S-Escape``, ``S-Space``, ``S-Tab``, ``S-BSpace`` and
their C-/M- stacks, ``C-S-<letter>``, ``C-M-Enter`` (3.3 also ``C-M-Tab``) —
while 3.5a delivers the entire emitted set. Everything with a legacy escape
sequence (``S-Up``, ``C-S-DC``, ``BTab``, ``M-Enter``…) is fine on every
version back to the fleet's 3.2 minimum.

Re-measured 2026-08-31 against the WHOLE emittable vocabulary — all 470 names
:func:`translate` can produce, not a hand-listed sample — on 3.2a
(ubuntu:22.04), 3.3a (debian:bookworm), 3.4 (ubuntu:24.04, the CI runner) and
3.7c. That found one class the sample had missed, now gated: SHIFTED
PUNCTUATION. Every ``C-S-<punct>``, ``M-S-<punct>`` and ``C-M-S-<punct>`` —
``M-S--`` from alt+shift+minus, ``C-S-@``, ``M-S-{`` … — is typed literally by
all three old versions, as are ``C-M--`` and ``C-M-/`` (tmux's own aliases for
``C-M-_``, which itself is fine). Everything else the sweep added arrives as a
key on every version: the triple-modifier stacks on cursor keys and function
keys (``C-M-S-Up``, ``C-M-S-F5``), ``C-S-F1``, ``M-S-F12``, ``C-M-<letter>``,
``C-M-@``/``[``/``\\``/``]``/``^``/``_`` and ``M-<uppercase letter>``.
"""

_EXTENDED_BASES = frozenset({"Enter", "Escape", "Space", "Tab", "BSpace"})
_MODIFIER_TOKENS = ("C-", "M-", "S-")
#: The two punctuation keys tmux < 3.5 types literally with ctrl AND alt held.
#: tmux resolves ``C--`` and ``C-/`` to ``C-_`` (:data:`CTRL_PUNCTUATION`); with
#: ``M-`` also held the old versions had no encoding for the result and spelled
#: the name out instead. The rest of the C-M- punctuation set is measured fine.
_CTRL_ALT_LITERAL = frozenset("-/")


def needs_extended_keys(name: str) -> bool:
    """Whether tmux < :data:`EXTENDED_MINIMUM` would TYPE ``name`` literally.

    The measured class (see :data:`EXTENDED_MINIMUM`): a shifted key with no
    legacy shifted escape (Enter/Escape/Space/Tab/BSpace, ctrl+shift on a
    letter, shift on any punctuation), plus ctrl+alt on those same bases and on
    the two punctuation aliases. Deliberately a little wider than the 3.4
    measurement alone — 3.3 mistypes ``C-M-Tab`` too, and a rare chord dropped
    on an old server is cheaper than nine characters typed into a running agent.
    """
    mods: set[str] = set()
    rest = name
    while len(rest) > 2 and rest[:2] in _MODIFIER_TOKENS:
        mods.add(rest[:2])
        rest = rest[2:]
    single = len(rest) == 1
    if "S-" in mods:
        if rest in _EXTENDED_BASES:
            return True
        if single and rest.isalpha() and "C-" in mods:
            return True
        if single and not rest.isalnum():
            # Shifted punctuation: no legacy escape on any version below 3.5.
            return True
    return "C-" in mods and "M-" in mods and (rest in _EXTENDED_BASES or rest in _CTRL_ALT_LITERAL)


def translate(
    key: str, character: str | None, *, printable: bool, extended_keys: bool = True
) -> Translation | None:
    """Translate one Textual key event; ``None`` when tmux has no safe name for it.

    ``extended_keys`` says whether the tmux SERVER is ≥ :data:`EXTENDED_MINIMUM`
    (the pane reads its version once): on an older server the extended-only
    chords are dropped here, because tmux would otherwise type their NAMES into
    the agent — the exact mistyping this module exists to prevent.
    """
    translation = _translate(key, character, printable=printable)
    if (
        translation is not None
        and translation.kind == "key"
        and not extended_keys
        and needs_extended_keys(translation.value)
    ):
        return None
    return translation


def _translate(key: str, character: str | None, *, printable: bool) -> Translation | None:
    """The table itself, capability-blind — ``translate`` applies the version gate.

    ``key`` is Textual's ``Key.key`` (``"ctrl+c"``, ``"shift+tab"``, ``"f5"``,
    ``"a"``), ``character`` its ``Key.character`` and ``printable`` its
    ``Key.is_printable``. Printable input is always literal, so a pasted ``é``
    or a typed ``[`` never goes through the name table at all — and neither does
    a shifted symbol, whose meaning only the keyboard layout knows.
    """
    if printable and character:
        return Translation("literal", character)
    if key in CHORDS:
        return Translation("key", CHORDS[key])
    if not key or key.endswith("+"):
        return None
    *modifiers, base = key.split("+")
    if any(modifier not in MODIFIERS for modifier in modifiers):
        return None
    ctrl = "ctrl" in modifiers
    alt = "alt" in modifiers or "meta" in modifiers
    shift = "shift" in modifiers
    prefix = ("C-" if ctrl else "") + ("M-" if alt else "") + ("S-" if shift else "")

    if base in SPECIAL:
        name = SPECIAL[base]
        if name == "Space" and not (ctrl or alt):
            return Translation("literal", " ")
        if ctrl and name in NO_CTRL:
            return None
        return Translation("key", prefix + name)

    if (match := _FUNCTION.fullmatch(base)) is not None:
        number = int(match.group(1))
        if number > MAX_FUNCTION_KEY:
            return None
        return Translation("key", f"{prefix}F{number}")

    char = _base_character(base)
    if char is None or char.isspace():
        return None
    if not modifiers:
        # A bare character the terminal did not flag printable (e.g. a control
        # picture): send it as text, never as a name it could collide with.
        return Translation("literal", char)
    if char.isalpha():
        if shift and not ctrl:
            # tmux lowercases ``S-a``; the uppercase letter IS the shifted key,
            # as text (plain shift) or after Meta (``M-A`` → ESC A).
            return Translation("key" if alt else "literal", ("M-" if alt else "") + char.upper())
        return Translation("key", prefix + char.lower())
    if char.isdigit():
        if ctrl or shift:
            # ``C-1`` reaches the agent as ``1``; ``shift+1`` is ``!`` on one
            # layout and ``+`` on another — without the character we cannot know.
            return None
        return Translation("key", prefix + char)
    # Punctuation with a modifier.
    if char == ARGV_SEPARATOR:
        return None
    if shift and not (ctrl or alt):
        return None
    if ctrl and char not in CTRL_PUNCTUATION:
        return None
    return Translation("key", prefix + char)
