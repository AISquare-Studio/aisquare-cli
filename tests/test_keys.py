"""``core.keys``: every Textual key the pane forwards arrives in tmux's vocabulary.

Three kinds of test. The table tests pin each row of docs/plans/fleet-tui.md §6
and the deliberate holes (a ``None`` for every key tmux would MISTYPE — it sends
an unknown name as literal text). The encoding tests pin the CSI-u bytes the
fleet sends for the chords tmux can name but cannot deliver to a pane, codepoint
by codepoint. The real-tmux tests (skipped without a ``tmux`` on PATH) send all
of it into a raw ``cat`` pane and read the bytes back from the file it wrote:
no name may arrive spelled out OR vanish, every sequence must arrive byte for
byte, and tmux's own encoding of the same key — in a pane that has extended keys
on, which is the only place tmux will produce one — must be the sequence we send.

What "all of it" covers is the point of this file, so it is stated: 350 names
sent as names, and every one of the 121 the fleet spells itself compared with
tmux's own encoding of the same name, plus ``S-Space`` and ``S-Tab``, which the
encoder must get right even though ``translate`` never builds them. Those are
THE MEASUREMENT's numbers in ``core.keys``, and
``test_the_vocabulary_is_the_one_the_module_docstring_counts`` fails if this
sweep and that prose stop describing the same vocabulary.

The read-back is a file rather than ``capture-pane`` so that what is compared is
bytes and not a rendering of them: ``ESC [ 13 ; 2 u`` and the seven characters
``S-Enter`` are both perfectly visible on a screen, and only one of them is the
Shift+Enter the user pressed.

The real-tmux expectations are one tmux's — 3.4, what ``ubuntu-latest`` ships.
tmux's key encoder has changed across the versions this repository supports (see
:data:`TMUX_EXTENDED_FORM` and the cross-check's own docstring for what was
measured on 3.2, 3.2a, 3.3a and 3.5a), so those tests carry the running version
in their failure messages: a red here on another build is a report about that
build, and reading it as one is the difference between learning something and
relaxing an assertion.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import re
import shutil
import string
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from textual.keys import Keys

from aisquare.core import keys as keys_module
from aisquare.core.keys import (
    ALT_BIT,
    ARGV_SEPARATOR,
    CHORDS,
    CSI_PARAMETERISED,
    CTRL_BIT,
    CTRL_PUNCTUATION,
    CTRL_US_ALIASES,
    MAX_FUNCTION_KEY,
    NO_CTRL,
    PUNCTUATION,
    SHIFT_BIT,
    SPECIAL,
    Translation,
    extended_bytes,
    translate,
)
from aisquare.core.tmux import BUNDLED_CONF, TmuxError, TmuxServer


def literal(text: str) -> Translation:
    return Translation("literal", text)


def key(name: str) -> Translation:
    return Translation("key", name)


# --- the §6 table -----------------------------------------------------------------------

TABLE: list[tuple[str, str]] = [
    ("enter", "Enter"),
    ("escape", "Escape"),
    ("tab", "Tab"),
    ("shift+tab", "BTab"),
    ("backspace", "BSpace"),
    ("delete", "DC"),
    ("insert", "IC"),
    ("up", "Up"),
    ("down", "Down"),
    ("left", "Left"),
    ("right", "Right"),
    ("home", "Home"),
    ("end", "End"),
    ("pageup", "PPage"),
    ("pagedown", "NPage"),
    ("f1", "F1"),
    ("f5", "F5"),
    ("f12", "F12"),
    ("ctrl+c", "C-c"),
    ("ctrl+o", "C-o"),
    ("ctrl+r", "C-r"),
    ("alt+x", "M-x"),
    ("meta+x", "M-x"),
    ("shift+up", "S-Up"),
    ("ctrl+up", "C-Up"),
    ("alt+enter", "M-Enter"),
    ("alt+backspace", "M-BSpace"),
    ("ctrl+delete", "C-DC"),
    ("ctrl+shift+delete", "C-S-DC"),  # shift on a key tmux parameterises: still a name
    ("ctrl+f5", "C-F5"),
    ("shift+f1", "S-F1"),
    ("alt+f3", "M-F3"),
    ("alt+ctrl+x", "C-M-x"),  # Textual sorts modifiers alphabetically
    ("ctrl+alt+x", "C-M-x"),  # a binding written the other way round
    ("alt+ctrl+shift+up", "C-M-S-Up"),
    ("ctrl+space", "C-Space"),
    ("alt+space", "M-Space"),
    ("ctrl+@", "C-@"),
    ("ctrl+at", "C-@"),
    ("ctrl+left_square_bracket", "C-["),
    ("ctrl+backslash", "C-\\"),
    ("ctrl+right_square_bracket", "C-]"),
    ("ctrl+circumflex_accent", "C-^"),
    ("ctrl+underscore", "C-_"),
    ("ctrl+question_mark", "C-?"),
    ("ctrl+slash", "C-/"),
    ("ctrl+minus", "C--"),
    ("alt+minus", "M--"),
    ("alt+left_square_bracket", "M-["),
    ("alt+comma", "M-,"),
    ("alt+1", "M-1"),
    ("alt+shift+a", "M-A"),  # tmux lowercases S-a; ESC A is the shifted letter
    ("alt+shift+tab", "M-BTab"),
]


@pytest.mark.parametrize(("textual", "tmux"), TABLE)
def test_named_keys_take_tmux_names(textual: str, tmux: str) -> None:
    assert translate(textual, None, printable=False) == key(tmux)


DROPPED: list[str] = [
    "print_screen",
    "menu",
    "caps_lock",
    "super+x",  # tmux has no super/hyper spelling
    "hyper+x",
    "f13",  # tmux knows F1-F12 only; F13 would be typed as three letters
    "f24",
    "ctrl+f13",
    "ctrl+backspace",  # tmux types or swallows "C-BSpace"; the user gets a notice
    "ctrl+escape",
    "ctrl+shift+backspace",
    "alt+ctrl+escape",
    "ctrl+comma",  # tmux sends a bare "," — the modifier lost, the agent misled
    "ctrl+full_stop",
    "ctrl+equals_sign",
    "ctrl+1",  # tmux sends "1"
    "ctrl+shift+2",
    "shift+1",  # "!" on one layout, "+" on another: unknowable without the character
    "shift+minus",
    "alt+semicolon",  # an argument ending in ";" is tmux's command separator
    "ctrl+semicolon",
    "return",
    "<any>",
    "",
    "+",
    "ctrl+",
]


@pytest.mark.parametrize("textual", DROPPED)
def test_keys_tmux_would_mistype_are_dropped(textual: str) -> None:
    assert translate(textual, None, printable=False) is None


# --- the chords tmux can name but cannot send -------------------------------------------

#: Textual chord → the exact bytes the fleet sends for it. Hand-written from the
#: measurement in ``core.keys``: each of these reaches a tmux 3.4 pane as its own
#: NAME in letters (``S-Enter``, seven characters into the agent's prompt) or as
#: nothing at all, and each is what the same key delivers once that pane has
#: extended keys on. Every codepoint and every modifier arithmetic is here:
#: 1 + 1·shift + 2·alt + 4·ctrl, and tmux's folds (ctrl+i IS Tab, ctrl+Space is
#: ctrl+``@`` = 64, ctrl+shift+a is the UPPERCASE A = 65).
EXTENDED_CHORDS: list[tuple[str, str]] = [
    ("shift+enter", "\x1b[13;2u"),  # the key Claude Code takes a newline from
    ("alt+shift+enter", "\x1b[13;4u"),
    ("ctrl+shift+enter", "\x1b[13;6u"),
    ("alt+ctrl+enter", "\x1b[13;7u"),
    ("alt+ctrl+shift+enter", "\x1b[13;8u"),
    ("ctrl+enter", "\x1b[13;5u"),  # tmux sends NOTHING for C-Enter
    ("shift+escape", "\x1b[27;2u"),
    ("alt+shift+escape", "\x1b[27;4u"),
    ("shift+backspace", "\x1b[127;2u"),  # tmux's BSpace is DEL, not BS
    ("alt+shift+backspace", "\x1b[127;4u"),
    ("alt+shift+space", "\x1b[32;4u"),
    ("ctrl+shift+space", "\x1b[64;6u"),  # ctrl+Space is ctrl+@ to tmux
    ("alt+ctrl+shift+space", "\x1b[64;8u"),
    ("ctrl+tab", "\x1b[9;5u"),  # tmux sends a bare TAB: ctrl lost
    ("ctrl+shift+tab", "\x1b[9;6u"),  # tmux sends NOTHING for C-BTab
    ("alt+ctrl+tab", "\x1b[9;7u"),
    ("ctrl+shift+a", "\x1b[65;6u"),
    ("ctrl+shift+z", "\x1b[90;6u"),
    ("alt+ctrl+shift+a", "\x1b[65;8u"),
    ("ctrl+shift+i", "\x1b[9;2u"),  # ctrl+i IS Tab; the ctrl is spent saying so
    ("ctrl+shift+m", "\x1b[13;2u"),  # and ctrl+m Enter
    ("ctrl+shift+left_square_bracket", "\x1b[27;2u"),  # and ctrl+[ Escape
    ("ctrl+shift+question_mark", "\x1b[127;2u"),  # and ctrl+? BSpace
    ("ctrl+shift+at", "\x1b[64;6u"),  # ctrl+@ is NUL: no fold to a named key
    ("ctrl+shift+backslash", "\x1b[92;6u"),
    ("alt+shift+comma", "\x1b[44;4u"),
    ("alt+shift+slash", "\x1b[47;4u"),
    ("alt+ctrl+minus", "\x1b[45;7u"),  # ctrl+- is US, and tmux has no ESC left
    ("alt+ctrl+slash", "\x1b[47;7u"),
]


@pytest.mark.parametrize(("textual", "sequence"), EXTENDED_CHORDS)
def test_chords_tmux_cannot_send_become_the_bytes_they_mean(textual: str, sequence: str) -> None:
    assert translate(textual, None, printable=False) == literal(sequence)


#: Fifteen of the names tmux 3.4 types out as text, and the bytes
#: :func:`extended_bytes` gives each one. Fifteen, not all of them: THE
#: MEASUREMENT in ``core.keys`` counts 116 names typed out on 3.4, and every one
#: of those is cross-checked against the binary by :data:`CROSS_CHECKED` below.
#: These fifteen are written out BY HAND — one per family, plus both ends of the
#: letters — so that the module's answer for each family is pinned in this file
#: and not only harvested from the module that produced it.
#:
#: Two of them (``S-Space`` and ``S-Tab``) :func:`translate` never builds — its
#: shift+space is a space and its shift+tab is ``BTab`` — and they are here
#: because the encoding must be right for the name, not only for the chords that
#: happen to reach it today. ``M-S-Tab`` looks like a third and is not:
#: ``alt+shift+tab`` is ``M-BTab``, but ``alt+meta+shift+tab`` reaches the
#: ordinary path and builds it.
TYPED_LITERALLY_BY_TMUX: dict[str, str] = {
    "C-M-Enter": "\x1b[13;7u",
    "C-S-Enter": "\x1b[13;6u",
    "C-S-Space": "\x1b[64;6u",
    "C-S-a": "\x1b[65;6u",
    "C-S-z": "\x1b[90;6u",
    "M-S-BSpace": "\x1b[127;4u",
    "M-S-Enter": "\x1b[13;4u",
    "M-S-Escape": "\x1b[27;4u",
    "M-S-Space": "\x1b[32;4u",
    "M-S-Tab": "\x1b[9;4u",
    "S-BSpace": "\x1b[127;2u",
    "S-Enter": "\x1b[13;2u",
    "S-Escape": "\x1b[27;2u",
    "S-Space": "\x1b[32;2u",
    "S-Tab": "\x1b[9;2u",
}


@pytest.mark.parametrize(("name", "sequence"), sorted(TYPED_LITERALLY_BY_TMUX.items()))
def test_every_name_tmux_types_out_has_bytes_of_its_own(name: str, sequence: str) -> None:
    assert extended_bytes(name) == sequence


@pytest.mark.parametrize(
    "name",
    [
        "Enter",  # no modifier at all: the key's own byte
        "M-Enter",  # ESC and the byte
        "M-Escape",
        "M-BSpace",
        "M-Space",
        "C-Space",  # NUL
        "Tab",
        "BTab",  # the shifted Tab has a sequence of its own
        "M-BTab",
        "S-Up",  # tmux parameterises the arrows: ESC [ 1 ; 2 A
        "C-S-Up",
        "C-M-S-Up",
        "S-DC",
        "C-S-DC",
        "S-F1",
        "C-F12",
        "C-a",  # the C0 fold
        "C-M-a",
        "M-x",
        "M-A",
        "C-@",
        "C-?",
        "C--",  # ctrl+- is US — but only with no alt (see EXTENDED_CHORDS)
        "C-/",
        "M-1",
    ],
)
def test_extended_bytes_leaves_alone_every_name_tmux_can_send(name: str) -> None:
    assert extended_bytes(name) is None


def test_the_modifier_parameter_is_one_plus_the_bits_that_are_held() -> None:
    """CSI-u's second parameter, spelled out on one key so the arithmetic is visible."""
    assert (SHIFT_BIT, ALT_BIT, CTRL_BIT) == (1, 2, 4)
    assert extended_bytes("S-Enter") == f"\x1b[13;{1 + SHIFT_BIT}u"
    assert extended_bytes("M-S-Enter") == f"\x1b[13;{1 + SHIFT_BIT + ALT_BIT}u"
    assert extended_bytes("C-Enter") == f"\x1b[13;{1 + CTRL_BIT}u"
    assert extended_bytes("C-S-Enter") == f"\x1b[13;{1 + SHIFT_BIT + CTRL_BIT}u"
    assert extended_bytes("C-M-Enter") == f"\x1b[13;{1 + ALT_BIT + CTRL_BIT}u"
    assert extended_bytes("C-M-S-Enter") == f"\x1b[13;{1 + SHIFT_BIT + ALT_BIT + CTRL_BIT}u"


