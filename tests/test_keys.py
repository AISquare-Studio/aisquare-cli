"""``core.keys``: every Textual key the pane forwards arrives in tmux's vocabulary.

Three kinds of test. The table tests pin each row of docs/plans/fleet-tui.md §6
and the deliberate holes (a ``None`` for every key tmux would MISTYPE — it
sends an unknown name as literal text). The parser tests feed the BYTES a
terminal sends into Textual's own ``XTermParser`` and translate what comes out,
because the table's alt exception was once written against hand-built events
the parser never produces and promised chords no terminal could reach (review
of #135). The real-tmux test (skipped without a ``tmux`` on PATH) sends every
name this module can emit into a raw-mode ``cat -v`` pane and reads back what
arrived: no name may come back spelled out, and ``Bogus`` must — the control
that proves the read-back can see a mistyped name at all.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import re
import shutil
import string
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from textual import _parser, events
from textual._xterm_parser import XTermParser

from aisquare.core.keys import (
    CHARACTERLESS_KEYS,
    CHORDS,
    CTRL_PUNCTUATION,
    ESC_INTRODUCERS,
    EXTENDED_MINIMUM,
    KEYPAD_LAYOUT_DEPENDENT,
    MAX_FUNCTION_KEY,
    MODIFIERS,
    NO_CTRL,
    PUNCTUATION,
    SPECIAL,
    UNSPELLABLE_MODIFIERS,
    Drop,
    DropReason,
    Translation,
    needs_extended_keys,
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
    ("ctrl+shift+a", "C-S-a"),
    ("shift+enter", "S-Enter"),
    ("shift+up", "S-Up"),
    ("ctrl+up", "C-Up"),
    ("alt+enter", "M-Enter"),
    ("alt+backspace", "M-BSpace"),
    ("ctrl+delete", "C-DC"),
    ("ctrl+shift+delete", "C-S-DC"),
    ("shift+escape", "S-Escape"),
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
    ("ctrl+shift+tab", "C-BTab"),
]


@pytest.mark.parametrize(("textual", "tmux"), TABLE)
def test_named_keys_take_tmux_names(textual: str, tmux: str) -> None:
    assert translate(textual, None, printable=False) == key(tmux)


#: Each with the reason the pane acts on: ``isinstance(..., Drop)`` alone
#: cannot see a name slide between one quiet line and total silence (review of
#: #161, round 4).
DROPPED: list[tuple[str, DropReason]] = [
    ("print_screen", "nothing_to_type"),
    ("menu", "nothing_to_type"),
    ("caps_lock", "nothing_to_type"),
    ("super+x", "command"),  # tmux has no super/hyper spelling
    ("hyper+x", "command"),
    ("foo+a", "no_name"),  # a modifier token this module has never met: not a command
    ("f13", "no_name"),  # tmux knows F1-F12 only; F13 would be typed as three letters
    ("f24", "no_name"),
    ("ctrl+f13", "no_name"),
    ("ctrl+backspace", "no_name"),  # tmux 3.7c types the eight characters "C-BSpace"
    ("ctrl+escape", "no_name"),
    ("ctrl+shift+backspace", "no_name"),
    ("alt+ctrl+escape", "no_name"),
    ("ctrl+comma", "no_name"),  # tmux sends a bare "," — the modifier lost, the agent misled
    ("ctrl+full_stop", "no_name"),
    ("ctrl+equals_sign", "no_name"),
    ("ctrl+1", "no_name"),  # tmux sends "1"
    ("ctrl+shift+2", "no_name"),
    ("shift+1", "no_name"),  # "!" on one layout, "+" on another: unknowable without the character
    ("shift+minus", "no_name"),
    ("alt+semicolon", "no_name"),  # an argument ending in ";" is tmux's command separator
    ("ctrl+semicolon", "no_name"),
    ("<any>", "no_name"),  # a binding wildcard, never an event: named if it ever arrives
    ("", "nothing_to_type"),  # malformed names: nothing to look up, nothing to say
    ("+", "nothing_to_type"),
    ("ctrl+", "nothing_to_type"),
]


@pytest.mark.parametrize(("textual", "reason"), DROPPED)
def test_keys_tmux_would_mistype_are_dropped(textual: str, reason: DropReason) -> None:
    assert translate(textual, None, printable=False) == Drop(reason)


# --- what the parser really delivers ---------------------------------------------------

Parse = Callable[[str], list[events.Key]]


@pytest.fixture
def parsed(monkeypatch: pytest.MonkeyPatch) -> Parse:
    """The key events Textual 8.2.8's ``XTermParser`` emits for a byte sequence.

    A lone ``ESC`` prefix is resolved by a timeout the parser reads off a clock;
    the clock is faked and jumped past ``ESCAPE_DELAY`` after the feed, so an
    ``ESC p`` resolves to its alt chord without the test waiting for it.
    """
    clock = [0.0]
    monkeypatch.setattr(_parser, "get_time", lambda: clock[0])

    def parse(sequence: str) -> list[events.Key]:
        parser = XTermParser()
        tokens = list(parser.feed(sequence))
        clock[0] += 10.0
        tokens += list(parser.tick())
        return [token for token in tokens if isinstance(token, events.Key)]

    return parse


def arrived(sequence: str, parse: Parse, *, extended: bool = True) -> list[Translation | Drop]:
    """What tmux is handed for each key the parser makes of ``sequence``."""
    return [
        translate(event.key, event.character, printable=event.is_printable, extended_keys=extended)
        for event in parse(sequence)
    ]


def test_the_parser_fixture_resolves_a_lone_escape_and_a_plain_letter(parsed: Parse) -> None:
    """The control on the fixture: if the clock did not fire, every alt test
    below would be about an empty list."""
    [esc] = parsed("\x1b")
    assert (esc.key, esc.character) == ("escape", "\x1b")
    [letter] = parsed("p")
    assert (letter.key, letter.character, letter.is_printable) == ("p", "p", True)


@pytest.mark.parametrize("letter", [c for c in string.ascii_lowercase if c not in "bf"])
def test_a_legacy_alt_letter_reaches_the_agent_as_the_chord(parsed: Parse, letter: str) -> None:
    """``ESC p`` — what xterm, VTE, Windows Terminal and tmux send for alt+p —
    is ``Key("alt+p", "p")`` to the parser: printable, and the very event the
    old "the text wins" rule typed as a bare letter."""
    [event] = parsed("\x1b" + letter)
    assert (event.key, event.character, event.is_printable) == (f"alt+{letter}", letter, True)
    assert arrived("\x1b" + letter, parsed) == [key(f"M-{letter}")]


def test_legacy_alt_b_and_alt_f_are_ctrl_arrows_to_the_parser(parsed: Parse) -> None:
    """Textual's sequence table maps ``ESC b`` / ``ESC f`` to ctrl+left /
    ctrl+right (iTerm's natural-editing keys) before this table sees them."""
    assert arrived("\x1bb", parsed) == [key("C-Left")]
    assert arrived("\x1bf", parsed) == [key("C-Right")]


def test_a_legacy_alt_shift_letter_keeps_its_case_unless_it_is_an_introducer(
    parsed: Parse,
) -> None:
    """``ESC A`` is alt+shift+a and travels as ``M-A``. ``ESC O`` and ``ESC P``
    are the SS3 and DCS introducers — a Node readline reading ``ESC O`` then
    ``A`` sees Up — so those chords are never spelled: the letter is typed."""
    assert arrived("\x1bA", parsed) == [key("M-A")]
    assert arrived("\x1bZ", parsed) == [key("M-Z")]
    assert sorted(ESC_INTRODUCERS) == ["N", "O", "P"]
    for letter in sorted(ESC_INTRODUCERS):
        [event] = parsed("\x1b" + letter)
        assert event.key == f"alt+shift+{letter.lower()}", "the premise: the parser sees a chord"
        assert arrived("\x1b" + letter, parsed) == [literal(letter)], letter


def test_legacy_alt_digits_and_alt_space_are_the_text_the_parser_makes_of_them(
    parsed: Parse,
) -> None:
    """The half of the old exception no terminal could reach: ``ESC 1`` arrives
    as ``¡`` and ``ESC SPACE`` as a plain space, with no alt token for this
    table to act on. They travel as that text, as they always did — and the
    docs may promise nothing more for a legacy terminal."""
    for digit, glyph in zip("1234567890", "¡™£¢∞§¶•ªº", strict=True):
        [event] = parsed("\x1b" + digit)
        assert "alt" not in event.key and event.character == glyph, (digit, event.key)
        assert arrived("\x1b" + digit, parsed) == [literal(glyph)]
    [space] = parsed("\x1b ")
    assert (space.key, space.character) == ("space", " ")
    assert arrived("\x1b ", parsed) == [literal(" ")]
    # ctrl+alt: the parser keeps the control character and loses the alt.
    assert arrived("\x1b\x00", parsed) == [key("C-@")], "ctrl+alt+space is a NUL"
    assert arrived("\x1b\x10", parsed) == [key("C-p")], "ctrl+alt+p is ctrl+p"


def test_kitty_alt_chords_without_text_are_chords_digits_and_space_included(
    parsed: Parse,
) -> None:
    """A kitty-protocol terminal that reports no text for an alt chord (alt held
    on Linux) delivers ``Key("alt+1", None)``: no character, so the name table
    answers — the only way ``M-1`` and ``M-Space`` are ever reached."""
    assert arrived("\x1b[112;3u", parsed) == [key("M-p")]
    assert arrived("\x1b[49;3u", parsed) == [key("M-1")]
    assert arrived("\x1b[32;3u", parsed) == [key("M-Space")]
    assert arrived("\x1b[32;7u", parsed) == [key("C-M-Space")]
    assert arrived("\x1b[32;7u", parsed, extended=False) == [Drop("too_old")], "below tmux 3.5"
    assert arrived("\x1b[49;7u", parsed) == [Drop("no_name")], "ctrl+alt+1: no name, no text"
    assert arrived("\x1b[97;4u", parsed) == [key("M-A")]
    assert arrived("\x1b[111;4u", parsed) == [Drop("no_name")], "alt+shift+o: an introducer"


def test_kitty_text_reports_drop_the_alt_token_before_this_table_sees_it(
    parsed: Parse,
) -> None:
    """Where the terminal reports the text a key produced — macOS Option makes
    alt+p a ``π`` — the parser drops the alt token and the text is typed: the
    documented limit, pinned so the docs cannot promise past it."""
    assert arrived("\x1b[112;3;112u", parsed) == [literal("p")]
    assert arrived("\x1b[112;3;960u", parsed) == [literal("π")]
    assert arrived("\x1b[49;3;49u", parsed) == [literal("1")]
    assert arrived("\x1b[32;3;32u", parsed) == [literal(" ")]


def test_a_kitty_meta_shift_letter_keeps_its_case(parsed: Parse) -> None:
    """``CSI 97;34;65u`` parses as ``Key("meta+A", "A")`` — the uppercase base
    and no shift token — and came out as a lowercase ``M-a`` (review of #135).
    The introducers are the same exception here as on a legacy terminal."""
    assert arrived("\x1b[97;34;65u", parsed) == [key("M-A")]
    assert arrived("\x1b[112;34;80u", parsed) == [literal("P")]


def test_super_and_shift_chords_through_the_parser(parsed: Parse) -> None:
    assert arrived("\x1b[99;9;99u", parsed) == [Drop("command")], "super+c: a command, dropped"
    assert arrived("\x1b[97;2;65u", parsed) == [literal("A")], "shift+a: the text"


# --- the table on its own ---------------------------------------------------------------


def test_printable_input_is_literal_under_the_modifiers_tmux_can_carry() -> None:
    assert translate("a", "a", printable=True) == literal("a")
    assert translate("left_square_bracket", "[", printable=True) == literal("[")
    assert translate("é", "é", printable=True) == literal("é")
    assert translate("A", "A", printable=True) == literal("A")
    # A terminal that reports the character alongside a shift/ctrl chord: the text wins.
    assert translate("shift+a", "A", printable=True) == literal("A")
    assert translate("ctrl+a", "a", printable=True) == literal("a")


def test_a_modifier_tmux_cannot_spell_drops_the_key_rather_than_typing_it() -> None:
    """``super``/``hyper`` is how macOS Cmd and the kitty protocol's extras
    arrive. There is no tmux name for the chord, and the character alone is not
    what was asked for — Cmd+V is not a request to type a ``v``. Moving the
    printable rule below the modifier gate made this so; the review asked for it
    to be deliberate and pinned rather than a side effect of the ordering."""
    assert translate("super+a", "a", printable=True) == Drop("command")
    assert translate("hyper+a", "a", printable=True) == Drop("command")
    assert translate("super+c", "c", printable=True) == Drop("command")
    assert translate("super+f5", None, printable=False) == Drop("command")


def test_alt_only_claims_the_ascii_letters_that_were_measured() -> None:
    """``str.isalpha`` is Unicode-aware, so an AltGr or accented layout — or
    Escape typed just before the character — put ``M-é`` and ``M-ф`` on the wire.
    Every name this module emits was measured against a real tmux and those
    never were, so they stay the text they have always been (review). A digit
    with a printable character is text too: no parser delivers such an event
    (the parser tests above), and the table invents no chord for one."""
    assert translate("alt+é", "é", printable=True) == literal("é")
    assert translate("alt+ф", "ф", printable=True) == literal("ф")
    assert translate("alt+٣", "٣", printable=True) == literal("٣")
    assert translate("alt+³", "³", printable=True) == literal("³")
    assert translate("alt+1", "1", printable=True) == literal("1")
    assert translate("alt+p", "p", printable=True) == key("M-p")


def test_alt_chords_keep_their_modifier_even_when_the_character_is_reported() -> None:
    """Textual's parser reads ``ESC p`` as ``Key("alt+p", character="p")`` — the
    character is always set for alt+letter and it is printable — so the
    printable-is-literal rule above typed a bare ``p`` into the agent and Claude
    Code's alt+p (switch model) never fired. Reported 2026-09-02 / 2026-09-10."""
    assert translate("alt+p", "p", printable=True) == key("M-p")
    assert translate("meta+p", "p", printable=True) == key("M-p")
    assert translate("alt+shift+a", "A", printable=True) == key("M-A")
    assert translate("ctrl+alt+p", "p", printable=True) == key("C-M-p")
    # Without the character it always worked; it must keep working.
    assert translate("alt+p", None, printable=False) == key("M-p")
    assert translate("alt+1", None, printable=False) == key("M-1")


#: Names that are not names: an empty base, an empty modifier token, or both.
#: Each round of review re-ordered the prologue that reads them and broke a
#: different one — `ctrl++` and `+` in round 4, `alt+` and `+a` in round 5 —
#: because the tests pinned the spellings that already worked. The whole shape
#: is here, asserted in both directions, so the next re-order cannot pick one off.
MALFORMED = ["", "+", "+a", "+left_square_bracket", "ctrl+", "ctrl++", "alt+", "meta+"]


@pytest.mark.parametrize("key", MALFORMED)
def test_a_malformed_key_name_types_its_character_and_names_nothing(key: str) -> None:
    assert translate(key, "x", printable=True) == literal("x")
    # An empty token is a broken NAME, never a modifier: not gated as a command
    # and not read as deliberate aim (review of #161, round 2).
    assert translate(key, None, printable=False) == Drop("nothing_to_type")


#: Chords the table deliberately refuses a name for, with a character reported
#: alongside them. A refusal is not a reason to swallow the keystroke: what
#: travels is the text, which is what this module did before any chord exception
#: existed. ``extended_keys=False`` is the tmux 3.2 floor, where the capability
#: gate is the thing refusing. Synthetic inputs — the parser tests above say
#: which of these a terminal can actually send — pinning the RULE, not a promise.
NO_SAFE_NAME: list[tuple[str, str, bool, DropReason]] = [
    ("ctrl+alt+1", "1", True, "no_name"),  # the shifted digit is layout-specific
    ("alt+shift+1", "1", True, "no_name"),
    ("ctrl+alt+space", " ", False, "too_old"),  # C-M-Space needs tmux >= 3.5
    ("alt+shift+space", " ", False, "too_old"),
    ("alt+shift+minus", "_", False, "too_old"),  # shifted punctuation, same gate
    ("alt+semicolon", ";", True, "no_name"),  # tmux's own argv separator
    ("alt+shift+o", "O", True, "no_name"),  # ESC O is the SS3 introducer
    ("meta+P", "P", True, "no_name"),  # ESC P is the DCS introducer
]


@pytest.mark.parametrize(("key", "character", "extended", "reason"), NO_SAFE_NAME)
def test_a_chord_with_no_safe_name_still_types_its_character(
    key: str, character: str, extended: bool, reason: DropReason
) -> None:
    assert translate(key, character, printable=True, extended_keys=extended) == literal(character)
    # Without a character there is nothing to fall back to, and nothing is
    # sent — and the reason says which nothing: a server too old to carry the
    # chord is a different loss from a chord with no safe name anywhere.
    assert translate(key, None, printable=False, extended_keys=extended) == Drop(reason)


def test_alt_space_is_a_chord_only_when_it_arrives_without_a_character() -> None:
    """``SPECIAL``'s ``not (ctrl or alt)`` guard emits ``M-Space`` for an alt+space
    event with no character — the shape a kitty-protocol terminal sends. A
    legacy terminal's ``ESC SPACE`` never carries an alt token (the parser tests
    above), so a printable ``alt+space`` is not an input that exists, and the
    table does not invent a chord for it: the space is typed."""
    assert translate("alt+space", None, printable=False) == key("M-Space")
    assert translate("meta+space", None, printable=False) == key("M-Space")
    assert translate("alt+space", " ", printable=True) == literal(" ")
    # Alt is what makes it a chord: plain and ctrl+space stay the space they were.
    assert translate("space", " ", printable=True) == literal(" ")
    assert translate("ctrl+space", " ", printable=True) == literal(" ")


def test_alt_on_punctuation_stays_the_character_it_always_was() -> None:
    """Through the name table alt+punctuation was dropped (``;``) or turned into
    a control-sequence introducer (``M-[`` is ``ESC [``) — where before the
    program simply received the character. Found in review; the letter fix
    must not widen to this."""
    assert translate("alt+semicolon", ";", printable=True) == literal(";")
    assert translate("alt+left_square_bracket", "[", printable=True) == literal("[")
    assert translate("alt+shift+minus", "_", printable=True, extended_keys=False) == literal("_")
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
        assert translate(f"ctrl+{name}", None, printable=False) == Drop("no_name"), char


def test_function_keys_stop_at_twelve() -> None:
    for number in range(1, MAX_FUNCTION_KEY + 1):
        assert translate(f"f{number}", None, printable=False) == key(f"F{number}")
    assert translate(f"f{MAX_FUNCTION_KEY + 1}", None, printable=False) == Drop("no_name")
    # ``f0`` is no key at all — and no key Textual reports as characterless, so it
    # is named, not silent: silence is a closed set (review of #161, round 4).
    assert translate("f0", None, printable=False) == Drop("no_name")


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

#: Textual key names that translate to nothing, by design. Everything else the
#: ``Keys`` enum can produce must translate to a name TMUX_NAME accepts.
#: Every name Textual can put in a key event — its ``Keys`` enum AND the kitty
#: protocol's functional-key vocabulary, which is where all of #151 lives and
#: which the enum does not contain (review of #161, round 4) — that
#: ``translate`` refuses, WITH the reason the pane acts on. A flat set could not
#: see the regression this branch is about: a name sliding from ``no_name``
#: (one quiet line) to ``nothing_to_type`` (a keystroke lost in silence) or
#: back (a toast for a key nobody pressed — #151 itself) left it green (review
#: of #161, round 3). A functional key Textual adds tomorrow lands here as a
#: complaint, never as inherited silence.
DELIBERATELY_DROPPED: dict[str, DropReason] = {
    # Binding wildcards, never events: a word if one ever arrives, not silence.
    "<any>": "no_name",
    "<ignore>": "no_name",
    "<scroll-down>": "no_name",
    "<scroll-up>": "no_name",
    # The kitty protocol's characterless keys — CHARACTERLESS_KEYS, spelled out
    # here a second time on purpose: the audit is the record, the set is the
    # behaviour, and each must fail when the other changes.
    **dict.fromkeys(
        (
            "left_shift",
            "left_control",
            "left_alt",
            "left_super",
            "left_hyper",
            "left_meta",
            "right_shift",
            "right_control",
            "right_alt",
            "right_super",
            "right_hyper",
            "right_meta",
            "iso_level3_shift",
            "iso_level5_shift",
            "caps_lock",
            "num_lock",
            "scroll_lock",
            "menu",
            "print_screen",
            "pause",
            "kp_begin",
            "raise_volume",
            "lower_volume",
            "mute_volume",
            "media_play",
            "media_pause",
            "media_play_pause",
            "media_stop",
            "media_reverse",
            "media_fast_forward",
            "media_rewind",
            "media_track_next",
            "media_track_previous",
            "media_record",
        ),
        "nothing_to_type",
    ),
    # Keystrokes lost with a word.
    "decimal": "no_name",  # the layout's to know — KEYPAD_LAYOUT_DEPENDENT
    "separator": "no_name",
    **{f"ctrl+{digit}": "no_name" for digit in range(10)},
    **{f"ctrl+shift+{digit}": "no_name" for digit in range(10)},
    **{f"f{number}": "no_name" for number in range(MAX_FUNCTION_KEY + 1, 36)},
    **{f"ctrl+f{number}": "no_name" for number in range(MAX_FUNCTION_KEY + 1, 25)},
}


def audit_holes(
    holes: dict[str, DropReason], dropped: dict[str, DropReason], names: set[str]
) -> list[str]:
    """Three directions of the ratchet: new holes, healed entries, changed reasons.

    Returned as complaints rather than asserted so the SHAPE can be tested (the
    test below). It used to be one ``A == B or not (A - B)``, which cannot fail
    on its equality half — so an entry that started translating stayed in
    ``DELIBERATELY_DROPPED`` unaudited, the allow list CONTRIBUTING warns about.
    """
    complaints: list[str] = []
    if unexpected := sorted(holes.keys() - dropped.keys()):
        complaints.append(f"translate to nothing and are not deliberate: {unexpected}")
    if stale := sorted((dropped.keys() & names) - holes.keys()):
        complaints.append(f"translate now — remove from DELIBERATELY_DROPPED: {stale}")
    if changed := sorted(
        f"{name}: {dropped[name]} -> {holes[name]}"
        for name in holes.keys() & dropped.keys()
        if holes[name] != dropped[name]
    ):
        complaints.append(f"dropped for a different reason than recorded: {changed}")
    return complaints


def test_every_textual_key_name_translates_or_is_deliberately_dropped() -> None:
    from textual._keyboard_protocol import FUNCTIONAL_KEYS, MODIFIER_FUNCTIONAL_KEYS
    from textual.keys import Keys

    enum = {member.value for member in Keys}
    kitty = set(FUNCTIONAL_KEYS.values()) | set(MODIFIER_FUNCTIONAL_KEYS)
    assert len(enum) > 100 and len(kitty - enum) > 50  # the sweep must still see both
    names = sorted(enum | kitty)
    translated: dict[str, Translation | Drop] = {
        name: translate(name, None, printable=False) for name in names
    }
    holes = {
        name: translation.reason
        for name, translation in translated.items()
        if isinstance(translation, Drop)
    }
    assert audit_holes(holes, DELIBERATELY_DROPPED, set(names)) == []
    for name, translation in translated.items():
        if isinstance(translation, Translation) and translation.kind == "key":
            assert TMUX_NAME.match(translation.value), (name, translation.value)


def test_the_hole_audit_complains_in_every_direction() -> None:
    """The ratchet must fire for a NEW hole, for one that healed, and for one
    whose REASON moved — a keystroke going from one quiet line to silence is
    the mute twin of #151, and a flat set could not see it."""
    names = {"enter", "ctrl+1", "shift+minus"}
    dropped: dict[str, DropReason] = {"ctrl+1": "no_name"}
    assert audit_holes({"ctrl+1": "no_name"}, dropped, names) == [], "the steady state is silent"
    assert audit_holes({"ctrl+1": "no_name", "shift+minus": "no_name"}, dropped, names) == [
        "translate to nothing and are not deliberate: ['shift+minus']"
    ]
    assert audit_holes({}, dropped, names) == [
        "translate now — remove from DELIBERATELY_DROPPED: ['ctrl+1']"
    ]
    assert audit_holes({"ctrl+1": "nothing_to_type"}, dropped, names) == [
        "dropped for a different reason than recorded: ['ctrl+1: no_name -> nothing_to_type']"
    ]
    # The negative control: an entry for a name the sweep does not see is not
    # "healed" — the table may keep covering enum members that came and went.
    # An invented name, so the premise cannot drift: round 4 used ``ctrl-at``
    # here, which Textual does have and this branch made translate (round 5).
    assert audit_holes({}, {"ctrl+eject": "no_name"}, names) == []


def test_the_tmux_grammar_control_rejects_what_tmux_would_mistype() -> None:
    """The guard above is only as good as its grammar: it must refuse these."""
    for bad in ("F13", "Bogus", "C-Bogus", "M-;", ";", "", "C- ", "Enter;"):
        assert TMUX_NAME.match(bad) is None, bad
    for good in ("Enter", "C-S-Up", "M-x", "C-@", "F12", "BTab", "C-BTab", "M-A"):
        assert TMUX_NAME.match(good), good


def test_no_emitted_name_ends_in_the_argv_separator() -> None:
    """A trailing ``;`` ends the tmux command: nothing here may produce one."""
    candidates = [
        *SPECIAL,
        *CHORDS,
        *(f"{mod}+{name}" for mod in ("ctrl", "alt", "shift") for name in PUNCTUATION),
    ]
    for name in candidates:
        translation = translate(name, None, printable=False)
        if isinstance(translation, Translation) and translation.kind == "key":
            assert not translation.value.endswith(";"), name
    # The negative half: the rule is reachable — the one chord that would end in
    # ";" is refused, not emitted.
    assert translate("alt+semicolon", None, printable=False) == Drop("no_name")


def test_no_ctrl_set_is_consulted() -> None:
    assert {"Escape", "BSpace"} == NO_CTRL
    assert translate("ctrl+backspace", None, printable=False) == Drop("no_name")
    assert translate("alt+backspace", None, printable=False) == key("M-BSpace")
    assert translate("shift+backspace", None, printable=False) == key("S-BSpace")


MEASURED_LITERAL_BELOW_35: set[str] = {
    # Typed literally by tmux 3.4 (and 3.3, which adds C-M-Tab) under the
    # bundled conf — the measurement behind EXTENDED_MINIMUM.
    "S-Enter", "S-Escape", "S-Space", "S-Tab", "S-BSpace",
    "C-S-Enter", "C-S-Space", "C-S-a", "C-S-z",
    "M-S-Enter", "M-S-Escape", "M-S-Space", "M-S-Tab", "M-S-BSpace",
    "C-M-Enter", "C-M-Tab",
    # 2026-08-31, the whole EMITTED sweep on 3.2a (ubuntu:22.04), 3.3a
    # (debian:bookworm) and 3.4 (ubuntu:24.04): all three type EVERY shifted
    # punctuation chord, plus tmux's two C-M- aliases for C-M-_, and the triple
    # stacks on the extended bases and on letters.
    "C-S--", "C-S-/", "C-S-?", "C-S-@", "C-S-[", "C-S-\\", "C-S-]", "C-S-^", "C-S-_",
    "M-S-!", "M-S-'", "M-S-,", "M-S--", "M-S-.", "M-S-/", "M-S-@", "M-S-[", "M-S-^",
    "M-S-`", "M-S-{", "M-S-|", "M-S-}", "M-S-~",
    "C-M-S--", "C-M-S-/", "C-M-S-?", "C-M-S-@", "C-M-S-[", "C-M-S-\\", "C-M-S-_",
    "C-M--", "C-M-/",
    "C-M-S-Enter", "C-M-S-Space", "C-M-S-Tab", "C-M-S-a", "C-M-S-z",
}  # fmt: skip

MEASURED_FINE_EVERYWHERE: set[str] = {
    # Legacy-encodable chords the same probes saw arrive as keys on 3.3/3.4.
    "S-Up", "S-F1", "S-DC", "C-S-DC", "C-S-Up", "C-S-Home", "C-BTab", "M-BTab",
    "C-Enter", "M-Enter", "C-M-Up", "C-M-DC", "C-M-F5",
    # …and, from the 2026-08-31 sweep on 3.2a/3.3a/3.4, the classes the hand
    # list had never sent: triple stacks on cursor and function keys, shift on
    # function keys, C-M- on letters and on the punctuation tmux still encodes,
    # and M-<uppercase letter> (alt+shift+z).
    "C-M-S-Up", "C-M-S-DC", "C-M-S-Home", "C-M-S-Left", "C-M-S-F5", "C-M-S-F12",
    "C-S-F1", "M-S-F1", "M-S-F12", "S-F12", "C-F2",
    "C-M-x", "C-M-@", "C-M-[", "C-M-\\", "C-M-]", "C-M-^", "C-M-_", "M-Z",
}  # fmt: skip


def test_the_extended_predicate_matches_the_measurement_both_ways() -> None:
    wrongly_kept = sorted(n for n in MEASURED_LITERAL_BELOW_35 if not needs_extended_keys(n))
    assert not wrongly_kept, f"measured literal on <3.5 yet not gated: {wrongly_kept}"
    wrongly_dropped = sorted(n for n in MEASURED_FINE_EVERYWHERE if needs_extended_keys(n))
    assert not wrongly_dropped, f"measured fine everywhere yet gated: {wrongly_dropped}"


def test_an_old_server_drops_extended_only_chords_instead_of_mistyping() -> None:
    """tmux < 3.5 TYPES these names into the agent (measured); None is the fix."""
    for textual in ("shift+enter", "shift+escape", "ctrl+shift+a", "ctrl+alt+enter"):
        assert translate(textual, None, printable=False, extended_keys=False) == Drop("too_old"), (
            textual
        )
    # The negative half: legacy-encodable chords still flow on an old server…
    assert translate("shift+up", None, printable=False, extended_keys=False) == key("S-Up")
    assert translate("ctrl+shift+delete", None, printable=False, extended_keys=False) == key(
        "C-S-DC"
    )
    assert translate("shift+tab", None, printable=False, extended_keys=False) == key("BTab")
    assert translate("alt+enter", None, printable=False, extended_keys=False) == key("M-Enter")
    # …and a capable server (the default) still gets the full vocabulary.
    assert translate("shift+enter", None, printable=False) == key("S-Enter")
    assert translate("ctrl+alt+enter", None, printable=False) == key("C-M-Enter")


# --- against a real tmux --------------------------------------------------------------

#: Every Textual chord that could reach ``translate``: each subset of the
#: modifiers it understands (``meta`` is an alias of ``alt`` and adds no name),
#: over every base the tables name. ``CHORDS`` keys are whole chords already.
MODIFIER_SETS: list[str] = [
    "+".join(combo)
    for count in range(4)
    for combo in itertools.combinations(("ctrl", "alt", "shift"), count)
]
BASES: list[str] = [
    *SPECIAL,
    *(f"f{number}" for number in range(1, MAX_FUNCTION_KEY + 1)),
    *string.ascii_lowercase,
    *string.digits,
    *PUNCTUATION,
]


def emittable_names() -> list[str]:
    """Every named key ``translate`` can emit, from ``translate`` itself.

    DERIVED, not hand-listed, because a hand-listed product understated the
    vocabulary for a whole round: its modifier list stopped at pairs, so every
    triple chord (``C-M-S-Up``, ``C-M-S-a``) and all the ``M-S-`` punctuation
    (``M-S--`` is alt+shift+minus) was never sent to a real tmux — and the
    live sweep below could not fail for that class. It also listed names
    ``translate`` never emits (``C-A``, ``S-Space``, ``C-S-Tab``), which the
    sweep then "proved" safe.
    """
    chords = [
        *CHORDS,
        *(f"{mods}+{base}" if mods else base for mods in MODIFIER_SETS for base in BASES),
    ]
    translated = (translate(chord, None, printable=False) for chord in chords)
    return sorted({t.value for t in translated if isinstance(t, Translation) and t.kind == "key"})


#: Every named key this module can emit, exercised against the real binary.
EMITTED: list[str] = emittable_names()


def test_the_swept_vocabulary_is_everything_translate_emits() -> None:
    """The sweep's input is the guard's reach: pin what it must and must not hold."""
    assert len(EMITTED) > 400, len(EMITTED)
    assert {name for _, name in TABLE} <= set(EMITTED), "every §6 row is swept"
    # The classes the old hand-written product missed entirely.
    for name in ("C-M-S-Up", "C-M-S-a", "M-S--", "C-S-@", "M-S-{", "C-M-/", "S-F12", "M-Z"):
        assert name in EMITTED, name
    # The negative control: names translate() never emits are NOT swept, so no
    # amount of green here can vouch for a chord the UI cannot produce.
    for name in ("C-A", "S-Space", "S-Tab", "C-S-Tab", "Space", "M-;", "Bogus", "F13"):
        assert name not in EMITTED, name
    assert all(TMUX_NAME.match(name) for name in EMITTED)


_needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")


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


def _wait_for(server: TmuxServer, pane: str, needle: str, timeout: float = 3.0) -> str:
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = server.run("capture-pane", "-p", "-J", "-S", "-", "-t", pane)
        if needle in text:
            return text
        time.sleep(0.05)
    return text


@_needs_tmux
def test_real_tmux_types_none_of_our_names_literally(
    real_server: TmuxServer, tmp_path: Path
) -> None:
    """Each emitted name goes into a raw-mode ``cat -v`` pane with a marker after it.

    A name tmux knows arrives as bytes (``^M``, ``^[[Z``, ``^A``…); a name it does
    not know arrives spelled out, right before its own marker — which is what
    the control at the end must see for ``Bogus`` and ``C-BSpace``, or the
    read-back proves nothing.

    All 470 of :data:`EMITTED` on 3.7c: 1.4 s, none typed literally. The same
    sweep was run by hand in containers on 2026-08-31 for the versions this
    machine cannot install — 3.2a (ubuntu:22.04), 3.3a (debian:bookworm) and
    3.4 (ubuntu:24.04, the CI runner). There the 352 names left after the
    :func:`needs_extended_keys` gate all arrived as keys too, and of the 118 the
    gate holds back, 117 were typed literally by those versions; the 118th is
    ``C-M-Space``, gated with the class it belongs to.
    """
    window = real_server.spawn_window(
        "keys",
        name="probe",
        cwd=tmp_path,
        command=["sh", "-c", "stty raw -echo; cat -v"],
        width=120,
        height=50,
    )
    pane = window.pane_id
    time.sleep(0.3)  # let stty run before the first key lands
    # Only what a pane on THIS server would emit: translate() gates the
    # extended-only chords below EXTENDED_MINIMUM, so the guard sends the same.
    version = real_server.version()
    extended = version is None or version >= EXTENDED_MINIMUM
    emitted = [name for name in EMITTED if extended or not needs_extended_keys(name)]
    for name in emitted:
        real_server.send_keys(pane, name)
        real_server.send_literal(pane, f" <{name}>\r\n")
    for control in ("Bogus", "C-BSpace"):
        real_server.send_keys(pane, control)
        real_server.send_literal(pane, f" <{control}>\r\n")
    text = _wait_for(real_server, pane, "<C-BSpace>")
    assert "<C-BSpace>" in text, text[-500:]
    for name in emitted:
        assert f"<{name}>" in text, f"marker for {name} never arrived"
        assert f"{name} <{name}>" not in text, f"tmux typed {name!r} literally"
    # The control: tmux DOES type an unknown name literally, and we can see it.
    assert "Bogus <Bogus>" in text
    # C-BSpace is refused by our table because tmux mistreats it — 3.7c types
    # it literally, 3.4 swallows it. Version-independent half: it must never
    # arrive as a WORKING backspace, the mistranslation the refusal prevents.
    assert "^? <C-BSpace>" not in text


# --- #151: the four reasons nothing is sent ---------------------------------------


def test_every_modifier_token_textual_emits_is_spelt_or_a_command() -> None:
    """The gate that makes ``Drop("command")`` is the only classifier for it, and
    the pane says nothing for that reason — so a modifier token Textual starts
    emitting that this module does not know would make every chord carrying it
    vanish without a toast or a red test (review of #161, round 3). Pinned on
    BEHAVIOUR rather than a name in Textual: each kitty modifier bit is sent on
    an ``x`` and whatever token the parser puts in front is read back. Textual
    8.2.8 reports nothing for the caps-lock and num-lock bits; the day it does,
    this fails and ``MODIFIERS`` is the place to answer it.
    """
    tokens: set[str] = set()
    for bit in range(8):
        for event in XTermParser().feed(f"\x1b[120;{1 + (1 << bit)}u"):
            if isinstance(event, events.Key):
                tokens.update(event.key.split("+")[:-1])
    assert tokens == {"shift", "alt", "ctrl", "super", "hyper", "meta"}
    assert tokens - set(MODIFIERS) == UNSPELLABLE_MODIFIERS, "the two tmux cannot spell"
    # And ONLY those two are a command: a token this module has never met is a
    # keystroke it cannot spell, said once — never folded into the silent
    # reason (review of #161, round 4) — and never a NAME with the token left
    # out, which would hand tmux the chord with its modifier lost. The text
    # the terminal reported still travels by the table's own rule, so the one
    # exception holds too: alt on a letter is the chord, not the letter, and
    # ``alt+foo+p`` must not reopen the alt+p bug (review of #161, round 5).
    assert translate("foo+a", None, printable=False) == Drop("no_name")
    assert translate("foo+a", "a", printable=True) == literal("a")
    assert translate("foo+1", "1", printable=True) == literal("1")
    assert translate("alt+foo+p", "p", printable=True) == Drop("no_name")
    assert translate("alt+foo+p", None, printable=False) == Drop("no_name")


def test_the_prefix_is_built_from_modifiers_so_a_token_added_there_reaches_the_name() -> None:
    """``MODIFIERS`` is the one place a modifier is read: the values build the
    prefix, in the dict's order. A hand-built prefix beside it once dropped any
    token added to the dict on the floor — the chord emitted under a name with
    the modifier missing, tmux delivering the bare key (review of #161, round
    5). Pinned by the shape of the name rather than by mutating the module."""
    assert list(dict.fromkeys(MODIFIERS.values())) == ["C-", "M-", "S-"]
    assert translate("ctrl+shift+up", None, printable=False) == key("C-S-Up")
    assert translate("alt+ctrl+shift+up", None, printable=False) == key("C-M-S-Up")
    assert translate("meta+shift+up", None, printable=False) == key("M-S-Up")


def test_the_characterless_keys_are_textuals_and_the_silence_is_stated_not_inherited() -> None:
    """``nothing_to_type`` is membership of ``CHARACTERLESS_KEYS`` and nothing
    else — never the fall-through of a lookup miss, which also caught every
    emoji and every key Textual adds tomorrow (review of #161, round 4). Every
    name in the set is one Textual can actually report, and it is the whole of
    what Textual reports without a character: the audit above holds each of
    them, and each name outside them, to its reason."""
    from textual._keyboard_protocol import FUNCTIONAL_KEYS, MODIFIER_FUNCTIONAL_KEYS

    reportable = set(FUNCTIONAL_KEYS.values()) | set(MODIFIER_FUNCTIONAL_KEYS)
    assert reportable >= CHARACTERLESS_KEYS
    assert set(MODIFIER_FUNCTIONAL_KEYS) <= CHARACTERLESS_KEYS
    silent = {
        name
        for name in reportable
        if translate(name, None, printable=False) == Drop("nothing_to_type")
    }
    assert silent == CHARACTERLESS_KEYS


@pytest.mark.parametrize(
    "key",
    [
        # A modifier or a lock, alone or with a modifier HELD — Textual keeps
        # the prefix on the locks, so ``shift+caps_lock`` arrives as such.
        "left_shift",
        "right_control",
        "iso_level3_shift",
        "caps_lock",
        "shift+caps_lock",
        "ctrl+num_lock",
        "shift+scroll_lock",
        # Whole keys a kitty-protocol terminal reports only because Textual asks
        # for every key: the #151 complaint with another key name on it. There
        # is no character behind them, so a modifier held changes nothing —
        # round 1 named ``ctrl+pause`` as a chord the reader meant, and round 2
        # kept that residue undocumented (review, round 3).
        "menu",
        "print_screen",
        "pause",
        "raise_volume",
        "media_play",
        "kp_begin",
        "ctrl+pause",
        "shift+menu",
        "alt+media_play",
        "ctrl+kp_begin",
        "media_track_next",
        "mute_volume",
    ],
)
def test_a_key_that_was_never_a_keystroke_is_nothing_to_type(key: str) -> None:
    assert translate(key, None, printable=False, extended_keys=True) == Drop("nothing_to_type")


@pytest.mark.parametrize(
    "key",
    [
        "f13",  # a function key past the twelve tmux knows
        "ctrl+f13",
        "ctrl+comma",  # a modifier held on a character: aimed at the program
        "ctrl+1",
        "ctrl+backspace",  # tmux 3.7c types the eight characters "C-BSpace"
        "alt+shift+o",  # an ESC introducer: no safe name, and the user meant it
        "alt+section_sign",  # bare it is a literal (below); M-§ was never measured
        "alt+no_break_space",
        "decimal",  # a keystroke whose text only the layout knows (below)
        "separator",
        # Everything this module cannot resolve is a keystroke LOST, never
        # silence: silence is CHARACTERLESS_KEYS and nothing else (round 4).
        "grinning_face",  # U+1F600, outside the read-back's blocks
        "null",  # a control's Unicode name: never a literal, and not characterless
        "line_separator",  # U+2028, likewise
        "\x85",  # NEL: no Unicode name, so Textual names the key after the byte
        "ctrl+\x85",
        "alt+ ",  # a whitespace byte as its own name, with a modifier
        "foo+a",  # a modifier token this module has never met
        "<any>",  # a binding wildcard, never an event
    ],
)
def test_a_chord_the_reader_meant_has_no_name(key: str) -> None:
    assert translate(key, None, printable=False, extended_keys=True) == Drop("no_name")


def test_a_control_byte_named_after_itself_is_never_sent() -> None:
    """U+0085 NEL has no ``unicodedata.name``, so Textual's ``_character_to_key``
    names the key after the raw byte; on the round-3 head the bare key reached
    ``send-keys -l`` — a C1 control into a running agent, the one thing this
    module exists to prevent — while the same key with ctrl held was refused
    (review of #161, round 4). Both are refused, and both say so — and the
    refusal holds for a caller that CLAIMS the byte printable: the fallbacks
    ask the character, not the flag (review of #161, round 5)."""
    assert translate("\x85", "\x85", printable=False) == Drop("no_name")
    assert translate("ctrl+\x85", None, printable=False) == Drop("no_name")
    assert translate("\x85", "\x85", printable=True) == Drop("no_name")
    assert translate("+", "\x07", printable=True) == Drop("nothing_to_type"), "malformed, a BEL"


def test_return_and_ctrl_at_are_their_meanings() -> None:
    """Textual's ``Keys.Return`` and ``Keys.ControlSpace`` (``"ctrl-at"``) are
    binding spellings its parser never emits — but each has one meaning, and a
    ratchet that blessed their silence would lose an Enter without a trace if
    one ever arrived (review of #161, round 4)."""
    assert translate("return", None, printable=False) == key("Enter")
    assert translate("ctrl-at", None, printable=False) == key("C-@")


@pytest.mark.parametrize("key", ["super+k", "super+f5", "hyper+x", "super+shift+a"])
def test_a_cmd_chord_is_a_command_for_the_os_and_not_a_lost_keystroke(key: str) -> None:
    """The reason that keeps macOS Cmd chords off the toast (review of #161,
    round 2): the shape rule read a held modifier as deliberate aim, and every
    Cmd+letter reaching the pane earned its own notice."""
    assert translate(key, "k", printable=True, extended_keys=True) == Drop("command")
    assert translate(key, None, printable=False, extended_keys=True) == Drop("command")


@pytest.mark.parametrize(
    ("name", "char"),
    [("add", "+"), ("subtract", "-"), ("multiply", "*"), ("divide", "/"), ("equal", "=")],
)
def test_a_numpad_operator_reported_without_its_text_is_typed(name: str, char: str) -> None:
    """Textual's names for ``KP_ADD`` … ``KP_EQUAL``. With the text reported
    the text wins; without it the name says what was typed — these are
    keystrokes, unlike ``menu``, and "bare and unnamed" once swallowed them
    (review of #161, round 2). With alt they are the same chord as the row
    above the keyboard, and no new tmux name."""
    assert translate(name, char, printable=True) == literal(char)
    assert translate(name, None, printable=False) == literal(char)
    assert translate(f"alt+{name}", None, printable=False) == key(f"M-{char}")


def test_the_keypads_decimal_and_separator_are_named_once_and_never_guessed() -> None:
    """Kitty key codes are physical: a German numpad's ``,`` arrives as
    ``decimal`` too, and the only terminal that sends the bare name is one that
    reports no text — exactly when the layout cannot be asked. Round 2 typed a
    ``.`` there, the mistyping this module exists to prevent (review, round
    3). With the text reported, the text wins as for every key."""
    assert {"decimal", "separator"} == KEYPAD_LAYOUT_DEPENDENT
    assert translate("decimal", ",", printable=True) == literal(",")
    assert translate("decimal", None, printable=False) == Drop("no_name")
    assert translate("separator", None, printable=False) == Drop("no_name")


def test_a_bare_key_named_after_its_unicode_character_is_that_character() -> None:
    """``section_sign``, ``plus_minus_sign``, ``«`` on a non-US layout, reported
    by a terminal that names the key but does not report its text: Textual
    spelt the name from ``unicodedata.name`` with hyphens AND spaces as
    underscores, and a ``lookup`` on the respelt name missed every hyphenated
    one (review of #161, round 3). Bare only — the chord table's names were
    measured against a tmux and ``M-§`` was not (above)."""
    assert translate("section_sign", None, printable=False) == literal("§")
    assert translate("degree_sign", None, printable=False) == literal("°")
    assert translate("plus_minus_sign", None, printable=False) == literal("±")
    assert translate("left_pointing_double_angle_quotation_mark", None, printable=False) == (
        literal("«")
    )
    # A NO-BREAK SPACE is a keystroke — AltGr+space on the French and Canadian
    # layouts — and ``send-keys -l`` carries it: it is not a tmux key NAME.
    assert translate("no_break_space", None, printable=False) == literal("\u00a0")
    assert translate("euro_sign", None, printable=False) == literal("€")
    assert translate("lozenge", None, printable=False) == literal("◊")  # macOS Option-Shift-V
    # Not a Latin allowlist: the punctuation engraved on keyboards outside the
    # Latin world is typed too (review of #161, round 5).
    assert translate("ideographic_full_stop", None, printable=False) == literal("。")  # JIS
    assert translate("katakana_middle_dot", None, printable=False) == literal("・")
    assert translate("arabic_question_mark", None, printable=False) == literal("؟")
    assert translate("devanagari_danda", None, printable=False) == literal("।")  # InScript
    assert translate("greek_question_mark", None, printable=False) == literal("\u037e")
    assert translate("fullwidth_tilde", None, printable=False) == literal("\uff5e")
    # Controls and the line/paragraph separators are never a literal — and
    # never silence either: they are not characterless keys, so they are named.
    assert translate("null", None, printable=False) == Drop("no_name")
    assert translate("line_separator", None, printable=False) == Drop("no_name")


def test_the_unicode_read_back_is_textuals_own_naming() -> None:
    """The table ``core.keys`` builds must agree with ``_character_to_key`` for
    every character it holds — the only disagreements being the six names
    Textual rewrites AFTER naming (``KEY_NAME_REPLACEMENTS``), every one of
    which ``PUNCTUATION`` carries under both spellings, so none reaches the
    read-back."""
    from types import MappingProxyType

    from textual.keys import KEY_NAME_REPLACEMENTS, _character_to_key

    from aisquare.core.keys import _named_characters

    table = _named_characters()
    assert isinstance(table, MappingProxyType), "read-only: @cache hands out the one instance"
    assert len(table) > 5000, len(table)  # the plane, not a Latin allowlist (round 5)
    # The ASCII names PUNCTUATION spells by hand are in the table too; the two
    # must agree on every shared name, or the redundancy is a fork waiting to
    # happen (review of #161, round 5).
    shared = set(table) & set(PUNCTUATION)
    assert len(shared) > 20
    assert {name: table[name] for name in shared} == {name: PUNCTUATION[name] for name in shared}
    disagreements = {name for name, char in table.items() if _character_to_key(char) != name}
    assert disagreements == set(KEY_NAME_REPLACEMENTS)
    assert disagreements <= set(PUNCTUATION)


def test_the_reasons_as_the_parser_delivers_them(parsed: Parse) -> None:
    """The kitty sequences a terminal actually sends, each landing on its reason."""
    assert arrived("\x1b[57441u", parsed) == [Drop("nothing_to_type")], "left_shift"
    assert arrived("\x1b[57358;2u", parsed) == [Drop("nothing_to_type")], "shift+caps_lock"
    assert arrived("\x1b[57363u", parsed) == [Drop("nothing_to_type")], "menu"
    assert arrived("\x1b[57362;5u", parsed) == [Drop("nothing_to_type")], "ctrl+pause"
    assert arrived("\x1b[57439u", parsed) == [Drop("nothing_to_type")], "raise_volume"
    assert arrived("\x1b[107;9u", parsed) == [Drop("command")], "super+k"
    assert arrived("\x1b[57376u", parsed) == [Drop("no_name")], "f13"
    assert arrived("\x1b[44;5u", parsed) == [Drop("no_name")], "ctrl+comma"
    assert arrived("\x1b[57409u", parsed) == [Drop("no_name")], "numpad decimal, no text"
    assert arrived("\x1b[128512u", parsed) == [Drop("no_name")], "an emoji key: lost, not silent"
    assert arrived("\x1b[133u", parsed) == [Drop("no_name")], "NEL as its own name: never sent"
    assert arrived("\x1b[133;5u", parsed) == [Drop("no_name")], "ctrl+NEL"
    assert arrived("\x1b[13;2u", parsed, extended=False) == [Drop("too_old")], "shift+enter, 3.4"
    assert arrived("\x1b[57413u", parsed) == [literal("+")], "numpad + without its text"
    assert arrived("\x1b[57413;1;43u", parsed) == [literal("+")], "numpad + with its text"
    assert arrived("\x1b[167u", parsed) == [literal("§")], "§ without its text"
    assert arrived("\x1b[177u", parsed) == [literal("±")], "± without its text"
    assert arrived("\x1b[160u", parsed) == [literal("\u00a0")], "NBSP without its text"


def test_a_key_the_table_can_answer_never_reaches_the_question() -> None:
    """The premise under every list above: a reason is only ever attached to a
    refusal. F1-F12 and shift+Enter have tmux names, so the pane sends them."""
    for name in ("f12", "shift+enter", "ctrl+a"):
        assert isinstance(translate(name, None, printable=False, extended_keys=True), Translation)
