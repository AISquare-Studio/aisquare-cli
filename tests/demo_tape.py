"""Read docs/demo.tape, and check what a render of it recorded.

The tape is the README's GIF and a test of the walkthrough it shows: vhs exits
1 when a ``Wait`` does not see its text in time. That half runs only where vhs
does, in docs/demo/render.sh's image, which .github/workflows/demo.yml runs on
the pull requests that touch the demo or the package. This module is the half
that runs everywhere:

- **The tape, statically.** :func:`parse` reads a tape into commands, and each
  ``*_problems`` rule says what in them cannot work. tests/test_demo_tape.py
  runs every rule over docs/demo.tape on every leg of the suite, with a
  positive control per shape and a negative control per rule.
- **The render.** :func:`snapshot_problems` reads the ``.txt`` Output and says
  whether a snapshot shows a traceback and whether the last one is still the
  screen the walkthrough ends on. render.sh runs it as
  ``python -m tests.demo_tape out/demo.txt docs/demo.tape``.

  The ``.txt`` is not every frame of the GIF. vhs writes one snapshot of the
  screen after each command of the tape (45 here; the GIF has ~530 frames),
  and a Wait's snapshot is taken once its text is already on screen. What is on
  screen only while a Wait polls, such as the Onboard view's log of ``init`` and
  ``doctor``, is in no snapshot, so render.sh asks doctor again itself. When a
  Wait times out, the file stops at the command before it, and vhs's error
  prints the screen it gave up on.

Standard library only: render.sh runs it on the render image's Python, which
has neither pytest nor this checkout's environment.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Command:
    """One command of a tape: ``Wait+Screen@10s /text/`` is Wait, Screen, ``/text/``."""

    line: int
    name: str
    target: str
    """What a ``Wait`` watches (``Screen`` or ``Line``); empty for everything else."""
    argument: str


def parse(text: str) -> list[Command]:
    """The commands of a tape, comments and blank lines left out."""
    commands: list[Command] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        word, _, argument = stripped.partition(" ")
        word = word.split("@", 1)[0]  # Type@100ms, Wait+Screen@10s: the timing is not the name
        name, target = word, ""
        if word.startswith("Wait+"):
            name, target = "Wait", word[len("Wait+") :]
        commands.append(Command(number, name, target, argument.strip()))
    return commands


def quoted(argument: str) -> str:
    """The text of a ``Type`` or ``Env`` value: vhs quotes with ``"``, ``'`` or a backtick."""
    if len(argument) >= 2 and argument[0] == argument[-1] and argument[0] in "\"'`":
        return argument[1:-1]
    return argument


def pattern(command: Command) -> str | None:
    """The ``/text/`` a ``Wait`` waits for; ``None`` for a bare ``Wait`` (the prompt)."""
    argument = command.argument
    if command.name == "Wait" and len(argument) >= 2 and argument[0] == argument[-1] == "/":
        return argument[1:-1]
    return None


def seconds(argument: str) -> float | None:
    """``500ms`` is 0.5, ``1.5s`` is 1.5, and a bare number is seconds, as vhs reads them."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m)?", argument.strip())
    if match is None:
        return None
    value = float(match.group(1))
    unit = match.group(2) or "s"
    return value / 1000 if unit == "ms" else value * 60 if unit == "m" else value


# --------------------------------------------------------------------- outputs


def output_problems(commands: Sequence[Command]) -> list[str]:
    """Outputs and Screenshots are relative paths under ``out/``, and there is a GIF and a .txt.

    A leading ``/`` is not a path to vhs: it lexes ``/…/`` as a regex and the
    tape fails to parse (measured with ``vhs validate``: "Invalid command").
    ``out/`` is where render.sh clears and the workflow uploads from, and the
    ``.txt`` is what :func:`snapshot_problems` reads.
    """
    problems: list[str] = []
    outputs: list[str] = []
    for command in commands:
        if command.name not in ("Output", "Screenshot"):
            continue
        path = command.argument
        if path.startswith("/"):
            problems.append(
                f"line {command.line}: {command.name} {path} is absolute, and vhs reads "
                "a leading / as a regex"
            )
        elif not path.startswith("out/"):
            problems.append(
                f"line {command.line}: {command.name} {path} is outside out/, "
                "where render.sh and the workflow collect"
            )
        if command.name == "Output":
            outputs.append(path)
    for suffix in (".gif", ".txt"):
        if not any(path.endswith(suffix) for path in outputs):
            problems.append(f"no Output ends in {suffix}")
    return problems


# ---------------------------------------------------------------------- colour


def colour_problems(commands: Sequence[Command]) -> list[str]:
    """``Env COLORTERM "truecolor"`` is set: without it Textual draws in 256 colours.

    Measured: the footer turns navy. The image sets it too, but the tape is
    what someone reads to learn what the render needs.
    """
    values = [
        quoted(value)
        for command in commands
        if command.name == "Env"
        for key, _, value in [command.argument.partition(" ")]
        if key == "COLORTERM"
    ]
    if not values:
        return ['no Env COLORTERM "truecolor": Textual falls back to 256 colours']
    return [
        f'Env COLORTERM "{value}" is not "truecolor"' for value in values if value != "truecolor"
    ]


# ----------------------------------------------------------------------- waits

#: Characters that make a Wait pattern a regex rather than text. A pattern is
#: checked against the source only as text, so a regex is refused, not guessed at.
_REGEX_SYNTAX = re.compile(r"[\\^$.|?*+()\[\]{}]")


def strings_in(path: Path) -> list[str]:
    """What ``path`` can put on screen: a Python file's strings, a script's lines.

    Text that is never drawn is left out, so prose cannot satisfy the guard. In
    Python that is a docstring, or any string that is a statement of its own; in
    a script, a whole-line ``#`` comment. Measured in the review of #250: a
    docstring in cli/ui/app.py kept "has no manager yet" found after the Manager
    tab stopped saying it. The literal parts of f-strings are drawn, so
    ``f"{name} has no manager yet."`` yields ``" has no manager yet."``.
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix != ".py":
        return [line for line in text.splitlines() if not line.strip().startswith("#")]
    tree = ast.parse(text)
    statements = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Expr)}
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in statements
    ]


