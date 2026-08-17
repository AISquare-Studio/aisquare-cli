"""The gate must refuse to grade a tree it is not importing.

`tests/test_packaging.py` has asserted this property for a while, and it works —
run the FULL suite against a stale installed package and it fails, deterministically,
with the path in the message. But a test only runs when it is selected, and the
invocation that gets this wrong is the one nobody selects it with.

Measured at 8fafdd4 in a fresh worktree with no `.venv`, under the pyenv shim:

    make test                     -> 17 collection errors, exit 2      (loud)
    pytest tests/test_packaging.py -> FAILS, naming site-packages       (loud)
    pytest tests/test_config.py   -> 5 passed                          (SILENT)

The third line is the defect. `pytest <one file>` is what every session types
dozens of times while iterating, and against a stale install it reports green
having graded something else entirely. The session-start hook in conftest closes
it: no selection can skip a hook.

This file guards the hook, since a hook that stops firing takes its protection
with it and nothing else would notice.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import ModuleType

CONFTEST = Path(__file__).resolve().parent / "conftest.py"


def _load_conftest() -> ModuleType:
    """Load the real conftest by PATH, not by module name.

    `import conftest` is tempting and does not work: pytest registers the file
    as a plugin rather than putting `tests/` on sys.path, so the import fails at
    run time, and mypy cannot resolve it either. Loading the file on disk is
    both version-independent and the honest thing to assert about — the
    protection is whatever that file contains, not whatever a name happens to
    resolve to.
    """
    spec = importlib.util.spec_from_file_location("conftest_under_test", CONFTEST)
    assert spec is not None and spec.loader is not None, f"cannot load {CONFTEST}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


conftest = _load_conftest()


def test_a_foreign_package_is_reported() -> None:
    """The failure case, with the shape that actually occurs."""
    reason = conftest._foreign_package_reason(
        Path("/home/user/.pyenv/versions/3.12.3/lib/python3.12/site-packages/aisquare/__init__.py"),
        Path("/repo/src"),
    )

    assert reason is not None
    assert "site-packages" in reason
    assert "/repo/src" in reason


def test_this_tree_is_not_reported() -> None:
    """The passing case must actually pass, or the guard blocks every run."""
    assert (
        conftest._foreign_package_reason(Path("/repo/src/aisquare/__init__.py"), Path("/repo/src"))
        is None
    )


def test_the_reason_carries_the_fix_and_the_trap() -> None:
    """A refusal that does not say how to proceed just moves the confusion.

    The PATH prefix specifically must be called out: it is the documented
    incantation, and in a fresh worktree it silently does nothing because
    `.venv` does not exist yet. Someone hitting this guard has very likely just
    typed exactly that.
    """
    reason = conftest._foreign_package_reason(Path("/elsewhere/aisquare/__init__.py"), Path("/s"))

    assert reason is not None
    assert "venv" in reason
    assert "make check" in reason
    assert "falls through" in reason, "the reason must name the trap, not just the remedy"


def test_the_hook_is_wired_and_exits_nonzero() -> None:
    """Asserted on the SOURCE, because importing conftest cannot show the wiring.

    The protection is `pytest_sessionstart` calling the checker and exiting. If
    a later edit keeps the function but drops the hook, every subset run goes
    back to grading whatever it happens to import, and no test would fail.
    """
    tree = ast.parse(CONFTEST.read_text(encoding="utf-8"))
    hooks = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "pytest_sessionstart"
    ]
    assert hooks, "conftest lost its pytest_sessionstart hook — subset runs are unguarded again"

    body = ast.dump(hooks[0])
    assert "_foreign_package_reason" in body, "the hook no longer consults the checker"
    assert "exit" in body, "the hook no longer stops the session"


def test_the_full_suite_guard_still_exists() -> None:
    """Do not let this file become an excuse to delete the older assertion.

    Two instruments on the same property is not duplication here: the hook stops
    a run before it starts, and the test states the invariant where a reader
    looking for it in the test suite would find it.
    """
    packaging = CONFTEST.parent / "test_packaging.py"
    source = packaging.read_text(encoding="utf-8")

    assert "test_the_suite_imports_this_tree_not_an_installed_copy" in source
    assert "is_relative_to" in source