def test_the_bytes_travel_as_literal_text_not_as_a_key_name() -> None:
    """``send-keys -l --`` is the transport: a name argument would be typed out."""
    translation = translate("shift+enter", None, printable=False)
    assert translation is not None
    assert translation.kind == "literal"
    assert translation.argv == ["-l", "--", "\x1b[13;2u"]


def test_printable_input_is_always_literal_even_with_modifiers() -> None:
    assert translate("a", "a", printable=True) == literal("a")
    assert translate("left_square_bracket", "[", printable=True) == literal("[")
    assert translate("é", "é", printable=True) == literal("é")
    assert translate("A", "A", printable=True) == literal("A")
    # A terminal that reports the character alongside the chord: the text wins.
    assert translate("shift+a", "A", printable=True) == literal("A")
    assert translate("ctrl+a", "a", printable=True) == literal("a")
    # Semicolon is text like any other here; escaping is the transport's job.
    assert translate("semicolon", ";", printable=True) == literal(";")


def test_non_printable_control_pictures_never_become_literal() -> None:
    # A key WITH a character that is not printable is a control key, not text.
    assert translate("ctrl+c", "\x03", printable=False) == key("C-c")
    assert translate("enter", "\r", printable=False) == key("Enter")
    assert translate("tab", "\t", printable=False) == key("Tab")
    assert translate("backspace", "\x7f", printable=False) == key("BSpace")


