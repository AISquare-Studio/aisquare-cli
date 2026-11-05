"""The fleet's lifecycle on a real Claude Code, driven through the CLI as a user drives it.

Run ``AISQUARE_TEST_CLAUDE=1 pytest tests/test_claude_native_fleet.py``, in the pattern of
``tests/test_claude_native.py`` (its loopback Messages stub and its ``native`` fixture): a
config dir of its own, a throwaway tmux server, no account, network or model spend.

- ``fleet spawn``: the first message rides on the command line and is taken (the row reads
  ``acked`` once the agent's ``UserPromptSubmit`` has it).
- ``fleet tell``: typed into an idle agent, which runs it as its next turn; to a busy one,
  a board note addressed to it, which reaches the model at its next prompt.
- ``fleet pause`` and ``fleet resume``: the agent exits through its own ``/exit`` (its
  ``SessionEnd``), and comes back under its label resuming the same conversation.
- ``fleet restart`` of a running agent: the same, one agent.
- ``fleet stop`` and ``fleet reap``: the agent exits, its row ends ``stopped``, and nothing
  live is left.
"""

from __future__ import annotations

import json
import shlex
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from aisquare.cli.app import app
from aisquare.core import agents, selfcli
from tests.test_claude_native import (  # noqa: F401 - `native` and `native_options` are fixtures
    AGENT,
    Native,
    Turn,
    native,
    native_options,
    options,
    pytestmark,
)

runner = CliRunner()


