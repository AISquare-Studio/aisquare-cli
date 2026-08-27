"""CI must run the suite against a machine that looks like a developer's.

THE CLASS THIS PINS. The ``check`` job installs ``.[dev]`` into a pristine
runner: no ``~/.aisquare``, nothing listening on any port, no ambient
explainability variables. A developer who has followed
``docs/connecting-your-agents-to-explainability.md`` has all of those. Three
separate fixtures in this repo were green on CI and red for them:

* a module-scoped fixture read the developer's REAL ``~/.aisquare`` while its
  own docstring claimed "a machine with nothing configured";
* a "proxy is down" premise was asserted against the CONFIGURED proxy URL,
  which is 9090 — the port its own docstring says to avoid because something is
  usually on it;
* a fixture patched the PATH lookup for a console script and not the lookup
  beside the interpreter, so the real script won over the stub.

The third was introduced while fixing the second. That is what moves this from
"be careful" to "have a job".

WHY THESE ASSERTIONS ARE STRUCTURAL RATHER THAN LITERAL. An independent review
mutated an earlier version of this file's checks and found five ways to make the
ambient job vacuous with every guard still green: drop ``AISQUARE_HOME`` from the
step that runs pytest; hardcode the stub's port so it stops matching the
configured one; delete either one of the two premise assertions; move ``pytest``
ahead of the listener inside the one step; invert the variant condition so the
labels swap. Every one of those is the same shape as the bugs above — a check
that passes without establishing what it claims — so the guards below assert on
the ORDER and PLACEMENT of things inside the job, not on the presence of a string
somewhere in the file.

They were also blocking edits someone will legitimately want to make: a third
matrix variant, or parameterising the port. Where a literal was load-bearing only
by accident, it is gone.

WHY TEXTUAL AT ALL. ``pyyaml`` is not a dependency of this project and adding one
so a test can read a config file is a bad trade. The doc guards in this directory
parse markdown with ``re`` for the same reason. The cost is that these checks see
text rather than structure, so they slice the file into the job and the step first
and then assert inside those slices.

WHAT THIS DELIBERATELY DOES NOT DO: run CI. It cannot know the job passes, only
that the job is still described. A guard that claimed more would be worse than
this one, because the reader would stop looking.
"""

from __future__ import annotations

import re
from itertools import pairwise
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"

#: A top-level entry under ``jobs:`` — two spaces of indent, then a name.
_JOB = re.compile(r"^  ([A-Za-z0-9_-]+):$", re.MULTILINE)

#: A step boundary inside a job.
_STEP = re.compile(r"^      - name: (.+)$", re.MULTILINE)

#: The command that runs the suite. Its position inside the step is what several
#: of the guards below are about, so it is named once.
SUITE = "pytest -ra"

#: An ASSIGNMENT of the ambient home, in either form — ``AISQUARE_HOME=`` in a
#: shell line (including inside an ``echo`` bound for ``$GITHUB_ENV``) and
#: ``AISQUARE_HOME:`` in a step-level YAML ``env:``. Counting only the first form
#: let a duplicate be re-added as step-level YAML with the guard green, which is
#: the very failure the test using this is about. A bare ``"$AISQUARE_HOME"`` USE
#: does not match, which is what keeps the count at the definitions.
_ASSIGNS_HOME = r"AISQUARE_HOME\s*[:=]"

#: The shell function the ambient job asserts its premise with. Named here so a
#: rename fails these guards loudly instead of leaving them quietly satisfied.
PREMISE = "assert_proxy"


def _calls(script: str, name: str) -> list[str]:
    """Lines that INVOKE ``name``, excluding the line that defines it.

    Load-bearing: ``assert_proxy() {`` sits before ``pytest`` in the same script,
    so ``"assert_proxy" in before`` was satisfied by the definition and deleting
    the pre-suite call left the guard green. Measured, not supposed.
    """
    return [line.strip() for line in script.splitlines() if line.strip().startswith(f"{name} ")]


def _commands(text: str) -> list[str]:
    """Executable lines, with any trailing ``#`` comment removed.

    ``_strip_comments`` only drops WHOLE-LINE comments, which is the honest thing
    for it to do — a ``#`` mid-line can be data. But it means ``true # aisquare
    explainability enable`` still contains the needle, so a guard that searched
    for the substring was satisfied by a command that had been disabled and
    documented in place. Measured. Asserting that a line STARTS with the command
    says what the guard actually means: this runs.
    """
    return [line.strip().split(" #")[0].strip() for line in _strip_comments(text).splitlines()]