def test_space_is_literal_text_unless_ctrl_or_alt_is_held() -> None:
    assert translate("space", " ", printable=True) == literal(" ")
    assert translate("space", None, printable=False) == literal(" ")
    assert translate("shift+space", None, printable=False) == literal(" ")
    assert translate("ctrl+space", None, printable=False) == key("C-Space")
    assert translate("alt+space", None, printable=False) == key("M-Space")


def test_shifted_letters_are_uppercase_text_because_tmux_lowercases_s_a() -> None:
    assert translate("shift+a", None, printable=False) == literal("A")
    assert translate("shift+z", None, printable=False) == literal("Z")
    # The negative: an unshifted letter without a character stays lowercase text.
    assert translate("a", None, printable=False) == literal("a")


def test_ctrl_punctuation_is_the_c0_set_and_nothing_else() -> None:
    for char in CTRL_PUNCTUATION:
        name = next(k for k, v in PUNCTUATION.items() if v == char)
        assert translate(f"ctrl+{name}", None, printable=False) == key(f"C-{char}")
    for char in ",.='`~!$%&*(){}<>|:":
        name = next(k for k, v in PUNCTUATION.items() if v == char)
        assert translate(f"ctrl+{name}", None, printable=False) is None, char


def test_function_keys_stop_at_twelve() -> None:
    for number in range(1, MAX_FUNCTION_KEY + 1):
        assert translate(f"f{number}", None, printable=False) == key(f"F{number}")
    assert translate(f"f{MAX_FUNCTION_KEY + 1}", None, printable=False) is None
    assert translate("f0", None, printable=False) is None


