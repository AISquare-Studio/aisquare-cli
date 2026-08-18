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
        check=True,
    )
    return {name for name in result.stdout.split() if "." not in name}


def test_the_integration_pulls_in_exactly_what_is_recorded() -> None:
    """Both directions, so the record cannot rot into a stale allow list."""
    added = _top_level_modules(_WITH) - _top_level_modules(_BASE)

    assert added == UNIQUELY_IMPORTED, (
        f"the explainability CLI now uniquely imports {sorted(added)}, recorded "
        f"as {sorted(UNIQUELY_IMPORTED)}.\n"
        "Added: a cost paid by every command that never uses this integration — "
        "deferring the import into the function that needs it is the usual fix.\n"
        "Removed: good; update the record so it keeps describing the truth."
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
