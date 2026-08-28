"""A damaged board database must cost the board row, never the launch.

``cli/launch.py`` states the bar twice in its own comments — "a broken config
must not block a launch", and "tracing is an observer: a broken config must cost
the trace, never the launch — the same fail-open bar as a dead proxy". Both
guard ``load_config``. The one call on that path which opens the DATABASE,
``team_service.activate()``, catches only ``TeamDisabledError``, so a
``sqlite3.DatabaseError`` walks straight out.

Measured against a corrupt ``context.db``, with a marker file rather than string
matching so the child's execution cannot be confused with the command echoed
back in an error:

    healthy store  -> exit 0, no traceback, CHILD RAN
    damaged store  -> exit 1, a traceback, CHILD NEVER STARTED

So a broken config fails open and a broken store is fatal, on the same path,
against a doctrine the file itself quotes. That asymmetry is the defect. At
08:05 with a wedged database, no agent can be launched at all — the whole team
surface is down, and what the operator gets is a stack trace.

WHAT FAILING OPEN COSTS, STATED PLAINLY: the session is not registered on the
board, so it has no board row and therefore no join to a gateway Run. That is a
lost trace, which is exactly what the doctrine says to spend. ``project`` is
used for one thing after the call — the banner's board name — so nothing
functional depends on it.

WHAT IS DELIBERATELY NOT CHANGED: ``TeamDisabledError`` still fails the command.
Team being switched off is a decision the operator made and a refusal they
should see, not a malfunction to route around. The distinction is
"you turned this off" versus "this is broken".
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.core import paths

CORRUPT = b"this is not a sqlite database, and a launch must survive it"


@pytest.fixture
def handover(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Intercept the exec, and record that the launch reached it.

    `launch` ends in ``os.execve``, which REPLACES the process — an in-process
    test that lets it happen loses pytest itself, and the run reports success
    having asserted nothing. (It did: an earlier version of this file exited 0
    with no output at all, which is what that looks like.) The existing launch
    tests already spy on ``_exec`` for this reason and this reuses their shape.

    Reaching the hand-over is also the better property to assert. Whether the
    agent then does anything is the agent's business; what this file is about is
    whether the CLI still hands over when the board database is unreadable.
    """
    captured: dict[str, Any] = {}

    def fake_exec(binary: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(binary=binary, argv=argv, env=env)

    monkeypatch.setattr(launch_cli, "_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/local/bin/{cmd}")
    return captured


def _launch() -> Any:
    return CliRunner().invoke(app, ["launch", "coder"], catch_exceptions=True)


def test_the_control_launches_on_a_healthy_store(
    isolated_home: Path, handover: dict[str, Any]
) -> None:
    """The control, and it shares the shape of the case exactly.

    Without this, a damaged-store test that saw no child could be passing
    because the harness never launches anything at all.
    """
    CliRunner().invoke(app, ["init", "--yes"], catch_exceptions=False)

    result = _launch()

    assert result.exit_code == 0, result.output
    assert handover, "the harness never reaches the exec even on a healthy store"


def test_a_damaged_store_does_not_cost_the_launch(
    isolated_home: Path, handover: dict[str, Any]
) -> None:
    """THE defect: against the current build the agent is never handed control."""
    CliRunner().invoke(app, ["init", "--yes"], catch_exceptions=False)
    paths.db_path().write_bytes(CORRUPT)

    result = _launch()

    assert handover, (
        "a damaged context.db stopped the agent from starting — tracing is an "
        "observer and may cost a trace, never a launch"
    )
    assert result.exit_code == 0, f"the launch reported failure: {result.output}"


def test_a_damaged_store_does_not_produce_a_traceback(
    isolated_home: Path, handover: dict[str, Any]
) -> None:
    """The second half of fail-open: say why, in words."""
    CliRunner().invoke(app, ["init", "--yes"], catch_exceptions=False)
    paths.db_path().write_bytes(CORRUPT)

    result = _launch()

    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"unhandled {type(result.exception).__name__} reaches the operator as a "
        f"Python traceback: {result.exception}"
    )


def test_the_reason_is_on_stderr_and_names_the_consequence(
    isolated_home: Path, handover: dict[str, Any]
) -> None:
    """Fail-open silently is worse than failing loudly.

    An operator whose session is missing from the board needs to know it is
    missing and why, or they will spend the morning looking for a row that was
    never written. The sibling boundary in this file sets the wording bar:
    "explainability: config unreadable (…) — launching untraced".
    """
    CliRunner().invoke(app, ["init", "--yes"], catch_exceptions=False)
    paths.db_path().write_bytes(CORRUPT)

    output = _launch().output

    assert "board" in output, f"nothing says the board row was lost: {output}"
    assert "context.db" in output or "database" in output, f"nothing names what is broken: {output}"


def test_team_disabled_still_refuses(
    isolated_home: Path, handover: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boundary. "You turned this off" is not "this is broken".

    Routing around a deliberate refusal would turn a fail-open fix into a way of
    ignoring the operator's own configuration.
    """
    CliRunner().invoke(app, ["init", "--yes"], catch_exceptions=False)
    monkeypatch.setenv("AISQUARE_TEAM", "0")

    result = _launch()

    assert result.exit_code != 0, "a disabled team no longer refuses"
    assert not handover, "a disabled team still handed control to an agent"