def test_translation_argv_shapes() -> None:
    assert literal("-x").argv == ["-l", "--", "-x"]
    assert key("C-c").argv == ["C-c"]


# --- the whole vocabulary --------------------------------------------------------------

#: tmux's key-name grammar as this module may use it: optional modifiers, then a
#: named key, a function key, or one character that is neither blank nor ``;``.
TMUX_NAME = re.compile(
    r"^(C-)?(M-)?(S-)?"
    r"(Enter|Escape|Tab|BTab|BSpace|DC|IC|Up|Down|Left|Right|Home|End|PPage|NPage|Space"
    r"|F([1-9]|1[0-2])|[^\s;])$"
)

#: The other shape a translation may take: CSI-u, as :func:`extended_bytes`
#: builds it. The modifier is at least 2 — a sequence with no modifier at all
#: would be a key that never needed one.
CSI_U = re.compile(r"^\x1b\[([1-9]\d*);([2-8])u$")

#: Textual key names that translate to nothing, by design. Everything else the
#: ``Keys`` enum can produce must translate to a name TMUX_NAME accepts, or to
#: text (a character, or the CSI-u bytes of a chord tmux cannot send).
DELIBERATELY_DROPPED = {
    "<any>",
    "<ignore>",
    "<scroll-down>",
    "<scroll-up>",
    "ctrl-at",  # a legacy spelling with a hyphen; events carry "ctrl+@"
    "return",
    *(f"ctrl+{digit}" for digit in range(10)),
    *(f"ctrl+shift+{digit}" for digit in range(10)),
    *(f"f{number}" for number in range(MAX_FUNCTION_KEY + 1, 25)),
    *(f"ctrl+f{number}" for number in range(MAX_FUNCTION_KEY + 1, 25)),
}


def _chords() -> list[str]:
    """Every chord Textual can hand the pane: its own key names, and modified ones.

    Textual spells a chord with its modifiers sorted alphabetically, so the
    combinations here are generated the same way. ``meta`` is Textual's other
    name for ``alt``; both are swept because a binding may be written either way.
    """
    bases = {
        *(member.value for member in Keys),
        *SPECIAL,
        *PUNCTUATION,
        *string.ascii_lowercase,
        *string.digits,
        *(f"f{number}" for number in range(1, 25)),
    }
    bases = {base for base in bases if "+" not in base}
    chords = set(bases)
    for size in (1, 2, 3):
        for combination in itertools.combinations(("alt", "ctrl", "meta", "shift"), size):
            chords.update("+".join((*combination, base)) for base in bases)
    return sorted(chords)


def _translations() -> dict[str, Translation]:
    """Every chord that translates to something, and what it translates to."""
    return {
        chord: translation
        for chord in _chords()
        if (translation := translate(chord, None, printable=False)) is not None
    }


def _spelled_names() -> dict[str, str]:
    """Every tmux key NAME :func:`translate` sends as BYTES, and the bytes it sends.

    A translation only carries its result, so a chord that leaves as
    ``ESC [ 13 ; 2 u`` no longer says which tmux name it was: the name is what
    ``send-keys`` takes, and the name is therefore what the cross-check against a
    real tmux has to send. So the sweep is run with the module's one
    name-to-translation step watched, and the names are taken from there.

    Harvested rather than re-derived from a grammar written out in this file,
    because a grammar here would be a second copy of ``translate``'s rules that
    could quietly stop describing it — and the whole complaint this test answers
    is a claim of coverage that had drifted from what was covered. Harvesting
    means a name the module starts or stops spelling shows up in
    :data:`CROSS_CHECKED` by itself.
    ``test_the_vocabulary_is_the_one_the_module_docstring_counts`` is the guard
    that the harvest and the public sweep still agree.
    """
    harvested: dict[str, str] = {}
    original = keys_module._named

    def watched(name: str) -> Translation:
        translation = original(name)
        if translation.kind == "literal":
            harvested[name] = translation.value
        return translation

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(keys_module, "_named", watched)
        _translations()
    return harvested