def wait_problems(commands: Sequence[Command], printed_by: Mapping[str, Path]) -> list[str]:
    """Each Wait pattern is plain text that the one file named for it prints.

    So copy that changes under the tape fails here, on every leg, and not only
    in the render, a minute in, as a timeout. One file per text, the one that
    draws it on the screen the tape is waiting for, not the whole UI: pooled,
    "aisquare fleet" stayed found through the help screen and a toast after
    Welcome's title changed (review of #250). A Wait with no file named for it
    is a problem too, so a new Wait has to say what prints it. A bare ``Wait``
    waits for the shell's prompt and is left alone.
    """
    problems: list[str] = []
    for command in commands:
        text = pattern(command)
        if text is None:
            continue
        where = f"line {command.line}: Wait /{text}/"
        if not text.strip():
            problems.append(f"{where} matches any screen")
        elif _REGEX_SYNTAX.search(text):
            problems.append(f"{where} is a regex; wait for plain text this guard can find")
        elif (source := printed_by.get(text)) is None:
            problems.append(f"{where}: no file is named as the one that prints it")
        elif not any(text in string for string in strings_in(source)):
            problems.append(f"{where}: {source.name} does not print that text")
    return problems


# ------------------------------------------------------------------------ keys

#: vhs's key commands, by the name Textual gives the key.
KEYS: dict[str, str] = {
    "Enter": "enter",
    "Tab": "tab",
    "Space": "space",
    "Backspace": "backspace",
    "Delete": "delete",
    "Insert": "insert",
    "Escape": "escape",
    "Up": "up",
    "Down": "down",
    "Left": "left",
    "Right": "right",
    "PageUp": "pageup",
    "PageDown": "pagedown",
}
_MODIFIERS: dict[str, str] = {"Ctrl": "ctrl", "Alt": "alt", "Shift": "shift"}

#: vhs's commands that press no key.
COMMANDS = frozenset(
    {
        "Output",
        "Require",
        "Set",
        "Env",
        "Sleep",
        "Type",
        "Hide",
        "Show",
        "Wait",
        "Screenshot",
        "Copy",
        "Paste",
        "Source",
    }
)


def key_of(command: Command) -> str | None:
    """The Textual key a key command presses (``Ctrl+C`` is ``ctrl+c``); ``None`` if no key."""
    *modifiers, last = command.name.split("+")
    if any(modifier not in _MODIFIERS for modifier in modifiers):
        return None
    if last in KEYS:
        key = KEYS[last]
    elif modifiers and len(last) == 1:
        key = last.lower()
    else:
        return None
    return "+".join([*(_MODIFIERS[modifier] for modifier in modifiers), key])


def _bindings_value(node: ast.AST) -> ast.expr | None:
    """The value of ``BINDINGS = …`` or ``BINDINGS: ClassVar = …``; ``None`` for anything else."""
    targets: list[ast.expr]
    value: ast.expr | None
    if isinstance(node, ast.Assign):
        targets, value = node.targets, node.value
    elif isinstance(node, ast.AnnAssign):
        targets, value = [node.target], node.value
    else:
        return None
    named = any(isinstance(target, ast.Name) and target.id == "BINDINGS" for target in targets)
    return value if named else None