def _strip_comments(text: str) -> str:
    """Drop whole-line ``#`` comments, so prose cannot satisfy a guard.

    Load-bearing: the job's comments *discuss* the hardcoded port that must not
    appear in a command, and an earlier guard would have been satisfied by the
    discussion.
    """
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


@pytest.fixture(scope="module")
def workflow() -> str:
    if not WORKFLOW.exists():  # pragma: no cover - the path is the point
        pytest.fail(f"the workflow this guard exists for is missing: {WORKFLOW}")
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ambient(workflow: str) -> str:
    """The ``ambient`` job alone, so a guard cannot be satisfied by another job.

    ``package`` also installs things and ``check`` also runs pytest; asserting
    against the whole file let one job's text stand in for another's.
    """
    starts = {match.group(1): match.start() for match in _JOB.finditer(workflow)}
    if "ambient" not in starts:
        pytest.fail(
            "the ambient-environment job is gone — three bugs were invisible "
            "without it, see this module's docstring before removing the guard too"
        )
    later = [pos for pos in starts.values() if pos > starts["ambient"]]
    end = min(later) if later else len(workflow)
    # Comments stripped HERE, not just in the step. Measured: with the raw text,
    # deleting the job-level `AISQUARE_HOME:` and commenting out `aisquare
    # explainability enable` both left the guards green — this job's own comments
    # NAME both of those things while explaining why they matter, so the prose
    # satisfied the assertion the code no longer did. Exactly the shape of bug the
    # module exists for, in the module itself.
    return _strip_comments(workflow[starts["ambient"] : end])


@pytest.fixture(scope="module")
def suite_step(ambient: str) -> str:
    """The step that runs pytest, comments stripped."""
    bounds = [match.start() for match in _STEP.finditer(ambient)] + [len(ambient)]
    for start, end in pairwise(bounds):
        if SUITE in ambient[start:end]:
            return _strip_comments(ambient[start:end])
    pytest.fail(f"no step in the ambient job runs `{SUITE}`")


def test_the_ambient_job_exists(ambient: str) -> None:
    """Deleting it should be a decision someone argues for, not a quiet edit."""
    assert ambient.startswith("  ambient:")


def test_both_proxy_states_are_covered(ambient: str) -> None:
    """ONE ambient configuration would have caught one of the two leaks.

    Measured, and measured in CI rather than only locally: with both regressions
    reintroduced on a throwaway branch, ``proxy-down`` errored on the three
    ``test_runbook_json_paths.py`` tests and ``proxy-up`` on
    ``test_json_stdout_is_empty_or_parseable[proxy-down]`` — disjoint, neither
    catching the other's, while all three ``check`` variants stayed green.
    Dropping either variant leaves a job that looks like it covers the class and
    covers half of it.

    Asserted per name rather than as the literal list, so adding a third variant
    or reordering the two is not a false failure.
    """
    matrix = ambient[ambient.index("matrix:") : ambient.index("steps:")]

    for variant in ("proxy-down", "proxy-up"):
        assert variant in matrix, (
            f"the ambient job no longer runs the {variant} variant; each proxy "
            "state catches a different leak and neither catches the other"
        )


def test_the_ambient_environment_is_declared_exactly_once(ambient: str) -> None:
    """A per-step ``AISQUARE_HOME`` can be dropped from the step that matters.

    Found by review, by mutation: delete it from the step that runs pytest — or
    typo it to a path the configure step never populated — and the populated home
    becomes invisible to the suite, the job degrades to "``check``, twice, one of
    them with a listener", and it all stays green.

    So there is ONE definition, exported through ``$GITHUB_ENV``. Not a job-level
    ``env:`` block: ``runner.temp`` is unavailable there and GitHub rejects the
    whole FILE for it — measured, and the failure is near-silent, a run with zero
    jobs and no log that reads as "CI has not started".
    """
    # Both assignment forms: `AISQUARE_HOME=` in a shell line and
    # `AISQUARE_HOME:` in a step-level YAML `env:`. Counting only the first let a
    # duplicate be re-added as step-level YAML with the guard green — measured,
    # which is the whole failure this test is about.
    defined = [line for line in _commands(ambient) if re.search(_ASSIGNS_HOME, line)]

    assert defined, "nothing declares AISQUARE_HOME for the ambient job"
    assert len(defined) == 1, (
        f"AISQUARE_HOME is declared {len(defined)} times — one of them can be "
        "dropped or typo'd while the other keeps the guards green"
    )
    assert "GITHUB_ENV" in ambient, (
        "the ambient environment is no longer exported through $GITHUB_ENV, so it "
        "is not guaranteed to reach the step that runs the suite"
    )
    assert ambient.index(defined[0]) < ambient.index(SUITE), (
        "the ambient environment is declared after the suite runs"
    )


