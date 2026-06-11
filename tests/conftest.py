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
    """Point AISQUARE_HOME at a temp dir so tests never touch ``~/.aisquare``."""
    home = tmp_path / "aisquare-home"
    monkeypatch.setenv(HOME_ENV_VAR, str(home))
    return home


@pytest.fixture(autouse=True)
def fresh_state() -> Iterator[None]:
    """Reset the global runtime state around every test."""
    reset_state()
    yield
    reset_state()


@pytest.fixture
def runner() -> CliRunner:
    """A Click test runner for invoking the Typer app."""
    return CliRunner()