def bound_keys(sources: Iterable[Path]) -> set[str]:
    """Every key a ``BINDINGS`` list in ``sources`` binds; ``"G,shift+g"`` is two keys."""
    keys: set[str] = set()
    for path in sources:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            value = _bindings_value(node)
            if not isinstance(value, ast.List | ast.Tuple):
                continue
            for item in value.elts:
                first: ast.expr | None = None
                if isinstance(item, ast.Tuple) and item.elts:
                    first = item.elts[0]
                elif isinstance(item, ast.Call):
                    first = item.args[0] if item.args else None
                    for keyword in item.keywords:
                        if keyword.arg == "key":
                            first = keyword.value
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    keys.update(key.strip() for key in first.value.split(","))
    return keys


def key_problems(
    commands: Sequence[Command], bound: Collection[str], char_key: Callable[[str], str]
) -> list[str]:
    """Each key the tape presses is bound, and so is each one-character ``Type``.

    One character typed at the UI is a key press, not text: ``Type "+"`` on
    Welcome did nothing and the next Wait ran out (measured), because nothing
    binds ``plus``. ``char_key`` is how Textual names the key a character is.
    A command this guard does not know is a problem too, so a typo is not
    skipped as "not a key".
    """
    problems: list[str] = []
    for command in commands:
        key = key_of(command)
        if command.name == "Type":
            text = quoted(command.argument)
            if len(text) == 1:
                key = char_key(text)
        elif key is None and command.name not in COMMANDS:
            problems.append(f"line {command.line}: {command.name} is not a vhs command or key")
            continue
        if key is not None and key not in bound:
            problems.append(
                f"line {command.line}: {command.name} {command.argument}".rstrip()
                + f" presses {key!r}, which nothing on the walkthrough's screens binds"
            )
    return problems


# ----------------------------------------------------------------- screenshots

#: How long a Screenshot needs before the next key: vhs takes it on a later
#: frame, so a key pressed straight after lands in the picture.
SETTLE = 0.5


def screenshot_problems(commands: Sequence[Command]) -> list[str]:
    """Each Screenshot is followed by a Sleep of at least :data:`SETTLE` seconds.

    Measured: with the next key straight after, two stills of different
    screens came out byte-identical. A Screenshot that ends the tape may not be
    taken at all.
    """
    problems: list[str] = []
    for index, command in enumerate(commands):
        if command.name != "Screenshot":
            continue
        after = commands[index + 1] if index + 1 < len(commands) else None
        if after is None:
            problems.append(f"line {command.line}: the tape ends on the Screenshot")
        elif after.name != "Sleep":
            problems.append(
                f"line {command.line}: {after.name} straight after the Screenshot lands in it"
            )
        elif (wait := seconds(after.argument)) is None or wait < SETTLE:
            problems.append(
                f"line {after.line}: Sleep {after.argument} after the Screenshot is under "
                f"{SETTLE} s"
            )
    return problems


# ---------------------------------------------------------------------- ending


def end_text(commands: Sequence[Command]) -> str | None:
    """What the last Wait waits for: the screen the walkthrough ends on."""
    texts = [text for command in commands if (text := pattern(command)) is not None]
    return texts[-1] if texts else None


def ending_problems(commands: Sequence[Command], end: str) -> list[str]:
    """The tape ends where the walkthrough does, and the still is taken there.

    Its last Wait is for ``end``; after it come only Sleeps and Screenshots, so
    nothing moves the screen the GIF ends on; and one of them is a Screenshot.
    """
    if not end.strip():
        return ["no end text to look for: an empty one is found on every screen"]
    waits = [index for index, command in enumerate(commands) if pattern(command) is not None]
    if not waits:
        return ["the tape waits for no text, so nothing says where it ends"]
    last = commands[waits[-1]]
    problems: list[str] = []
    if pattern(last) != end:
        problems.append(f"line {last.line}: the last Wait is for /{pattern(last)}/, not /{end}/")
    tail = commands[waits[-1] + 1 :]
    problems.extend(
        f"line {command.line}: {command.name} after the last Wait moves the screen it ends on"
        for command in tail
        if command.name not in ("Sleep", "Screenshot")
    )
    if not any(command.name == "Screenshot" for command in tail):
        problems.append("no Screenshot after the last Wait, so the still is not the end")
    return problems


# -------------------------------------------------------------------- stand-in


