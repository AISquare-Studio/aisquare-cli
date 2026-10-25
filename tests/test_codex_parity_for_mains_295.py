"""Codex parity for main's #295 features (SCOPE #113 295), on the fleet's fake tmux and
Codex's own hook payloads.

- W1a, the first message: a Codex spawn's first message, a ``--task`` kickoff included, rides
  its command line from the file ``launch`` reads and deletes (``--first-prompt-file``), as
  Claude Code's does: never in the window's command, nothing typed. Codex's
  ``UserPromptSubmit`` acknowledges it.
- W1b, an idle agent reads waiting: asked of a quiet ``working`` row, Codex's own screen
  says idle at its empty prompt, and not with a draft in its composer.
- W2, a note at turn end: Codex's ``Stop`` hands a busy agent the note addressed to it.
- W3, the refusal memory, per-family windows and ``[accounts] prefer`` in the cross-agent
  pick (``choose_any``): a Codex limit mark keeps the pick off that account until its reset;
  a family's mark keeps it off for that family's agents alone; ``reset-soonest`` takes the
  account whose week resets first, of either agent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aisquare.core.agent_adapters import get_adapter
from aisquare.core.store import store_session
from aisquare.models import (
    ClaudeAccount,
    ClaudeUsage,
    FleetAgent,
    ProjectInfo,
    TeamTask,
    UsageWindow,
)
from aisquare.services import claude_accounts as service
from aisquare.services import delivery, pane_idle
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_codex_fleet_parity import (  # noqa: F401 - fixture, by name
    DRAFT,
    IDLE_PROMPT,
    THREAD_ONE,
    Codex,
    _row,
    codex_agent,
)
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    _add_task,
    _command,
    _first_lines,
    _settings,
    claude_on_path,
    project,
    repo,
    tmux,
)
from tests.test_one_priority_order_across_agents import (  # noqa: F401 - fixture, by name
    CLAUDE,
    CODEX,
    _claude,
    _codex,
    _pick,
    machine,
)

CODEX_ADAPTER = get_adapter(CODEX)
SOON = datetime.now(tz=UTC) + timedelta(days=1)


# --- W1a: the first message -------------------------------------------------------------------


def test_a_codex_spawns_first_message_rides_its_command_line_from_the_file(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    receipt = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, prompt="-- read the board first"
    )

    command = _command(tmux)
    assert delivery.FIRST_PROMPT_FLAG in command
    assert "-- read the board first" not in command, "never in the window's command"
    assert _first_lines(tmux) == ["-- read the board first"] and tmux.typed == []
    assert (receipt.prompt_typed, receipt.prompt_delivery) == ("yes", "argv")
    assert receipt.agent.first_prompt == "pending"


def test_a_codex_task_spawn_with_no_prompt_is_given_the_tasks_kickoff(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    task = _add_task(project, "Ship auth")
    receipt = fleet_service.spawn(project, "coder", agent="codex", worktree=False, task_id=task.id)

    [first] = _first_lines(tmux)
    assert f"ASSIGNED TO YOU: {task.id}" in first and "Ship auth" in first, first
    assert receipt.prompt_delivery == "argv" and tmux.typed == []


def test_a_codex_spawn_under_first_prompt_type_has_it_typed_with_one_enter(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``[fleet] first_prompt = "type"`` types it for every agent; a Codex composer is not read
    back, so its one Enter is all there is, a key of its own after the paste."""
    _settings(monkeypatch, first_prompt="type")
    receipt = fleet_service.spawn(project, "coder", agent="codex", worktree=False, prompt="go")

    assert delivery.FIRST_PROMPT_FLAG not in _command(tmux)
    pane = receipt.agent.pane_id
    assert tmux.typed == [(pane, "paste", "go"), (pane, "key", "Enter")]
    assert receipt.prompt_delivery == "typed"


def test_codexs_prompt_hook_acknowledges_its_first_message(
    codex_agent: tuple[Codex, TeamTask],  # noqa: F811
) -> None:
    """The task's kickoff is pending from the spawn on; Codex's ``UserPromptSubmit``, which
    comes with that first prompt, takes it (``store.mark_prompted``): the acknowledgement
    ``fleet spawn`` waits for and ``fleet ls`` reads."""
    codex, _ = codex_agent
    assert _row(codex.agent.id).first_prompt == "pending"

    codex.start(THREAD_ONE)
    codex.hook("UserPromptSubmit", THREAD_ONE, turn_id="turn-1", prompt="start on it")

    row = _row(codex.agent.id)
    with store_session() as store:
        session = store.get_session(codex.board_id(THREAD_ONE))
    assert row.first_prompt == "acked"
    assert session is not None and session.prompted_at is not None


# --- W1b: an idle agent reads waiting ---------------------------------------------------------


def _codex_row() -> FleetAgent:
    return FleetAgent(
        id="agt_codex1",
        project_id="prj_x",
        role="coder",
        label="coder-1",
        binary="codex",
        agent="codex",
        pane_id="%1",
        cwd=Path("/w"),
        created_at=datetime.now(tz=UTC),
    )


