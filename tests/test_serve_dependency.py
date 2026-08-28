"""``aisquare serve`` must name what is wrong with its dependency, not traceback.

Dependabot #55 proposes relaxing ``mcp>=1.10,<2`` to ``<3``. That resolves to
mcp 2.0.0, which deleted ``mcp.server.fastmcp`` — the module
``services/mcp_server.build_server`` imports. Reviewing it surfaced a second,
independent defect: the guard in ``cli/serve.py`` probes the DISTRIBUTION
(``find_spec("mcp")``) rather than the symbol the server actually needs, so an
incompatible major sails through it and the user gets a raw
``ModuleNotFoundError`` traceback instead of the CLI's error contract.

The distinction matters because the two failures need different fixes — "install
the extra" versus "the mcp you have is too new" — and a traceback tells the user
neither.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from aisquare.cli import serve as serve_cli
from aisquare.cli.app import app


def test_a_missing_extra_is_reported_as_the_error_contract(runner: CliRunner) -> None:
    """No mcp at all: the existing behaviour, now pinned."""

    def _absent(name: str) -> None:
        return None

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(serve_cli, "_find_spec", _absent)
        result = runner.invoke(app, ["serve", "--stdio"])

    assert result.exit_code == 1
    assert "serve extra is not installed" in result.output
    assert "aisquare-cli[serve]" in result.output
    assert "Traceback" not in result.output


def test_an_incompatible_mcp_major_is_reported_as_such(runner: CliRunner) -> None:
    """mcp present, ``mcp.server.fastmcp`` gone — exactly mcp 2.x.

    Before this test the guard passed and ``build_server`` raised a bare
    ModuleNotFoundError through the CLI. The message must distinguish this from
    a missing extra, because "install the extra" is the wrong advice here — the
    extra IS installed, at a version that cannot work.
    """

    def _only_the_distribution(name: str) -> object | None:
        return None if name == serve_cli.REQUIRED_MODULE else object()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(serve_cli, "_find_spec", _only_the_distribution)
        result = runner.invoke(app, ["serve", "--stdio"])

    assert result.exit_code == 1
    assert serve_cli.REQUIRED_MODULE in result.output
    assert "Traceback" not in result.output
    assert "not installed" not in result.output, (
        "the extra IS installed here — saying otherwise sends the user to the wrong fix"
    )


def test_a_parent_that_cannot_be_imported_is_not_an_unhandled_error(
    runner: CliRunner,
) -> None:
    """``find_spec('mcp.server.fastmcp')`` RAISES when ``mcp`` is absent.

    Not a hypothetical: that is the standard-library contract for a dotted name
    whose parent will not import, and it is the shape a user without the extra
    actually hits.
    """

    def _raises(name: str) -> object | None:
        if name == serve_cli.REQUIRED_MODULE:
            raise ModuleNotFoundError("No module named 'mcp'")
        return None

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(serve_cli, "_find_spec", _raises)
        result = runner.invoke(app, ["serve", "--stdio"])

    assert result.exit_code == 1
    assert "serve extra is not installed" in result.output


def test_the_guard_probes_the_symbol_the_server_imports() -> None:
    """The two must not drift: a rename in mcp_server has to move this too."""
    from pathlib import Path

    source = Path(serve_cli.__file__).resolve().parent.parent / "services" / "mcp_server.py"
    body = source.read_text(encoding="utf-8")

    assert f"from {serve_cli.REQUIRED_MODULE} import" in body, (
        f"cli/serve.py guards on {serve_cli.REQUIRED_MODULE}, but mcp_server.py no longer "
        "imports it — the guard is now checking for something nobody needs"
    )


def test_an_error_message_is_data_not_a_rich_template(runner: CliRunner) -> None:
    """Found here, but not specific to serve: `fail()` was rendering markup.

    Rich reads ``[...]`` as a style tag and deletes it, so the shipped hint
    reached the user as ``pip install 'aisquare-cli'`` — the extra name, the one
    token that makes the command work, silently removed. Every fail message
    interpolates user-controlled text, so a path, ref or role name containing
    brackets was mangled the same way.
    """

    def _absent(name: str) -> None:
        return None

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(serve_cli, "_find_spec", _absent)
        result = runner.invoke(app, ["serve", "--stdio"])

    assert "aisquare-cli[serve]" in result.output, "the extra name must survive rendering"


def test_bracketed_text_in_any_failure_survives(runner: CliRunner) -> None:
    """The general case, pinned so the fix cannot be reverted by tidying."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(serve_cli, "_find_spec", lambda name: None)
        patch.setattr(
            serve_cli,
            "_dependency_error",
            lambda: "cannot read /home/me/[archive]/repo — check the path",
        )
        result = runner.invoke(app, ["serve", "--stdio"])

    assert "[archive]" in result.output