def test_every_textual_key_name_translates_or_is_deliberately_dropped() -> None:
    names = sorted({member.value for member in Keys})
    assert len(names) > 100  # the sweep must still see the enum
    translated: dict[str, Translation | None] = {
        name: translate(name, None, printable=False) for name in names
    }
    unexpected_holes = [n for n, t in translated.items() if t is None]
    assert set(unexpected_holes) == DELIBERATELY_DROPPED & set(names) or not (
        set(unexpected_holes) - DELIBERATELY_DROPPED
    ), sorted(set(unexpected_holes) - DELIBERATELY_DROPPED)
    for name, translation in translated.items():
        if translation is not None and translation.kind == "key":
            assert TMUX_NAME.match(translation.value), (name, translation.value)


def test_every_chord_becomes_a_tmux_name_a_character_or_a_csi_u_sequence() -> None:
    """The whole sweep, not only the enum: nothing else may reach ``send-keys``."""
    for chord, translation in _translations().items():
        if translation.kind == "key":
            assert TMUX_NAME.match(translation.value), (chord, translation.value)
        elif translation.value.startswith("\x1b"):
            assert CSI_U.match(translation.value), (chord, translation.value)
        else:
            assert "\x1b" not in translation.value, (chord, translation.value)


def test_the_tmux_grammar_control_rejects_what_tmux_would_mistype() -> None:
    """The guard above is only as good as its grammar: it must refuse these."""
    for bad in ("F13", "Bogus", "C-Bogus", "M-;", ";", "", "C- ", "Enter;"):
        assert TMUX_NAME.match(bad) is None, bad
    for good in ("Enter", "C-S-Up", "M-x", "C-@", "F12", "BTab", "C-BTab", "M-A"):
        assert TMUX_NAME.match(good), good
    for bad in ("\x1b[13;1u", "\x1b[13;2", "\x1b[0;2u", "\x1b[13;9u", "\x1b[13;2A"):
        assert CSI_U.match(bad) is None, bad


def test_the_vocabulary_is_the_one_the_module_docstring_counts() -> None:
    """THE MEASUREMENT's vocabulary, re-counted here so the prose cannot drift off it.

    ``core.keys`` states one measurement and every other count in the module
    quotes it. These are its vocabulary numbers. They are pinned rather than
    recomputed-and-trusted because a count nothing checks is exactly how three
    numbers in that file — 122 chords, 119 of 122, 15 names — came to describe
    three different vocabularies and none of them this one.

    A textual release may legitimately move them: :func:`_chords` is built over
    ``Keys``, and a key added there is a chord more to sweep. When that happens
    the answer is to re-measure against a real tmux and rewrite THE MEASUREMENT
    — not to edit these numbers until the test is quiet again.
    """
    assert (len(_chords()), len(_translations())) == (1785, 1179)
    assert len(set(EMITTED) | set(SPELLED)) == 471
    assert (len(EMITTED), len(SPELLED), len(EMITTED_SEQUENCES)) == (350, 121, 112)
    # The harvest is only worth its name if it saw the same keys the public
    # sweep did: same sequences, and no name counted as both.
    assert set(SPELLED.values()) == set(EMITTED_SEQUENCES)
    assert not set(EMITTED) & set(SPELLED)


def test_the_three_ctrl_chords_that_share_one_byte_are_recorded_not_closed() -> None:
    """``ctrl+-``, ``ctrl+/`` and ``ctrl+_`` are one byte to the agent, on purpose.

    A measured collision that this module chooses to keep, so it is pinned here:
    all three keep tmux NAMES and none of them is spelled in bytes.
    :data:`aisquare.core.keys.CTRL_US_ALIASES` carries the measurement and the
    reason (in short: ``0x1f`` IS this ctrl, unlike ``C-Tab``'s bare TAB, and
    tmux keeps ``0x1f`` for ``C-_`` even where it can spell CSI-u). Closing it
    later has to be a decision somebody takes against this test, not a silent
    drift into it.
    """
    assert frozenset("-/_") == CTRL_US_ALIASES
    assert CTRL_US_ALIASES < CTRL_PUNCTUATION  # it is part of the ctrl set, not beside it
    for textual, name in (("minus", "C--"), ("slash", "C-/"), ("underscore", "C-_")):
        assert translate(f"ctrl+{textual}", None, printable=False) == key(name)
        assert extended_bytes(name) is None, "a spelled chord would not collide"


def test_no_emitted_name_ends_in_the_argv_separator() -> None:
    """A trailing ``;`` ends the tmux command: nothing here may produce one."""
    for translation in _translations().values():
        if translation.kind == "key":
            assert not translation.value.endswith(ARGV_SEPARATOR), translation.value
    # The negative half: the rule is reachable — the one chord that would end in
    # ";" is refused, not emitted.
    assert translate("alt+semicolon", None, printable=False) is None


def test_the_back_tab_chords_are_the_two_tmux_actually_delivers() -> None:
    """``ctrl+shift+tab`` left this table because ``C-BTab`` reaches nobody."""
    assert CHORDS == {"shift+tab": "BTab", "alt+shift+tab": "M-BTab"}
    assert translate("shift+tab", None, printable=False) == key("BTab")
    assert translate("alt+shift+tab", None, printable=False) == key("M-BTab")
    assert translate("ctrl+shift+tab", None, printable=False) == literal("\x1b[9;6u")


def test_no_ctrl_set_is_consulted() -> None:
    assert {"Escape", "BSpace"} == NO_CTRL
    assert translate("ctrl+backspace", None, printable=False) is None
    assert translate("alt+backspace", None, printable=False) == key("M-BSpace")
    # Shift on BSpace is not refused — it is spelled out in bytes instead.
    assert translate("shift+backspace", None, printable=False) == literal("\x1b[127;2u")


