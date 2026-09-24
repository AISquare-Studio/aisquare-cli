"""Shared fixtures: isolated home directory, fresh runtime state, CLI runner.

Also the session-start check that this run is judging THIS tree — see
``_foreign_package_reason``. It lives here rather than in a test file because a
test only runs when it is selected, and the invocation that gets this wrong is
the narrow one nobody selects it with.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as metadata_version
from pathlib import Path

import pytest
from typer.testing import CliRunner

import aisquare
from aisquare.core.paths import HOME_ENV_VAR
from aisquare.core.state import reset_state
from aisquare.services import ci_client

SRC = Path(__file__).resolve().parents[1] / "src"


def _foreign_package_reason(module_file: str | Path | None, src: Path) -> str | None:
    """Why this run is not judging `src`, or None when it is.

    With the src layout, a pytest from a sibling interpreter resolves
    ``aisquare`` out of that interpreter's site-packages, so the suite grades a
    stale snapshot while appearing to grade the checkout. Both directions of
    that lie have now cost this project time: a false RED on 2026-08-07, when
    tests for new code failed against an old install; and a false GREEN, when a
    run against an installed copy passed and was reported as a gate.

    Measured on 2026-08-17 at 8fafdd4, in a fresh worktree with no ``.venv``:
    `PATH=$PWD/.venv/bin:$PATH` expands to a directory that does not exist, so
    PATH falls through to the pyenv shim. The FULL suite still fails loudly —
    17 collection errors, and `tests/test_packaging.py` asserts this same
    property — but `pytest tests/test_config.py` reported **5 passed** against
    the stale package, because that file does not select the guard. A subset run
    is what everyone types while iterating, so that is the hole this closes.

    ``module_file`` is ``aisquare.__file__``, which is ``None`` when the package
    resolved as a PEP 420 NAMESPACE package rather than a real one. @9bbc8ed7
    spotted that `Path(None)` raises, which in a session-start hook means pytest
    dies with a raw TypeError traceback — the least explanatory failure in the
    repo, produced by the one function whose whole job is to explain a failure.
    They could not construct a route to it; it is constructible, and the route is
    worth knowing. PEP 420 only forms a namespace package when NO regular
    ``aisquare/__init__.py`` exists anywhere on ``sys.path``, so an editable
    checkout can never reach it — the real package always wins, verified by
    putting a bare ``aisquare/`` directory FIRST on the path and watching
    ``__file__`` still resolve to ``src``. It takes no real package on the path
    at all, plus a namespace tree supplying the two modules this file imports at
    module level. Reproduced under ``python -S`` with exactly that: ``__file__``
    is None, these imports succeed, and the hook raises.
    """
    if module_file is None:
        return (
            "aisquare has no __file__, which means it resolved as a namespace "
            "package rather than a real one: there is no aisquare/__init__.py "
            f"anywhere on sys.path, and something is supplying its submodules.\n"
            f"This run cannot be grading {src}. Install the checkout — "
            'python3 -m venv .venv && ./.venv/bin/python -m pip install -e ".[dev]" '
            "— and check sys.path for a stray directory named aisquare."
        )
    resolved = Path(module_file).resolve()
    if resolved.is_relative_to(src):
        return None
    return (
        f"aisquare imported from {resolved}, not from {src}.\n"
        "This run would grade an installed copy rather than this checkout, so "
        "both a pass and a failure would be meaningless.\n"
        "Fix: create the venv and install into it — python3 -m venv .venv && "
        './.venv/bin/python -m pip install -e ".[dev]" — then run '
        "PATH=$PWD/.venv/bin:$PATH make check.\n"
        "Note that PATH=$PWD/.venv/bin:$PATH is NOT enough on its own: if "
        ".venv does not exist yet, that prefix is a non-existent directory and "
        "PATH falls through to whatever python comes next."
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    """Refuse to grade the wrong tree, before a single test runs.

    The raw ``__file__`` is handed over unresolved on purpose: it can be None,
    and the checker is where that is handled and tested. Resolving here would put
    the one unguarded conversion outside everything that tests it.
    """
    reason = _foreign_package_reason(aisquare.__file__, SRC)
    if reason is not None:
        pytest.exit(reason, returncode=4)


def _sdk_installed() -> bool:
    """Whether the SDK distribution is present in THIS interpreter."""
    try:
        metadata_version("aisquare")
    except PackageNotFoundError:
        return False
    return True


_SDK_AT_START = _sdk_installed()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Refuse to end a run that installed something into its own interpreter.

    ``sessionstart`` proves the run is grading this tree. Nothing proved it was
    still grading it at the end, and it was not: ``doctor --fix --yes`` used to
    reach ``pip install aisquare[explainability]``, three tests invoke that
    command, and the SDK shares this package's import name. The install landed
    in site-packages, which precedes the editable ``src`` on ``sys.path``, so
    from that moment every subprocess got a CLI with no ``aisquare.cli`` — 15
    failures, none of them in the test that caused it, and the venv stayed
    broken after pytest exited.

    Checked at the END rather than per-test because the mechanism is not
    specific to pip or to that command: anything that writes a distribution
    into ``sys.executable``'s environment invalidates the whole run, and the
    honest report is "these results do not describe this tree" rather than one
    unlucky test's traceback. Stated as a warning plus a non-zero status: the
    per-test failures are already loud, and this is the sentence that explains
    them.
    """
    if _sdk_installed() and not _SDK_AT_START:
        session.exitstatus = max(exitstatus, 1)
        print(
            "\nFATAL: this run installed the 'aisquare' distribution into "
            f"{sys.executable}.\n"
            "It shares this package's import name, so every result after the "
            "install graded a shadowed CLI, and this environment is now broken "
            "for ordinary use.\n"
            "Recover with: pip uninstall aisquare\n"
            "Then find the caller — a test reaching a real install rather than "
            "a patched one."
        )


