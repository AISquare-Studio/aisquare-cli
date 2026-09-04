"""Shared fixtures: isolated home directory, fresh runtime state, CLI runner.

Also the session-start check that this run is judging THIS tree — see
``_foreign_package_reason``. It lives here rather than in a test file because a
test only runs when it is selected, and the invocation that gets this wrong is
the narrow one nobody selects it with.
"""

from __future__ import annotations

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
#: was nothing to compare against.
AMBIENT_ENV_VARS = (
    "AISQUARE_TEAM",
    "AISQUARE_ROLE",
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
    "AISQUARE_MODEL_PLANNER",
    "AISQUARE_MODEL_CODER",
    "AISQUARE_MODEL_RUNNER",
    "AISQUARE_MODEL_VALIDATOR",
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
)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point AISQUARE_HOME at a temp dir so tests never touch ``~/.aisquare``.

    ``CLAUDE_CONFIG_DIR`` is cleared too: agent detection honours it, and a
    developer running the suite from inside a Claude session must never have
    tests write hooks into their real config directory.
    """
    home = tmp_path / "aisquare-home"
    monkeypatch.setenv(HOME_ENV_VAR, str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    # Read from the ambient env; cleared so the suite is hermetic (an embedding
    # user's AISQUARE_BRAIN_EMBED=1 must not change what tests build/assert),
    # each test opting in explicitly instead.
    for knob in AMBIENT_ENV_VARS:
        monkeypatch.delenv(knob, raising=False)
    return home


@pytest.fixture(autouse=True)
def fresh_state() -> Iterator[None]:
    """Reset the global runtime state around every test."""
    reset_state()
    yield
    reset_state()


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
