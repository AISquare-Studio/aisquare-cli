"""``--version`` prints the installed package version."""

from __future__ import annotations

import importlib

import pytest
from typer.testing import CliRunner

from aisquare import __version__
from aisquare.cli.app import app


def test_version_flag(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert f"aisquare {__version__}" in result.output


def test_version_short_flag(runner: CliRunner) -> None:
    result = runner.invoke(app, ["-V"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_cli_survives_a_shadowed_root_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI must not read its version off the ``aisquare`` package root.

    The Explainability SDK publishes as distribution ``aisquare`` and ships a
    REGULAR package of that same name, so ``pip install`` of the extra
    overwrites ``site-packages/aisquare/__init__.py`` with the SDK's — and
    ``pip uninstall`` of it deletes the shared file outright, leaving a
    namespace package with no attributes at all. Verified on 2026-08-17: with
    ``from aisquare import __version__`` at import time, both directions ended
    in ``ImportError: cannot import name '__version__' from 'aisquare'`` on
    EVERY command, an unrecoverable-looking brick for an operator mid-cutover.
    Distribution metadata survives both, so that is what we read.
    """
    import aisquare
    import aisquare.cli.app as app_module

    monkeypatch.delattr(aisquare, "__version__", raising=False)
    reloaded = importlib.reload(app_module)
    try:
        result = CliRunner().invoke(reloaded.app, ["--version"])
        assert result.exit_code == 0, result.output
        assert "aisquare " in result.output
        assert "0.0.0+uninstalled" not in result.output
    finally:
        importlib.reload(app_module)
