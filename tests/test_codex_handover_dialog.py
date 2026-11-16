"""The automatic usage-limit hand-over of a Codex agent at a dialog of its own (SMOKE7 of #113).

Codex 0.162.1 opens *Switch to gpt-6-luna for lower credit usage?* after a turn at its
limit, the failed turn included: measured with a loopback provider at 100 % and the
operator's ``[notice] hide_rate_limit_model_nudge = false``, which the fleet's own
``-c`` yields to. The hand-over's stop killed the window there with no ``/exit``, so
Codex's ``SessionEnd`` never ran. The session it left stayed limited on the board, and
the doctor warned of an agent parked on its limit until the reset while the agent ran on
the account with room. One Esc (the adapter's ``dismiss_key``) leaves that dialog on the
current model, back at the empty prompt within 0.1 s, and ``/exit`` there ends the
session with status 0: the path Remote's Switch with ``dismiss_dialog`` takes.

The fake tmux plays Codex's part (:func:`_answers_as_codex`): Esc at the dialog draws its
prompt, and ``/exit`` at that prompt fires its ``SessionEnd`` through Codex's own decoder.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import claude_accounts as core
from aisquare.core.agent_adapters import recorded_adapter
from aisquare.core.store import store_session
from aisquare.models import ClaudeAccount, DoctorCheck, ProjectInfo, TeamTask
from aisquare.services import diagnostics
from aisquare.services import fleet as fleet_service
from aisquare.services import hooks as hooks_service
from aisquare.services import team as team_service
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_codex_accounts import _rollout, _sign_in, _token_count, _window
from tests.test_codex_fleet_parity import CHEVRON, THREAD_ONE, Codex, _claimed_by, _row, _task
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    _events,
    _two_slots_with_usage,
    _with_transcript,
    claude_on_path,
    clock,
    project,
    repo,
    tmux,
)

#: Codex 0.162.1 under tmux 3.7c after its first prompt met the provider's ``429
#: usage_limit_reached`` (the provider's headers at 100 %), trimmed: the error, and the
#: dialog drawn under it.
AT_ITS_LIMIT = [
    f"{CHEVRON} LIMIT please",
    "",
    "\u25a0 You\u2019ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
    "visit https://chatgpt.com/settings/usage to purchase more credits or try again",
    "at Oct 20th, 2026 2:10 AM.",
    "",
    "  Approaching rate limits",
    "  Switch to gpt-6-luna for lower credit usage?",
    "",
    f"{CHEVRON} 1. Switch to gpt-6-luna                   Fast and affordable model for easier "
    "tasks.",
    "  2. Keep current model",
    "  3. Keep current model (never show again)  Hide future rate limit reminders about "
    "switching models",
    "",
    "  enter select \u00b7 esc back",
]
#: The same pane 0.1 s after one Escape: the dialog gone, the model kept, the empty prompt.
AFTER_ESCAPE = [
    f"{CHEVRON} LIMIT please",
    "",
    "\u25a0 You\u2019ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
    "visit https://chatgpt.com/settings/usage to purchase more credits or try again",
    "at Oct 20th, 2026 2:10 AM.",
    "",
    "                                    \u26a0 5h limit: only <1% left \u00b7 resets at 02:10 on "
    "20 Oct \u00b7 /status",
    "",
    f"{CHEVRON} Ask Codex to do anything",
    "",
    "  local-fixture default \u00b7 /work/repo",
    "  ? for shortcuts                                    \u26a0 2 warnings \u00b7 f2 to view",
]


def _answers_as_codex(
    server: FakeTmux, codex: Codex, monkeypatch: pytest.MonkeyPatch, *, closes: bool = True
) -> list[str]:
    """Make the fake tmux answer keys in ``codex``'s pane as Codex 0.162.1 does: Esc at the
    dialog draws its empty prompt (unless the dialog does not ``close``), and ``/exit`` at
    that prompt fires its ``SessionEnd`` before it exits (``FakeTmux.honours_exit``).
    Returns what the agent took, in order: ``escaped``, ``exited``, or ``exit in dialog``."""
    pane = codex.agent.pane_id
    took: list[str] = []
    send_keys, send_literal = server.send_keys, server.send_literal

    def keys(pane_id: str, *names: str) -> None:
        send_keys(pane_id, *names)
        if pane_id == pane and names == ("Escape",) and server.screens[pane] == AT_ITS_LIMIT:
            took.append("escaped")
            if closes:
                server.screens[pane] = AFTER_ESCAPE

    def literal(pane_id: str, text: str) -> None:
        if pane_id == pane and text == "/exit":
            if server.screens[pane] != AFTER_ESCAPE:
                took.append("exit in dialog")  # its Enter picks the cheaper model
            else:
                took.append("exited")
                codex.become()
                codex.hook("SessionEnd", THREAD_ONE)
        send_literal(pane_id, text)

    monkeypatch.setattr(server, "send_keys", keys)
    monkeypatch.setattr(server, "send_literal", literal)
    return took


def _codex_limits() -> list[DoctorCheck]:
    return [c for c in diagnostics._other_accounts_checks() if c.name == "codex-account-limits"]


@pytest.fixture
def limited(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Codex, TeamTask, ClaudeAccount]:
    """A Codex coder on a Codex slot at 100 %, its session limited and its task claimed,
    with a second Codex slot at 15 % to hand it over to; ``(agent, task, that slot)``."""
    now = datetime.now(tz=UTC)
    full = core.create_account("codex")
    roomy = core.create_account("codex")
    for slot, email in ((full, "full@example.com"), (roomy, "roomy@example.com")):
        _sign_in(slot.config_dir, email)
    _rollout(full.config_dir, [_token_count(now, _window(100, 300, now + timedelta(hours=2)))])
    _rollout(roomy.config_dir, [_token_count(now, _window(15, 300, now + timedelta(hours=4)))])
    task = _task(project, "the task the limited codex coder holds")
    agent = fleet_service.spawn(
        project, "coder", agent="codex", task_id=task.id, worktree=False, account=str(full.slot)
    ).agent
    monkeypatch.setenv("CODEX_HOME", str(full.config_dir))  # the window's
    codex = Codex(agent, project, full.config_dir, tmux, monkeypatch)
    codex.start(THREAD_ONE)
    board = codex.board_id(THREAD_ONE)
    assert team_service.claim_task(task.id, session_ref=board).status == "doing"
    with store_session() as store:
        store.mark_limited(board, now + timedelta(hours=2))
    tmux.set_command(agent.pane_id, "codex")
    tmux.screens[agent.pane_id] = AT_ITS_LIMIT
    [parked] = _codex_limits()
    assert "1 agent(s) parked on a Codex usage limit" in parked.detail, parked
    return codex, task, roomy


def test_the_automatic_hand_over_of_a_codex_at_its_switch_dialog_ends_the_session_it_leaves(
    limited: tuple[Codex, TeamTask, ClaudeAccount],
    tmux: FakeTmux,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hand-over's worker presses the adapter's dismiss key once, as the page's
    ``dismiss_dialog`` presses Esc, then types ``/exit`` at the prompt that came back.
    Codex's own ``SessionEnd`` ends the session it leaves, its claim parked for the
    replacement, and the doctor names no agent parked on a limit. It killed the window
    unanswered, the session stayed limited, and the doctor warned until the reset."""
    codex, task, roomy = limited
    took = _answers_as_codex(tmux, codex, monkeypatch)
    board = codex.board_id(THREAD_ONE)
    pane = codex.agent.pane_id

    hooks_service.hand_over(board, reason="session limit")

    assert took == ["escaped", "exited"]
    assert [(kind, text) for pane_id, kind, text in tmux.typed if pane_id == pane] == [
        ("key", "Escape"),
        ("literal", "/exit"),
        ("key", "Enter"),
    ]
    assert pane in tmux.killed  # the dead window, removed
    with store_session() as store:
        left = store.get_session(board)
        kinds = [event.kind for event in store.recent_events(codex.project.id, limit=20)]
    assert left is not None and left.ended_at is not None, "its own SessionEnd ended it"
    assert _codex_limits() == [], "no agent parked on a Codex usage limit"
    old = _row(codex.agent.id)
    assert old.ended_at is not None and old.exit_status == 0
    with store_session() as store:
        [replacement] = store.fleet_agents(codex.project.id, live_only=True)
    assert replacement.agent == "codex" and replacement.account_slot == roomy.slot
    assert replacement.label == codex.agent.label
    # The claim waits for the replacement's first hook, never back to the pool.
    assert _claimed_by(task.id) == board
    assert "switched" in kinds and "task_released" not in kinds and "agent_exited" not in kinds