def test_the_keys_tmux_parameterises_keep_their_names_through_every_modifier() -> None:
    """Arrows, editing keys, back-tab and F-keys: the CSI sequence carries the lot."""
    for name in CSI_PARAMETERISED | {"F1", "F12"}:
        for prefix in ("", "C-", "M-", "S-", "C-M-", "C-S-", "M-S-", "C-M-S-"):
            assert extended_bytes(prefix + name) is None, prefix + name


# --- against a real tmux --------------------------------------------------------------

#: Every named key :func:`translate` can emit, harvested from the sweep — so the
#: probe below covers exactly what the fleet can send, not a sample of it.
EMITTED: list[str] = sorted({t.value for t in _translations().values() if t.kind == "key"})

#: And every byte sequence it can emit.
EMITTED_SEQUENCES: list[str] = sorted(
    {
        t.value
        for t in _translations().values()
        if t.kind == "literal" and t.value.startswith("\x1b")
    }
)

#: Every tmux NAME the fleet spells itself, and the bytes it spells it with —
#: the 121 names of THE MEASUREMENT, harvested from ``translate`` so this cannot
#: be a sample pretending to be the set.
SPELLED: dict[str, str] = _spelled_names()

#: Names tmux's own key parser refuses (``bind-key Bogus`` → ``unknown key``),
#: which is what makes ``send-keys`` type them out. They are the control: if the
#: read-back cannot see THESE spelled out, it proves nothing about the others.
UNKNOWN_TO_TMUX: tuple[str, ...] = ("Bogus", "C-Bogus", "F13")

#: What tmux 3.4 itself delivers for the ctrl-on-Tab names when the pane HAS
#: extended keys on. It agrees with :func:`extended_bytes` on 118 of the 121
#: names the fleet spells; these three are the whole of the difference (``C-Tab``
#: is here for contrast — on that one tmux 3.4 agrees, and it is checked like the
#: rest). For the other three 3.4 keeps a legacy form, and a legacy form has
#: nowhere to put the ctrl, so the agent gets a plain Tab or a plain back-tab and
#: cannot tell the chords apart. Ours can. Measured 2026-08-29 against builds of
#: 3.2, 3.2a, 3.3a and 3.4: tmux 3.3a spells all three the way we do
#: (``C-S-Tab`` → ``ESC [ 9 ; 6 u``), so this table is 3.4 losing them.
TMUX_EXTENDED_FORM: dict[str, str] = {
    "C-Tab": "\x1b[9;5u",
    "C-M-Tab": "\x1b\t",
    "C-S-Tab": "\x1b[1;5Z",
    "C-M-S-Tab": "\x1b\x1b[Z",
}

#: Every name whose bytes are compared with tmux's own: all 121 the fleet spells
#: (:data:`SPELLED`), plus the two :data:`TYPED_LITERALLY_BY_TMUX` carries that
#: :func:`translate` never builds. Not a sample — the earlier version of this
#: list held 31 names while ``core.keys`` claimed all 122 were verified, which is
#: the claim this file now has to earn.
CROSS_CHECKED: list[str] = sorted(set(SPELLED) | set(TYPED_LITERALLY_BY_TMUX))


def test_the_divergence_table_is_exactly_where_tmux_and_this_module_differ() -> None:
    """:data:`TMUX_EXTENDED_FORM` must not quietly grow into a list of excuses.

    Three of its four rows are a real divergence: tmux 3.4's legacy form, which
    has lost the ctrl. The fourth, ``C-Tab``, is tmux AGREEING with us and is
    kept for the contrast. So the shape is pinned — exactly one row may equal
    what the fleet sends — and with it the count ``core.keys`` states, 118 of the
    121 spelled by tmux itself. A fourth divergence has to be added here on
    purpose, with a measurement, rather than by relaxing the test that found it.
    """
    agreeing = {name for name, form in TMUX_EXTENDED_FORM.items() if form == extended_bytes(name)}
    assert agreeing == {"C-Tab"}
    assert set(TMUX_EXTENDED_FORM) - agreeing == {"C-M-Tab", "C-S-Tab", "C-M-S-Tab"}
    assert len(SPELLED) - len(set(TMUX_EXTENDED_FORM) - agreeing) == 118


_needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")


def _spelled(name: str) -> str:
    """The bytes the fleet sends for ``name``, which must be a chord it spells itself."""
    sequence = extended_bytes(name)
    assert sequence is not None, f"{name} is a name the fleet sends as a name"
    return sequence


@pytest.fixture
def real_server(tmp_path: Path) -> Iterator[TmuxServer]:
    conf = tmp_path / "tmux.conf"
    conf.write_text(BUNDLED_CONF, encoding="utf-8")
    server = TmuxServer(f"asq-test-{os.getpid()}-keys", conf=conf)
    try:
        yield server
    finally:
        with contextlib.suppress(TmuxError):
            server.run("kill-server")