def test_the_ambient_job_populates_that_home(ambient: str) -> None:
    """An empty ``AISQUARE_HOME`` reproduces nothing.

    The leak was a fixture reading a CONFIGURED home. Setting the variable at a
    fresh directory and configuring nothing in it would pass exactly as the
    pristine runner does.
    """
    commands = _commands(ambient)

    assert any(line.startswith("aisquare explainability enable") for line in commands), (
        "the ambient home is no longer configured, so it no longer differs from "
        "the pristine runner in the way that mattered"
    )
    assert any("explainability-key" in line for line in commands), (
        "no key is written, so `status` resolves none — and `key_set` was part of "
        "the state the leaked fixture was sampling"
    )


def test_the_configured_home_asserts_its_own_premise(ambient: str) -> None:
    """The proxy axis is not the only premise that can fail to hold.

    ``set -e`` catches an ``enable`` that FAILS. It cannot catch one that succeeds
    while leaving a home the suite reads as unconfigured — and observed while
    building this job, that state is worse than a hard failure: ``status`` exits 0
    whatever the proxy is doing when tracing is off, so ``proxy-up`` goes
    VACUOUSLY GREEN and ``proxy-down`` fails for the wrong reason. Both variants
    are meaningless without the home, so the home is asserted, not assumed.
    """
    configure = ambient[: ambient.index(SUITE)]

    assert '"enabled", True' in configure, (
        "nothing asserts that the ambient home actually has tracing enabled — a "
        "home that quietly failed to configure makes proxy-up vacuously green"
    )
    assert '"key_source", "file"' in configure, (
        "nothing asserts the key resolves from the FILE, which is the state the "
        "leaked fixture was sampling"
    )


def test_the_ambient_environment_variables_are_set(ambient: str) -> None:
    """The variables ``conftest`` goes out of its way to clear.

    ``tests/conftest.py`` unsets these before each test, and its own comment says
    why: "an operator's shell has these sourced from their explainability env
    file". That makes them the identical escape hatch to ``AISQUARE_HOME`` — a
    module- or session-scoped fixture reads them straight out of ``os.environ``,
    and nothing on a pristine runner would notice.
    """
    exported = "\n".join(_commands(ambient)[: len(_commands(ambient))])

    for name in ("AISQUARE_EXPLAINABILITY_TARGET=", "EXPLAINABILITY_GATEWAY_URL="):
        assert name in exported, (
            f"{name} is no longer set for the ambient job, so an axis conftest "
            "itself flags as dangerous is unreproduced again"
        )


def test_the_premise_is_read_from_the_config_not_hardcoded(suite_step: str) -> None:
    """A literal port re-creates leak #2 inside the job written to prevent it.

    Found by review, by mutation: hardcode the stub's port (or let the config
    default move out from under it) and the stub binds a port the CLI no longer
    probes — every premise check still passes, and ``proxy-up`` is a silent second
    ``proxy-down``. Which is, verbatim, *"a 'proxy is down' premise asserted
    against the CONFIGURED proxy URL"* from this module's own list.

    So the port is read back out of the config, and the premise is asserted with
    the CLI: it probes whatever the config says, and it checks the
    ``service``/``mode`` contract rather than "something answered 200".
    """
    assert "explainability status --json" in suite_step, (
        "the ambient job no longer reads the configured proxy out of the config, "
        "so the port it occupies can drift from the port the suite probes"
    )
    assert '"$port"' in suite_step, "the stub is no longer started on the port read from the config"
    assert "9090" not in suite_step, (
        "a literal port is back in a command — that is leak #2 of this module's "
        "three, re-created inside the job that exists to catch it"
    )


def test_the_premise_is_asserted_on_both_sides_of_the_suite(suite_step: str) -> None:
    """``before`` alone cannot catch a listener that dies mid-suite.

    Found by review, by mutation: an earlier guard checked that the assertion
    existed *somewhere in the file*, so deleting either the pre-suite or the
    post-suite one left it green — only deleting both failed. A stub that died
    halfway would then leave a GREEN run that silently tested the other ambient,
    and a job cannot notice that it tested the wrong thing.
    """
    before, _, after = suite_step.partition(SUITE)

    assert _calls(before, PREMISE), f"nothing asserts the ambient premise before `{SUITE}`"
    assert _calls(after, PREMISE), (
        f"nothing re-asserts the ambient premise after `{SUITE}` — a listener that "
        "died mid-suite would leave a green run that tested the wrong ambient"
    )


