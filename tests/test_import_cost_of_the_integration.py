"""What this integration costs every command that never uses it.

@9bbc8ed7 measured the doctrine clause "never a millisecond on the primary
path" at RUNTIME and it holds: tracing adds ~0 to a hook, and a configured but
dead proxy costs nothing. They decomposed the hook's 353 ms into ~326 ms of CLI
IMPORT and ~27 ms of hook, and recorded the import cost rather than filing it —
"real, on the primary path, and NOT this integration doing".

Most of it is not. Some of it is, and nobody had split that. The cost is paid
by `aisquare status` on a machine that has never configured explainability,
because Typer imports the module to register its commands.

MEASURED BY CPU, NOT BY CLOCK, and the first version of this file got that
wrong. Medians of nine `-X importtime` runs, self time per module:

    services.explainability        7.2 ms
    services.explainability_ops    5.4 ms
    ssl                            3.2 ms
    http                           1.0 ms
    cli.explainability             0.6 ms
    sqlite3                        0.3 ms
    ------------------------------------
    identifiable                  ~17 ms

An earlier revision reported 26 ms from subprocess wall-clock timing. That
number was noise: on this box, under three concurrent test runs, the BASE
measurement alone spread 36 ms between its fastest and slowest sample — a noise
floor larger than the signal — and repeating it produced a median marginal of
131 ms. The per-module CPU figures above hold to within a millisecond across
nine runs, so they are what this file states.

I had quoted @9bbc8ed7's rule approvingly while writing the first version — "a
wall-clock bound in CI is flaky by construction" — and pinned module identity
rather than milliseconds for exactly that reason. Then I put a wall-clock number
in the prose as though it were solid. THE RULE APPLIED TO THE TEST AND NOT TO MY
OWN REPORT.

WHAT IT IS: module definitions only. No work happens at import — no config
read, no filesystem, no network, and NO SDK, which the no-hard-dependency rule
requires. The cost is the modules it pulls in, and this file records exactly
which ones so the set cannot grow in silence.

WHY A RECORD RATHER THAN A FIX, AND THE DECOMPOSITION SETTLES IT. An earlier
revision suggested deferring `ssl`, `http` and `sqlite3` into the functions that
use them, on the grounds that it would recover most of the cost. It would not:
those three total ~4.5 ms of ~17 ms. THE MAJORITY IS OUR OWN TWO MODULE BODIES —
12.7 ms of dataclass definitions, compiled regexes and constants — which no
amount of deferring imports touches, because it is the module executing, not its
imports. So the restructure targets a quarter of the cost, on a module every
command loads, on a handed-off train, for a saving no human perceives. It is not
worth doing, and that is a firmer answer than "the owner's risk to spend".

What is defensible is making the cost visible and bounded, which is this file.

NOT A WALL-CLOCK ASSERTION, for @9bbc8ed7's reason: "a wall-clock bound in CI is
flaky by construction and A MUTED TEST IS WORSE THAN NONE." The stable,
deterministic thing underneath the milliseconds is WHICH MODULES get imported,
so that is what is pinned.
"""

from __future__ import annotations

import subprocess
import sys

#: Top-level modules that importing the explainability CLI pulls in and the rest
#: of the CLI does not. Measured, not guessed. A RATCHET in both directions:
#: something new here is a cost added to every command that never uses this
#: integration; something missing means it was deferred or dropped and the
#: record should say so.
#:
#:   ssl, _ssl, http     TLS and HTTP for the gateway probes
#:   sqlite3, _sqlite3   the spool's store
#:   hashlib, _hashlib, _blake2   correlation and spool naming
#:   shlex               quoting for the printed proxy command
UNIQUELY_IMPORTED = {
    "_blake2",
    "_hashlib",
    "_sqlite3",
    "_ssl",
    "hashlib",
    "http",
    "shlex",
    "sqlite3",
    "ssl",
}

