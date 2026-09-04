"""The suite must not depend on the shell it was started from.

Two mechanisms in this package read routing and model-selection variables off
the LIVE environment, and both do the right thing when they find them set:

- ``core.harness.interfering_env`` reports them, because an endpoint the
  operator redirected can answer a probe with any ``modelUsage`` it likes.
- ``services.explainability.wire_session`` STANDS DOWN — "already set — not
  overriding your routing, launching untraced" — so no session id is pinned
  and no headers are built.

Correct behaviour, and it makes every test that asserts a clean machine or a
traced launch depend on whose terminal ran the suite.

Found on Windows while verifying an unrelated merge, with ``ANTHROPIC_BASE_URL``
exported — which every Claude Code session does. On the tree where the Windows
port has landed the whole suite moved from **6 failed, 1757 passed** to **1764
passed, 22 skipped, 0 failed** on unsetting that one variable. On THIS branch's
base the whole-suite figure is dominated by the Windows port not having landed
on ``main`` yet, so the honest measurement to quote here is the four tests that
flip on their own:

    test_harness.py::test_interfering_env_lists_only_set_vars
    test_harness.py::test_doctor_harness_check_reports_fable_fallback
    test_role_profile.py::TestTracingReadsTheBoundBinaryNotTheFlag
        ::test_an_unbound_role_still_gets_its_id_pinned
    test_no_network_on_the_primary_path.py
        ::test_a_command_that_should_reach_the_network_still_does

Two per mechanism, which is the shape of the bug rather than a coincidence.

``conftest.isolated_home`` already cleared six of the nine names
``interfering_env`` looks at and none of the two ``wire_session`` looks at, so
this was not one missing variable but two hand-maintained copies of overlapping
sets that had drifted. This module is the latch on that: the STATIC guard fails
when the product grows a name conftest does not clear, and the BEHAVIOURAL one
fails when conftest stops clearing.

WHY THE BEHAVIOURAL GUARD DIRTIES THE SHELL ITSELF. Asserting that these names
are absent inside a test passes on any machine whose shell never had them —
which is every CI runner, and exactly the vacuity that let the first version of
the Windows ACL fix look tested. So the fixture below SETS them first, and the
assertion can only pass if something removed a value that was really there.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from aisquare.core import harness
from tests.conftest import AMBIENT_ENV_VARS

#: A shell dirtied the way a real one is. The values are deliberately usable —
#: a malformed URL would take a different branch in ``wire_session`` and the
#: guard would then be testing the error path rather than the routing one.
DIRTY_SHELL = {
    "ANTHROPIC_BASE_URL": "https://gateway.example.invalid",
    "ANTHROPIC_CUSTOM_HEADERS": "X-Pipeline-Id: someone-elses-run",
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "CLAUDE_CODE_USE_VERTEX": "1",
}


@pytest.fixture(scope="module", autouse=True)
def _dirty_shell() -> Iterator[None]:
    """Export the interfering variables for every test in this module.

    MODULE scope is load-bearing, not tidiness. pytest instantiates fixtures
    outermost-scope-first, so this runs BEFORE conftest's function-scoped
    autouse ``isolated_home`` — which is the only ordering in which "the
    variable is gone" is evidence of anything. Function scope here would run
    after the clearing and the assertions would be testing this fixture.

    ``pytest.MonkeyPatch.context()`` rather than the ``monkeypatch`` fixture,
    which is function-scoped and cannot be requested from module scope.
    """
    with pytest.MonkeyPatch.context() as patch:
        for name, value in DIRTY_SHELL.items():
            patch.setenv(name, value)
        yield


def test_an_ambient_shell_is_cleared_before_a_test_runs() -> None:
    """Every name this module exported is gone by the time a test body runs."""
    still_set = {name: os.environ[name] for name in DIRTY_SHELL if name in os.environ}
    assert not still_set, (
        f"conftest left {sorted(still_set)} set, so the suite still inherits the "
        "shell it was started from — a developer running it from inside a coding "
        "agent gets failures that CI cannot reproduce"
    )


def test_the_harness_sees_a_clean_machine() -> None:
    """The product's own report agrees, not just ``os.environ``.

    ``interfering_env`` is what ``doctor`` prints and what
    ``test_interfering_env_lists_only_set_vars`` asserts is empty. Reading it
    here states the property in the product's terms rather than the fixture's.
    """
    assert harness.interfering_env() == [], (
        "the harness still sees interfering variables inside a test, so anything "
        "asserting an unpinned model or a traced launch is at the mercy of the "
        "caller's shell"
    )


# --- the static half: conftest's list against the product's own ------------------
#
# The behavioural guard above only sees the names DIRTY_SHELL happens to list. It
# cannot notice a variable the product starts reading tomorrow — and that is the
# defect being fixed here, since conftest was already missing four. So the lists
# are compared directly, the way tests/test_spawn_seams.py pins TRACING_ENV_VARS
# against RESERVED_ENV_VARS.
#
# Compared as SETS, not sequences: unlike that pair, nothing here is user-visible
# text, conftest's order is iteration order for `delenv`, and the two lists are
# grouped differently on purpose — conftest's is organised by where a variable
# comes from, and the product's by what reads it.


def test_conftest_clears_everything_the_harness_calls_interfering() -> None:
    """A name `interfering_env` reports must be one the suite does not inherit.

    These are the same list read for two purposes: the product reports them so
    an operator knows their shell is overriding model selection, and the suite
    clears them so its own results are not that operator's shell. A name added
    to the product and not to conftest makes `interfering_env() == []` — which
    `test_harness.py` asserts — depend on who ran pytest.
    """
    missing = sorted(set(harness.INTERFERING_ENV_VARS) - set(AMBIENT_ENV_VARS))
    assert not missing, (
        f"core.harness.INTERFERING_ENV_VARS names {missing}, which conftest does "
        "not clear. The suite now inherits them from the caller's shell: any test "
        "asserting an unpinned model passes in CI and fails on the machine of "
        "whoever has them set."
    )


def test_conftest_clears_everything_the_wiring_stands_down_for() -> None:
    """Same, for the variables that cost a TRACED LAUNCH rather than a model.

    Both spellings are read because they are deliberately two copies —
    ``core`` must not import ``services`` — and covering only one would leave
    the suite inheriting whichever name the other list grows first.
    """
    from aisquare.core import spawn
    from aisquare.services import explainability

    reserved = set(explainability.RESERVED_ENV_VARS) | set(spawn.TRACING_ENV_VARS)
    missing = sorted(reserved - set(AMBIENT_ENV_VARS))
    assert not missing, (
        f"the wiring stands down on {missing} — 'not overriding your routing, "
        "launching untraced' — and conftest does not clear them, so every test "
        "that asserts a traced launch depends on the caller's shell"
    )
