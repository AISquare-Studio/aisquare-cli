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
import subprocess
import sys
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


def _skips_unconditionally(function: ast.FunctionDef) -> bool:
    """Whether the first thing this test does is skip itself.

    A test that always skips passes without asserting anything, counts in the
    total, and satisfies a claims registry — the same three properties as a
    gutted one. I shipped one earlier tonight and removed it when @9bbc8ed7's
    phrase named it: "a test that always skips is dead weight pretending to be
    coverage". There are none today; this is the detector so there are none
    tomorrow either.
    """
    first = function.body[0] if function.body else None
    if not (isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)):
        return False
    called = getattr(first.value.func, "attr", None) or getattr(first.value.func, "id", None)
    return called == "skip"


def _has_empty_parameter_set(function: ast.FunctionDef) -> bool:
    """Whether a `parametrize` decorator hands this test an EMPTY set.

    The third way to be unable to fail, and the one that hid from both my
    guards. pytest COLLECTS such a test — it reports "got empty parameter set"
    and SKIPS it — so it has a node id, my collection comparison sees no
    asymmetry, and the body contains a perfectly good assertion that never
    executes. Measured: `@pytest.mark.parametrize("x", [])` runs zero cases and
    every check in this file passed it.

    Detectable only when the set is a LITERAL. A computed list that happens to
    be empty — a comprehension that filters everything out, a constant imported
    from elsewhere — is invisible here, and that limit is real: the shape most
    likely to become empty by accident is exactly the computed one.
    """
    for decorator in function.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        name = getattr(decorator.func, "attr", None) or getattr(decorator.func, "id", None)
        if name != "parametrize":
            continue
        for argument in decorator.args[1:]:
            if isinstance(argument, ast.List | ast.Tuple | ast.Set) and not argument.elts:
                return True
    return False


def _can_fail(function: ast.FunctionDef) -> bool:
    """Whether anything in this body could make the test fail."""
    if _skips_unconditionally(function) or _has_empty_parameter_set(function):
        return False
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
    """Every test in the suite, named the way PYTEST names it.

    `ast.walk` finds functions inside classes too, but flattening them to
    `file::name` gives an identity pytest does not use: 64 tests live in
    classes, and pytest calls them `file::Class::name`. Two consequences, one
    latent and one immediate.

    LATENT: a flattened name is not unique. Two classes in one file may each
    define `test_the_default_wins`, and a single entry in
    ASSERTS_BY_NOT_RAISING would then excuse BOTH — one exception silently
    covering two tests, which is the one-broad-exclusion disease in miniature.
    Measured: zero collisions today, so this is a detector rather than a fix.

    IMMEDIATE: a name this file prints in a failure could not be pasted into
    `pytest` to run the offending test. A guard whose output you have to
    translate by hand is one people stop reading.
    """
    found: list[tuple[str, ast.FunctionDef]] = []
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                found.append((f"{path.name}::{node.name}", node))
            elif isinstance(node, ast.ClassDef):
                for member in node.body:
                    if isinstance(member, ast.FunctionDef) and member.name.startswith("test_"):
                        found.append((f"{path.name}::{node.name}::{member.name}", member))
    return found


def _unable_to_fail(functions: list[tuple[str, ast.FunctionDef]]) -> list[str]:
    """The tests in `functions` that cannot fail, excluding recorded exceptions.

    EXTRACTED SO A CONTROL CAN CALL IT WITH KNOWN-BAD INPUT. This loop lived
    inline in the test body, and @9bbc8ed7 showed what that costs: one line
    inside such a loop makes it flag nothing while every surrounding check stays
    green. Reproduced here before fixing — `if False and …` left all five tests
    passing, because the only thing controlled was the PREDICATE.

    A predicate unit-tested on synthetic bodies proves the predicate. It says
    nothing about whether anything still CALLS it. @dfd9a883 established this is
    per-file work rather than a sweep, because each guard's loop has its own
    shape; this is that work for this file, and the census guard next door
    turned out to be immune already.
    """
    return [
        name
        for name, function in functions
        if not _can_fail(function) and name not in ASSERTS_BY_NOT_RAISING
    ]


def _functions_in(source: str, filename: str = "synthetic.py") -> list[tuple[str, ast.FunctionDef]]:
    """Parse a synthetic module into the shape `_unable_to_fail` consumes."""
    found: list[tuple[str, ast.FunctionDef]] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            found.append((f"{filename}::{node.name}", node))
    return found


def test_no_test_is_incapable_of_failing() -> None:
    """The property the whole file exists for."""
    unable = _unable_to_fail(_test_functions())

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

    empty_set = ast.parse(
        '@pytest.mark.parametrize("x", [])\ndef test_x(x):\n    assert x == 1\n'
    ).body[0]
    assert isinstance(empty_set, ast.FunctionDef)
    assert not _can_fail(empty_set), "an empty parameter set was read as able to fail"

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


def test_every_test_file_yields_at_least_one_test() -> None:
    """Guard the guard, WITHOUT a number anyone can lower.

    The first version asserted `len(found) >= 900`. That is the
    constant-that-can-be-lowered category I named two cycles ago and then
    committed again in the same file that closes the residual about guards
    which stop guarding — you cannot defend a constant by adding another
    constant, and `>= 900` becomes `>= 0` in one keystroke.

    So there is no threshold: EVERY `test_*.py` on disk must yield at least one
    test function. A walk that breaks yields zero from every file and fails by
    name; a file that loses its last test fails too, which is worth knowing on
    its own. Nothing to lower, because nothing is typed.
    """
    on_disk = sorted(path.name for path in TESTS.glob("test_*.py"))
    assert on_disk, "no test files found at all — TESTS is pointing somewhere wrong"

    swept = {name.split("::", 1)[0] for name, _function in _test_functions()}

    empty = [name for name in on_disk if name not in swept]
    assert not empty, (
        f"these test files yielded no test functions: {empty}. Either the sweep "
        "stopped parsing them — in which case every assertion in this file is "
        "passing over less than it claims — or they genuinely contain no tests."
    )