#: Stdlib names that DRIFT across interpreters, and the reason this file no
#: longer asserts set equality.
#:
#: The measurement is a difference of two import closures, and both sides move
#: with the interpreter. Measured on the same commit: 3.13 additionally reports
#: ``array`` and ``socket``; 3.14 reports ``socket``, ``_socket`` and
#: ``tempfile``. Nothing in this package changed between those two runs — the
#: modules simply left the BASE closure, so the subtraction started attributing
#: them here. A per-version table would need one entry per interpreter in the
#: matrix, would be wrong the day a floating dependency reshuffles its own
#: imports, and would be maintained by whoever is next made to read a red CI
#: they did not cause. That is a muted test with extra steps.
#:
#: So the rule became: nothing outside a recorded ALLOWANCE may appear, and the
#: members that are actually load-bearing must still be there. Both halves of
#: the ratchet survive where they carry meaning — a new dependency or a heavy
#: module still fails the first, deferring `ssl` or `sqlite3` still fails the
#: second — and the part that only ever tracked CPython's own reorganisation is
#: gone. `test_nothing_heavier_than_the_standard_library_is_imported` is the
#: assertion that catches the failure this file exists for, and it is exact.
DRIFTS_BY_INTERPRETER = {"_socket", "array", "socket", "tempfile"}

#: Stdlib names that drift by PLATFORM rather than by interpreter, recorded the
#: same way and for the same reason.
#:
#: ``nturl2path`` is Windows-only: ``urllib.request`` imports it there to turn a
#: ``file://`` URL into a path, and it does not exist in the closure anywhere
#: else. It is stdlib, it is pure Python, and it arrives through an import this
#: package already makes deliberately — so it is an allowance, not a finding.
#: Kept as its own set rather than folded into the one above because the two
#: answer different questions when this next goes red: "which interpreter" and
#: "which OS" are separate investigations.
DRIFTS_BY_PLATFORM = {"nturl2path"}

#: Everything the diff is permitted to contain, on any interpreter or platform.
ALLOWED = UNIQUELY_IMPORTED | DRIFTS_BY_INTERPRETER | DRIFTS_BY_PLATFORM

#: The members tied to behaviour rather than to a version: TLS for the gateway
#: probes, the spool's store, HTTP, quoting for the printed proxy command, and
#: the correlation/spool naming hash. If one of these stops appearing, the
#: import was deferred or the feature left — either way the record is stale, and
#: that is the "removed" direction still doing its job.
LOAD_BEARING = {"hashlib", "http", "shlex", "sqlite3", "ssl"}


_BASE = "import aisquare.cli.common, aisquare.core.config, aisquare.models"
_WITH = f"{_BASE}, aisquare.cli.explainability"


def _top_level_modules(code: str) -> set[str]:
    """Top-level module names present after running `code` in a fresh process.

    A subprocess because this process has already imported everything; asking
    `sys.modules` here would report the test suite's imports and prove nothing.
    """
    result = subprocess.run(
        [sys.executable, "-c", f"{code}\nimport sys\nprint(' '.join(sorted(sys.modules)))"],
        capture_output=True,
        text=True,
        check=False,
    )
    # NOT check=True. It raises CalledProcessError, whose message carries the
    # returncode and not the captured stderr, and capture_output means nothing
    # else prints it either — so the interpreter's own explanation is thrown
    # away at the only moment anyone wants it. That cost real time: a CI run
    # where these five tests failed reported "returned non-zero exit status 1"
    # and nothing more, and the actual message ("No module named
    # 'aisquare.cli'", from a shadowing install) named the root cause outright.
    assert result.returncode == 0, (
        f"the measurement subprocess failed (exit {result.returncode}); it says:\n"
        f"{(result.stderr or result.stdout or '<no output>').strip()}"
    )
    return {name for name in result.stdout.split() if "." not in name}


def _uniquely_imported(with_code: str = _WITH, base_code: str = _BASE) -> set[str]:
    """What `with_code` imports that `base_code` does not.

    THE RULE, extracted so the real assertion and its controls run the SAME
    code. My first attempt at controlling this file called `_top_level_modules`
    directly from a control — which proved the HELPER works and left the rule
    replaceable: `added = set(UNIQUELY_IMPORTED)` still passed everything.

    That is the sentence I wrote one cycle earlier, about a different guard, and
    then repeated here: A HELPER TEST PROVES THE HELPER. IT SAYS NOTHING ABOUT
    WHETHER THE ASSERTION STILL CALLS IT. The control has to go through the same
    door as the test.
    """
    return _top_level_modules(with_code) - _top_level_modules(base_code)


