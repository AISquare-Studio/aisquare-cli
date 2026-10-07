"""docs/demo.tape cannot work unless these hold, and its render cannot run on every leg.

The tape is the README's GIF and a test of the walkthrough it records: vhs
exits 1 when a Wait does not see its text (docs/demo/render.sh, which
.github/workflows/demo.yml runs on the pull requests that touch the demo or
the UI). That job needs docker and a minute or more. These rules need neither,
so a tape that cannot work fails the suite on all four legs, in seconds:

- every Wait is plain text that the UI, the onboarding verdict, the seed or the
  stand-in prints, so copy that changes under the tape fails here first;
- every key the tape presses is bound, and one character typed at the UI is a
  key press: ``Type "+"`` on Welcome did nothing until a Wait ran out
  (measured), because nothing binds ``plus`` yet;
- the Outputs are relative paths under out/, with a GIF and the .txt that
  render.sh's frame check reads; COLORTERM is truecolor; every Screenshot has
  a Sleep after it;
- the tape ends where the walkthrough does, and takes its still there;
- the stand-in agent ends by exec'ing a program the fleet takes for an agent.

The rules live in tests/demo_tape.py beside the frame check render.sh runs.
Each has a positive control per shape it claims and a negative control
(CONTRIBUTING, "Writing a guard that still guards"), and each check of the
real tape also asserts something it must still see, so a rule gone blind
cannot pass by finding nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from textual.binding import Binding
from textual.keys import _character_to_key
from textual.screen import Screen
from textual.widgets import Button, Input

from aisquare.services.fleet import _agent_running
from tests import demo_tape
from tests.demo_tape import Command, parse

REPO = Path(__file__).resolve().parents[1]
TAPE = REPO / "docs" / "demo.tape"
STAND_IN = REPO / "docs" / "demo" / "bin" / "claude"
SEED = REPO / "docs" / "demo" / "seed.sh"
UI = REPO / "src" / "aisquare" / "cli" / "ui"
ONBOARDING = REPO / "src" / "aisquare" / "services" / "onboarding.py"

#: Where the walkthrough ends today: the manager's window, running the stand-in.
END = "stand-in agent"


def _tape() -> list[Command]:
    return parse(TAPE.read_text(encoding="utf-8"))


def _corpus() -> list[str]:
    """Everything the walkthrough can put on screen: the UI, the Onboard view's verdict
    line (services/onboarding.py), the seed's marker and the stand-in's banner."""
    sources = [*sorted(UI.rglob("*.py")), ONBOARDING, SEED, STAND_IN]
    return [text for path in sources for text in demo_tape.strings_in(path)]


def _bound() -> set[str]:
    """The keys the walkthrough's screens bind: the UI's own ``BINDINGS``, and
    Textual's for the three things the tape does with built-in widgets: move focus
    (Screen: tab), press the focused button (Button: enter), submit the path box
    (Input: enter)."""
    keys = demo_tape.bound_keys(sorted(UI.rglob("*.py")))
    for widget in (Screen, Button, Input):
        for binding in widget.BINDINGS:
            key = binding.key if isinstance(binding, Binding) else binding[0]
            keys.update(part.strip() for part in key.split(","))
    return keys


# ------------------------------------------------------------------ the real tape


def test_the_guard_reads_the_walkthrough() -> None:
    """What every rule below must still see: the tape parses into the walkthrough.

    A parser that returned nothing would pass every rule in this file.
    """
    commands = _tape()
    typed = [demo_tape.quoted(c.argument) for c in commands if c.name == "Type"]
    waits = [text for c in commands if (text := demo_tape.pattern(c)) is not None]

    assert "asq" in typed, typed
    assert len(waits) >= 4, waits
    assert [c.name for c in commands].count("Screenshot") >= 1
    assert demo_tape.end_text(commands) == END


def test_the_outputs_land_where_render_collects_them() -> None:
    assert demo_tape.output_problems(_tape()) == []


def test_the_tape_asks_for_truecolor() -> None:
    assert demo_tape.colour_problems(_tape()) == []


def test_every_wait_is_text_the_walkthrough_prints() -> None:
    corpus = _corpus()
    assert any("Start manager" in text for text in corpus), "the corpus lost the UI's words"

    assert demo_tape.wait_problems(_tape(), corpus) == []


def test_every_key_the_tape_presses_is_bound() -> None:
    bound = _bound()
    assert {"down", "enter", "tab", "q"} <= bound, "the binding read lost the UI's keys"

    assert demo_tape.key_problems(_tape(), bound, _character_to_key) == []


def test_every_screenshot_settles_before_the_next_key() -> None:
    assert demo_tape.screenshot_problems(_tape()) == []


def test_the_tape_ends_where_the_walkthrough_does() -> None:
    assert demo_tape.ending_problems(_tape(), END) == []


def test_the_stand_in_leaves_an_agent_in_the_foreground() -> None:
    assert demo_tape.stand_in_problems(STAND_IN.read_text(encoding="utf-8"), _agent_running) == []


def test_the_stand_in_is_executable_in_git() -> None:
    """Without the bit, the seed refuses and the fleet finds no ``claude`` on PATH.

    Read from git's index, which keeps the mode on every platform; a Windows
    checkout has no execute bit to look at.
    """
    staged = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "--stage", "--", "docs/demo/bin/claude"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert staged.startswith("100755 "), f"docs/demo/bin/claude is staged as {staged!r}"


# ------------------------------------------------------------------ the parser


def test_the_parser_separates_timing_and_target_from_the_name() -> None:
    """The tape uses ``Wait+Screen@15s``; a parser that kept ``@15s`` in the name
    would hand every rule a command none of them knows."""
    wait, typed, key = parse('# a comment\nWait+Screen@15s /seeded:/\nType@100ms "hi"\nTab@1s 2\n')

    assert (wait.name, wait.target, demo_tape.pattern(wait)) == ("Wait", "Screen", "seeded:")
    assert (typed.name, demo_tape.quoted(typed.argument)) == ("Type", "hi")
    assert (key.name, demo_tape.key_of(key), key.line) == ("Tab", "tab", 4)


# ------------------------------------------------------------- the controls

#: A tape every rule accepts, for the negative controls: each rule is handed the
#: same synthetic corpus, bindings and end, so a rule that accuses everything
#: fails here rather than passing its positive control for free.
_GOOD = """\
Output out/demo.gif
Output out/demo.txt
Env COLORTERM "truecolor"
Type "asq"
Enter
Wait+Screen /aisquare fleet/
Down
Type "q"
Type "~/acme-api"
Wait+Line /stand-in agent/
Sleep 1s
Screenshot out/welcome.png
Sleep 500ms
"""
_CORPUS = ["aisquare fleet\n", 'line "stand-in agent: $name"']
_BOUND = {"down", "enter", "tab", "q"}


def _sh(body: str) -> str:
    return f"#!/bin/sh\n# a stand-in\nprintf 'banner'\n{body}\n"


_OUTPUT_SHAPES = {
    "an absolute Output": "Output /tmp/demo.gif\nOutput out/demo.txt\n",
    "an absolute Screenshot": (
        "Output out/demo.gif\nOutput out/demo.txt\nScreenshot /tmp/still.png\nSleep 1s\n"
    ),
    "an Output outside out/": "Output demo.gif\nOutput out/demo.txt\n",
    "no GIF": "Output out/demo.txt\n",
    "no .txt": "Output out/demo.gif\n",
}
_COLOUR_SHAPES = {
    "no COLORTERM": 'Output out/demo.gif\nType "asq"\n',
    "256 colours": 'Env COLORTERM "256color"\n',
}
_WAIT_SHAPES = {
    "text nothing prints": "Wait+Screen /no such words/\n",
    "a regex": "Wait+Screen /aisquare.*fleet/\n",
    "an empty pattern": "Wait+Screen //\n",
    "a Line wait for text nothing prints": "Wait+Line /no such words/\n",
}
_KEY_SHAPES = {
    "a key nothing binds": "PageDown\n",
    "a modified key nothing binds": "Ctrl+X\n",
    "one character nothing binds": 'Type "+"\n',
    "a command that is not vhs's": "Dwon\n",
}
_SCREENSHOT_SHAPES = {
    "a key straight after": "Screenshot out/a.png\nEnter\n",
    "a short Sleep": "Screenshot out/a.png\nSleep 100ms\nEnter\n",
    "a short bare-number Sleep": "Screenshot out/a.png\nSleep 0.2\nEnter\n",
    "the tape ends on it": "Screenshot out/a.png\n",
}
_ENDING_SHAPES = {
    "the last Wait is elsewhere": (
        "Wait+Screen /stand-in agent/\nWait+Screen /aisquare fleet/\n"
        "Screenshot out/a.png\nSleep 1s\n"
    ),
    "a key after the last Wait": (
        "Wait+Screen /stand-in agent/\nEnter\nScreenshot out/a.png\nSleep 1s\n"
    ),
    "no Screenshot after the last Wait": (
        "Screenshot out/a.png\nSleep 1s\nWait+Screen /stand-in agent/\nSleep 1s\n"
    ),
    "no Wait at all": 'Type "asq"\nScreenshot out/a.png\nSleep 1s\n',
}
_STAND_IN_SHAPES = {
    "a read loop": 'while read -r line; do echo "$line"; done',
    "exec of a shell": "exec sh",
    "exec of a shell by path": "exec /bin/bash -c cat",
    "exec of tmux": "exec tmux",
    "exec of Python": "exec python3 -m http.server",
    "exec of nothing": "exec",
}


@pytest.mark.parametrize("shape", sorted(_OUTPUT_SHAPES))
def test_the_output_rule_fires_on_each_shape(shape: str) -> None:
    assert demo_tape.output_problems(parse(_OUTPUT_SHAPES[shape])), f"missed: {shape}"


def test_the_output_rule_accepts_a_sound_tape() -> None:
    assert demo_tape.output_problems(parse(_GOOD)) == []


@pytest.mark.parametrize("shape", sorted(_COLOUR_SHAPES))
def test_the_colour_rule_fires_on_each_shape(shape: str) -> None:
    assert demo_tape.colour_problems(parse(_COLOUR_SHAPES[shape])), f"missed: {shape}"


def test_the_colour_rule_accepts_a_sound_tape() -> None:
    assert demo_tape.colour_problems(parse(_GOOD)) == []


@pytest.mark.parametrize("shape", sorted(_WAIT_SHAPES))
def test_the_wait_rule_fires_on_each_shape(shape: str) -> None:
    assert demo_tape.wait_problems(parse(_WAIT_SHAPES[shape]), _CORPUS), f"missed: {shape}"


def test_the_wait_rule_accepts_a_sound_tape() -> None:
    """Text inside an f-string's literal part and inside a shell line both count,
    and a bare Wait (the prompt) is not a pattern."""
    assert demo_tape.wait_problems(parse(_GOOD + "Wait\n"), _CORPUS) == []


@pytest.mark.parametrize("shape", sorted(_KEY_SHAPES))
def test_the_key_rule_fires_on_each_shape(shape: str) -> None:
    found = demo_tape.key_problems(parse(_KEY_SHAPES[shape]), _BOUND, _character_to_key)

    assert found, f"missed: {shape}"


def test_the_key_rule_accepts_a_sound_tape() -> None:
    """Bound keys, a bound one-character Type, and text typed into a box."""
    assert demo_tape.key_problems(parse(_GOOD), _BOUND, _character_to_key) == []


@pytest.mark.parametrize("shape", sorted(_SCREENSHOT_SHAPES))
def test_the_screenshot_rule_fires_on_each_shape(shape: str) -> None:
    assert demo_tape.screenshot_problems(parse(_SCREENSHOT_SHAPES[shape])), f"missed: {shape}"


def test_the_screenshot_rule_accepts_a_sound_tape() -> None:
    assert demo_tape.screenshot_problems(parse(_GOOD)) == []


@pytest.mark.parametrize("shape", sorted(_ENDING_SHAPES))
def test_the_ending_rule_fires_on_each_shape(shape: str) -> None:
    assert demo_tape.ending_problems(parse(_ENDING_SHAPES[shape]), END), f"missed: {shape}"


def test_the_ending_rule_accepts_a_sound_tape() -> None:
    assert demo_tape.ending_problems(parse(_GOOD), END) == []


def test_the_ending_rule_refuses_an_empty_end() -> None:
    """``"" == ""`` and ``"" in frame`` are always true: an empty end must not pass."""
    assert demo_tape.ending_problems(parse(_GOOD), "") != []


@pytest.mark.parametrize("shape", sorted(_STAND_IN_SHAPES))
def test_the_stand_in_rule_fires_on_each_shape(shape: str) -> None:
    found = demo_tape.stand_in_problems(_sh(_STAND_IN_SHAPES[shape]), _agent_running)

    assert found, f"missed: {shape}"


@pytest.mark.parametrize("ending", ["exec cat", "exec /bin/cat", "exec -a claude cat"])
def test_the_stand_in_rule_accepts_a_program_the_fleet_takes_for_an_agent(ending: str) -> None:
    assert demo_tape.stand_in_problems(_sh(ending), _agent_running) == []


# --------------------------------------------------------- the render's frames


def _render(*shown: str) -> str:
    """A ``.txt`` Output as vhs writes it: each frame followed by the rule line."""
    return "".join(f"{frame}\n{demo_tape.FRAME_RULE}\n" for frame in shown)


_FRAME_SHAPES = {
    "a Python traceback mid-walkthrough": _render(
        "aisquare fleet", 'Traceback (most recent call last):\n  File "app.py"', END
    ),
    "a Rich traceback": _render("aisquare fleet", "╭─ Traceback (most recent call last) ─╮", END),
    "a last frame that left the end": _render(END, "$ "),
    "nothing recorded": "",
    "only blank frames": _render("", "   "),
}


@pytest.mark.parametrize("shape", sorted(_FRAME_SHAPES))
def test_the_frame_check_fires_on_each_shape(shape: str) -> None:
    assert demo_tape.frame_problems(_FRAME_SHAPES[shape], END), f"missed: {shape}"


def test_the_frame_check_accepts_a_sound_render() -> None:
    assert demo_tape.frame_problems(_render("aisquare fleet", f"{END}: manager"), END) == []


def test_frames_split_on_the_rule_line_and_only_on_it() -> None:
    """vhs's rule is 80 U+2500 alone on a line; a box the UI draws is not one."""
    border = "╭" + "─" * 80 + "╮"

    assert demo_tape.frames(_render("first", "second")) == ["first", "second"]
    assert demo_tape.frames(f"{border}\nboxed") == [f"{border}\nboxed"]


def test_the_render_check_exits_nonzero_on_a_bad_render(tmp_path: Path) -> None:
    """render.sh trusts the exit code: a traceback must make it non-zero."""
    rendered = tmp_path / "demo.txt"
    rendered.write_text(
        _render("aisquare fleet", "Traceback (most recent call last):", END),
        encoding="utf-8",
        newline="\n",
    )

    assert demo_tape.main([str(rendered), str(TAPE)]) == 1


def test_the_render_check_exits_zero_on_a_sound_render(tmp_path: Path) -> None:
    rendered = tmp_path / "demo.txt"
    rendered.write_text(
        _render("aisquare fleet", f"{END}: manager"), encoding="utf-8", newline="\n"
    )

    assert demo_tape.main([str(rendered), str(TAPE)]) == 0
