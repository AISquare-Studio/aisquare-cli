"""Textual key events → tmux ``send-keys`` arguments. Pure, and unit-tested.

The fleet UI forwards every key the embedded pane receives to the agent
running inside tmux. Printable characters travel as literal text
(``send-keys -l``); everything else must be spelled in tmux's own key
vocabulary (``Enter``, ``BSpace``, ``C-c``, ``M-x``, ``BTab``…), or — for the
chords tmux can name but cannot send (below) — as the bytes that key means,
again as literal text. A key this table does not know is dropped and the caller
says so once: silently sending the wrong thing to a running agent is worse than
sending nothing.

Why the table is conservative: tmux TYPES AN UNKNOWN KEY NAME LITERALLY.
Measured against tmux 3.7c on 2026-08-28, and re-measured on 3.4 with a
raw-mode ``cat`` pane: ``send-keys Bogus`` puts those five characters into the
agent's input, and so do ``F13``, ``C-Bogus`` and ``S-F13``. What tmux does with
a name it parses but cannot encode is worse, because it varies: ``C-BSpace`` and
``C-Escape`` were typed out on 3.7c and VANISH on 3.4; ``S-a`` arrived as a
lowercase ``a`` on 3.7c and as the three characters ``S-a`` on 3.4; ``C-1`` as a
bare ``1`` on 3.7c and as nothing on 3.4. An argument ending in ``;`` is tmux's
command separator on both, so ``M-;`` types ``M-`` and starts a new command.
Every ``None`` below is one of those: a chord no tmux the fleet supports can put
in front of an agent honestly, dropped where the caller can say so once.

Naming a key is only half of it: tmux must also ENCODE it for the pane, and its
legacy (vt10x) encoding has nowhere to put a shift. The keys tmux parameterises
into a CSI sequence take every modifier (``S-Up`` is ``ESC [ 1 ; 2 A``,
``C-S-DC`` is ``ESC [ 3 ; 6 ~``), but Enter, Escape, Tab, BSpace, Space and
every ordinary character are single bytes with no room for one. When tmux cannot
encode the key it does not fail — ``cmd-send-keys`` falls back to typing the NAME
as text. Measured on tmux 3.4 (what ``ubuntu-latest`` ships, and the tmux this
repository runs) with the fleet's own configuration, by sending every one of the
473 names this module built BEFORE this fix into a ``stty raw -echo; cat`` pane
and reading the file back: 117 arrived spelled out — ``send-keys S-Enter`` put
the seven characters ``S-Enter`` in the pane — ``C-Enter`` and ``C-BTab``
arrived as nothing at all, and ``C-Tab`` arrived as a bare TAB, which the agent
cannot tell from the Tab key. Shift+Enter is a key Claude Code uses. Nothing is
added to the vocabulary here and nothing taken out of it: the 122 chords this
module already claimed to send are simply sent.

That is NOT a version gap, and raising :data:`aisquare.core.tmux.MIN_VERSION`
would not close it. tmux 3.4 knows every one of those names (``bind-key
S-Enter`` takes them; only ``Bogus`` is refused) and encodes all 117 correctly
the moment the PANE'S application turns extended keys on: with
``printf '\\033[>4;2m'`` — xterm's modifyOtherKeys — before ``cat``, ``S-Enter``
arrives as ``ESC [ 13 ; 2 u``. What tmux 3.4 does not understand is the OTHER
request: the kitty keyboard protocol's ``ESC [ > 1 u``, measured here to leave
the pane in legacy mode, is the one Claude Code makes. tmux publishes no format
variable for a pane's key mode either (``display-message -a`` is identical for
both panes on 3.4), so the fleet cannot ask and cannot switch it on.

So this module writes those bytes itself — :func:`extended_bytes`, sent as
literal text — and the pane's mode stops mattering. The encoding is tmux's own,
byte for byte: ``ESC [ <codepoint> ; <1 + shift + 2·alt + 4·ctrl> u``, with the
codepoint tmux uses, verified against the running binary for all 122 sequences
by ``tests/test_keys.py``. tmux's quirks are part of that: ctrl folds into the
character before the codepoint is taken, so ``C-S-a`` is ``ESC [ 65 ; 6 u`` (the
UPPERCASE A, not 97) and ``C-S-Space`` is 64 (``C-@``), while ``C-S-i`` is
``ESC [ 9 ; 2 u`` — ctrl+i IS Tab, and tmux spends the ctrl on saying so. Where
tmux and the kitty protocol disagree (kitty would spell ctrl+shift+a with the
unshifted 97), tmux's is the spelling an agent inside tmux already gets from a
modifyOtherKeys terminal, and the one that can be checked against the binary.
Three chords are ours alone: tmux keeps a legacy form for ctrl on Tab even in
extended mode (``C-M-Tab`` → ``ESC TAB``, ``C-S-Tab`` → ``ESC [ 1 ; 5 Z``),
which is a plain Tab or a plain back-tab to the agent, so we send the CSI-u that
carries the ctrl instead.

An agent that speaks neither dialect sees an unrecognised CSI sequence, which is
what it saw before minus the letters: in a ``bash`` pane on 3.4, Shift+Enter
after ``echo A`` left ``echo AS-EnterB`` on the line when it was sent by name
and ``echo A;2uB`` when it was sent as bytes. The agent the fleet actually runs
speaks both — the Claude Code binary carries the kitty requests (``ESC [ > 1 u``,
``ESC [ > 5 u``, ``ESC [ < u``) and xterm's ``ESC [ > 4 ; 2 m`` — and
``ESC [ 13 ; 2 u`` is Shift+Enter in either.

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

#: Whole chords tmux names differently from "modifier + key". ``BTab`` is the
#: back-tab (``CSI Z``) Claude Code cycles modes with, and ``M-BTab`` its
#: ESC-prefixed twin (``ESC ESC [ Z``); both were measured arriving as exactly
#: those bytes on tmux 3.4. That is why shift+tab keeps a NAME where every other
#: shifted chord below leaves as bytes: ``S-Tab`` is one of the names tmux types
#: out, and ``ESC [ Z`` is the back-tab every terminal application already knows.
#:
#: ``ctrl+shift+tab`` is NOT here: ``send-keys C-BTab`` puts nothing at all in a
#: pane on 3.4 (the keystroke is simply lost), and even a pane with extended
#: keys on gets ``ESC [ 1106343 ; 5 u`` — tmux's internal key code leaking into
#: the sequence. It goes through the ordinary path instead, where it is Tab with
#: ctrl and shift and :func:`extended_bytes` spells it ``ESC [ 9 ; 6 u``.
CHORDS: dict[str, str] = {
    "shift+tab": "BTab",
    "alt+shift+tab": "M-BTab",
}

#: Textual modifier → tmux modifier prefix. ``super`` and ``hyper`` have no
#: tmux spelling and make the whole chord untranslatable.
MODIFIERS: dict[str, str] = {"ctrl": "C-", "alt": "M-", "meta": "M-", "shift": "S-"}

#: Named keys no tmux puts a ctrl on: ``C-Escape`` and ``C-BSpace`` come out as
#: those literal strings on 3.7c and as nothing at all on 3.4. They are refused
#: rather than spelled in bytes — the user is told the chord went nowhere, which
#: is the honest answer for a key the fleet was never able to send.
NO_CTRL: frozenset[str] = frozenset({"Escape", "BSpace"})

#: Punctuation tmux can put a control modifier on (the classic C0 mappings:
#: ``C-@`` → NUL … ``C-_`` → US, ``C-?`` → DEL; ``C-/`` and ``C--`` are ``C-_``).
#: Everything else — ``C-,``, ``C-.``, ``C-=`` … — tmux sends as the bare
#: character (3.7c) or not at all (3.4), so those chords are dropped, with the
#: notice, rather than mistyped or invented.
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

# --- the chords tmux cannot hand to a pane ----------------------------------------------

#: Keys tmux parameterises into a CSI sequence of their own, whose modifier
#: parameter carries every combination there is (measured on 3.4: ``S-Up`` →
#: ``ESC [ 1 ; 2 A``, ``C-S-DC`` → ``ESC [ 3 ; 6 ~``, ``S-F1`` → ``ESC [ 1 ; 2 P``,
#: and identically whatever mode the pane is in). ``BTab`` is one of them: it IS
#: the shifted Tab (``ESC [ Z``). F1-F12 behave the same way.
CSI_PARAMETERISED: frozenset[str] = frozenset(
    {"Up", "Down", "Left", "Right", "Home", "End", "DC", "IC", "PPage", "NPage", "BTab"}
)

#: The named keys tmux sends as ONE control byte, and the codepoint CSI-u spells
#: them with: the byte itself, except that tmux's ``BSpace`` is DEL (0x7f) and
#: not BS. A single byte has no room for a shift, which is why every shifted
#: chord on these five is a name tmux would type out instead of sending.
CONTROL_CODEPOINT: dict[str, int] = {
    "Tab": 9,
    "Enter": 13,
    "Escape": 27,
    "Space": 32,
    "BSpace": 127,
}

#: The two of those five tmux cannot put a ctrl on. Measured on 3.4:
#: ``send-keys C-Enter`` reaches the pane as NOTHING, and ``C-Tab`` as a bare
#: ``TAB`` — the agent cannot tell it from the Tab key. ``C-Space`` is real
#: (NUL), and ctrl on Escape or BSpace never gets here (:data:`NO_CTRL`).
CTRL_UNSENDABLE: frozenset[str] = frozenset({"Enter", "Tab"})

#: Characters whose ctrl chord tmux folds into a key it has a NAME for, with
#: that key's codepoint: ctrl+i IS Tab, ctrl+m Enter, ctrl+[ Escape, ctrl+?
#: BSpace. The ctrl is spent on the fold, so ``C-S-i`` is ``ESC [ 9 ; 2 u`` —
#: Tab with shift — and not ``I`` with ctrl and shift. Measured, all four.
CTRL_FOLDS_TO_KEY: dict[str, int] = {"i": 9, "m": 13, "[": 27, "?": 127}

#: Characters tmux folds a ctrl INTO, to the C0 byte (``C-a`` → 0x01, ``C-\``
#: → 0x1c): the letters, ``@`` through ``_``, and ``?``. ``-`` and ``/`` are
#: NOT in it — tmux maps those to US (0x1f) later, in the encoder, which then
#: has nothing left for an alt, so ``C-M--`` and ``C-M-/`` are among the names
#: it types out as text while ``C-M-@`` and ``C-M-a`` are sent properly.
CTRL_FOLDED: frozenset[str] = frozenset(
    "abcdefghijklmnopqrstuvwxyz?" + "".join(chr(code) for code in range(64, 96))
)

#: CSI-u's modifier parameter is 1 plus the sum of the bits that are held.
SHIFT_BIT, ALT_BIT, CTRL_BIT = 1, 2, 4


def _split_modifiers(name: str) -> tuple[bool, bool, bool, str]:
    """A tmux key name as ``(ctrl, alt, shift, base)``; prefixes come in that order."""
    ctrl = name.startswith("C-")
    name = name[2:] if ctrl else name
    alt = name.startswith("M-")
    name = name[2:] if alt else name
    shift = name.startswith("S-")
    return ctrl, alt, shift, name[2:] if shift else name


def extended_bytes(name: str) -> str | None:
    """The bytes a tmux key NAME means, when tmux itself cannot send it; else ``None``.

    ``None`` is the ordinary answer — the 350 names this module still builds
    reach the agent correctly as names, and a name is what tmux and the agent
    both prefer. The other 122 are the chords tmux's legacy encoding has no room
    for (the module docstring): shift on anything that is not a
    :data:`CSI_PARAMETERISED` key, ctrl on Enter or Tab, and ctrl+alt on a
    character tmux does not fold a ctrl into. Sending one of those by name puts
    its own letters in the agent's prompt, or nothing at all.

    The sequence is CSI-u — ``ESC [ <codepoint> ; <modifier> u`` — where the
    modifier is 1 plus :data:`SHIFT_BIT`, :data:`ALT_BIT` and :data:`CTRL_BIT`
    for the modifiers held, and the codepoint is the one TMUX uses, fold and all
    (:data:`CTRL_FOLDS_TO_KEY`, and ctrl+Space → 64 because tmux reads it as
    ``C-@``). That is not decoration: 119 of these 122 sequences are byte for
    byte what the same ``send-keys <name>`` delivers when the pane has extended
    keys on, so the fleet's fallback puts nothing new in front of the agent.
    ``tests/test_keys.py`` re-measures every one against the running tmux.
    """
    ctrl, alt, shift, base = _split_modifiers(name)
    if base in CSI_PARAMETERISED:
        return None
    if base in CONTROL_CODEPOINT:
        if not (shift or (ctrl and base in CTRL_UNSENDABLE)):
            return None
        codepoint = 64 if base == "Space" and ctrl else CONTROL_CODEPOINT[base]
    elif len(base) == 1:
        if not (shift or (ctrl and alt and base not in CTRL_FOLDED)):
            return None
        if ctrl and base in CTRL_FOLDS_TO_KEY:
            codepoint, ctrl = CTRL_FOLDS_TO_KEY[base], False
        elif ctrl and base.isalpha():
            codepoint = ord(base.upper())
        else:
            codepoint = ord(base)
    else:
        return None  # F1-F12 and anything unknown: parameterised, or not ours to spell
    modifier = 1 + SHIFT_BIT * shift + ALT_BIT * alt + CTRL_BIT * ctrl
    return f"\x1b[{codepoint};{modifier}u"


def _named(name: str) -> Translation:
    """One tmux key name, as the name or as the bytes tmux could not send it with."""
    sequence = extended_bytes(name)
    return Translation("key", name) if sequence is None else Translation("literal", sequence)


def _base_character(base: str) -> str | None:
    """The single character a Textual base name stands for, or ``None``."""
    if len(base) == 1:
        return base
    return PUNCTUATION.get(base)


def translate(key: str, character: str | None, *, printable: bool) -> Translation | None:
    """Translate one Textual key event; ``None`` when tmux has no safe name for it.

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
        return _named(prefix + name)

    if (match := _FUNCTION.fullmatch(base)) is not None:
        number = int(match.group(1))
        if number > MAX_FUNCTION_KEY:
            return None
        return _named(f"{prefix}F{number}")

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
        return _named(prefix + char.lower())
    if char.isdigit():
        if ctrl or shift:
            # ``C-1`` reaches the agent as ``1``; ``shift+1`` is ``!`` on one
            # layout and ``+`` on another — without the character we cannot know.
            return None
        return _named(prefix + char)
    # Punctuation with a modifier.
    if char == ARGV_SEPARATOR:
        return None
    if shift and not (ctrl or alt):
        return None
    if ctrl and char not in CTRL_PUNCTUATION:
        return None
    return _named(prefix + char)
