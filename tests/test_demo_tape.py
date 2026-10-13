"""docs/demo.tape cannot work unless these hold, and its render cannot run on every leg.

The tape is the README's GIF and a test of the walkthrough it records: vhs
exits 1 when a Wait does not see its text (docs/demo/render.sh, which
.github/workflows/demo.yml runs on the pull requests that touch the demo or
the package). That job needs docker and a minute or more. These rules need
neither, so a tape that cannot work fails the suite on all four legs, in
seconds:

- every Wait is plain text that the one file named for it prints, docstrings
  and comments left out, so copy that changes under the tape fails here first;
- every key the tape presses is bound, and one character typed at the UI is a
  key press: ``Type "+"`` on Welcome did nothing until a Wait ran out
  (measured), because nothing binds ``plus`` yet;
- the Outputs are relative paths under out/, with a GIF and the .txt that
  render.sh's snapshot check reads; COLORTERM is truecolor; every Screenshot
  has a Sleep after it;
- the tape ends where the walkthrough does, and takes its still there;
- render.sh's end-screen texts are the coders the page starts and the
  stand-in's path, the screen's disclosure that its Claude Code is a stand-in;
- the stand-in agent ends by exec'ing a program the fleet takes for an agent,
  in a form dash runs;
- the seed refuses outside the render image, and says why when it refuses.

The rules live in tests/demo_tape.py beside the snapshot check render.sh runs.
Each has a positive control per shape it claims and a negative control
(CONTRIBUTING, "Writing a guard that still guards"), and each check of the
real tape also asserts something it must still see, so a rule gone blind
cannot pass by finding nothing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from textual.binding import Binding
from textual.keys import _character_to_key
from textual.screen import Screen
from textual.widgets import Button

from aisquare.services import first_run
from aisquare.services.fleet import _agent_running
from tests import demo_tape
from tests.demo_tape import Command, parse

REPO = Path(__file__).resolve().parents[1]
TAPE = REPO / "docs" / "demo.tape"
STAND_IN = REPO / "docs" / "demo" / "stand-in" / "claude"
SEED = REPO / "docs" / "demo" / "seed.sh"
UI = REPO / "src" / "aisquare" / "cli" / "ui"

WELCOME = UI / "views" / "welcome.py"

#: Where the walkthrough ends: the Welcome page's last step, a manager and two
#: coders live (welcome.py's FLEET_UP, "the stable string a recording can wait for").
END = "Your fleet is up"

#: The one file that prints each text the tape waits for, on the screen it waits
#: on. Checked against that file alone, docstrings and comments left out: against
#: the pooled UI, three of the first tape's six stayed found after their screen
#: stopped showing them (review of #250).
PRINTED_BY: dict[str, Path] = {
    "seeded:": SEED,
    "Pick the folder your agents will work in": WELCOME,
    "· this folder": WELCOME,
    "Choose another": WELCOME,
    "the manager gets its instructions through": WELCOME,
    "answer Claude Code's question": WELCOME,
    END: WELCOME,
}


def _tape() -> list[Command]:
    return parse(TAPE.read_text(encoding="utf-8"))


#: The coders the end screen shows, as the Welcome page labels them: first_run's
#: own labels, so a third coder or a new label format changes this list too.
_CODERS = list(first_run._free_labels("coder", first_run.CODERS, set()))

#: The end screen's disclosure that its "Claude Code" is the stand-in: step 2
#: prints the path the agent resolves to, and the stand-in's directory says so.
DISCLOSURE = "stand-in/claude"


def _bound() -> set[str]:
    """The keys the walkthrough's screens bind: the UI's own ``BINDINGS``, and
    Textual's for the two things the tape does with built-in widgets: move focus
    (Screen: tab) and press the focused button (Button: enter). Not Input's: the
    tape types into no box, and its editing keys would pass an arrow pressed at
    the page's buttons, which bind none (review of #256)."""
    keys = demo_tape.bound_keys(sorted(UI.rglob("*.py")))
    for widget in (Screen, Button):
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


