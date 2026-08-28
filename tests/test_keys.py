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
import os
import re
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from aisquare.core.keys import (
    CHORDS,
    CTRL_PUNCTUATION,
    MAX_FUNCTION_KEY,
    NO_CTRL,
    PUNCTUATION,
    SPECIAL,
    Translation,
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


def test_every_textual_key_name_translates_or_is_deliberately_dropped() -> None:
    from textual.keys import Keys

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


# --- against a real tmux --------------------------------------------------------------

#: Every named key this module can emit, exercised against the real binary.
EMITTED: list[str] = sorted(
    {
        *SPECIAL.values(),
        *CHORDS.values(),
        *(f"{mod}{name}" for mod in ("C-", "M-", "S-", "C-S-", "C-M-", "M-S-")
          for name in SPECIAL.values() if not (mod.startswith("C-") and name in NO_CTRL)),
        *(f"F{n}" for n in range(1, MAX_FUNCTION_KEY + 1)),
        *(f"{mod}F{n}" for mod in ("C-", "M-", "S-") for n in (1, 12)),
        *(f"C-{char}" for char in CTRL_PUNCTUATION),
        *(f"C-{char}" for char in "azAZ"),
        *(f"M-{char}" for char in "azAZ019,.-=[]/\\'`~"),
        *(f"C-S-{char}" for char in "az"),
        *(f"C-M-{char}" for char in "az"),
    }
)  # fmt: skip

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
    for name in EMITTED:
        real_server.send_keys(pane, name)
        real_server.send_literal(pane, f" <{name}>\r\n")
    for control in ("Bogus", "C-BSpace"):
        real_server.send_keys(pane, control)
        real_server.send_literal(pane, f" <{control}>\r\n")
    text = _wait_for(real_server, pane, "<C-BSpace>")
    assert "<C-BSpace>" in text, text[-500:]
    for name in EMITTED:
        assert f"<{name}>" in text, f"marker for {name} never arrived"
        assert f"{name} <{name}>" not in text, f"tmux typed {name!r} literally"
    # The control: tmux DOES type an unknown name literally, and we can see it.
    assert "Bogus <Bogus>" in text
    assert "C-BSpace <C-BSpace>" in text
