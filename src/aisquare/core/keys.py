"""Textual key events → tmux ``send-keys`` arguments. Pure, and unit-tested.

The fleet UI forwards every key the embedded pane receives to the agent
running inside tmux. Printable characters travel as literal text
(``send-keys -l``); everything else must be spelled in tmux's own key
vocabulary (``Enter``, ``BSpace``, ``C-c``, ``M-x``, ``BTab``…), or — for the
chords tmux can name but cannot deliver (THE MEASUREMENT below) — as the bytes
that key means, again as literal text. A key this table does not know is dropped
and the caller says so once: silently sending the wrong thing to a running agent
is worse than sending nothing.

Why the table is conservative: tmux TYPES AN UNKNOWN KEY NAME LITERALLY.
``send-keys Bogus`` puts those five characters into the agent's input, and so do
``C-Bogus``, ``F13`` and ``S-F13`` — on every build measured below. tmux's own
parser is the tell: on 3.2, 3.4 and 3.5a alike, ``bind-key Bogus`` and
``bind-key F13`` answer ``unknown key``, while ``bind-key S-Enter``, ``C-S-a``,
``M-S-Tab`` and ``C-Enter`` are accepted. What tmux does with a name it PARSES
but cannot encode is worse, because it varies by version. Measured 2026-08-29 on
builds of 3.2, 3.2a, 3.3a, 3.4 and 3.5a, each name sent into a
``stty raw -echo; cat > file`` pane with the file's bytes read back:

* ``C-BSpace`` — nothing on 3.2 through 3.4; typed out on 3.5a.
* ``C-Escape`` — typed out on 3.2, nothing on 3.2a/3.3a/3.4, typed out on 3.5a.
* ``S-a`` — typed out on 3.2 through 3.4; the bare ``a`` on 3.5a.
* ``C-1`` — nothing on 3.2 through 3.4; the bare ``1`` on 3.5a.
* ``C-,`` ``C-.`` ``C-=`` — nothing on 3.2 through 3.4; the bare character on 3.5a.

(A reST grid would be the natural shape for that, but its ``=`` rules read as
merge-conflict markers to ``tests/test_no_unresolved_conflicts.py``.)

An argument ending in ``;`` is tmux's command separator on all of them, so
``send-keys M-;`` puts ``M-`` in the pane and starts a new command (and
``bind-key M-;`` answers ``unknown key: M-``). Every ``None`` below is one of
those: a chord no tmux the fleet supports can put in front of an agent honestly,
dropped where the caller can say so once.

Naming a key is only half of it: tmux must also ENCODE it for the pane, and its
legacy (vt10x) encoding has nowhere to put a shift. The keys tmux parameterises
into a CSI sequence take every modifier (``S-Up`` is ``ESC [ 1 ; 2 A``,
``C-S-DC`` is ``ESC [ 3 ; 6 ~``, ``S-F1`` is ``ESC [ 1 ; 2 P``), but Enter,
Escape, Tab, BSpace, Space and every ordinary character are single bytes with no
room for one. When tmux cannot encode the key it does not fail —
``cmd-send-keys`` falls back to typing the NAME as text.

THE MEASUREMENT. This is the one count the rest of the module quotes; anything
below that states a number states THIS one, and ``tests/test_keys.py`` re-takes
it (``test_the_vocabulary_is_the_one_the_module_docstring_counts`` and the
real-tmux tests) so it cannot drift. Taken 2026-08-29 against tmux 3.4
(``/usr/bin/tmux`` — what ``ubuntu-latest`` ships, and the tmux this repository
runs) with the fleet's own configuration, by sending every name into a raw
``cat`` pane and comparing the FILE'S bytes:

* The vocabulary. :func:`translate` is swept over 1785 Textual chords —
  Textual 8.2.8's ``Keys`` enum plus this module's own base names, each with
  every one-, two- and three-modifier combination. 1179 of them translate to
  something, and between them they name 471 distinct tmux keys.
* Sent BY NAME into a pane in tmux's legacy mode, 121 of those 471 do not
  arrive as themselves: 116 arrive as their own letters (``send-keys S-Enter``
  puts the seven characters ``S-Enter`` in the pane), ``C-Enter`` arrives as
  nothing at all, and four arrive as ANOTHER key's bytes — ``C-Tab`` as a bare
  TAB, ``C-S-Tab`` as ``ESC [ Z`` (a plain back-tab), ``C-M-Tab`` as
  ``ESC TAB``, ``C-M-S-Tab`` as ``ESC ESC [ Z``. Shift+Enter is a key Claude
  Code uses.
* So this module spells those 121 names itself (:func:`extended_bytes`), as 112
  distinct CSI-u sequences. The other 350 travel as names, which is what tmux
  and the agent both prefer.
* In a pane that HAS extended keys on, tmux encodes 118 of the 121 as exactly
  the bytes written here; the three exceptions are the ctrl-on-Tab family
  below.

Nothing is added to the vocabulary and nothing taken out of it: the chords this
module already claimed to send are simply sent.

That is NOT a version gap, and raising :data:`aisquare.core.tmux.MIN_VERSION`
would not close it. tmux 3.4 knows every one of those names (``bind-key`` takes
them; only a name like ``Bogus`` is refused) and encodes 118 of the 121 — every
one but the ctrl-on-Tab family below — the moment the PANE'S application turns
extended keys on: with ``printf '\\033[>4;2m'`` — xterm's modifyOtherKeys —
before ``cat``, ``S-Enter`` arrives as ``ESC [ 13 ; 2 u``. What tmux 3.4 does
not understand is the OTHER request, the kitty keyboard protocol's
``ESC [ > 1 u`` (nor ``ESC [ > 5 u``): measured here, a pane that sends either
still gets the seven characters ``S-Enter``. tmux
publishes no format variable for a pane's key mode either — ``display-message
-a`` prints the same 120 variables for a legacy and a modifyOtherKeys pane,
differing only in the ones that identify the pane (``pane_id``, ``pane_pid``,
``pane_tty``, the session and window names, the layout), and its only two
key-ish variables, ``keypad_flag`` and ``keypad_cursor_flag``, are ``0`` in
both. So the fleet can neither ask nor switch it on.

So this module writes those bytes itself — :func:`extended_bytes`, sent as
literal text — and the pane's mode stops mattering. The encoding is tmux's own,
byte for byte: ``ESC [ <codepoint> ; <1 + shift + 2·alt + 4·ctrl> u``, with the
codepoint TMUX uses. tmux's quirks are part of that: ctrl folds into the
character before the codepoint is taken, so ``C-S-a`` is ``ESC [ 65 ; 6 u`` (the
UPPERCASE A, not 97) and ``C-S-Space`` is 64 (``C-@``), while ``C-S-i`` is
``ESC [ 9 ; 2 u`` — ctrl+i IS Tab, and tmux spends the ctrl on saying so. All
three were read back out of tmux 3.4's own extended pane, not reasoned about.
Where tmux and the kitty protocol disagree (kitty would spell ctrl+shift+a with
the unshifted 97), tmux's is the spelling an agent inside tmux already gets from
a modifyOtherKeys terminal, and the one that can be checked against the binary.

Three chords are ours alone, and they are the whole gap between 121 and 118:
tmux 3.4 keeps a legacy form for ctrl on Tab even in an extended pane
(``C-M-Tab`` → ``ESC TAB``, ``C-S-Tab`` → ``ESC [ 1 ; 5 Z``, ``C-M-S-Tab`` →
``ESC ESC [ Z``), and a legacy form has nowhere to put the ctrl, so the agent
gets a plain Tab or a plain back-tab and cannot tell those chords from Tab and
back-tab. We send the CSI-u that carries the ctrl instead — which is, byte for
byte, what tmux 3.3a produces for all three, so this is 3.4 losing them rather
than us inventing something.

ESC is what makes this transport work at all. ``send-keys -l`` is not always
transparent: measured on tmux 3.5a, in a pane with extended keys on, of the 32
control bytes 0x01-0x1f plus DEL only FOUR survive — TAB (0x09), CR (0x0d), ESC
(0x1b) and DEL (0x7f); the other 28 are silently dropped. (On 3.4, in either
pane mode, and on 3.5a in a legacy pane, all 32 arrive.) Every sequence here is
ESC followed by printable ASCII, so it crosses that intact.

An agent that speaks neither dialect sees an unrecognised CSI sequence, which is
what it saw before minus the letters: in a ``bash --norc -i`` pane on 3.4,
Shift+Enter after ``echo A`` left ``echo AS-EnterB`` on the line when it was
sent by name and ``echo A;2uB`` when it was sent as bytes — both measured.
``ESC [ 13 ; 2 u`` is Shift+Enter to a terminal application that has asked for
either protocol. Which of them the Claude Code binary asks for is NOT measured
here, and is the one claim in this docstring that is not: scanning its 214 MB
compiled bundle found the word ``kitty`` 43 times and none of the four request
sequences as bytes, which in a compiled bundle settles nothing either way.

Modifier chords beyond ctrl/alt on letters depend on the OUTER terminal
speaking the kitty keyboard protocol. Textual asks for it — the 8.2.8 installed
here writes ``ESC [ > <flags> u`` when its Linux driver starts and ``ESC [ < u``
when it stops (``textual/drivers/linux_driver.py``) — but where the terminal
does not answer, ``shift+enter`` simply arrives as ``enter`` and there is
nothing to translate. Textual names chords with the modifiers sorted
alphabetically (``alt+ctrl+x``, ``ctrl+shift+a``); the order is irrelevant here.
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

#: Named keys no tmux puts a ctrl on. Measured across the five builds: on 3.4
#: (and 3.2a, 3.3a) ``C-Escape`` and ``C-BSpace`` both reach the pane as NOTHING
#: at all; on 3.5a both are typed out as their own letters, and ``C-Escape`` is
#: typed out on 3.2 too. Refused rather than spelled in bytes — the user is told
#: the chord went nowhere, which is the honest answer for a key the fleet was
#: never able to send on any tmux it supports.
NO_CTRL: frozenset[str] = frozenset({"Escape", "BSpace"})

#: ctrl on ``-``, ``/`` and ``_``: three different keys, ONE byte. Measured in a
#: raw pane on 3.2a, 3.3a, 3.4 and 3.5a, ``C--``, ``C-/`` and ``C-_`` all arrive
#: as US (``0x1f``), so an agent cannot tell which of the three was pressed.
#: Recorded here, deliberately not closed, and it is NOT the ``C-Tab`` case:
#: ``C-Tab`` arrived as a bare TAB — the byte of a DIFFERENT key the user can
#: also press on its own — whereas ``0x1f`` is the byte this ctrl actually
#: means. tmux keeps that byte even where it CAN spell CSI-u: in an extended
#: pane on 3.4, ``C--`` is ``ESC [ 45 ; 5 u`` and ``C-/`` is ``ESC [ 47 ; 5 u``,
#: but ``C-_`` is STILL ``0x1f``. Spelling the first two in bytes would buy two
#: thirds of a distinction and take ``0x1f`` — which every one of those builds
#: delivers today — away from an agent that does not decode CSI-u, for a chord
#: tmux is not mistyping. docs/plans/fleet-tui.md §6 also specifies ``C-<x>``.
#:
#: One build is worse, and this is the cost of the decision: on tmux 3.2 —
#: :data:`aisquare.core.tmux.MIN_VERSION` — ``C-/`` is not in the key table at
#: all (``bind-key C-/`` answers ``unknown key: C-/``), so ``send-keys C-/``
#: TYPES those three characters. Of the 350 names this module sends as names,
#: that is the only one TYPED OUT as its own letters by any of the five builds
#: measured (3.2, 3.2a, 3.3a, 3.4, 3.5a), and only by 3.2; 3.2a is where tmux
#: learned the name.
CTRL_US_ALIASES: frozenset[str] = frozenset("-/_")

#: Punctuation tmux can put a control modifier on (the classic C0 mappings:
#: ``C-@`` → NUL … ``C-_`` → US, ``C-?`` → DEL, and :data:`CTRL_US_ALIASES`).
#: Everything else — ``C-,``, ``C-.``, ``C-=`` … — tmux sends as the bare
#: character (3.5a) or not at all (3.2 through 3.4), so those chords are
#: dropped, with the notice, rather than mistyped or invented.
CTRL_PUNCTUATION: frozenset[str] = frozenset("@[\\]^?") | CTRL_US_ALIASES

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
#: start of the next, so no key NAME may end in it: ``send-keys M-;`` puts the
#: two characters ``M-`` in the pane and starts a second command (measured on
#: 3.2, 3.4 and 3.5a).
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
#: ``TAB`` (0x09) — the agent cannot tell it from the Tab key, which arrives as
#: the same byte. ``C-Space`` is real (NUL, measured), and ctrl on Escape or
#: BSpace never gets here (:data:`NO_CTRL`).
CTRL_UNSENDABLE: frozenset[str] = frozenset({"Enter", "Tab"})

#: Characters whose ctrl chord tmux folds into a key it has a NAME for, with
#: that key's codepoint: ctrl+i IS Tab, ctrl+m Enter, ctrl+[ Escape, ctrl+?
#: BSpace. The ctrl is spent on the fold, so ``C-S-i`` is ``ESC [ 9 ; 2 u`` —
#: Tab with shift — and not ``I`` with ctrl and shift. All four were read back
#: out of tmux 3.4's own extended pane, which spells them exactly this way.
CTRL_FOLDS_TO_KEY: dict[str, int] = {"i": 9, "m": 13, "[": 27, "?": 127}

#: Characters tmux folds a ctrl INTO, to the C0 byte (``C-a`` → 0x01, ``C-\``
#: → 0x1c): the letters, ``@`` through ``_``, and ``?``. ``-`` and ``/`` are
#: NOT in it (:data:`CTRL_US_ALIASES`) — tmux maps those to US (0x1f) later, in
#: the encoder, which then has nothing left for an alt, so on 3.4 ``C-M--`` and
#: ``C-M-/`` arrive as their own five characters while ``C-M-@`` arrives as
#: ``ESC NUL`` and ``C-M-a`` as ``ESC 0x01``.
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
    both prefer. The other 121 are the chords tmux's legacy encoding has no room
    for (THE MEASUREMENT in the module docstring, which is where every count
    here comes from): shift on anything that is not a :data:`CSI_PARAMETERISED`
    key, ctrl on Enter or Tab, and ctrl+alt on a character tmux does not fold a
    ctrl into. Sending one of those by name puts its own letters in the agent's
    prompt, nothing at all, or another key's bytes.

    The sequence is CSI-u — ``ESC [ <codepoint> ; <modifier> u`` — where the
    modifier is 1 plus :data:`SHIFT_BIT`, :data:`ALT_BIT` and :data:`CTRL_BIT`
    for the modifiers held, and the codepoint is the one TMUX uses, fold and all
    (:data:`CTRL_FOLDS_TO_KEY`, and ctrl+Space → 64 because tmux reads it as
    ``C-@``). That is not decoration: 118 of these 121 names are spelled by tmux
    3.4 itself, byte for byte, when the same ``send-keys <name>`` goes to a pane
    that has extended keys on, so the fleet's fallback puts nothing new in front
    of the agent. The three that are not are the ctrl-on-Tab family, where 3.4
    keeps a legacy form that loses the ctrl.

    ``tests/test_keys.py`` re-measures that against whatever tmux is on PATH:
    ``test_our_sequences_are_the_ones_this_tmux_sends_when_it_can`` sends all
    121 of these names — plus ``S-Space`` and ``S-Tab``, which the encoder must
    get right even though :func:`translate` never builds them — into an extended
    pane and compares the bytes. The expectations there are tmux 3.4's; tmux's
    own encoder has changed across versions and that test says so with its
    measurements.
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
