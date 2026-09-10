"""``core.keys``: every Textual key the pane forwards arrives in tmux's vocabulary.

Two kinds of test. The table tests pin each row of docs/plans/fleet-tui.md §6
and the deliberate holes (a ``None`` for every key tmux would MISTYPE — it
sends an unknown name as literal text). The real-tmux test (skipped without a
``tmux`` on PATH) sends every name this module can emit into a raw-mode
``cat -v`` pane and reads back what arrived: no name may come back spelled
out, and ``Bogus`` must — the control that proves the read-back can see a
mistyped name at all.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import re
import shutil
import string
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from aisquare.core.keys import (
    CHORDS,
    CTRL_PUNCTUATION,
    EXTENDED_MINIMUM,
    MAX_FUNCTION_KEY,
    NO_CTRL,
    PUNCTUATION,
    SPECIAL,
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
    assert translate(textual, None, printable=False) is None


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
    translated: dict[str, Translation | None] = {
        name: translate(name, None, printable=False) for name in names
    }
    holes = {name for name, translation in translated.items() if translation is None}
    assert audit_holes(holes, DELIBERATELY_DROPPED, set(names)) == []
    for name, translation in translated.items():
        if translation is not None and translation.kind == "key":
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
        if translation is not None and translation.kind == "key":
            assert not translation.value.endswith(";"), name
    # The negative half: the rule is reachable — the one chord that would end in
    # ";" is refused, not emitted.
    assert translate("alt+semicolon", None, printable=False) is None


def test_no_ctrl_set_is_consulted() -> None:
    assert {"Escape", "BSpace"} == NO_CTRL
    assert translate("ctrl+backspace", None, printable=False) is None
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
        assert translate(textual, None, printable=False, extended_keys=False) is None, textual
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
    return sorted({t.value for t in translated if t is not None and t.kind == "key"})


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
