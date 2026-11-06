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
import time
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from aisquare.cli.app import app
from aisquare.core import agents
from aisquare.core.agent_adapters import claude_screen, get_adapter
from aisquare.services import fleet, team
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


#: A board change between an agent's start and its restart, which its resume's snapshot holds.
MOVED_ON = "the board moved on between the start and the restart"


@options()
def test_real_claude_code_restarted_after_a_board_change_gets_the_fresh_board(
    native: Native,  # noqa: F811
) -> None:
    """Its resume line rides on its command line (0.9.0's delivery, ruling 11087), and Claude
    Code 2.1.318 waits for its SessionStart all the same; what it does not add is a context
    byte-identical to one the resumed conversation already holds (tester 846e8c78, seq
    11202, correcting R6's F3). So a board that moved on since the start reaches the
    resumed request in the resume's snapshot: this fails if aisquare's hook or the restart
    loses it."""
    receipt = native.spawn(prompt="before the restart")
    row = receipt.agent
    native.heard("before the restart")
    native.until(lambda: native.state(row.session_id) == "waiting", 30, "idle", row.pane_id)
    noted = runner.invoke(app, ["note", MOVED_ON], catch_exceptions=False)  # from its folder
    assert noted.exit_code == 0, noted.output
    asq_json(native, "fleet", "restart", row.label)
    turn = native.heard("restarted and resumed")
    resumed = turn.everything.split("SessionStart:resume hook success: <aisquare-team>", 1)
    assert len(resumed) == 2, "the resume's context reached the model"
    assert MOVED_ON in resumed[1].split("</aisquare-team>", 1)[0], "with the fresh board"


@options(suggestions=True)
def test_real_claude_code_spawned_with_no_message_is_told_at_its_suggestion_placeholder(
    native: Native,  # noqa: F811
) -> None:
    """#92's P1 (ruled FIX): with prompt suggestions on, a fresh 2.1.318 session draws its
    placeholder with the cursor on its first letter, which the box read as a draft ``T``, so
    a tell to an agent spawned with no first message became a board note. It types now; a
    draft the human left in the box is still never typed over."""
    row = native.spawn().agent
    pane = row.pane_id
    native.until(lambda: native.session(row.session_id), 30, "its SessionStart", pane)
    native.until(lambda: native.state(row.session_id) == "waiting", 30, "waiting", pane)
    native.ready(pane)
    time.sleep(1)  # the placeholder drawn
    assert claude_screen.composer_text(native.srv.capture(pane).lines) == ""
    told = asq_json(native, "fleet", "tell", row.label, "told at the placeholder")
    assert told["typed"] == "yes", told
    native.heard("told at the placeholder")
    native.until(lambda: native.state(row.session_id) == "waiting", 30, "waiting", pane)
    native.ready(pane)
    native.srv.send_literal(pane, "my own draft")
    native.until(
        lambda: claude_screen.composer_text(native.srv.capture(pane).lines) == "my own draft",
        10,
        "the draft in its box",
        pane,
    )
    kept = asq_json(native, "fleet", "tell", row.label, "never over the draft")
    assert kept["typed"] == "no" and "draft" in kept["how"], kept
    assert claude_screen.composer_text(native.srv.capture(pane).lines) == "my own draft"


