"""Claude Code's SessionEnd closes no turn, as 0.9.2's ``session-end`` did (review r40 of #113,
finding 4).

Claude Code fires no ``Stop`` for a turn the human interrupts (Esc). That turn stays open and
is left out of the wall-clock figures (``ContextStore.close_turn``'s rule: never closed with a
made-up end time). The shared event handler closed the newest open turn at every agent's
``SessionEnd``, so such a turn, followed by ``/exit`` or ``fleet stop``, got the session's end
as its end time and entered ``metrics``' median. Codex's ``SessionEnd`` still closes it
(``AgentCapabilities.session_end_closes_turn``): it fires no ``Stop`` for a failed turn.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.agent_adapters import get_adapter
from aisquare.services import metrics as metrics_service
from aisquare.services.agent_events import session_key

pytestmark = pytest.mark.usefixtures("project_capture")


def _payload(tmp_path: Path, session: str, **extra: object) -> str:
    return json.dumps({"session_id": session, "cwd": str(tmp_path), **extra})


def test_claude_codes_session_end_leaves_an_interrupted_turn_open(
    isolated_home: Path, tmp_path: Path, runner: CliRunner
) -> None:
    """Through the hook commands Claude Code runs: a prompt, no Stop (Esc), then the end."""
    session = "claude-esc"
    prompt = runner.invoke(
        app, ["hook", "user-prompt-submit"], input=_payload(tmp_path, session, prompt="go")
    )
    ended = runner.invoke(
        app, ["hook", "session-end"], input=_payload(tmp_path, session, reason="prompt_input_exit")
    )

    assert prompt.exit_code == 0 and ended.exit_code == 0, (prompt.output, ended.output)
    (turn,) = metrics_service.recent(session_id=session)
    assert turn.ended_at is None, "no Stop, no end time: open, as 0.9.2 left it"


def test_codexs_session_end_closes_its_open_turn(
    isolated_home: Path, tmp_path: Path, runner: CliRunner
) -> None:
    session = "codex-failed"
    home = tmp_path / ".codex"

    def hook(**fields: object) -> None:
        result = runner.invoke(
            app,
            ["hook", "codex", "--config-dir", str(home)],
            input=_payload(tmp_path, session, **fields),
        )
        assert result.exit_code == 0, result.output

    hook(hook_event_name="UserPromptSubmit", prompt="go", turn_id="one")
    hook(hook_event_name="SessionEnd", reason="exit")

    key = session_key(get_adapter("codex"), home, session)
    (turn,) = metrics_service.recent(session_id=key)
    assert turn.ended_at is not None
