"""Shared fixtures: isolated home directory, fresh runtime state, CLI runner."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.core.paths import HOME_ENV_VAR
from aisquare.core.state import reset_state


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
    # The team-bus and brain knobs are read from the ambient env; clear them so
    # the suite is hermetic (an embedding user's AISQUARE_BRAIN_EMBED=1 must not
    # change what tests build/assert), each test opting in explicitly instead.
    for knob in (
        "AISQUARE_TEAM",
        "AISQUARE_ROLE",
        "AISQUARE_TEAM_HUB",
        "AISQUARE_TEAM_DELTA",
        "AISQUARE_TEAM_LEASE_MIN",
        "AISQUARE_BRAIN",
        "AISQUARE_BRAIN_EMBED",
        "AISQUARE_BRAIN_EMBED_MODEL",
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