#: Every variable this package reads off the AMBIENT environment, cleared before
#: each test so the suite grades this tree rather than the shell that started it.
#: A module constant rather than an inline tuple because
#: ``tests/test_conftest_is_hermetic.py`` compares it against the product's own
#: lists — the four routing names below were missing for exactly as long as there
#: was nothing to compare against. The per-role families, which no list of names
#: can cover, are :data:`AMBIENT_ENV_PREFIXES` below.
AMBIENT_ENV_VARS = (
    # Agent detection honours CLAUDE_CONFIG_DIR, and a developer running the
    # suite from inside a Claude session must never have tests write hooks into
    # their real config directory. In the tuple rather than on its own
    # `delenv` line so this really is the single answer to "what does the suite
    # clear" — the guards below read only this name, so a variable cleared
    # elsewhere would be reported as uncleared and send the reader to the wrong
    # file.
    "CLAUDE_CONFIG_DIR",
    # Its sibling: the two are what a Claude account IS for a launch
    # (`core.claude_accounts.LAUNCH_VARS`). A launch on the default account
    # restores the shell's own, and a sign-in window carries this process's
    # (`services.claude_accounts.carry_environment`), so a developer with a
    # second login exported would see its directory in both. The account tests
    # cleared it locally; this makes it the suite's answer rather than theirs.
    "CLAUDE_CODE_TMPDIR",
    # The copies of the shell's own two a launch onto a managed slot keeps
    # (`core.claude_accounts.PLAIN_VARS`). `plain_environment` reads them
    # whenever CLAUDE_CONFIG_DIR names a managed slot, which the account tests
    # set up, so a suite run from a fleet pane on one of the developer's slots
    # resolved slot 1 to THEIR directory: four tests went red, and the
    # hand-over test read that directory's login (review of #205, seventh
    # round).
    "AISQUARE_PLAIN_CLAUDE_CONFIG_DIR",
    "AISQUARE_PLAIN_CLAUDE_CODE_TMPDIR",
    "AISQUARE_TEAM",
    "AISQUARE_ROLE",
    # Read off the ambient env by `services/mcp_server.py` to attribute remote
    # calls — same family as the two above, and missed for the same reason.
    "AISQUARE_SERVE_CLIENT",
    "AISQUARE_SERVE_ROLE",
    # `aisquare serve`'s --port and --close-after, read through typer's
    # `envvar=` rather than `os.environ`, so a sweep for the latter misses them.
    # Measured: AISQUARE_SERVE_PORT=1 in the shell fails test_serve.py's
    # show-token tests, which print the port a client should dial.
    "AISQUARE_SERVE_PORT",
    "AISQUARE_SERVE_CLOSE_AFTER",
    "AISQUARE_TEAM_HUB",
    "AISQUARE_TEAM_DELTA",
    "AISQUARE_TEAM_LEASE_MIN",
    "AISQUARE_DB_BUSY_MS",
    "AISQUARE_BRAIN",
    "AISQUARE_BRAIN_EMBED",
    "AISQUARE_BRAIN_EMBED_MODEL",
    "AISQUARE_HARNESS_PROBE",
    "AISQUARE_EFFORT",
    "AISQUARE_EFFORT_PLANNER",
    "AISQUARE_EFFORT_CODER",
    "AISQUARE_EFFORT_RUNNER",
    "AISQUARE_EFFORT_VALIDATOR",
    "CLAUDE_EFFORT",
    # A fleet agent's identity, and the process behind it. Both are ambient
    # for a developer running the suite from inside a fleet pane or from
    # inside Claude Code (which exports CLAUDE_PID to every subprocess) —
    # left set, every session start would resolve THEIR row and THEIR pid.
    "AISQUARE_FLEET_AGENT",
    "CLAUDE_PID",
    "AISQUARE_MODEL_PLANNER",
    "AISQUARE_MODEL_CODER",
    "AISQUARE_MODEL_RUNNER",
    "AISQUARE_MODEL_VALIDATOR",
    # The executable every role launches on unless something more specific
    # names one (`core.harness.resolve_binary`). Left set, `launch` and
    # `fleet spawn` resolve the developer's wrapper instead of the default —
    # measured: test_role_profile.py's launch tests fail with it exported.
    "AISQUARE_AGENT_BIN",
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    # An operator's shell has these sourced from their explainability env
    # file; leaving them set would resolve THEIR gateway and key inside the
    # suite, so "this target is unconfigured" would pass or fail depending
    # on whose terminal ran it.
    "AISQUARE_EXPLAINABILITY_TARGET",
    "EXPLAINABILITY_GATEWAY_URL",
    "EXPLAINABILITY_API_KEY",
    # The same file's inbox path: `_init_sdk` keeps an operator's instead of
    # pinning one under the isolated home. Its AISQUARE_AGENT_NAME and
    # EXPLAINABILITY_AGENTS are not here because this package never reads them
    # — only the SDK does, and the checkout the suite grades cannot have the
    # SDK installed beside it (`services.explainability.EDITABLE_INSTALL_HINT`).
    "EXPLAINABILITY_INBOX_PATH",
    # The escape hatch that keeps `--session-id` out of the agent's argv. An
    # operator who turned pinning off would fail every test asserting a pinned
    # launch — measured on test_role_profile.py's
    # test_an_unbound_role_still_gets_its_id_pinned.
    "AISQUARE_PIN_SESSION_ID",
    # The routing half, and the half that was missing. Two mechanisms read these
    # and both do the right thing on finding them set, which is what made the
    # omission invisible: `core.harness.interfering_env` REPORTS them, and
    # `wire_session` STANDS DOWN — "already set — not overriding your routing,
    # launching untraced". So a test asserting an unpinned model or a traced
    # launch passed in CI and failed for anyone whose shell had them.
    #
    # EVERY Claude Code session exports ANTHROPIC_BASE_URL — that is, the
    # machine of anyone who develops this with an agent. Measured here: the four
    # tests named in tests/test_conftest_is_hermetic.py fail with these set and
    # pass with them unset, on one tree, one commit, one machine.
    "ANTHROPIC_BASE_URL",  # both mechanisms
    "ANTHROPIC_CUSTOM_HEADERS",  # wire_session
    "CLAUDE_CODE_USE_BEDROCK",  # interfering_env
    "CLAUDE_CODE_USE_VERTEX",  # interfering_env
    # The MARKER half of a tracing identity (core.spawn.MARKER_ENV_VARS).
    # `core.insights.run_key` files every insight under AISQUARE_PIPELINE_ID
    # when it is set, so a suite run from inside a traced session grades
    # whoever launched it. This is the likeliest name of all to be set for the
    # audience above: a traced `aisquare launch` / `team spawn` exports it into
    # every child, and the fleet's tmux server hands its environment to every
    # window it opens.
    "AISQUARE_PIPELINE_ID",
    "AISQUARE_TRACE_AGENT_NAME",
    # The third. `MARKER_ENV_VARS` "went from two names to three once, and the
    # copies that were prose rather than reads had to be chased down one at a
    # time" — its own words. This tuple was one of those copies, and the guard
    # below is what made it a read: it named this variable on merging main
    # without anyone going looking.
    "AISQUARE_RUN_TRACE_ID",
    # A sign-in token in the operator's shell would make every test run as them.
    "AISQUARE_TOKEN",
    # What `core.browser.open_url` launches. Its `is_headless` also reads
    # SSH_CONNECTION, SSH_TTY, CI, CODESPACES, DISPLAY and WAYLAND_DISPLAY, and
    # those stay out on purpose: it answers "headless" whenever stdout is not a
    # terminal, and under CliRunner or pytest's capture it never is, so no
    # command a test runs can reach a browser whatever they say — the tests of
    # the detection itself pass an environ. Clearing CI would cost something:
    # pytest reads it while it explains a failing assert, and it is why CI's
    # log shows the whole diff rather than a truncated one.
    "BROWSER",
    # The CI test bed's switches. An operator who has them exported would
    # otherwise run the suite's hooks against THEIR endpoint, with THEIR
    # token — measured once: four real POSTs to a listener during a green
    # run. Off is the state every test starts from; tests opt in. The
    # staging override is cleared with them: left set, it would turn every
    # direct_api descriptor a test serves into one that delivers.
    "AISQUARE_CI",
    "AISQUARE_CI_URL",
    "AISQUARE_CI_KEY",
    "AISQUARE_CI_RUN",
    "AISQUARE_CI_DELIVERY_OVERRIDE",
    # Read off the ambient env, and none of them fails the suite TODAY — swept
    # out of `src/` rather than waited for, because the docstring above claims
    # completeness and two guards now read this tuple, so a claim that is only
    # nearly true is worse than one that is checked.
    "XDG_CONFIG_HOME",  # diagnostics: resolves the developer's real gh config dir
    "GH_CONFIG_DIR",  # same
    "GH_TOKEN",  # diagnostics: "gh is not authenticated" by whose shell
    "GITHUB_TOKEN",  # same
    "EDITOR",  # core.editor: a seam that escapes its patch launches the real one
    "VISUAL",  # same
    "TERM",  # rendering assertions vary by terminal
    "TMUX_TMPDIR",  # core.tmux socket path
    "TMUX",  # services.fleet: shutdown's "inside the fleet's own server" guard
    # How a terminal renders. Read by the libraries rather than by `src/`, which
    # is why the sweep above missed them: every rich Console reads them when it
    # is built — `core.console` builds one per call, typer one per help or error
    # panel — and textual's App reads NO_COLOR. Measured on 0941fd0 in the
    # gate's clean env: COLUMNS=40 fails 11 tests (panels and tables wrap at 40
    # and split the sentences they assert), NO_COLOR=1 fails two in
    # test_terminal_pane.py (the pane renders monochrome). The rest fail nothing
    # today (measured) and are the same Console's height, colour and terminal
    # switches; COLORTERM is TERM's partner in choosing a colour system. typer
    # reads FORCE_COLOR, PY_COLORS and GITHUB_ACTIONS once, at import, before
    # any fixture can clear them — which is why `tests/rendered.py` strips
    # styling at the assert site.
    "COLUMNS",
    "LINES",
    "NO_COLOR",
    "FORCE_COLOR",
    "COLORTERM",
    "TTY_COMPATIBLE",
    "TTY_INTERACTIVE",
)

