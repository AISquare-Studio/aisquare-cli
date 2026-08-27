"""Every CLI call that can write config must translate its expected failures.

Two correct changes composed into a defect neither contained. One translated
``PermissionError`` at the boundaries it knew about; the other made a broken
dotfiles link raise ``FileNotFoundError`` rather than materialise a directory
tree. Both were tested, both were folded green, and the most foreseeable
failure on the whole dotfiles path printed 43 lines of traceback. The gate
proved correctness and could not see legibility.

Vigilance is not the fix — a guard is. This is the same shape as the spawn-seam
inventory: the SET of call sites is derived from the source rather than listed
here, so a new config-writing command fails this test instead of reaching an
operator. That is precisely how ``init`` was missed the first time (it writes
through ``lifecycle``, not through the modules its siblings share) and how
``config redaction`` was missed the second.

The writers themselves are derived too: any function in ``services/`` that
calls ``save_config`` is one, so adding a new mutator there and calling it from
a command is covered without editing this file.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "aisquare"
_GUARD = "expected_config_write_errors"


def _swallows(node: ast.Try) -> bool:
    """A handler that reports instead of re-raising ends the exception here."""
    return any(
        not any(isinstance(stmt, ast.Raise) for stmt in ast.walk(handler))
        for handler in node.handlers
    )


def _writes_and_propagates(func: ast.FunctionDef) -> bool:
    """Does a failed ``save_config`` in this function reach its CALLER?

    ``apply_fixes`` writes config and catches its own failure, reporting "could
    not write the config" as one of the actions it returns — the doctor must
    never crash. Nothing propagates, so requiring the CLI to translate there
    would assert a failure mode that cannot occur. Derived rather than
    allowlisted, so a future writer that starts or stops swallowing is
    reclassified without editing this file.
    """
    for outer in ast.walk(func):
        if not isinstance(outer, ast.Try) or not _swallows(outer):
            continue
        if any(
            isinstance(call, ast.Call) and getattr(call.func, "id", None) == "save_config"
            for stmt in outer.body
            for call in ast.walk(stmt)
        ):
            return False
    return any(
        isinstance(call, ast.Call) and getattr(call.func, "id", None) == "save_config"
        for call in ast.walk(func)
    )


def _functions_that_write_config() -> set[str]:
    """Service functions whose config-write failure reaches their caller."""
    writers: set[str] = {"save_config"}
    for path in sorted((_SRC / "services").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and _writes_and_propagates(node):
                writers.add(node.name)
    return writers


class _Sites(ast.NodeVisitor):
    """Calls to a config writer, and whether the guard lexically encloses them."""

    def __init__(self, module: str, writers: set[str]) -> None:
        self.module = module
        self.writers = writers
        self.guarded: set[str] = set()
        self.unguarded: set[str] = set()
        self._depth = 0

    def visit_With(self, node: ast.With) -> None:
        opens_guard = any(
            isinstance(item.context_expr, ast.Call)
            and getattr(item.context_expr.func, "id", None) == _GUARD
            for item in node.items
        )
        self._depth += 1 if opens_guard else 0
        self.generic_visit(node)
        self._depth -= 1 if opens_guard else 0

    def visit_Call(self, node: ast.Call) -> None:
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name in self.writers:
            where = f"{self.module}:{node.lineno} {name}"
            (self.guarded if self._depth else self.unguarded).add(where)
        self.generic_visit(node)


def _survey() -> tuple[set[str], set[str]]:
    writers = _functions_that_write_config()
    guarded: set[str] = set()
    unguarded: set[str] = set()
    for path in sorted((_SRC / "cli").glob("*.py")):
        visitor = _Sites(f"cli/{path.name}", writers)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        guarded |= visitor.guarded
        unguarded |= visitor.unguarded
    return guarded, unguarded


def test_the_survey_actually_finds_config_writers() -> None:
    """A guard that inspects nothing passes for the same reason a clean one does.

    Asserted before the claim it supports, because an AST walk over an empty set
    is indistinguishable from an AST walk over a compliant tree — this shift
    caught two guards certifying blind checkers that way.
    """
    writers = _functions_that_write_config()
    assert {"save_config", "set_value", "set_redaction", "bind_role"} <= writers, sorted(writers)

    guarded, unguarded = _survey()
    assert len(guarded | unguarded) >= 6, sorted(guarded | unguarded)


def test_every_cli_config_write_translates_its_expected_failures() -> None:
    """The durable half: a new command cannot ship without the translation.

    An unguarded write means an expected, documented failure — the config is not
    writable, or a followed symlink's directory is missing — reaches the
    operator as a Rich traceback instead of the one-line ✗ this CLI uses
    everywhere else.
    """
    _, unguarded = _survey()

    assert not unguarded, (
        "these CLI calls can write config outside "
        f"{_GUARD}(), so their expected failures print a traceback:\n  "
        + "\n  ".join(sorted(unguarded))
    )