def stand_in_problems(script: str, is_agent: Callable[[str], bool]) -> list[str]:
    """The stand-in's last line execs a program the fleet takes for the agent.

    ``is_agent`` is the fleet's own reading of a pane's foreground command
    (``services.fleet._agent_running``): a shell, tmux or Python is never the
    agent. A stand-in that loops in sh leaves sh in the foreground, so its row
    never reads as the agent and a prompt typed at spawn waits out the 20 s
    PROMPT_TIMEOUT first.

    The word after ``exec`` must be the program itself. The stand-in is
    ``#!/bin/sh``, which is dash in the render image, and dash's ``exec`` takes
    no options: ``exec -a claude cat`` runs a command named ``-a`` and exits 127
    after the banner has printed (measured; ``--`` fails the same way).
    """
    lines = [
        stripped
        for line in script.splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
    ]
    if not lines:
        return ["the stand-in is empty"]
    last = lines[-1]
    words = last.split()
    if words[0] != "exec":
        return [f"its last line, {last!r}, execs nothing: the shell stays in the foreground"]
    if len(words) < 2:
        return [f"its last line, {last!r}, names no program"]
    program = words[1]
    if program.startswith("-"):
        return [f"its last line, {last!r}, gives exec {program}, which dash runs as the command"]
    name = program.rsplit("/", 1)[-1]
    if not is_agent(name):
        return [f"its last line, {last!r}, leaves {name} in the foreground, which is not an agent"]
    return []


# ------------------------------------------------------------------- snapshots

#: The line vhs writes after each snapshot of a ``.txt`` Output (measured: 80 of
#: U+2500, alone on its line; no screen of the walkthrough draws one of its own).
SNAPSHOT_RULE = "─" * 80

#: What a traceback leaves on screen, whichever slice of it a snapshot holds. The
#: header alone is not enough: a long traceback scrolls it away, and a narrow box
#: such as the Onboard log (~46 columns) word-wraps it in two (review of #250).
#: So each frame line counts too: Python's ``File "…", line N, in …``, and the
#: ``❱`` (U+2771) Rich puts on the line that raised in every frame, a character
#: nothing in src/ draws.
TRACEBACK = re.compile(r'Traceback \(most recent call last\)|File "[^"]*", line \d+, in |❱')


def snapshots(text: str) -> list[str]:
    """The snapshots of a ``.txt`` Output that show anything, in order."""
    found: list[list[str]] = [[]]
    for line in text.splitlines():
        if line == SNAPSHOT_RULE:
            found.append([])
        else:
            found[-1].append(line)
    return [joined for lines in found if (joined := "\n".join(lines)).strip()]


def snapshot_problems(text: str, ends: Sequence[str]) -> list[str]:
    """No snapshot shows a traceback, and the last one still shows every text in ``ends``.

    ``ends`` is the end screen: the text the tape's last Wait waits for, and any
    other text that screen must show which no Wait can name, such as the labels
    the fleet gives its coders. The last snapshot is checked because a Wait
    passes the moment its text appears: an app that fell over just after would
    still have passed every Wait.
    """
    if not ends or not all(end.strip() for end in ends):
        return ["no end text to look for: an empty one is found in every snapshot"]
    shown = snapshots(text)
    if not shown:
        return ["no snapshots: the render recorded nothing"]
    problems = [
        f"snapshot {number} of {len(shown)} shows a traceback"
        for number, snapshot in enumerate(shown, start=1)
        if TRACEBACK.search(snapshot)
    ]
    problems.extend(
        f"the last snapshot does not show {end!r}, where the walkthrough ends"
        for end in ends
        if end not in shown[-1]
    )
    return problems


def main(argv: Sequence[str]) -> int:
    """``python -m tests.demo_tape <demo.txt> <tape> [text the end screen shows...]``.

    0 when the render is sound. The end screen is the tape's last Wait, plus any
    texts given after the tape.
    """
    if len(argv) < 2:
        print(
            "usage: python -m tests.demo_tape <demo.txt> <tape> [text the end screen shows...]",
            file=sys.stderr,
        )
        return 2
    rendered, tape = Path(argv[0]), Path(argv[1])
    end = end_text(parse(tape.read_text(encoding="utf-8")))
    if end is None:
        print(f"{tape}: waits for no text, so nothing says where it ends", file=sys.stderr)
        return 1
    ends = [end, *argv[2:]]
    text = rendered.read_text(encoding="utf-8")
    problems = snapshot_problems(text, ends)
    for problem in problems:
        print(f"{rendered}: {problem}", file=sys.stderr)
    if problems:
        return 1
    shows = ", ".join(repr(end) for end in ends)
    print(f"{rendered}: {len(snapshots(text))} snapshots, no traceback, the last shows {shows}")
    return 0


if __name__ == "__main__":  # pragma: no cover - docs/demo/render.sh's entry point
    raise SystemExit(main(sys.argv[1:]))
