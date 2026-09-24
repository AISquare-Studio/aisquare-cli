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
    # A developer running the suite from a fleet pane, with a wrapper bound and
    # session-id pinning turned off. The per-role names are ones conftest spells
    # out nowhere — fleet roles, and a role only a team profile would bind — so
    # only its AMBIENT_ENV_PREFIXES can clear them.
    "TMUX": "/tmp/tmux-1000/aisquare-fleet,4242,0",
    "AISQUARE_AGENT_BIN": "claude-wrapper",
    "AISQUARE_BIN_UI_TESTER": "claude-wrapper",
    "AISQUARE_BIN_CODE_REVIEWER": "claude-wrapper",
    "AISQUARE_MODEL_TESTER": "claude-someone-elses-pin",
    "AISQUARE_EFFORT_REVIEWER": "low",
    "AISQUARE_PIN_SESSION_ID": "0",
    "AISQUARE_SERVE_PORT": "1",
    "EXPLAINABILITY_INBOX_PATH": "/somewhere/theirs.db",
    # A second Claude login's scratch directory, exported the way the README's
    # role bindings set it.
    "CLAUDE_CODE_TMPDIR": "/home/someone/.cache/claude-account1",
    # The shell's own two, as a launch onto one of that login's managed slots
    # keeps them (`claude_accounts.PLAIN_VARS`) — read as slot 1 whenever a test
    # points CLAUDE_CONFIG_DIR at a managed slot, as the account tests do.
    "AISQUARE_PLAIN_CLAUDE_CONFIG_DIR": "/home/someone/.claude",
    "AISQUARE_PLAIN_CLAUDE_CODE_TMPDIR": "/home/someone/.cache/claude",
    # A narrow terminal that has turned colour off. The width and NO_COLOR
    # alone fail 13 tests left set; conftest clears them with TERM.
    "COLUMNS": "40",
    "LINES": "10",
    "NO_COLOR": "1",
    "COLORTERM": "truecolor",
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


def test_every_role_resolves_to_the_trees_defaults() -> None:
    """The harness's per-role answers are this tree's, whichever role is asked.

    ``AISQUARE_BIN_<ROLE>``, ``AISQUARE_MODEL_<ROLE>`` and
    ``AISQUARE_EFFORT_<ROLE>`` are families rather than names, so the roles read
    here are every role ``launch`` offers plus one only a team profile would
    bind — a name no list in conftest could have spelled out. Session-id
    pinning is asked too: a shell that turned it off fails every test asserting
    a pinned launch.
    """
    from aisquare.cli.launch import ROLES
    from aisquare.services import explainability

    for role in (*ROLES, "code-reviewer"):
        assert harness.resolve_binary(role).source == "default", role
        assert harness.role_model_override(role) is None, role
        assert harness.role_effort_override(role) is None, role
    assert explainability.accepts_session_id(harness.DEFAULT_AGENT_BINARY), (
        "the caller's AISQUARE_PIN_SESSION_ID turned session-id pinning off inside a test"
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

    ``IDENTITY_ENV_VARS``, not ``TRACING_ENV_VARS``: the first version of this
    guard reached for the narrower of two constants defined twelve lines apart,
    and so missed the MARKER half — ``AISQUARE_PIPELINE_ID`` and
    ``AISQUARE_TRACE_AGENT_NAME``, which `core.insights.run_key` reads straight
    off the environment. ``core/spawn.py``'s own module docstring says which one
    a stripping seam uses, and this is the same question.
    """
    from aisquare.core import spawn
    from aisquare.services import explainability

    reserved = set(explainability.RESERVED_ENV_VARS) | set(spawn.IDENTITY_ENV_VARS)
    missing = sorted(reserved - set(AMBIENT_ENV_VARS))
    assert not missing, (
        f"the wiring stands down on {missing} — 'not overriding your routing, "
        "launching untraced' — and conftest does not clear them, so every test "
        "that asserts a traced launch depends on the caller's shell"
    )


def test_conftest_clears_both_variables_an_account_is() -> None:
    """Both of ``core.claude_accounts.LAUNCH_VARS``, not only the one detection reads.

    A launch on the default account restores the SHELL's value of each, and a
    sign-in window carries this process's, so either left set makes a test's
    "plain claude of this shell" the developer's second login. Conftest cleared
    ``CLAUDE_CONFIG_DIR`` for agent detection and not ``CLAUDE_CODE_TMPDIR``
    beside it — the account tests cleared that one themselves, which is why
    nothing failed (review of #203, round 1).
    """
    from aisquare.core import claude_accounts

    missing = sorted(set(claude_accounts.LAUNCH_VARS) - set(AMBIENT_ENV_VARS))
    assert not missing, (
        f"a Claude account is {list(claude_accounts.LAUNCH_VARS)} for a launch, and "
        f"conftest does not clear {missing}, so the default account a test launches "
        "is whichever login the caller's shell pointed at"
    )


def test_conftest_clears_the_copies_a_launch_onto_a_managed_slot_keeps() -> None:
    """Both of ``core.claude_accounts.PLAIN_VARS``' copies, beside the two above.

    ``plain_environment`` reads them as slot 1's whenever ``CLAUDE_CONFIG_DIR``
    names a managed slot, which the account tests set up, so a suite run from a
    fleet pane on one of the developer's slots resolved slot 1 to THEIR
    directory: four tests went red, and the hand-over test read that
    directory's login (review of #205, seventh round). #205 cleared them in a
    loop of its own in ``isolated_home``; since the #205 fold they are in
    ``AMBIENT_ENV_VARS``, the one list these guards read, and this keeps them
    there.
    """
    from aisquare.core import claude_accounts

    missing = sorted(set(claude_accounts.PLAIN_VARS.values()) - set(AMBIENT_ENV_VARS))
    assert not missing, (
        f"a launch onto a managed slot keeps the shell's own account under {missing}, "
        "and conftest does not clear them, so a test's slot 1 is the login of "
        "whoever ran the suite from a fleet pane"
    )