def test_the_integration_pulls_in_nothing_unrecorded() -> None:
    """The "added" direction: no module may appear that the record does not name."""
    added = _uniquely_imported()

    assert added <= ALLOWED, (
        f"the explainability CLI now uniquely imports {sorted(added - ALLOWED)}, "
        f"which the record does not name (recorded: {sorted(ALLOWED)}).\n"
        "This is a cost paid by every command that never uses this integration — "
        "deferring the import into the function that needs it is the usual fix.\n"
        "If it is stdlib drift rather than a real addition, add it to "
        "DRIFTS_BY_INTERPRETER and say which interpreter moved it."
    )


def test_the_load_bearing_imports_are_still_there() -> None:
    """The "removed" direction, restricted to the members that mean something.

    Set equality used to cover this and could not survive stdlib drift (see
    DRIFTS_BY_INTERPRETER). These five are the ones whose absence is a fact
    about this package rather than about CPython: lose `ssl` and the gateway
    probe is not doing TLS from module scope any more, lose `sqlite3` and the
    spool's store moved. Either is worth a red test; `array` arriving on 3.13
    is not.
    """
    added = _uniquely_imported()

    assert added >= LOAD_BEARING, (
        f"{sorted(LOAD_BEARING - added)} no longer arrives with the explainability "
        "CLI.\nIf that is deliberate — an import deferred into the function that "
        "needs it, or a feature removed — update LOAD_BEARING so the record keeps "
        "describing the truth."
    )


def test_nothing_heavier_than_the_standard_library_is_imported() -> None:
    """The line that would be a real defect rather than a cost.

    Importing the SDK at module scope would make it a hard dependency in
    practice — every command would pay for it and a machine without the extra
    would be one import error from a broken CLI. The doctrine forbids that, and
    this is the cheapest place to notice it.
    """
    third_party = {
        name
        for name in _top_level_modules(_WITH) - _top_level_modules(_BASE)
        if name not in sys.stdlib_module_names and not name.startswith("_")
    }

    assert not third_party, f"the explainability CLI imports {sorted(third_party)} at module scope"


def test_the_measurement_is_looking_at_something() -> None:
    """Guard the guard: if the base set stopped importing, the diff would be
    everything and the record would look wrong for the wrong reason — or if the
    two commands became identical, the diff would be empty and every assertion
    above would pass over nothing.
    """
    base = _top_level_modules(_BASE)

    assert len(base) > 50, f"only {len(base)} modules in the base set — it did not import"
    assert "ssl" not in base, (
        "the base CLI now imports ssl by itself, so this file is no longer "
        "measuring what the explainability integration adds"
    )


def test_the_measurement_can_still_see_a_difference() -> None:
    """POSITIVE control on the RULE, not on the walk.

    Every check in this file is satisfied by "the diff equals the record" — and
    replacing the measurement with the record itself produces that for free.
    Measured: `added = set(UNIQUELY_IMPORTED)` left all three tests green. The
    guard would then certify an import cost it never measured.

    Same shape @9bbc8ed7 named in the AST guards: the surrounding checks watch
    the walk — that the base set imported, that ssl is not in it — while the
    RULE has stopped comparing anything.

    Controlled against a module the base set demonstrably does NOT import, so
    the difference is real and known in advance.
    """
    assert "xml" not in _top_level_modules(_BASE), (
        "the control's premise is gone; pick another module"
    )

    added = _uniquely_imported(f"{_BASE}\nimport xml.etree.ElementTree")

    assert "xml" in added, (
        "the measurement no longer reports a module that was demonstrably "
        "imported — it cannot certify what this integration costs if it cannot "
        "detect an import at all"
    )


def test_the_measurement_reports_nothing_when_nothing_was_added() -> None:
    """NEGATIVE control, so "report everything" is not a way to pass the above.

    A rule that returned every loaded module would satisfy the positive control
    and then fail the real assertion for the wrong reason — or, if the record
    were widened to match, hide a genuine addition inside the noise.
    """
    added = _uniquely_imported(_BASE)

    assert added == set(), f"the measurement invents imports that were not added: {sorted(added)}"