def _probe(
    server: TmuxServer,
    tmp_path: Path,
    items: Sequence[tuple[str, Sequence[str]]],
    *,
    extended: bool = False,
) -> dict[str, str]:
    """Run each ``send-keys`` in a raw pane and return the bytes each one delivered.

    The pane is ``stty raw -echo`` and a ``cat`` writing to a file, so what comes
    back is the byte stream the agent would have read — no echo, no line
    discipline, no screen. Each item is followed by a marker (``SOH<label>STX``,
    two control bytes no key here sends) so the stream splits back into one field
    per item, and a missing marker fails the probe rather than shifting a field.

    Nothing is sent until the pane says READY twice over, because a key that
    lands early lands in a DIFFERENT terminal: before ``stty`` runs the line is
    cooked and echoing, and ``C-c`` is a signal rather than a byte. The screen
    carrying READY means tmux has parsed the pane's output that far — the
    ``extended`` request included — and the log file existing means the shell has
    reached ``cat``, which it only does once ``stty`` has returned.

    The log path is an ARGUMENT to ``sh``, read back as ``"$1"``, and never
    interpolated into the script text. Interpolated, a ``tmp_path`` under a root
    with a space in it (``TMPDIR="/tmp/my tmp"`` reproduces it) split the
    redirection, killed the pane, and cost three tests a 20-second deadline each
    before they failed for a reason that named none of this.

    What comes back is read as BYTES and decoded latin-1, never
    :meth:`~pathlib.Path.read_text`: text mode applies universal newlines, so a
    CR from ``Enter`` would be handed back as the LF that ``C-j`` sends, and
    telling those two apart is the entire point of reading a file instead of a
    screen.

    ``extended`` has the pane ask for extended keys with xterm's
    ``CSI > 4 ; 2 m``, the only such request tmux 3.4 understands, and the state
    in which it will encode the chords it otherwise types out. Commands are
    chained (``;``) in batches, one fork per batch instead of one per key: there
    are some 700 of them, and a fork each would cost more than the rest of this
    file put together.

    The markers are why an ``extended`` probe is a tmux 3.4 measurement and not
    a portable one: measured on a 3.5a build, a pane with extended keys on gets
    only four control bytes through ``send-keys -l`` — TAB, CR, ESC and DEL —
    so ``SOH``/``STX`` are dropped there and the probe fails on a missing marker
    rather than on a key. On 3.4 all 32 arrive, in either pane mode.
    """
    log = tmp_path / f"probe-{'extended' if extended else 'plain'}.bin"
    request = 'printf "\\033[>4;2m"; ' if extended else ""
    window = server.spawn_window(
        f"keys-{'extended' if extended else 'plain'}",
        name="probe",
        cwd=tmp_path,
        command=[
            "sh",
            "-c",
            f'stty raw -echo; {request}printf READY; cat > "$1"',
            "sh",
            str(log),
        ],
        width=200,
        height=50,
    )
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if "READY" in server.run("capture-pane", "-p", "-t", window.pane_id) and log.exists():
            break
        time.sleep(0.02)
    else:
        screen = server.run("capture-pane", "-p", "-t", window.pane_id)
        pytest.fail(f"the probe pane never reached its raw-mode cat: {screen!r}")
    for start in range(0, len(items), 40):
        chained: list[str] = []
        for label, argv in items[start : start + 40]:
            if chained:
                chained.append(ARGV_SEPARATOR)
            chained += ["send-keys", "-t", window.pane_id, *argv, ARGV_SEPARATOR]
            chained += ["send-keys", "-t", window.pane_id, "-l", "--", f"\x01{label}\x02"]
        server.run(*chained)
    last = f"\x01{items[-1][0]}\x02"
    deadline = time.monotonic() + 20.0
    stream = ""
    while time.monotonic() < deadline:
        stream = log.read_bytes().decode("latin-1")
        if last in stream:
            break
        time.sleep(0.05)
    received: dict[str, str] = {}
    for label, _ in items:
        arrived, marker, stream = stream.partition(f"\x01{label}\x02")
        assert marker, f"the marker for {label!r} never arrived; the probe saw {stream[-200:]!r}"
        received[label] = arrived
    return received


@_needs_tmux
def test_real_tmux_types_none_of_our_names_literally(
    real_server: TmuxServer, tmp_path: Path
) -> None:
    """Every name the fleet can send, into a raw pane: bytes must come back, not letters.

    Two ways for a keystroke to be wrong, and on tmux 3.4 both happen, so both
    are checked: a name tmux cannot encode is TYPED OUT (``send-keys S-Enter``
    put those seven characters in the agent's prompt) and one it can neither
    encode nor type VANISHES (``C-Enter``, ``C-BTab``). The controls at the end
    are names tmux's own parser rejects, which is what makes ``send-keys`` type
    a name at all: if they do not come back spelled out, this probe cannot see a
    mistyped key and the run above proves nothing.
    """
    items = [(name, [name]) for name in [*EMITTED, *UNKNOWN_TO_TMUX]]
    received = _probe(real_server, tmp_path, items)
    for name in EMITTED:
        assert received[name] != name, f"tmux typed {name!r} out as text"
        assert received[name] != "", f"tmux swallowed {name!r}: the keystroke is lost"
    for control in UNKNOWN_TO_TMUX:
        assert received[control] == control, f"the probe cannot see {control!r} typed out"


