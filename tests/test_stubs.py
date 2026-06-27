"""Every *unimplemented* leaf command is wired to the shared stub: exit 70.

``result.output`` is the mixed stdout+stderr stream, so the stub's stderr
message is visible there. Commands that have since been implemented are listed
in ``IMPLEMENTED`` and skipped here — they have their own dedicated tests.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.stubs import EXIT_NOT_IMPLEMENTED
from tests.cli_tree import leaf_invocations

# Leaf command paths that are now real (canonical and alias forms). Keep in sync
# as commands graduate from stub to service.
IMPLEMENTED: set[tuple[str, ...]] = {
    ("remember",),
    ("inject",),
    ("why",),
    *(
        (group, command)
        for group in ("context", "ctx")
        for command in (
            "add",
            "list",
            "show",
            "edit",
            "remove",
            "search",
            "promote",
            "import",
            "export",
            "preview",
        )
    ),
}


def _is_stubbed(argv: list[str]) -> bool:
    return not any(tuple(argv[: len(path)]) == path for path in IMPLEMENTED)


@pytest.mark.parametrize(
    "argv",
    [argv for argv in leaf_invocations() if _is_stubbed(argv)],
    ids=lambda a: " ".join(a),
)
def test_leaf_commands_are_stubbed(runner: CliRunner, argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == EXIT_NOT_IMPLEMENTED, result.output
    assert "is not implemented yet" in result.output


def test_every_implemented_command_is_still_a_leaf() -> None:
    # Guard against IMPLEMENTED drifting out of sync with the real command tree.
    leaves = {tuple(argv) for argv in leaf_invocations()}
    for path in IMPLEMENTED:
        assert any(leaf[: len(path)] == path for leaf in leaves), path


def test_stub_message_goes_to_stderr(runner: CliRunner) -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == EXIT_NOT_IMPLEMENTED
    assert "⚠ aisquare doctor is not implemented yet (planned: v0)" in result.stderr
    assert result.stdout == ""


def test_workspace_alias_reports_canonical_command(runner: CliRunner) -> None:
    # An alias (workspace → project) still reports the canonical command name.
    result = runner.invoke(app, ["workspace", "info"])
    assert result.exit_code == EXIT_NOT_IMPLEMENTED
    assert "aisquare project info is not implemented yet" in result.output