#: The per-role FAMILIES the harness reads — ``<PREFIX><ROLE>`` for whatever role
#: it is asked about (``core.harness._bin_env_var``, ``role_model_override``,
#: ``role_effort_override``). A team profile can bind a role no list knows, and
#: ``code-reviewer`` reads ``AISQUARE_BIN_CODE_REVIEWER``, so no tuple of names
#: can be complete: ``isolated_home`` clears every ambient name under these. The
#: MODEL and EFFORT names spelled out above predate this and cover four of
#: ``cli.launch.ROLES``' eight roles; tester, reviewer, manager and ui-tester
#: were read and not cleared.
AMBIENT_ENV_PREFIXES = ("AISQUARE_BIN_", "AISQUARE_MODEL_", "AISQUARE_EFFORT_")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point AISQUARE_HOME at a temp dir so tests never touch ``~/.aisquare``.

    Everything else it clears is :data:`AMBIENT_ENV_VARS` and every name under
    :data:`AMBIENT_ENV_PREFIXES`, which together are the single answer to "what
    does the suite clear" — including ``CLAUDE_CONFIG_DIR``, which used to be
    cleared on a line of its own here.
    """
    home = tmp_path / "aisquare-home"
    monkeypatch.setenv(HOME_ENV_VAR, str(home))
    # Read from the ambient env; cleared so the suite is hermetic (an embedding
    # user's AISQUARE_BRAIN_EMBED=1 must not change what tests build/assert),
    # each test opting in explicitly instead.
    for knob in AMBIENT_ENV_VARS:
        monkeypatch.delenv(knob, raising=False)
    for knob in [name for name in os.environ if name.startswith(AMBIENT_ENV_PREFIXES)]:
        monkeypatch.delenv(knob)
    # The command sweeps invoke `login` with no arguments. Without this it would
    # resolve config.toml's default and contact the REAL API from inside the test
    # suite. A loopback port nothing listens on refuses instantly, so the command
    # exits through its `unreachable` message and never leaves the machine. Tests
    # that exercise sign-in point --api-url at their own stub server.
    monkeypatch.setenv("AISQUARE_API_URL", "http://127.0.0.1:9")
    # The experiment settings are read once per process (ci_client._settings is
    # lru_cached, like core.insights._config), so a cached read from the previous
    # test's HOME would outlive the home it came from.
    ci_client.reset_cache()
    return home


@pytest.fixture(autouse=True)
def fresh_state() -> Iterator[None]:
    """Reset the global runtime state around every test."""
    reset_state()
    yield
    reset_state()


@pytest.fixture(autouse=True)
def isolated_agent_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point agent detection at a temp home so tests never read ``~/.claude*``.

    ``core.agents._home`` is the indirection its own docstring offers for this.
    Without it the claude-code doctor row read the developer's REAL
    ``~/.claude/settings.json`` — and since #84 it also globs ``~/.claude*`` for
    sibling installs and grades the binary each one's hooks name. A doctor row
    that depends on how the author's own machine is hooked is the ambient leak
    ``.github/workflows/ci.yml``'s ``ambient`` job exists to catch; green there
    and red on a hooked laptop, or the reverse. Tests that want Claude Code
    detected build the tree under their own fixture (``fake_home`` in
    test_agents.py) and re-point ``_home`` at it, which overrides this.
    """
    home = tmp_path / "agent-home"
    monkeypatch.setattr("aisquare.core.agents._home", lambda: home)
    return home