def test_every_wait_is_text_its_screen_prints() -> None:
    """Each Wait against the file that draws it, and every Wait has one named."""
    waited = {text for c in _tape() if (text := demo_tape.pattern(c)) is not None}
    assert waited == set(PRINTED_BY), "name the file each Wait's text comes from"

    assert demo_tape.wait_problems(_tape(), PRINTED_BY) == []


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
    checkout has no execute bit to look at. The checked-out file can still lose
    it (an editor's atomic save under ``core.fileMode=false``); the seed's own
    check says so when it does.
    """
    staged = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "--stage", "--", "docs/demo/stand-in/claude"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert staged.startswith("100755 "), f"docs/demo/stand-in/claude is staged as {staged!r}"


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
#: same synthetic sources, bindings and end, so a rule that accuses everything
#: fails here rather than passing its positive control for free.
_GOOD = f"""\
Output out/demo.gif
Output out/demo.txt
Env COLORTERM "truecolor"
Type "asq"
Enter
Wait+Screen /aisquare fleet/
Down
Type "q"
Type "~/acme-api"
Wait+Line /{END}/
Sleep 1s
Screenshot out/welcome.png
Sleep 500ms
"""
_BOUND = {"down", "enter", "tab", "q"}


def _sh(body: str) -> str:
    return f"#!/bin/sh\n# a stand-in\nprintf 'banner'\n{body}\n"


def _source(tmp_path: Path, name: str, text: str) -> Path:
    """A real file for the wait rule to read, so its controls go through strings_in."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


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
#: shape -> (tape, the file named for the tape's text, that file's contents).
_WAIT_SHAPES = {
    "text the file does not print": (
        "Wait+Screen /aisquare fleet/\n",
        "ui.py",
        'TITLE = "asq"\n',
    ),
    "a Line wait for text the file does not print": (
        "Wait+Line /aisquare fleet/\n",
        "ui.py",
        'TITLE = "asq"\n',
    ),
    "text only in a module docstring": (
        "Wait+Screen /aisquare fleet/\n",
        "ui.py",
        '"""Draws aisquare fleet."""\nTITLE = "asq"\n',
    ),
    "text only in a function docstring": (
        "Wait+Screen /aisquare fleet/\n",
        "ui.py",
        'def title() -> str:\n    """Says aisquare fleet."""\n    return "asq"\n',
    ),
    "text only in an attribute docstring": (
        "Wait+Screen /aisquare fleet/\n",
        "ui.py",
        'TITLE = "asq"\n"""Once aisquare fleet."""\n',
    ),
    "text only in a script's comment": (
        "Wait+Screen /stand-in agent/\n",
        "claude",
        '# the stand-in agent\nline "demo agent: $name"\n',
    ),
    "a regex": ("Wait+Screen /aisquare.*fleet/\n", "ui.py", 'TITLE = "aisquare fleet"\n'),
    "an empty pattern": ("Wait+Screen //\n", "ui.py", 'TITLE = "aisquare fleet"\n'),
}
#: shape -> (file name, contents that must count as printing "aisquare fleet").
_DRAWN = {
    "a string constant": ("ui.py", 'TITLE = "aisquare fleet"\n'),
    "an f-string's literal part": (
        "ui.py",
        'def title(name: str) -> str:\n    return f"{name} aisquare fleet"\n',
    ),
    "a call's argument": ("ui.py", 'print("aisquare fleet")\n'),
    "a script's echo": ("claude", 'echo "aisquare fleet"\n'),
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
        f"Wait+Screen /{END}/\nWait+Screen /aisquare fleet/\nScreenshot out/a.png\nSleep 1s\n"
    ),
    "a key after the last Wait": f"Wait+Screen /{END}/\nEnter\nScreenshot out/a.png\nSleep 1s\n",
    "no Screenshot after the last Wait": (
        f"Screenshot out/a.png\nSleep 1s\nWait+Screen /{END}/\nSleep 1s\n"
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
    "exec with an option dash's exec does not take": "exec -a claude cat",
    "exec with an end-of-options marker": "exec -- cat",
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
def test_the_wait_rule_fires_on_each_shape(shape: str, tmp_path: Path) -> None:
    tape, name, text = _WAIT_SHAPES[shape]
    commands = parse(tape)
    waited = demo_tape.pattern(commands[0])
    assert waited is not None
    printed_by = {waited: _source(tmp_path, name, text)}

    assert demo_tape.wait_problems(commands, printed_by), f"missed: {shape}"


def test_the_wait_rule_fires_on_a_wait_no_file_is_named_for() -> None:
    """A new Wait has to name the file that prints it, or it is checked against nothing."""
    assert demo_tape.wait_problems(parse("Wait+Screen /aisquare fleet/\n"), {}) != []


@pytest.mark.parametrize("shape", sorted(_DRAWN))
def test_the_wait_rule_counts_text_that_is_drawn(shape: str, tmp_path: Path) -> None:
    """The negative control per kind of drawn text, through strings_in as the real
    check reads it; a bare Wait (the prompt) is not a pattern."""
    name, text = _DRAWN[shape]
    printed_by = {"aisquare fleet": _source(tmp_path, name, text)}

    assert demo_tape.wait_problems(parse("Wait+Screen /aisquare fleet/\nWait\n"), printed_by) == []


def test_the_wait_rule_accepts_a_sound_tape(tmp_path: Path) -> None:
    printed_by = {
        "aisquare fleet": _source(tmp_path, "ui.py", 'TITLE = "aisquare fleet"\n'),
        END: _source(tmp_path, "fleet.py", 'FLEET_UP = "Your fleet is up."\n'),
    }

    assert demo_tape.wait_problems(parse(_GOOD), printed_by) == []


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
    """``"" == ""`` and ``"" in snapshot`` are always true: an empty end must not pass."""
    assert demo_tape.ending_problems(parse(_GOOD), "") != []


@pytest.mark.parametrize("shape", sorted(_STAND_IN_SHAPES))
def test_the_stand_in_rule_fires_on_each_shape(shape: str) -> None:
    found = demo_tape.stand_in_problems(_sh(_STAND_IN_SHAPES[shape]), _agent_running)

    assert found, f"missed: {shape}"


@pytest.mark.parametrize("ending", ["exec cat", "exec /bin/cat"])
def test_the_stand_in_rule_accepts_a_program_the_fleet_takes_for_an_agent(ending: str) -> None:
    assert demo_tape.stand_in_problems(_sh(ending), _agent_running) == []


# ------------------------------------------------------ the render's snapshots


def _render(*shown: str) -> str:
    """A ``.txt`` Output as vhs writes it: each snapshot followed by the rule line."""
    return "".join(f"{snapshot}\n{demo_tape.SNAPSHOT_RULE}\n" for snapshot in shown)


#: A traceback in any slice a snapshot can hold: the header, or a frame line from
#: the middle once the header has scrolled away or wrapped (review of #250).
_SNAPSHOT_SHAPES = {
    "a Python traceback's header": _render(
        "aisquare fleet", "Traceback (most recent call last):", END
    ),
    "a Python traceback's middle": _render(
        "aisquare fleet", '  File "/opt/asq/lib/emit.py", line 180, in emit_doctor', END
    ),
    "a Rich traceback's header": _render(
        "aisquare fleet", "╭─ Traceback (most recent call last) ─╮", END
    ),
    "a Rich traceback's middle": _render(
        "aisquare fleet", "│ ❱ 180 │   emit_doctor(checks)            │", END
    ),
    "a last snapshot that left the end": _render(END, "$ "),
    "nothing recorded": "",
    "only blank snapshots": _render("", "   "),
}
#: What the walkthrough draws that comes closest to the markers, and must pass.
_SOUND_RENDERS = {
    "the walkthrough's own screens": _render("aisquare fleet", f"{END}: manager"),
    "prose about files and lines": _render(
        "File a bug if line 3 of the log, in red, says so", f"{END}: manager"
    ),
    "the arrows and disclosures the UI draws": _render(
        "✓ spawned manager → asq-mighty-gibbon %0", "▸ acme-api ▾", f"{END}: manager"
    ),
}


@pytest.mark.parametrize("shape", sorted(_SNAPSHOT_SHAPES))
def test_the_snapshot_check_fires_on_each_shape(shape: str) -> None:
    assert demo_tape.snapshot_problems(_SNAPSHOT_SHAPES[shape], [END]), f"missed: {shape}"


@pytest.mark.parametrize("shape", sorted(_SOUND_RENDERS))
def test_the_snapshot_check_accepts_a_sound_render(shape: str) -> None:
    assert demo_tape.snapshot_problems(_SOUND_RENDERS[shape], [END]) == []


@pytest.mark.parametrize(
    "last",
    [f"{END}. manager", f"{END}. manager coder-1", f"{END}. manager coder-2"],
)
def test_the_snapshot_check_fires_on_an_end_screen_missing_a_coder(last: str) -> None:
    assert demo_tape.snapshot_problems(_render("aisquare fleet", last), [END, *_CODERS])


def test_the_snapshot_check_accepts_an_end_screen_with_both_coders() -> None:
    last = f"{END}. ✓ manager ✓ coder-1 ✓ coder-2"

    assert demo_tape.snapshot_problems(_render("aisquare fleet", last), [END, *_CODERS]) == []


def test_the_snapshot_check_refuses_an_empty_end_text() -> None:
    """``"" in snapshot`` is always true: an empty text to look for must not pass."""
    assert demo_tape.snapshot_problems(_render(END), [END, ""]) != []


def test_snapshots_split_on_the_rule_line_and_only_on_it() -> None:
    """vhs's rule is 80 U+2500 alone on a line; a box the UI draws is not one."""
    border = "╭" + "─" * 80 + "╮"

    assert demo_tape.snapshots(_render("first", "second")) == ["first", "second"]
    assert demo_tape.snapshots(f"{border}\nboxed") == [f"{border}\nboxed"]


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


def test_render_sh_checks_the_coders_and_the_stand_in_on_the_end_screen() -> None:
    """The texts render.sh passes after the tape are the coders the page starts and
    the stand-in's disclosure: read back from render.sh, not copied (review of #256)."""
    script = (REPO / "docs" / "demo" / "render.sh").read_text(encoding="utf-8")
    joined = script.replace("\\\n", " ")
    commands = [line for line in joined.splitlines() if not line.lstrip().startswith("#")]
    calls = [line for line in commands if "-m tests.demo_tape" in line]
    assert len(calls) == 1, calls
    words = calls[0].split()
    passed = words[words.index("docs/demo.tape") + 1 :]

    assert passed == [*_CODERS, DISCLOSURE]
    assert STAND_IN.as_posix().endswith(f"/{DISCLOSURE}"), "the stand-in moved: update DISCLOSURE"


def test_the_render_check_reads_the_end_screen_texts_render_sh_passes(tmp_path: Path) -> None:
    """render.sh passes the coders' labels after the tape; without them on the last
    snapshot the check fails, with them it passes."""
    rendered = tmp_path / "demo.txt"
    rendered.write_text(_render(f"{END}. manager coder-1"), encoding="utf-8", newline="\n")
    complete = tmp_path / "complete.txt"
    complete.write_text(_render(f"{END}. manager coder-1 coder-2"), encoding="utf-8", newline="\n")

    assert demo_tape.main([str(rendered), str(TAPE), *_CODERS]) == 1
    assert demo_tape.main([str(complete), str(TAPE), *_CODERS]) == 0


# ------------------------------------------------------------------- the seed

_POSIX_SH = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("sh") is None,
    reason="the seed is POSIX sh for the Linux render image; Windows has no sh and no "
    "execute bits for it to read",
)


def _source_seed(
    tmp_path: Path, *, stand_in_mode: int | None, in_image: bool = True
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Source a copy of the seed from a scratch checkout, as the tape's hidden opening
    does, with ``HOME`` a fresh directory under ``tmp_path``."""
    checkout = tmp_path / "checkout"
    (checkout / "docs" / "demo" / "stand-in").mkdir(parents=True)
    shutil.copyfile(SEED, checkout / "docs" / "demo" / "seed.sh")
    if stand_in_mode is not None:
        stand_in = checkout / "docs" / "demo" / "stand-in" / "claude"
        shutil.copyfile(STAND_IN, stand_in)
        stand_in.chmod(stand_in_mode)
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home)}
    if in_image:
        env["AISQUARE_DEMO_IMAGE"] = "1"
    result = subprocess.run(
        ["sh", "-c", ". docs/demo/seed.sh"],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return result, home


@_POSIX_SH
def test_the_seed_refuses_outside_the_render_image(tmp_path: Path) -> None:
    """It points PATH at a stand-in and writes into HOME: never on a workstation."""
    result, home = _source_seed(tmp_path, stand_in_mode=0o755, in_image=False)

    assert result.returncode != 0
    assert "only inside the demo image" in result.stderr
    assert list(home.iterdir()) == []


@_POSIX_SH
def test_the_seed_names_a_stand_in_that_cannot_run(tmp_path: Path) -> None:
    """A stand-in that lost its execute bit is said to be that, not a wrong directory
    (review of #250): the tape sources the seed from the repository root."""
    result, home = _source_seed(tmp_path, stand_in_mode=0o644)

    assert result.returncode != 0
    assert "not executable" in result.stderr
    assert "repository root" not in result.stderr
    assert list(home.iterdir()) == []


@_POSIX_SH
def test_the_seed_names_a_wrong_directory(tmp_path: Path) -> None:
    result, home = _source_seed(tmp_path, stand_in_mode=None)

    assert result.returncode != 0
    assert "repository root" in result.stderr
    assert list(home.iterdir()) == []


@_POSIX_SH
@pytest.mark.skipif(shutil.which("git") is None, reason="the sample project is a git repository")
def test_the_seed_leaves_an_empty_home_with_the_sample_project(tmp_path: Path) -> None:
    """The negative control for the three refusals: the image, the root, the bit."""
    result, home = _source_seed(tmp_path, stand_in_mode=0o755)

    assert result.returncode == 0, result.stderr
    assert "seeded:" in result.stdout
    assert sorted(path.name for path in home.iterdir()) == ["acme-api"]
    assert (home / "acme-api" / ".git").is_dir()
