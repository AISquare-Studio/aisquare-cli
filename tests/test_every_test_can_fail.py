"""No test in this suite may be incapable of failing.

@dfd9a883 carried the residual: "REQUIRED_CLAIMS pins that a test EXISTS, not
that it still asserts anything — gutting a body to `assert True` passes."
Reproduced at 1b26c8d: `test_two_sessions_do_not_share_an_id` cut from eleven
lines to `assert True`, and BOTH meta-guards stayed green — the claims registry
in `test_correlation_spine.py`, which checks the function is still defined, and
`test_docs_cite_files_that_exist.py`, which checks the file is still there.
Eleven passed. The doctrine section would still have said that clause has a
test behind it.

This is the layer under both: a name that resolves and a file that exists prove
nothing if the body cannot fail. Measured across the whole suite — 997 test
functions, ONE not falsifiable, and that one asserts by not raising, which is a
real assertion written differently.

WHAT COUNTS AS ABLE TO FAIL: an `assert` whose expression is not a truthy
constant, a `pytest.raises`/`warns` block, or a call to `pytest.fail`. What does
not: `assert True`, `assert 1`, `pass`, or a body with no assertion at all.

WHAT THIS DOES NOT CATCH, said plainly because the gap is the same shape one
level in: WEAKENING. `assert result is not None` where the intent was
`assert result == expected` is falsifiable and passes here. This catches a test
being GUTTED, not a test being made lenient — and gutting is the one that
happens by accident, when someone silences a failure to get a branch green.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).parent

#: Tests whose assertion is that a call does NOT raise. That is a real
#: assertion — the test fails if the call throws — expressed without the
#: keyword, so the rule below cannot see it. Recorded rather than excused by a
#: looser rule: one broad exception is how a guard stops guarding, which is the
#: census defect from earlier tonight.
ASSERTS_BY_NOT_RAISING = {
    "test_console.py::test_a_non_wrapper_stream_is_ignored": (
        "calls _ensure_utf8 on a StringIO and fails if it raises"
    ),
}

_RAISING_HELPERS = {"raises", "warns", "fail"}


def _can_fail(function: ast.FunctionDef) -> bool:
    """Whether anything in this body could make the test fail."""
    for node in ast.walk(function):
        if isinstance(node, ast.Assert):
            if isinstance(node.test, ast.Constant) and node.test.value:
                continue  # `assert True` — cannot fail
            return True
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in _RAISING_HELPERS:
                return True
        if isinstance(node, ast.With | ast.AsyncWith):
            for item in node.items:
                if isinstance(item.context_expr, ast.Call):
                    called = getattr(item.context_expr.func, "attr", None)
                    if called in _RAISING_HELPERS:
                        return True
    return False


def _test_functions() -> list[tuple[str, ast.FunctionDef]]:
    found: list[tuple[str, ast.FunctionDef]] = []
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                found.append((f"{path.name}::{node.name}", node))
    return found


def test_no_test_is_incapable_of_failing() -> None:
    """The property the whole file exists for."""
    unable = [
        name
        for name, function in _test_functions()
        if not _can_fail(function) and name not in ASSERTS_BY_NOT_RAISING
    ]

    assert not unable, (
        f"these tests cannot fail, whatever the code does: {sorted(unable)}\n"
        "A test that cannot fail still counts in the total, still satisfies a "
        "claims registry, and still makes a document's citation look honest.\n"
        "If the assertion is that a call does not raise, add it to "
        "ASSERTS_BY_NOT_RAISING with the reason — do not loosen the rule."
    )


def test_the_predicate_recognises_the_shapes_that_cannot_fail() -> None:
    """Unit-test the rule itself, against the exact gutting that motivated it.

    Asserted on synthetic bodies rather than by editing a real test, so this
    proves the predicate without a file the suite also runs.
    """
    cannot = ["assert True", "assert 1", "pass", "x = 2 + 2"]
    can = [
        "assert x == 1",
        "assert not unable, 'why'",
        "with pytest.raises(ValueError):\n        boom()",
        "pytest.fail('no')",
    ]

    for body in cannot:
        node = ast.parse(f"def test_x():\n    {body}\n").body[0]
        assert isinstance(node, ast.FunctionDef)
        assert not _can_fail(node), f"{body!r} was read as able to fail"
    for body in can:
        node = ast.parse(f"def test_x():\n    {body}\n").body[0]
        assert isinstance(node, ast.FunctionDef)
        assert _can_fail(node), f"{body!r} was read as unable to fail"


def test_the_recorded_exceptions_still_exist() -> None:
    """A stale exception is a hiding place.

    If that test is renamed or deleted, its entry becomes an unexamined claim
    that some test is fine without an assertion — and the renamed one walks out
    of coverage with nothing said.
    """
    known = {name for name, _function in _test_functions()}

    missing = sorted(name for name in ASSERTS_BY_NOT_RAISING if name not in known)

    assert not missing, f"recorded as asserting-by-not-raising but gone: {missing}"


def test_the_sweep_is_looking_at_the_suite() -> None:
    """Guard the guard: an empty walk satisfies every assertion above."""
    found = _test_functions()

    assert len(found) >= 900, f"only {len(found)} test functions found — the walk broke"
    names = {name for name, _function in found}
    assert any(name.startswith("test_correlation_spine.py::") for name in names), (
        "the file whose gutting motivated this is not being swept"
    )