@pytest.fixture(autouse=True)
def no_hook_binary_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never run a hook's aisquare for its version; answer as this install.

    Hooks written by ``agents connect`` under the suite name whatever
    ``_aisquare_command`` resolves: the console script beside this interpreter
    when PATH has it, otherwise the machine's ``aisquare`` — a pyenv shim on the
    author's box. Which one is ambient state, and running it would grade the
    developer's PATH rather than this tree. Same shape as ``no_repomix`` below.
    Tests of the probe itself capture the real function at import and call it
    against fake scripts they write (test_doctor_stale_hook_binary.py).
    """
    from aisquare.core import agents
    from aisquare.core.version import __version__

    monkeypatch.setattr(agents, "hook_binary_version", lambda argv, **_kwargs: __version__)


@pytest.fixture(autouse=True)
def no_repomix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the repomix subprocess by default so tests never shell out.

    Snapshot generation degrades to "skipped". Tests that exercise the packing
    logic override ``snapshot._run_repomix`` with a fake returning synthetic XML.
    """
    from aisquare.core import snapshot

    def _unavailable(*_args: object, **_kwargs: object) -> tuple[str, str]:
        raise snapshot.RepomixUnavailableError("repomix disabled in tests")

    monkeypatch.setattr(snapshot, "_run_repomix", _unavailable)


@pytest.fixture(autouse=True)
def no_detached_distill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests from launching detached distiller processes.

    Team commands fire-and-forget `aisquare team distill` after durable events;
    in tests that would race the temp home and outlive the test. Distiller
    behaviour is tested by calling ``distill.drain`` directly (test_brain.py).
    """
    from aisquare.services import distill

    monkeypatch.setattr(distill, "spawn_drain", lambda cwd=None, *, root=None: None)


@pytest.fixture
def runner() -> CliRunner:
    """A Click test runner for invoking the Typer app."""
    return CliRunner()


@pytest.fixture(autouse=True)
def no_model_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable harness availability probes by default so tests never shell out.

    Ladder resolution degrades to "optimistic" (pick the head rung unprobed).
    Tests that exercise probing override ``harness.probe_model`` with a fake.
    """
    monkeypatch.setenv("AISQUARE_HARNESS_PROBE", "0")