@options(idle_ms=2000)
def test_real_claude_code_idle_notice_reaches_aisquare_and_rings_no_bell(
    native: Native,  # noqa: F811
) -> None:
    """Claude Code's ``Notification`` of type ``idle_prompt`` (its prompt idle past
    ``messageIdleNotifThresholdMs``, 60 s by default, 2 s here), round-tripped: a hook of the
    operator's own beside aisquare's records what Claude Code sent, aisquare's adapter reads
    it as the idle notice, and the board stays as it was, ``waiting`` with no bell and no feed
    line (#153: nine bells in ten were these)."""
    seen = native.outside / "notifications.jsonl"
    path = native.config_dir / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    own = {"type": "command", "command": f"cat >> {shlex.quote(str(seen))}; echo >> {seen}"}
    settings["hooks"]["Notification"].append({"hooks": [own]})
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    row = native.spawn(prompt="then sit idle").agent
    native.heard("then sit idle")
    native.until(lambda: native.state(row.session_id) == "waiting", 30, "waiting", row.pane_id)

    def idle() -> dict[str, Any] | None:
        lines = seen.read_text(encoding="utf-8").splitlines() if seen.exists() else []
        sent = [json.loads(line) for line in lines if line.strip()]
        return next((p for p in sent if p.get("notification_type") == "idle_prompt"), None)

    payload = native.until(idle, 40, "Claude Code's idle notice", row.pane_id)
    event = get_adapter(AGENT).decode_event(payload)
    assert event is not None and (event.kind, event.notification_type) == (
        "notification",
        "idle_prompt",
    )
    assert event.session_id == row.session_id
    assert team.classify_notification(event.notification_type, event.message) == "quiet"
    time.sleep(2)  # aisquare's hook beside the operator's, done by now
    assert native.state(row.session_id) == "waiting", "no bell"
    assert not [e for e in native.events() if e.kind in ("attention", "notice")]


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


@options()
def test_real_claude_code_whose_hooks_never_ran_is_told_at_its_idle_prompt(
    native: Native,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#87's N5 (ruled 11142): its hooks name an aisquare that is gone (an install moved
    since they were written), so no SessionStart reaches the board; its screen reads its
    idle prompt, and a tell types there, as 0.9.2 did, rather than only filing a note."""
    agents.remove_hooks(AGENT, native.config_dir)
    gone = native.outside / "moved" / "aisquare"
    monkeypatch.setattr(agents, "_aisquare_command", lambda *args, **kwargs: str(gone))
    agents.install_hooks(AGENT, native.config_dir)
    receipt = native.spawn(prompt="no hook hears this")
    row = receipt.agent
    native.heard("no hook hears this")
    native.ready(row.pane_id)
    assert native.session(row.session_id) is None, "no hook reached the board"
    # With no board row its state is its pane's: waiting once it has been quiet a while.
    native.until(lambda: fleet.status_of(row).state == "waiting", 30, "quiet", row.pane_id)
    told = asq_json(native, "fleet", "tell", row.label, "typed with no hooks")
    assert told["typed"] == "yes", told
    native.heard("typed with no hooks")


@options()
def test_real_claude_code_whose_reply_ends_in_a_yes_no_question_is_told_at_its_idle_prompt(
    native: Native,  # noqa: F811
) -> None:
    """r40's nit: ``dialog_region`` drops the rules on the last rows (the box's top rule a
    dialog was drawn over). A reply whose last line ends ``(y/n)?``, above the input box's
    rule, must not read as a y/n prompt: the tell would be refused (F5), and the captain
    could "press" y into the input box. Every frame of the turn is read, and the idle pane:
    none is a prompt, and the tell types there, as 0.9.2 typed it."""
    question = "Shall I go on (y/n)?"
    receipt = native.spawn(prompt="before the question")
    row = receipt.agent
    native.heard("before the question")
    native.until(lambda: native.state(row.session_id) == "waiting", 30, "idle", row.pane_id)
    native.ready(row.pane_id)
    native.type(row.pane_id, f"[[say {question}]]")
    native.heard(question)
    frames: list[list[str]] = []

    def answered() -> bool:
        frames.append(native.rows(row.pane_id))
        return native.state(row.session_id) == "waiting" and any(
            line.rstrip().endswith(question) and "heard you" in line for line in frames[-1]
        )

    native.until(answered, 30, "the reply on screen, idle", row.pane_id)
    native.ready(row.pane_id)
    frames.append(native.rows(row.pane_id))
    misread = [
        frame
        for frame in frames
        if claude_screen.prompt_showing(frame) is not None
        or claude_screen.modal_showing(frame) is not None
    ]
    assert not misread, "\n---\n".join("\n".join(frame) for frame in misread[:2])
    told = asq_json(native, "fleet", "tell", row.label, "told after the question")
    assert told["typed"] == "yes", told
    native.heard("told after the question")