def asq(on: Native, *args: str) -> Result:
    """``aisquare <args> -P <project>``, as typed, its exit code checked."""
    result = runner.invoke(app, [*args, "-P", on.project.id], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return result


def asq_json(on: Native, *args: str) -> Any:
    return json.loads(asq(on, "--json", *args).stdout)


@options()
def test_real_claude_code_spawn_takes_its_first_message_and_tell_reaches_it_idle_and_busy(
    native: Native,  # noqa: F811
) -> None:
    receipt = asq_json(native, "fleet", "spawn", "coder", "--no-worktree", "--prompt", "begin here")
    label = receipt["agent"]["label"]
    native.heard("begin here")
    native.until(lambda: native.row(label).first_prompt == "acked", 30, "the first message taken")
    row = native.row(label)
    native.until(lambda: native.state(row.session_id) == "waiting", 30, "idle", row.pane_id)
    native.ready(row.pane_id)
    told = asq_json(native, "fleet", "tell", label, "told while idle")
    assert (told["typed"], told["delivered"]) == ("yes", True), told
    native.heard("told while idle")
    native.until(lambda: native.state(row.session_id) == "waiting", 30, "idle", row.pane_id)
    native.ready(row.pane_id)
    native.type(row.pane_id, "a long turn [[hold]]")
    native.until(native.stub.holding.is_set, 30, "the turn held", row.pane_id)
    native.until(lambda: native.state(row.session_id) == "working", 30, "busy", row.pane_id)
    busy = asq_json(native, "fleet", "tell", label, "told while busy")
    assert busy["typed"] == "no", busy
    assert "told while busy" not in native.screen(row.pane_id), "nothing typed into its turn"
    native.stub.release()
    native.until(lambda: native.state(row.session_id) == "waiting", 30, "idle", row.pane_id)
    native.ready(row.pane_id)
    native.type(row.pane_id, "the next prompt")
    after = native.heard("the next prompt")
    assert "told while busy" in after.everything, "the note reached its next prompt"
    assert native.prompts().count("told while idle") == 1


@options()
def test_real_claude_code_pause_and_resume_bring_it_back_on_the_same_conversation(
    native: Native,  # noqa: F811
) -> None:
    receipt = native.spawn(prompt="before the pause")
    row = receipt.agent
    native.heard("before the pause")
    native.until(lambda: native.state(row.session_id) == "waiting", 30, "idle", row.pane_id)
    paused = asq(native, "fleet", "pause")
    assert row.label in paused.output, paused.output
    native.until(lambda: native.ended(row.session_id), 30, "its SessionEnd", row.pane_id)
    stopped = native.row(row.label)
    assert stopped.paused_at is not None and stopped.ended_by == "stopped"
    assert not native.listed(), "nothing live while paused"
    resumed = asq(native, "fleet", "resume")
    assert row.label in resumed.output, resumed.output
    [back] = native.until(native.listed, 30, "the agent back")
    assert back.label == row.label and back.session_id == row.session_id
    native.until(lambda: not native.ended(row.session_id), 30, "its SessionStart", back.pane_id)
    native.ready(back.pane_id)
    native.type(back.pane_id, "after the resume")
    turn = native.heard("after the resume")
    assert "before the pause" in turn.everything, "the same conversation"


def _restart_running(on: Native) -> tuple[Any, Any, Turn]:
    """A coder that ran a turn, restarted by ``fleet restart`` while it waits: the
    receipt, the replaced row and the resumed agent's first turn."""
    receipt = on.spawn(prompt="before the restart")
    row = receipt.agent
    on.heard("before the restart")
    on.until(lambda: on.state(row.session_id) == "waiting", 30, "idle", row.pane_id)
    restarted = asq_json(on, "fleet", "restart", row.label)
    return restarted, row, on.heard("restarted and resumed")


@options()
def test_real_claude_code_restart_of_a_running_agent_resumes_its_session(
    native: Native,  # noqa: F811
) -> None:
    restarted, row, turn = _restart_running(native)
    assert restarted["resumed"] is True and restarted["was_running"] is True, restarted
    assert restarted["started"]["session_id"] == row.session_id, "the same session id"
    assert restarted["replaced"]["id"] != restarted["started"]["id"], "a new row"
    [back] = native.listed()
    assert back.id == restarted["started"]["id"]
    # Resumed on its command line, the line that says so its first turn, on the same
    # conversation, the old process ended through its SessionEnd and the new one's
    # SessionStart (source ``resume``) taking the session back on the board.
    session = native.session(row.session_id)
    assert session is not None and session.ended_at is None
    args = " ".join(native.agent_argv(back.pane_id))
    assert f"--resume {session.transcript_path} -- " in args, args
    assert "before the restart" in turn.everything, "the same conversation"
    assert [e.kind for e in native.events()].count("restarted") == 1


@pytest.mark.xfail(
    strict=True,
    reason=(
        "F3 (board seq 11086), pre-existing on 0.9.2: Claude Code 2.1.318 sends a resume's "
        "command-line prompt without waiting for its SessionStart hooks, and drops the "
        "context of aisquare's (about 1 s)"
    ),
)
@options()
def test_real_claude_code_restarted_with_a_line_gets_its_session_start_context(
    native: Native,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resume line rides on the command line (0.9.0's delivery, kept: ruling 11087), and
    Claude Code 2.1.318 sends it without waiting for its SessionStart hooks, whose context
    is then lost. aisquare's hook takes about a second, which loses or wins by the machine's
    speed; held at 1.5 s here, as in the plain-Claude repro, it loses every time, so this
    reads XFAIL until Claude Code waits, and XPASS (red, strict) the day it does."""
    slow = native.outside / "slow-session-start.sh"
    slow.write_text(
        "#!/bin/sh\n"
        'case "$*" in *session-start*) sleep 1.5 ;; esac\n'
        f'exec {shlex.join(selfcli.argv_for([]))} "$@"\n',
        encoding="utf-8",
    )
    slow.chmod(0o755)
    agents.remove_hooks(AGENT, native.config_dir)
    monkeypatch.setattr(agents, "_aisquare_command", lambda *args, **kwargs: str(slow))
    agents.install_hooks(AGENT, native.config_dir)
    _, _, turn = _restart_running(native)
    assert "SessionStart:resume hook success: <aisquare-team>" in turn.everything


@options()
def test_real_claude_code_stop_ends_it_through_exit_and_reap_leaves_nothing_live(
    native: Native,  # noqa: F811
) -> None:
    receipt = native.spawn(prompt="about to stop")
    row = receipt.agent
    native.heard("about to stop")
    native.until(lambda: native.state(row.session_id) == "waiting", 30, "idle", row.pane_id)
    asq(native, "fleet", "stop", row.label)
    native.until(lambda: native.ended(row.session_id), 30, "its SessionEnd", row.pane_id)
    ended = native.row(row.label)
    assert ended.ended_at is not None and ended.ended_by == "stopped"
    asq(native, "fleet", "reap")
    assert native.listed() == []
    listing = asq_json(native, "fleet", "ls")
    assert not [a for a in _agents(listing) if a.get("state") not in ("exited", "lost", None)]


def _agents(listing: Any) -> list[dict[str, Any]]:
    """The agents of a ``fleet ls --json``, whatever its envelope."""
    if isinstance(listing, list):
        return listing
    for key in ("agents", "rows", "fleet"):
        if isinstance(listing.get(key), list):
            return listing[key]  # type: ignore[no-any-return]
    return []
