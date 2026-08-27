"""CI must run the suite against a machine that looks like a developer's.

THE CLASS THIS PINS. The `check` job installs `.[dev]` into a pristine runner:
no `~/.aisquare`, nothing listening on any port, no optional extra. A developer
who has followed `docs/connecting-your-agents-to-explainability.md` has all
three. Three separate fixtures in this repo were green on CI and red for them:

* a module-scoped fixture read the developer's REAL `~/.aisquare` while its own
  docstring claimed "a machine with nothing configured";
* a "proxy is down" premise was asserted against the CONFIGURED proxy URL,
  which is 9090 — the port its own docstring says to avoid because something is
  usually on it;
* a fixture patched the PATH lookup for a console script and not the lookup
  beside the interpreter, so the real script won over the stub.

The third was introduced while fixing the second. That is what moves this from
"be careful" to "have a job".

WHY THE ASSERTIONS ARE TEXTUAL. `pyyaml` is not a dependency of this project and
adding one so a test can read a config file is a bad trade. The doc guards in
this directory parse markdown with `re` for the same reason. The cost is that
these checks see text rather than structure, so they are written to be specific
about the things that carry meaning and silent about formatting.

WHAT THIS DELIBERATELY DOES NOT DO: run CI. It cannot know the job passes, only
that the job is still described. A guard that claimed more would be worse than
this one, because the reader would stop looking.
"""

from __future__ import annotations

from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def workflow() -> str:
    if not WORKFLOW.exists():  # pragma: no cover - the path is the point
        pytest.fail(f"the workflow this guard exists for is missing: {WORKFLOW}")
    return WORKFLOW.read_text(encoding="utf-8")


def test_the_ambient_job_exists(workflow: str) -> None:
    """Deleting it should be a decision someone argues for, not a quiet edit."""
    assert "\n  ambient:\n" in workflow, (
        "the ambient-environment job is gone — three bugs were invisible without "
        "it, see this file's docstring before removing the guard too"
    )


def test_both_proxy_states_are_covered(workflow: str) -> None:
    """ONE ambient configuration would have caught one of the two leaks.

    Measured rather than assumed: with a configured home and the proxy DOWN, the
    module-scoped-fixture leak fires and the proxy-down-premise leak does not;
    with the proxy UP it is the other way round. Dropping either variant leaves a
    job that looks like it covers the class and covers half of it.
    """
    assert "ambient: [proxy-down, proxy-up]" in workflow, (
        "the ambient job no longer runs both proxy states; each catches a "
        "different leak and neither catches the other"
    )


def test_the_ambient_job_populates_a_home(workflow: str) -> None:
    """An empty `AISQUARE_HOME` reproduces nothing.

    The leak was a fixture reading a CONFIGURED home. Setting the variable at a
    fresh directory and configuring nothing in it would pass exactly as the
    pristine runner does.
    """
    assert "AISQUARE_HOME" in workflow
    assert "aisquare explainability enable" in workflow, (
        "the ambient home is no longer configured, so it no longer differs from "
        "the pristine runner in the way that mattered"
    )
    assert "explainability-key" in workflow, (
        "no key is written, so `status` resolves none — and `key_set` was part of "
        "the state the leaked fixture was sampling"
    )


def test_the_proxy_up_variant_asserts_its_own_premise(workflow: str) -> None:
    """A variant whose distinguishing condition failed to start is a duplicate.

    If the stub never binds, `proxy-up` silently becomes a second `proxy-down`
    and the job reports two greens for one configuration — the same shape as a
    fixture asserting a premise it never established, which is the bug class this
    job exists for.
    """
    # The UNREDIRECTED form specifically. The wait loop above it runs the same
    # curl with `>/dev/null 2>&1 && break`, which cannot fail the step — so
    # matching the command loosely was satisfied by the loop and would have
    # passed with the assertion deleted. Measured: removing the assertion line
    # left this guard green until it was narrowed to the bare invocation.
    lines = [line.strip() for line in workflow.splitlines()]

    assert "curl -fsS http://127.0.0.1:9090/health" in lines, (
        "the proxy-up variant no longer FAILS when nothing is listening — a "
        "polling loop that gives up quietly leaves a variant that silently "
        "duplicates proxy-down"
    )


def test_the_listener_starts_in_the_same_step_as_the_suite(workflow: str) -> None:
    """A `&` background process does not survive its own step.

    GitHub kills a step's process group when the step ends, so starting the stub
    in an "occupy the port" step left the port FREE during pytest — `proxy-up`
    silently became a second `proxy-down`.

    PROVEN IN CI, not reasoned about: with both regressions reintroduced, the two
    variants failed IDENTICALLY on the same commit, which is only possible if
    they ran the same ambient state. That is the exact failure this file's other
    guard warns about, committed anyway, and caught by running the proof instead
    of trusting the local measurement.

    So the stub start and the suite have to share one shell.
    """
    blocks = workflow.split("- name:")
    suite = [b for b in blocks if "pytest -ra" in b]

    assert suite, "no step runs the suite any more"
    assert any("tests.proxy_stub" in block for block in suite), (
        "the proxy stub is started in a different step from `pytest`, so it is "
        "killed before the suite runs and proxy-up tests the same state as "
        "proxy-down"
    )


def test_the_workflow_reuses_the_test_suites_proxy_stub(workflow: str) -> None:
    """`probe_proxy` checks `service` and `mode`, so a stub is a CONTRACT.

    An inline server in the workflow would be a second copy of that contract,
    and the copy nobody runs locally is the one that drifts — at which point the
    job goes green against a payload the CLI would reject.
    """
    assert "python -m tests.proxy_stub 9090" in workflow, (
        "the ambient job no longer uses tests/proxy_stub.py"
    )
    assert "aisquare-proxy" not in workflow.replace("python -m tests.proxy_stub", ""), (
        "the workflow names the proxy service string itself, which means it is "
        "carrying its own copy of the health contract"
    )


def test_the_package_job_installs_the_extra(workflow: str) -> None:
    """The install line the setup guide gives people.

    Nothing tested it until this job. It is a genuinely different environment:
    the SDK shares this package's top-level import name, so both distributions
    land in one `site-packages/aisquare/` and the last writer wins the shared
    `__init__.py`. A build that read `__version__` off the top-level package
    died at import with the extra installed, and no job would have caught it.
    """
    assert "[explainability]" in workflow, (
        "the packaging job no longer installs the extra, so the collision between "
        "our import name and the SDK's is untested again"
    )
    assert "import aisquare.explainability, aisquare.cli.app" in workflow, (
        "nothing asserts that BOTH packages are importable after the collision — "
        "which is the whole property the extra has to preserve"
    )
