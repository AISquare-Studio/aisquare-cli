"""Every fleet command a person or an agent is told to run names the project it is about.

Without ``--project`` a fleet command acts on the active project, which is the machine-wide
pin before the working directory (``core.workspace.active_project``). A hint written by hand
as ``aisquare fleet reap`` therefore reaped whichever project was pinned in some other shell,
not the one the message was about. Rounds 19 and 20 of the review of #259 fixed it one site at
a time, and round 21 found five more. So the rule is enforced over the source:
``services.project.fleet_command`` builds each one, and no string literal holds a hand-built
``aisquare fleet <verb>``. The exceptions are the sweeps of every project (``reap --all``,
``shutdown --all``) and the few literals in :data:`EXEMPT`: no command to run, or one about
no project in particular.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "aisquare"

_HAND_BUILT = re.compile(r"\baisquare fleet (?!(?:reap|shutdown) --all\b)[a-z]")
"""``aisquare fleet`` and a word: any verb, except the two that walk every project."""

_NO_PROJECT_IN_QUESTION = (
    "the fleet UI's refusal: it covers every project, and the listing it points to is the "
    "JSON form of whichever one the reader asks for"
)
EXEMPT: dict[tuple[str, str], str] = {
    (
        "services/fleet.py",
        "# aisquare fleet worktrees (docs/plans/fleet-tui.md §3.5)\n{…}\n",
    ): "the comment written above the worktree root's line in .git/info/exclude",
    (
        "cli/fleet.py",
        "the fleet UI has no --json form — for machine-readable fleet state use "
        "`aisquare fleet ls --json`",
    ): _NO_PROJECT_IN_QUESTION,
    (
        "cli/fleet.py",
        "the fleet UI needs an interactive terminal ({…}) — run `aisquare` in a terminal, or "
        "use `aisquare fleet ls --json`",
    ): _NO_PROJECT_IN_QUESTION,
}
"""Literals that hold ``aisquare fleet <word>`` and are no hint to name a project in, with
why. Keyed by the literal's text, so a change to one is looked at again."""


def _literals(tree: ast.AST) -> Iterator[tuple[int, str]]:
    """Each string literal's line and text, f-strings with their fields left out — every
    one but a docstring, or any other string standing as a statement of its own."""
    standing = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant | ast.JoinedStr)
    }
    parts: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts.update(id(value) for value in node.values)
    for node in ast.walk(tree):
        if id(node) in standing or id(node) in parts:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(
                str(value.value) if isinstance(value, ast.Constant) else "{…}"
                for value in node.values
            )
            yield node.lineno, text


def _hand_built() -> list[str]:
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        where = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for line, text in _literals(tree):
            if _HAND_BUILT.search(text) and (where, text) not in EXEMPT:
                found.append(f"{where}:{line}: {text!r}")
    return found


def test_no_fleet_command_hint_is_built_by_hand() -> None:
    assert _hand_built() == []


def test_every_exemption_is_still_in_the_source() -> None:
    """An exemption whose literal is gone or changed is dropped, not left to cover another."""
    found: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        where = path.relative_to(SRC).as_posix()
        found.update((where, text) for _line, text in _literals(ast.parse(path.read_text("utf-8"))))
    assert set(EXEMPT) - found == set()


def test_the_check_finds_a_hand_built_hint_and_passes_what_it_should() -> None:
    """The check itself: a bare verb, a placeholder and an f-string are each found; a sweep
    of every project, a docstring and an attribute's docstring are not."""
    module = '''
"""aisquare fleet reap in a docstring."""
A = "run `aisquare fleet reap` to remove it"
B = f"`aisquare fleet restart {label}`"
C = "aisquare fleet " "tell <label> …"
D = "`aisquare fleet reap --all` and `aisquare fleet shutdown --all --yes`"
E = 1
"""aisquare fleet stop, an attribute's docstring."""
F = "`aisquare fleet ls --all` is this project's, ended rows included"
'''
    hits = [text for _line, text in _literals(ast.parse(module)) if _HAND_BUILT.search(text)]
    assert hits == [
        "run `aisquare fleet reap` to remove it",
        "`aisquare fleet restart {…}`",
        "aisquare fleet tell <label> …",
        "`aisquare fleet ls --all` is this project's, ended rows included",
    ]
