"""Shared fixtures: isolated home directory, fresh runtime state, CLI runner.

Also the session-start check that this run is judging THIS tree — see
``_foreign_package_reason``. It lives here rather than in a test file because a
test only runs when it is selected, and the invocation that gets this wrong is
the narrow one nobody selects it with.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

import aisquare
from aisquare.core.paths import HOME_ENV_VAR
from aisquare.core.state import reset_state

SRC = Path(__file__).resolve().parents[1] / "src"


def _foreign_package_reason(module_file: Path, src: Path) -> str | None:
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
    """
    if module_file.is_relative_to(src):
        return None
    return (
        f"aisquare imported from {module_file}, not from {src}.\n"
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

    ``__file__ or ""`` matches the convention already in the tree (see
    ``services/explainability.py``'s ``_package_root``): a namespace package
    has ``__file__ = None``, and ``Path(None)`` raises ``TypeError``. Nobody
    could construct a realistic route to that state for ``aisquare`` — the SDK
    we actually collide with ships a real ``__init__.py`` — but this hook runs
    before EVERY pytest invocation in the repo, and its entire purpose is to
    replace a confusing failure with an explanatory one. Dying in a traceback
    would be the one outcome it exists to prevent.
    """
    reason = _foreign_package_reason(Path(aisquare.__file__ or "").resolve(), SRC)
    if reason is not None:
        pytest.exit(reason, returncode=4)


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
    # The orchestrator and brain knobs are read from the ambient env; clear them so
    # the suite is hermetic (an embedding user's AISQUARE_BRAIN_EMBED=1 must not
    # change what tests build/assert), each test opting in explicitly instead.
    for knob in (
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
    ):
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