def test_a_dialog_its_dismiss_key_does_not_close_gets_no_exit_command(
    limited: tuple[Codex, TeamTask, ClaudeAccount],
    tmux: FakeTmux,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One key, never a second, and never ``/exit`` into a screen that still shows no
    empty prompt: its Enter would pick the dialog's first option. The window is killed
    as before, saying the key did not bring the prompt back, and the hand-over goes on."""
    codex, _, roomy = limited
    took = _answers_as_codex(tmux, codex, monkeypatch, closes=False)
    real_stop = fleet_service.stop
    stops: list[fleet_service.StopReceipt] = []

    def stop_and_keep(*args: Any, **kwargs: Any) -> fleet_service.StopReceipt:
        stops.append(real_stop(*args, **kwargs))
        return stops[-1]

    monkeypatch.setattr(fleet_service, "stop", stop_and_keep)

    receipt = fleet_service.switch(
        codex.project, codex.agent.label, reason="session limit", automatic=True
    )

    assert took == ["escaped"]
    pane = codex.agent.pane_id
    assert [(kind, text) for pane_id, kind, text in tmux.typed if pane_id == pane] == [
        ("key", "Escape")
    ]
    [stopped] = stops
    assert stopped.killed is not None and "still did after Escape" in stopped.killed, stopped
    assert stopped.dismissed == "Escape at a dialog of its own"
    assert pane in tmux.killed
    assert receipt.to_slot == roomy.slot


@pytest.mark.parametrize("by_hand", ["stop", "switch"])
def test_a_stop_or_switch_by_hand_at_the_dialog_still_presses_nothing(
    by_hand: str,
    limited: tuple[Codex, TeamTask, ClaudeAccount],
    tmux: FakeTmux,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: answering a dialog is the human's to ask for (Remote's ``dismiss_dialog``),
    so a ``fleet stop`` or ``fleet switch`` by hand kills the window unanswered, as it did."""
    codex, _, _ = limited
    took = _answers_as_codex(tmux, codex, monkeypatch)
    do: dict[str, Callable[[], object]] = {
        "stop": lambda: fleet_service.stop(codex.project, codex.agent.label, grace=0),
        "switch": lambda: fleet_service.switch(codex.project, codex.agent.label),
    }

    do[by_hand]()

    assert took == []
    assert [entry for entry in tmux.typed if entry[0] == codex.agent.pane_id] == []
    assert codex.agent.pane_id in tmux.killed


def test_a_claude_code_hand_over_types_its_exit_as_before(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Control: Claude Code's stop reads no screen (it has no empty-prompt mark), so its
    automatic hand-over types ``/exit`` and its Enter, and no dismiss key, whatever its
    pane shows."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    _with_transcript(agent, tmp_path / "missing.jsonl")
    tmux.screens[agent.pane_id] = ["  Do you want to proceed?", "\u276f 1. Yes", "  2. No"]

    receipt = fleet_service.switch(project, agent.label, reason="session limit", automatic=True)

    assert receipt.to_slot == 3
    assert [(kind, text) for pane_id, kind, text in tmux.typed if pane_id == agent.pane_id] == [
        ("literal", "/exit"),
        ("key", "Enter"),
    ]
    assert not any("stopped with" in note for note in receipt.notes)
    assert _events(project, "switched")


def test_the_dismiss_key_is_the_adapters_own(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fleet presses what the row's adapter names, never a key of its own: Codex's,
    read off its recorded adapter, here changed to prove it."""
    agent = fleet_service.spawn(project, "coder", agent="codex", worktree=False).agent
    adapter = recorded_adapter("codex")
    assert adapter is not None and adapter.dismiss_key == "Escape"
    monkeypatch.setattr(adapter, "dismiss_key", "C-g")
    pane = agent.pane_id
    tmux.screens[pane] = AT_ITS_LIMIT
    send_keys = tmux.send_keys

    def keys(pane_id: str, *names: str) -> None:
        send_keys(pane_id, *names)
        if names == ("C-g",):
            tmux.screens[pane_id] = AFTER_ESCAPE

    monkeypatch.setattr(tmux, "send_keys", keys)

    receipt = fleet_service.stop(project, agent.label, grace=0, dismiss_dialog=True)

    assert [(kind, text) for pane_id, kind, text in tmux.typed if pane_id == pane][:2] == [
        ("key", "C-g"),
        ("literal", "/exit"),
    ]
    assert receipt.killed is None and receipt.dismissed == "C-g at a dialog of its own, then /exit"