def test_the_sweep_sees_exactly_what_pytest_runs() -> None:
    """The universe question, asked of the instrument rather than assumed.

    Every assertion in this file is over tests found by parsing `tests/*.py`.
    If pytest runs something this sweep cannot see — a file excluded by
    configuration, a test generated at import time, a directory added to
    testpaths — then "no test is incapable of failing" is a claim about a
    smaller suite than the one that actually runs, and it would never say so.
    That is the narrow-universe defect this shift has produced in four separate
    instruments, including two of mine.

    Asked by collecting with pytest itself and comparing NODE IDS, which is
    also why `_test_functions` now names class-based tests the way pytest does.
    Parametrised cases collapse to their function id — a parametrisation is one
    body, and the body is what falsifiability is a property of.
    """
    collected: set[str] = set()
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(TESTS)],
        capture_output=True,
        text=True,
        cwd=TESTS.parent,
    )
    assert result.returncode == 0, f"collection failed:\n{result.stdout[-2000:]}"
    for line in result.stdout.splitlines():
        if "::" not in line:
            continue
        path, _, rest = line.partition("::")
        collected.add(f"{Path(path).name}::{rest.split('[')[0]}")

    swept = {name for name, _function in _test_functions()}

    assert collected, "pytest collected nothing — the comparison would be vacuous"
    unseen = sorted(collected - swept)
    assert not unseen, (
        f"pytest runs {len(unseen)} tests this sweep cannot see, e.g. {unseen[:5]}. "
        "Every assertion in this file is therefore about a smaller suite than "
        "the one that runs."
    )
    phantom = sorted(swept - collected)
    assert not phantom, (
        f"this sweep checks {len(phantom)} tests pytest never runs, e.g. "
        f"{phantom[:5]} — they are being audited and never executed."
    )


#: One synthetic module per shape the loop must report, and correct code it must
#: not. Synthetic rather than real tests, because a control anchored to a real
#: test stops controlling anything the day that test is rewritten — and gutting
#: a real one to prove the point would be shipping the defect to prove it exists.
_MUST_BE_REPORTED = {
    "gutted body": "def test_x():\n    assert True\n",
    "no assertion at all": "def test_x():\n    value = 2 + 2\n",
    "always skips": 'def test_x():\n    pytest.skip("later")\n    assert 1 == 2\n',
    "empty parameter set": '@pytest.mark.parametrize("v", [])\ndef test_x(v):\n    assert v\n',
}
_MUST_BE_LEFT_ALONE = {
    "an ordinary assertion": "def test_x():\n    assert value == 1\n",
    "raises block": "def test_x():\n    with pytest.raises(ValueError):\n        boom()\n",
    "explicit fail": 'def test_x():\n    if bad:\n        pytest.fail("no")\n',
    "a real parameter set": '@pytest.mark.parametrize("v", [1])\ndef test_x(v):\n    assert v\n',
}


def test_the_loop_still_reports_every_shape_it_claims() -> None:
    """POSITIVE controls on the LOOP, not on the predicate.

    The predicate already had unit tests and they did not help: `if False and
    …` inside the offender loop left all five tests green, because nothing
    called the loop with input it should reject. A rule is only controlled by
    being run against something it must flag.

    Each shape separately, so a failure names which one stopped being reported.
    """
    missed = [
        name
        for name, source in _MUST_BE_REPORTED.items()
        if not _unable_to_fail(_functions_in(source))
    ]

    assert not missed, (
        f"the loop no longer reports: {missed}. It cannot certify a suite it "
        "cannot fault — a loop that examines nothing is indistinguishable from "
        "a suite where every test can fail."
    )


def test_the_loop_still_leaves_correct_tests_alone() -> None:
    """NEGATIVE controls, so "report everything" is not a way to pass the above.

    Without these the cheapest fix for a broken loop is one that flags every
    test, which would fail on the whole suite and be deleted rather than
    repaired.
    """
    accused = {
        name: _unable_to_fail(_functions_in(source))
        for name, source in _MUST_BE_LEFT_ALONE.items()
        if _unable_to_fail(_functions_in(source))
    }

    assert not accused, f"the loop now reports tests that CAN fail: {accused}"


def test_a_recorded_exception_is_still_honoured_by_the_loop() -> None:
    """The allow list must be consulted BY THE LOOP, not merely exist.

    `test_the_recorded_exceptions_still_exist` checks the entries name real
    tests. That is the walk again: an entry can be perfectly valid while the
    loop has stopped reading the list, and every test in this file would pass.
    """
    functions = _functions_in("def test_x():\n    do_something()\n", "synthetic.py")
    assert _unable_to_fail(functions), "the fixture is not a test that cannot fail"

    with_exception = dict.fromkeys(["synthetic.py::test_x"], "control")
    original = dict(ASSERTS_BY_NOT_RAISING)
    ASSERTS_BY_NOT_RAISING.update(with_exception)
    try:
        assert not _unable_to_fail(functions), "the loop ignored the recorded exception"
    finally:
        ASSERTS_BY_NOT_RAISING.clear()
        ASSERTS_BY_NOT_RAISING.update(original)
