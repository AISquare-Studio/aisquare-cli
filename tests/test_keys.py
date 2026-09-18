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
    CHORDS,
    CTRL_PUNCTUATION,
    ESC_INTRODUCERS,
    EXTENDED_MINIMUM,
    MAX_FUNCTION_KEY,
    MODIFIER_ONLY_KEYS,
    NO_CTRL,
    PUNCTUATION,
    SPECIAL,
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


DROPPED: list[str] = [
    "print_screen",
    "menu",
    "caps_lock",
    "super+x",  # tmux has no super/hyper spelling
    "hyper+x",
    "f13",  # tmux knows F1-F12 only; F13 would be typed as three letters
    "f24",
    "ctrl+f13",
    "ctrl+backspace",  # tmux 3.7c types the eight characters "C-BSpace"
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
    assert isinstance(translate(textual, None, printable=False), Drop)


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
    assert translate("f0", None, printable=False) == Drop("nothing_to_type")


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


def audit_holes(holes: set[str], dropped: set[str], names: set[str]) -> list[str]:
    """Both directions of the ratchet: new holes, and entries that no longer hole.

    Returned as complaints rather than asserted so the SHAPE can be tested (the
    test below). It used to be one ``A == B or not (A - B)``, which cannot fail
    on its equality half — so an entry that started translating stayed in
    ``DELIBERATELY_DROPPED`` unaudited, the allow list CONTRIBUTING warns about.
    """
    complaints: list[str] = []
    if unexpected := sorted(holes - dropped):
        complaints.append(f"translate to nothing and are not deliberate: {unexpected}")
    if stale := sorted((dropped & names) - holes):
        complaints.append(f"translate now — remove from DELIBERATELY_DROPPED: {stale}")
    return complaints


def test_every_textual_key_name_translates_or_is_deliberately_dropped() -> None:
    from textual.keys import Keys

    names = sorted({member.value for member in Keys})
    assert len(names) > 100  # the sweep must still see the enum
    translated: dict[str, Translation | Drop] = {
        name: translate(name, None, printable=False) for name in names
    }
    holes = {name for name, translation in translated.items() if isinstance(translation, Drop)}
    assert audit_holes(holes, DELIBERATELY_DROPPED, set(names)) == []
    for name, translation in translated.items():
        if isinstance(translation, Translation) and translation.kind == "key":
            assert TMUX_NAME.match(translation.value), (name, translation.value)


def test_the_hole_audit_complains_in_both_directions() -> None:
    """The ratchet must fire for a NEW hole and for one that healed."""
    names = {"enter", "ctrl+1", "shift+minus"}
    dropped = {"ctrl+1"}
    assert audit_holes({"ctrl+1"}, dropped, names) == [], "the steady state is silent"
    assert audit_holes({"ctrl+1", "shift+minus"}, dropped, names) == [
        "translate to nothing and are not deliberate: ['shift+minus']"
    ]
    assert audit_holes(set(), dropped, names) == [
        "translate now — remove from DELIBERATELY_DROPPED: ['ctrl+1']"
    ]
    # The negative control: an entry Textual no longer has at all is not
    # "healed" — the set may keep covering enum members that came and went.
    assert audit_holes(set(), {"ctrl-at"}, names) == []


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


def test_the_modifier_set_is_textuals_own_list_plus_the_three_locks() -> None:
    """The claim ``MODIFIER_ONLY_KEYS``' comment makes, asked of Textual.

    ONE direction, deliberately. A name Textual ADDS to its modifier subset is
    silent under the rule already (bare, not ``fN``), so a superset assertion
    would go red on an upstream change that needs no change here (review of
    #161, round 2). A name Textual moves OUT of its subset starts arriving with
    its prefix — the shape that let ``shift+caps_lock`` through — and that is
    what the surplus pins: it is the three locks, and nothing else.
    """
    from textual._keyboard_protocol import MODIFIER_FUNCTIONAL_KEYS

    assert MODIFIER_ONLY_KEYS - set(MODIFIER_FUNCTIONAL_KEYS) == {
        "caps_lock",
        "num_lock",
        "scroll_lock",
    }


@pytest.mark.parametrize(
    "key",
    [
        # A modifier or a lock on its own, and — the case an exact match on
        # event.key missed — a lock with a modifier HELD (review).
        "left_shift",
        "right_control",
        "iso_level3_shift",
        "caps_lock",
        "shift+caps_lock",
        "ctrl+num_lock",
        "shift+scroll_lock",
        # Whole keys a kitty-protocol terminal reports only because Textual asks
        # for every key. Never in any set: the #151 complaint with another key
        # name on it.
        "menu",
        "print_screen",
        "pause",
        "raise_volume",
        "media_play",
        "kp_begin",
        # A control character's Unicode name is not a literal to send.
        "null",
    ],
)
def test_a_key_that_was_never_a_keystroke_is_nothing_to_type(key: str) -> None:
    assert translate(key, None, printable=False, extended_keys=True) == Drop("nothing_to_type")


@pytest.mark.parametrize(
    "key",
    [
        "f13",  # a function key past the twelve tmux knows
        "ctrl+f13",
        "ctrl+comma",  # a modifier held: aimed at the program, deliberately
        "ctrl+1",
        "alt+shift+o",  # an ESC introducer: no safe name, and the user meant it
        "alt+section_sign",  # bare it is a literal (below); M-§ was never measured
    ],
)
def test_a_chord_the_reader_meant_has_no_name(key: str) -> None:
    assert translate(key, None, printable=False, extended_keys=True) == Drop("no_name")


@pytest.mark.parametrize("key", ["super+k", "super+f5", "hyper+x", "super+shift+a"])
def test_a_cmd_chord_is_a_command_for_the_os_and_not_a_lost_keystroke(key: str) -> None:
    """The reason that keeps macOS Cmd chords off the toast (review of #161,
    round 2): the shape rule read a held modifier as deliberate aim, and every
    Cmd+letter reaching the pane earned its own notice."""
    assert translate(key, "k", printable=True, extended_keys=True) == Drop("command")
    assert translate(key, None, printable=False, extended_keys=True) == Drop("command")


@pytest.mark.parametrize(
    ("name", "char"),
    [
        ("add", "+"),
        ("subtract", "-"),
        ("multiply", "*"),
        ("divide", "/"),
        ("equal", "="),
        ("decimal", "."),
        ("separator", ","),
    ],
)
def test_a_numpad_operator_reported_without_its_text_is_typed(name: str, char: str) -> None:
    """Textual's names for ``KP_ADD`` … ``KP_SEPARATOR``. With the text reported
    the text wins; without it the name says what was typed — these are
    keystrokes, unlike ``menu``, and "bare and unnamed" once swallowed them
    (review of #161, round 2). With alt they are the same chord as the row
    above the keyboard, and no new tmux name."""
    assert translate(name, char, printable=True) == literal(char)
    assert translate(name, None, printable=False) == literal(char)
    assert translate(f"alt+{name}", None, printable=False) == key(f"M-{char}")


def test_a_bare_key_named_after_its_unicode_character_is_that_character() -> None:
    """``section_sign``, ``degree_sign``, ``pound_sign`` on a non-US layout,
    reported by a terminal that names the key but does not report its text:
    Textual spelt the name from ``unicodedata.name``, so it can be read back.
    Bare only — the chord table's names were measured against a tmux and
    ``M-§`` was not (above)."""
    assert translate("section_sign", None, printable=False) == literal("§")
    assert translate("degree_sign", None, printable=False) == literal("°")
    assert translate("pound_sign", None, printable=False) == literal("£")
    assert translate("currency_sign", None, printable=False) == literal("¤")
    # Controls and spaces are never a literal, whatever their name.
    assert translate("null", None, printable=False) == Drop("nothing_to_type")
    assert translate("no_break_space", None, printable=False) == Drop("nothing_to_type")


def test_the_reasons_as_the_parser_delivers_them(parsed: Parse) -> None:
    """The kitty sequences a terminal actually sends, each landing on its reason."""
    assert arrived("\x1b[57441u", parsed) == [Drop("nothing_to_type")], "left_shift"
    assert arrived("\x1b[57358;2u", parsed) == [Drop("nothing_to_type")], "shift+caps_lock"
    assert arrived("\x1b[57363u", parsed) == [Drop("nothing_to_type")], "menu"
    assert arrived("\x1b[57439u", parsed) == [Drop("nothing_to_type")], "raise_volume"
    assert arrived("\x1b[107;9u", parsed) == [Drop("command")], "super+k"
    assert arrived("\x1b[57376u", parsed) == [Drop("no_name")], "f13"
    assert arrived("\x1b[44;5u", parsed) == [Drop("no_name")], "ctrl+comma"
    assert arrived("\x1b[13;2u", parsed, extended=False) == [Drop("too_old")], "shift+enter, 3.4"
    assert arrived("\x1b[57413u", parsed) == [literal("+")], "numpad + without its text"
    assert arrived("\x1b[57413;1;43u", parsed) == [literal("+")], "numpad + with its text"
    assert arrived("\x1b[167u", parsed) == [literal("§")], "§ without its text"


def test_a_key_the_table_can_answer_never_reaches_the_question() -> None:
    """The premise under every list above: a reason is only ever attached to a
    refusal. F1-F12 and shift+Enter have tmux names, so the pane sends them."""
    for name in ("f12", "shift+enter", "ctrl+a"):
        assert isinstance(translate(name, None, printable=False, extended_keys=True), Translation)
