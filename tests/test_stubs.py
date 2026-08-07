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
    ("init",),
    ("inject",),
    ("why",),
    ("status",),
    ("doctor",),
    ("log",),
    ("note",),
    ("board",),
    ("recall",),
    ("launch",),
    ("serve",),
    ("explainability", "status"),
    ("explainability", "env"),
    ("hook", "session-start"),
    ("hook", "user-prompt-submit"),
    ("hook", "session-end"),
    ("hook", "stop"),
    ("hook", "notification"),
    *(
        ("team", command)
        for command in (
            "on",
            "status",
            "focus",
            "role",
            "log",
            "distill",
            "prune",
            "verify",
            "signal",
            "signals",
            "spawn",
            "harness",
        )
    ),
    *(
        ("task", command)
        for command in (
            "add",
            "list",
            "show",
            "claim",
            "next",
            "review",
            "reopen",
            "done",
            "block",
            "drop",
            "release",
        )
    ),
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
    *(
        (group, command)
        for group in ("project", "workspace")
        for command in ("info", "list", "switch", "link", "onboard")
    ),
    *(("config", command) for command in ("list", "get", "set", "redaction")),
    *(("agents", command) for command in ("list", "scan", "status", "connect", "disconnect")),
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
    result = runner.invoke(app, ["open"])
    assert result.exit_code == EXIT_NOT_IMPLEMENTED
    assert "⚠ aisquare open is not implemented yet (planned: v0)" in result.stderr
    assert result.stdout == ""


def test_a_stubbed_group_command_still_reports_canonically(runner: CliRunner) -> None:
    # Group subcommands report their full canonical path in the stub message.
    result = runner.invoke(app, ["auth", "status"])
    assert result.exit_code == EXIT_NOT_IMPLEMENTED
    assert "aisquare auth status is not implemented yet" in result.output
