"""Textual key events → tmux ``send-keys`` arguments. Pure, and unit-tested.

The fleet UI forwards every key the embedded pane receives to the agent
running inside tmux. Printable characters travel as literal text
(``send-keys -l``); everything else must be spelled in tmux's own key
vocabulary (``Enter``, ``BSpace``, ``C-c``, ``M-x``, ``S-Enter``…). A key this
table has no safe name for is dropped — silently sending the wrong thing to a
running agent is worse than sending nothing — and :func:`translate` says WHY,
as a :class:`Drop`, because the caller's answer depends on it: a chord the
reader meant is named once, a tmux too old to carry it is a warning, and a
modifier tapped on its own or a Cmd chord is not mentioned at all (#151).

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
import unicodedata
from dataclasses import dataclass
from functools import cache
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


DropReason = Literal["command", "nothing_to_type", "no_name", "too_old"]


@dataclass(frozen=True)
class Drop:
    """Nothing is sent, and why — the pane's guide to what, if anything, to say.

    Each refusal states its own reason at the site that makes it — the table's
    eight, the modifier gate's, the version gate's — because the pane cannot
    recover that from the key's shape, and a heuristic that tried (reviews of
    #161, rounds 2 and 3) misread a Cmd chord as deliberate aim, numpad ``+``
    as a key nobody pressed, ``±`` and ``«`` on a European layout as nothing at
    all, and a tmux too old for shift+enter as a key with no spelling anywhere.

    - ``command``: a modifier tmux cannot spell — ``super``/``hyper``, which is
      how macOS Cmd and the kitty protocol's extras arrive. Cmd+V is a command
      for the terminal or the OS, not a request to type a ``v``, and not a
      keystroke aimed at the agent: nothing to say.
    - ``nothing_to_type``: no keystroke in the event, whatever is held with
      it. A modifier or a lock, or a whole key a kitty-protocol terminal
      reports only because Textual asks it for every key — Menu, PrtSc, Pause,
      the volume and media keys, the keypad's centre: there is no character
      behind the key, so ``ctrl+pause`` is as empty as ``pause``. A malformed
      name. Nothing to say (#151).
    - ``no_name``: a keystroke LOST. A chord the reader meant — a modifier held
      on a character, a function key past the twelve tmux knows, a chord on a
      character this table never measured a tmux name for — that arrived
      without its text; or a keypad key whose text only the layout knows
      (:data:`KEYPAD_LAYOUT_DEPENDENT`). Worth one quiet line.
    - ``too_old``: this tmux SERVER cannot carry the chord (below
      :data:`EXTENDED_MINIMUM` it would type the chord's NAME into the agent)
      and no text came with it. A loss the reader can fix, so a warning that
      names the version.
    """

    reason: DropReason


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

#: Uppercase letters whose ``ESC``-prefixed form is an escape-sequence INTRODUCER
#: rather than a chord: ``ESC N`` (SS2), ``ESC O`` (SS3) and ``ESC P`` (DCS) are
#: what a program's own key parser holds and joins with whatever comes NEXT —
#: Node's readline turns ``ESC O`` followed by ``A`` into Up. tmux writes ``M-O``
#: as exactly those two bytes, so the chord is never spelled: the letter is typed
#: when the terminal reported it, and nothing is sent when it did not (review of
#: #135, finding 15).
ESC_INTRODUCERS: frozenset[str] = frozenset("NOP")

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
    # The numeric keypad's operator keys, as the kitty protocol names them
    # (``KP_ADD`` … ``KP_EQUAL``, Textual's ``FUNCTIONAL_KEYS``). A terminal
    # that reports the key's text sends ``+`` alongside and the text wins above;
    # one that honours REPORT_ALL_KEYS but not REPORT_ASSOCIATED_TEXT sends the
    # bare name, and these ARE keystrokes — numpad ``+`` is typed, not a key
    # nobody meant (review of #161, round 2). Only the five engraved the same
    # on every layout; the other two are :data:`KEYPAD_LAYOUT_DEPENDENT`.
    "add": "+",
    "subtract": "-",
    "multiply": "*",
    "divide": "/",
    "equal": "=",
}

#: The keypad's ``decimal`` and ``separator`` keys. Kitty key codes are physical,
#: so a German or French numpad's ``,`` key arrives as ``decimal`` too — and the
#: only terminal that sends the bare name is one that reports no text, which is
#: exactly when the layout cannot be asked. Guessing ``.`` would type the wrong
#: character into a running agent, the one thing this module exists to prevent,
#: so they are a keystroke lost with a word, never a glyph (review of #161,
#: round 3).
KEYPAD_LAYOUT_DEPENDENT: frozenset[str] = frozenset({"decimal", "separator"})

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


@cache
def _named_characters() -> dict[str, str]:
    """Textual's name for every typeable character → the character.

    Textual names a key it has no word for after ``unicodedata.name`` of its
    character, lowercased, with BOTH hyphens and spaces made underscores
    (``textual.keys._character_to_key``; ``tests/test_keys.py`` holds this
    table against it). Read back by table rather than by ``unicodedata.lookup``
    on a respelt name, because ``lookup`` is exact and cannot know which
    underscores were hyphens: ``plus_minus_sign`` (``±``, a key on the Canadian
    layout) and the ``«`` ``»`` of several European ones never resolved that
    way (review of #161, round 3). Built once, on first use, over the Basic
    Multilingual Plane — 11 ms, 5.8k names.

    Letters and digits are their own names and never reach this (a one-character
    base is its character). Controls are never a literal to send, nor are the
    line and paragraph separators; a NO-BREAK SPACE is — AltGr+space on the
    French and Canadian layouts — and ``send-keys -l`` carries it fine, since
    it is not a tmux key NAME and none of the mistyping hazards apply.
    """
    table: dict[str, str] = {}
    for codepoint in range(0x20, 0x10000):
        char = chr(codepoint)
        if char.isalnum():
            continue
        category = unicodedata.category(char)
        if category[0] == "C" or category in ("Zl", "Zp"):
            continue
        try:
            name = unicodedata.name(char)
        except ValueError:
            continue
        table[name.lower().replace("-", "_").replace(" ", "_")] = char
    return table


def _unicode_character(base: str) -> str | None:
    """The character a bare key named after its Unicode character stands for."""
    return _named_characters().get(base)


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
) -> Translation | Drop:
    """Translate one Textual key event; a :class:`Drop` when tmux has no safe name for it.

    ``extended_keys`` says whether the tmux SERVER is ≥ :data:`EXTENDED_MINIMUM`
    (the pane reads its version once): on an older server the extended-only
    chords are dropped here, because tmux would otherwise type their NAMES into
    the agent — the exact mistyping this module exists to prevent.

    TWO ANSWERS WHEN THERE IS NO NAME, and the difference is the user's intent.
    A modifier tmux cannot spell — ``super``/``hyper``, which is how macOS Cmd
    and the kitty protocol's extras arrive — means a COMMAND was pressed: Cmd+V
    is not a request to type a ``v``, so it is dropped and never falls back.
    Anything else with no name is a chord whose text the terminal already
    decided: shift on a digit or on punctuation, alt+shift on a letter whose
    ``ESC`` form is an escape introducer (:data:`ESC_INTRODUCERS`), a chord this
    server is too old to carry. There the reported character still travels,
    which is what this module did before any chord exception existed; with no
    character reported there is nothing to type, and the :class:`Drop` says
    which kind of nothing this was.

    The key name is split ONCE, here, and the parts are handed down: an empty
    token (``"+a"``, ``"ctrl+"``) is a malformed NAME, never a modifier, so it
    is neither gated as one nor read as deliberate aim — the reported
    character is typed, as it always was, and with none there is nothing to
    say (reviews of the third to fifth versions of #117, and of #161, which
    each broke a different one of those spellings). Every other reason is the
    table's own, stated at the refusal that knows it: this function adds only
    the two gates — a modifier tmux cannot spell, a server too old — and the
    fallback to reported text (review of #161, round 3).
    """
    *modifiers, base = key.split("+")
    if any(modifier and modifier not in MODIFIERS for modifier in modifiers):
        return Drop("command")
    if not base or not all(modifiers):
        # A malformed name — ``""``, ``"+"``, ``"+a"``, ``"ctrl+"``, ``"ctrl++"``,
        # ``"alt+"``. Nothing to look up and no modifier to read: an empty
        # token is a broken NAME, never a modifier that happens to be
        # unspellable and never deliberate aim. The reported character is
        # typed, as it always was; with none there is nothing to say.
        if printable and character:
            return Translation("literal", character)
        return Drop("nothing_to_type")
    translation = _translate(key, modifiers, base, character, printable=printable)
    if (
        isinstance(translation, Translation)
        and translation.kind == "key"
        and not extended_keys
        and needs_extended_keys(translation.value)
    ):
        translation = Drop("too_old")
    if isinstance(translation, Drop) and printable and character:
        # Every refusal falls back to the text the terminal reported, which is
        # what this module did before any chord exception existed.
        return Translation("literal", character)
    return translation


def _is_ascii_letter(character: str) -> bool:
    return len(character) == 1 and character.isascii() and character.isalpha()


def _translate(
    key: str, modifiers: list[str], base: str, character: str | None, *, printable: bool
) -> Translation | Drop:
    """The table itself, capability-blind — ``translate`` applies the version gate.

    ``key`` is Textual's ``Key.key`` (``"ctrl+c"``, ``"shift+tab"``, ``"f5"``,
    ``"a"``), already split by ``translate`` into its ``modifiers`` and ``base``
    (a well-formed name: no empty token); ``character`` is ``Key.character``
    and ``printable`` its ``Key.is_printable``. Printable input is literal, so a pasted ``é`` or a
    typed ``[`` never goes through the name table at all — and neither does a
    shifted symbol, whose meaning only the keyboard layout knows.

    EXCEPT alt/meta on an ASCII LETTER. Textual's parser reads a legacy
    terminal's ``ESC p`` as ``Key("alt+p", character="p")`` — the character is
    set and printable — so "the text wins" typed a bare ``p`` into the agent and
    Claude Code's alt+p (switch model) never fired (reported 2026-09-02 and
    2026-09-10 from the fleet UI). With alt held the chord is the meaning; the
    character is only how the terminal spelt it. Letters are the whole of the
    exception because they are the whole of what a parser delivers with an alt
    token AND a printable character. Measured against Textual 8.2.8's
    ``XTermParser`` — ``tests/test_keys.py`` drives it — rather than against
    hand-built events, which had promised alt+digit and alt+space chords the
    parser never sends (review of #135, finding 15):

    - a legacy terminal's ``ESC 1`` … ``ESC 0`` and ``ESC SPACE`` reach this
      table as the glyphs Textual's sequence table maps them to (``¡ ™ £ ¢ ∞ §
      ¶ • ª º``, a plain space) with no alt token at all, and travel as that
      text; ``ESC b`` and ``ESC f`` arrive as ctrl+left / ctrl+right (Textual's
      iTerm "natural editing" rows) and travel as ``C-Left`` / ``C-Right``; an
      ``ESC`` before a control character loses the alt (``ESC DLE`` is ctrl+p);
    - a kitty-protocol terminal that reports the typed text has the alt token
      dropped by the parser itself (``_parse_extended_key``), so alt+p there is
      a bare ``p`` and macOS Option makes it a ``π``; one that reports no text
      delivers ``Key("alt+1", None)`` and ``Key("alt+space", None)`` — no
      character — and those reach the name table below as ``M-1``, ``M-Space``.

    ASCII, because every name this module can emit was measured against a real
    tmux (see the module docstring) and ``M-é`` / ``M-ф`` never were: an AltGr
    or accented layout reaches here through the same ``ESC`` prefix. Alt on
    PUNCTUATION stays text: through the name table it would be dropped (``;``
    is tmux's separator) or worse — ``M-[`` is ``ESC [``, the CSI introducer.
    An alt chord on a SHIFTED letter keeps its case — ``ESC A`` is what
    alt+shift+a sends, and a kitty ``meta+P`` names the uppercase base with no
    ``shift`` token, which used to come out as a lowercase ``M-p`` — except the
    :data:`ESC_INTRODUCERS`, which have no safe name at all.

    A modifier tmux has no spelling for — ``super``/``hyper``, which is how
    macOS Cmd and the kitty protocol's own extras arrive — drops the key rather
    than falling through to its character: Cmd+V is not a request to type a
    ``v``. That is ``translate``'s modifier gate, deliberate and pinned by a
    test (review of the second version of #117).
    """
    ctrl = "ctrl" in modifiers
    alt = "alt" in modifiers or "meta" in modifiers
    if printable and character and not (alt and _is_ascii_letter(character)):
        return Translation("literal", character)
    if key in CHORDS:
        return Translation("key", CHORDS[key])
    shift = "shift" in modifiers
    prefix = ("C-" if ctrl else "") + ("M-" if alt else "") + ("S-" if shift else "")

    if base in SPECIAL:
        name = SPECIAL[base]
        if name == "Space" and not (ctrl or alt):
            return Translation("literal", " ")
        if ctrl and name in NO_CTRL:
            return Drop("no_name")
        return Translation("key", prefix + name)

    if (match := _FUNCTION.fullmatch(base)) is not None:
        number = int(match.group(1))
        if number > MAX_FUNCTION_KEY:
            return Drop("no_name")
        return Translation("key", f"{prefix}F{number}")

    char = _base_character(base)
    if char is None:
        # Not a key this table names and not a character it spells: either a
        # key Textual named after its Unicode character — ``section_sign``,
        # ``plus_minus_sign``, ``no_break_space`` on a non-US layout — reported
        # without its text, or a key with NO character behind it at all.
        char = _unicode_character(base)
        if char is None:
            if base in KEYPAD_LAYOUT_DEPENDENT:
                return Drop("no_name")
            # A modifier or a lock, Menu, PrtSc, Pause, a volume or media key,
            # the keypad's centre — what a kitty-protocol terminal reports
            # because Textual asks for every key it has, and nobody's message
            # to an agent. Whatever is held with it: ``shift+caps_lock`` and
            # ``ctrl+pause`` are as empty as the bare key (#151; reviews of
            # #161, rounds 2 and 3).
            return Drop("nothing_to_type")
        if modifiers:
            # The text wins above whenever a terminal reports it; the name
            # says what was typed only for the BARE key. ``M-§`` was never
            # measured against a tmux, so a chord here is a keystroke lost.
            return Drop("no_name")
    if not modifiers:
        # A bare character the terminal did not flag printable (a control
        # picture, a NO-BREAK SPACE): send it as text, never as a name it
        # could collide with.
        return Translation("literal", char)
    if char.isspace():
        return Drop("no_name")
    if char.isalpha():
        # The letter's case IS the shift: ``ESC A`` is what alt+shift+a sends,
        # and a kitty ``meta+P`` names the uppercase base with no shift token.
        # tmux lowercases ``S-a``, so the shifted letter travels as text (plain
        # shift) or after Meta (``M-A`` → ESC A) — never as an introducer.
        if (shift or char.isupper()) and not ctrl:
            if alt and char.upper() in ESC_INTRODUCERS:
                return Drop("no_name")
            return Translation("key" if alt else "literal", ("M-" if alt else "") + char.upper())
        return Translation("key", prefix + char.lower())
    if char.isdigit():
        if ctrl or shift:
            # ``C-1`` reaches the agent as ``1``; ``shift+1`` is ``!`` on one
            # layout and ``+`` on another — without the character we cannot know.
            # No name: ``translate`` types the character when there is one.
            return Drop("no_name")
        return Translation("key", prefix + char)
    # Punctuation with a modifier.
    if char == ARGV_SEPARATOR:
        return Drop("no_name")
    if shift and not (ctrl or alt):
        return Drop("no_name")
    if ctrl and char not in CTRL_PUNCTUATION:
        return Drop("no_name")
    return Translation("key", prefix + char)