@_needs_tmux
def test_every_name_the_fleet_spells_is_one_this_tmux_gets_wrong(
    real_server: TmuxServer, tmp_path: Path
) -> None:
    """The other half of THE MEASUREMENT: these 121 names have to be spelled.

    The test above proves the 350 names travel fine. This one proves the
    remainder is a remainder — each of the 121 sent BY NAME into a legacy pane,
    none of them arriving as the bytes the fleet sends instead. Without it,
    ``core.keys`` claims a hole nothing looks in, and the module could keep
    hand-encoding chords tmux had since learned to deliver.

    The three ways of being wrong are pinned separately because they are three
    different things happening to the agent: 116 names arrive as their own
    letters, ``C-Enter`` arrives as nothing at all, and four arrive as ANOTHER
    key's bytes — which is the worst of the three, because it is indistinguishable
    from a key the user could have pressed on purpose.
    """
    names = sorted(SPELLED)
    received = _probe(real_server, tmp_path, [(name, [name]) for name in names])
    typed = {name for name in names if received[name] == name}
    vanished = {name for name in names if received[name] == ""}
    another_key = {name: received[name] for name in names if name not in typed | vanished}
    assert (len(typed), vanished) == (116, {"C-Enter"}), (
        f"against tmux {real_server.version()}; the expectations here are tmux 3.4's"
    )
    assert another_key == {
        "C-Tab": "\t",  # a plain Tab: the ctrl gone
        "C-S-Tab": "\x1b[Z",  # a plain back-tab
        "C-M-Tab": "\x1b\t",
        "C-M-S-Tab": "\x1b\x1b[Z",
    }
    assert not [name for name in names if received[name] == SPELLED[name]]


@_needs_tmux
def test_real_tmux_delivers_our_sequences_byte_for_byte(
    real_server: TmuxServer, tmp_path: Path
) -> None:
    """``send-keys -l --`` is transparent: ESC and all, the bytes arrive as written."""
    items = [(sequence, ["-l", "--", sequence]) for sequence in EMITTED_SEQUENCES]
    received = _probe(real_server, tmp_path, items)
    assert received == {sequence: sequence for sequence in EMITTED_SEQUENCES}


@_needs_tmux
def test_our_sequences_are_the_ones_this_tmux_sends_when_it_can(
    real_server: TmuxServer, tmp_path: Path
) -> None:
    """The arithmetic, checked against the binary instead of against a memory of it.

    A pane that has asked for extended keys gets the very chords tmux otherwise
    types out, encoded by tmux itself — so every codepoint and every modifier
    here has a second opinion, including the folds that make ``C-S-a`` an
    uppercase 65 and ``C-S-i`` a Tab. EVERY name the fleet spells goes through
    it, all 121 of them, which is what lets ``core.keys`` say its sequences are
    checked against the running binary; the version of this test that checked 31
    of them could not. Where tmux keeps a legacy form instead
    (:data:`TMUX_EXTENDED_FORM`) the expectation says so exactly: a new
    divergence is a change in tmux worth reading about, not a line to relax.

    The expectations are one tmux's. That is not a hedge, it is the finding:
    measured 2026-08-29 over these same names, tmux 3.3a produces our bytes for
    all 121, tmux 3.4 for 118, tmux 3.2a for 61 and tmux 3.2 for 58 — 3.2 and
    3.2a spell ctrl+alt+shift chords off the C0 byte (``C-M-S-a`` is
    ``ESC [ 1 ; 4 u`` there, not ``ESC [ 65 ; 8 u``). tmux 3.5a cannot be asked
    at all through this probe: with extended keys on it drops all but four
    control bytes out of ``send-keys -l``, markers included, and it defaults
    ``extended-keys-format`` to ``xterm`` (``S-Enter`` comes back as
    ``ESC [ 27 ; 2 ; 13 ~``). So a failure here on another build is a report
    about that build, and the version is in the message to say which.
    """
    items = [(name, [name]) for name in [*CROSS_CHECKED, "M-Tab", "M-BTab"]]
    received = _probe(real_server, tmp_path, items, extended=True)
    assert {name: received[name] for name in CROSS_CHECKED} == {
        name: TMUX_EXTENDED_FORM.get(name, _spelled(name)) for name in CROSS_CHECKED
    }, f"against tmux {real_server.version()}; the expectations above are tmux 3.4's"
    # What tmux's legacy forms cost, since it is the reason we do not use them:
    # they are the chord with the ctrl missing, byte for byte.
    assert received["C-M-Tab"] == received["M-Tab"] != _spelled("C-M-Tab")
    assert received["C-M-S-Tab"] == received["M-BTab"] != _spelled("C-M-S-Tab")


@_needs_tmux
def test_the_ctrl_us_aliases_really_do_collide_on_this_tmux(
    real_server: TmuxServer, tmp_path: Path
) -> None:
    """The recorded collision, re-measured — both halves of the argument for keeping it.

    :data:`aisquare.core.keys.CTRL_US_ALIASES` says ctrl on ``-``, ``/`` and
    ``_`` reach a pane as one byte, and that tmux keeps that byte for ``C-_``
    even in a pane where it CAN spell CSI-u — which is why spelling the other two
    would buy two thirds of a distinction rather than close it. Neither half is
    remembered here: the legacy pane and the extended pane are both measured,
    because the second one is the whole reason the first is tolerated.
    """
    names = ["C--", "C-/", "C-_"]
    items = [(name, [name]) for name in names]
    legacy = _probe(real_server, tmp_path, items)
    assert legacy == dict.fromkeys(names, "\x1f"), "the collision this module records"
    extended = _probe(real_server, tmp_path, items, extended=True)
    assert extended == {"C--": "\x1b[45;5u", "C-/": "\x1b[47;5u", "C-_": "\x1f"}, (
        f"against tmux {real_server.version()}; the expectations above are tmux 3.4's"
    )