def test_both_variants_assert_their_own_premise(suite_step: str) -> None:
    """Symmetry, because the asymmetry was the bug.

    ``proxy-up`` checking twice while ``proxy-down`` assumed was odd in a job
    whose thesis is "assert the premise rather than assuming it", and it becomes
    real on a self-hosted or container runner where something may already hold the
    port. Both branches must fail loudly, so both must exit non-zero.
    """
    body = suite_step[suite_step.index(f"{PREMISE}()") :].partition(SUITE)[0]

    assert body.count("exit 1") >= 2, (
        "the premise function no longer fails on BOTH variants — one of them is "
        "assuming its ambient state instead of asserting it"
    )


def test_the_listener_starts_before_the_suite_in_the_same_step(suite_step: str) -> None:
    """A ``&`` background process does not survive its own step.

    GitHub kills a step's process group when the step ends, so starting the stub
    in an "occupy the port" step left the port FREE during pytest — ``proxy-up``
    silently became a second ``proxy-down``.

    PROVEN IN CI, not reasoned about: with both regressions reintroduced, the two
    variants failed IDENTICALLY on the same commit, which is only possible if they
    ran the same ambient state. That is the exact failure this module's other
    guards warn about, committed anyway, and caught by running the proof instead
    of trusting the local measurement.

    Also pins the ORDER. Review found that moving ``pytest`` ahead of the stub
    start, inside this one step, passed every guard while testing the wrong
    ambient.
    """
    before, found, _ = suite_step.partition(SUITE)

    assert found, f"the ambient job no longer runs `{SUITE}`"
    assert "tests.proxy_stub" in before, (
        "the proxy stub is not started before the suite in the same step, so it "
        "is either killed before pytest runs or started too late — either way "
        "proxy-up tests the same state as proxy-down"
    )


def test_the_listener_is_gated_on_the_proxy_up_variant(suite_step: str) -> None:
    """Inverting the condition swaps the labels and nothing notices.

    Found by review, by mutation: gate the stub on ``proxy-down`` instead and the
    job runs both variants, both green, each under the other's name — a matrix
    that reports the wrong two things.
    """
    needle = '"$AMBIENT" = "'
    start = suite_step.index("tests.proxy_stub")
    # The NEAREST preceding gate. Searching for the literal `= "proxy-up"` found
    # the one inside the premise function, which is also above the stub — so
    # inverting the stub's own gate left this guard green. Measured.
    gate = suite_step.rindex(needle, 0, start) + len(needle)

    assert suite_step[gate:].split('"')[0] == "proxy-up", (
        "the stub is no longer started under the proxy-up branch, so the two "
        "variants may be running under each other's names"
    )


def test_the_workflow_reuses_the_test_suites_proxy_stub(ambient: str) -> None:
    """``probe_proxy`` checks ``service`` and ``mode``, so a stub is a CONTRACT.

    An inline server in the workflow would be a second copy of that contract, and
    the copy nobody runs locally is the one that drifts — at which point the job
    goes green against a payload the CLI would reject.

    The port is deliberately NOT asserted here: it is read from the config now
    (see ``test_the_premise_is_read_from_the_config_not_hardcoded``), and pinning
    a literal was what blocked that fix.
    """
    assert "python -m tests.proxy_stub" in ambient, (
        "the ambient job no longer uses tests/proxy_stub.py"
    )
    for inlined in ("http.server", "BaseHTTPRequestHandler", "HTTPServer"):
        assert inlined not in ambient, (
            f"the workflow inlines its own server ({inlined}) — that is a second "
            "copy of the health contract, and the copy nobody runs locally is the "
            "one that drifts"
        )


def test_the_package_job_installs_the_extra(workflow: str) -> None:
    """The install line the setup guide gives people.

    Nothing tested it until this job. It is a genuinely different environment: the
    SDK shares this package's top-level import name, so both distributions land in
    one ``site-packages/aisquare/`` and the last writer wins the shared
    ``__init__.py``. A build that read ``__version__`` off the top-level package
    died at import with the extra installed, and no job would have caught it.

    This is also the ONLY job that covers that axis. The ambient job installs
    ``.[dev]`` and cannot install the extra — it shadows an editable checkout, and
    ``conftest.pytest_sessionstart`` refuses to grade a non-editable one — so the
    coverage here is at import level, not suite level. Said in the job's own
    comment too, because a reader who counts three ambient conditions will
    otherwise assume three are reproduced.
    """
    assert "[explainability]" in workflow, (
        "the packaging job no longer installs the extra, so the collision between "
        "our import name and the SDK's is untested again"
    )
    assert "import aisquare.explainability, aisquare.cli.app" in workflow, (
        "nothing asserts that BOTH packages are importable after the collision — "
        "which is the whole property the extra has to preserve"
    )