@pytest.mark.parametrize(
    ("screen", "idle"), [(IDLE_PROMPT, True), (DRAFT, False)], ids=["empty", "draft"]
)
def test_a_quiet_codex_pane_reads_idle_by_codexs_own_screen(screen: list[str], idle: bool) -> None:
    """``fleet._derive`` asks this of a quiet ``working`` row (#123): Codex's empty prompt is
    idle, as Claude Code's idle input box is; a draft in its composer is not."""
    server = SimpleNamespace(capture=lambda pane_id: SimpleNamespace(lines=list(screen)))
    assert pane_idle.box_idle_now(server, "%1", _codex_row()) is idle  # type: ignore[arg-type]


# --- W2: a note at the end of a busy agent's turn ---------------------------------------------


def test_codexs_stop_hands_a_busy_coder_the_note_addressed_to_it(
    codex_agent: tuple[Codex, TeamTask],  # noqa: F811
) -> None:
    """A note to a Codex coder at work reaches it at the end of its turn: its ``Stop`` hands
    the note back as the next turn's prompt (``stop_reason_as_prompt``), as Claude Code's
    ``Stop`` keeps that agent going on it."""
    codex, _ = codex_agent
    codex.start(THREAD_ONE)
    codex.hook("UserPromptSubmit", THREAD_ONE, turn_id="turn-1", prompt="start on it")
    team_service.add_note(
        "rebase onto main before you push",
        kind="note",
        to_role=codex.agent.label,
        session_ref=None,
        cwd=codex.project.root,
    )

    reply = codex.hook("Stop", THREAD_ONE, turn_id="turn-1")

    assert "rebase onto main before you push" in reply, reply


# --- W3: the refusal memory, per-family windows and prefer in choose_any ------------------------


def _readings(
    monkeypatch: pytest.MonkeyPatch, used: dict[int, tuple[float, datetime | None]]
) -> None:
    """Each slot's reading: its session ``used`` percent, and when its week resets (``None``:
    a slot not listed reads as unreadable)."""

    def cached(accounts: Any, **_: Any) -> dict[int, ClaudeUsage]:
        out: dict[int, ClaudeUsage] = {}
        for account in accounts:
            if account.slot not in used:
                out[account.slot] = ClaudeUsage(available=False, reason="unread")
                continue
            percent, resets = used[account.slot]
            out[account.slot] = ClaudeUsage(
                available=True,
                windows=(
                    UsageWindow(name="session", minutes=300, used_percent=percent),
                    UsageWindow(name="week", minutes=10080, used_percent=1.0, resets_at=resets),
                ),
            )
        return out

    monkeypatch.setattr(service, "cached_usage", cached)
    monkeypatch.setattr(service, "read_usage", lambda accounts, **k: cached(accounts))


def _slot(pick: service.AgentPick) -> int | None:
    return pick.account.slot if pick.account is not None else None


def _team() -> tuple[ClaudeAccount, ClaudeAccount]:
    """A Claude Code account, then a Codex one, in the one order (the plain slot first)."""
    return _claude("c@example.com"), _codex("x@example.com")


def test_a_codex_limit_mark_keeps_the_cross_agent_pick_off_that_account(
    machine: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude, codex = _team()
    _readings(monkeypatch, {codex.slot: (10.0, None)})
    assert _slot(service.choose_any()) == codex.slot

    # What Codex's limit leaves (read off its rollout, ``hooks._plan_hand_over``).
    service.remember_limit(codex.slot, "week", SOON)
    pick = service.choose_any()

    assert _slot(pick) == claude.slot and pick.agent == CLAUDE, "the one account left"
    assert any(
        f"account {codex.slot} skipped" in note and "week limit until" in note
        for note in pick.notes
    )


def test_a_familys_mark_keeps_the_pick_off_for_that_familys_agents_alone(
    machine: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude, codex = _team()
    _readings(monkeypatch, {claude.slot: (10.0, None), codex.slot: (20.0, None)})
    service.remember_limit(claude.slot, "opus", SOON)

    assert _slot(service.choose_any(family="opus")) == codex.slot
    assert _slot(service.choose_any(family="sonnet")) == claude.slot
    assert _slot(service.choose_any()) == claude.slot, "a family's mark shuts no agent of none"


def test_prefer_reset_soonest_takes_the_week_that_resets_first_across_agents(
    machine: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude, codex = _team()
    _readings(
        monkeypatch,
        {claude.slot: (10.0, SOON + timedelta(days=3)), codex.slot: (20.0, SOON)},
    )

    assert _slot(service.choose_any()) == claude.slot, "priority: the first in the one order"
    _pick(prefer="reset-soonest")
    pick = service.choose_any()
    assert _slot(pick) == codex.slot and pick.agent == CODEX
    assert any("resets first" in note for note in pick.notes)
