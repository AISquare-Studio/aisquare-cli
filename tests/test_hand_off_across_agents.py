"""A hand-off across agent types (task #15 of #113), end to end on the fleet's fake tmux.

No conversation moves between agents, so the replacement starts FRESH on the other agent,
and a brief carries the work (``services.handoff``): the source's conversation as its own
adapter's transcript reader renders it, the board and the tree, delivered as the TARGET's
first message the way #295 delivers any (on the command line through the prompt file, for
Codex and Claude Code alike, which take it as their argument).
The claims move onto the new row, the old session ends, and the new launch record names
the new agent, so its restart keeps it.

The automatic hand-over prefers its own agent's account with room (a native resume); only
once every own account was read over the line does it cross, and ``[accounts]
cross_agent_handover = false`` keeps it on its agent as before. The synthetic third agent's
legs, both directions, are in the conformance suite (``tests/test_adapter_contract.py``).
"""

from __future__ import annotations

import os
import re
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import claude_accounts as core
from aisquare.core import launch_seal
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.config import load_config, save_config
from aisquare.core.store import store_session
from aisquare.core.tmux import WindowInfo
from aisquare.models import (
    ClaudeAccount,
    FleetAgent,
    ProjectInfo,
    TeamEvent,
    TeamSession,
    TeamTask,
)
from aisquare.services import fleet as fleet_service
from aisquare.services import handoff
from aisquare.services import team as team_service
from aisquare.services import transcript as transcript_service
from tests.test_adapter_contract import _moments, _written
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_codex_accounts import _rollout, _sign_in, _token_count, _window
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    _command,
    _first_lines,
    _flag,
    _task,
    _task_now,
    claude_on_path,
    clock,
    project,
    repo,
    tmux,
)
from tests.test_usage_aware_accounts import _payload, _slot, _Usage


def _claude_slot(monkeypatch: pytest.MonkeyPatch, used: float) -> ClaudeAccount:
    """A signed-in Claude Code slot whose scripted usage reads ``used`` percent."""
    from aisquare.services import claude_accounts as accounts_service

    account = _slot("claude@example.com", "tok-claude", expires_in=timedelta(days=3650))
    monkeypatch.setattr(accounts_service, "_http_get", _Usage({"tok-claude": _payload(used)}))
    return account


def _claude_slots(monkeypatch: pytest.MonkeyPatch, *used: float) -> list[ClaudeAccount]:
    """Signed-in Claude Code slots, one per figure, under one scripted usage endpoint."""
    from aisquare.services import claude_accounts as accounts_service

    accounts = [
        _slot(f"c{n}@example.com", f"tok-c{n}", expires_in=timedelta(days=3650))
        for n in range(len(used))
    ]
    monkeypatch.setattr(
        accounts_service,
        "_http_get",
        _Usage({f"tok-c{n}": _payload(figure) for n, figure in enumerate(used)}),
    )
    return accounts


def _codex_slot(used: float, email: str = "codex@example.com") -> ClaudeAccount:
    """A signed-in Codex slot whose newest rollout reads ``used`` percent of its window."""
    now = datetime.now(tz=UTC)
    account = core.create_account("codex")
    _sign_in(account.config_dir, email)
    _rollout(account.config_dir, [_token_count(now, _window(used, 300, now + timedelta(hours=2)))])
    return account


def _ready_as(server: FakeTmux, monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    """Each new pane looks ready, as its agent's own process at its prompt: a multi-line first
    prompt is typed only into one (``fleet._type_prompt``)."""
    original = server.spawn_window

    def ready(*args: Any, **kwargs: Any) -> WindowInfo:
        window = original(*args, **kwargs)
        server.set_command(window.pane_id, command)
        return window

    monkeypatch.setattr(server, "spawn_window", ready)


def _ran(agent: FleetAgent, transcript: Path, *, native: str | None = None) -> str:
    """What the agent's own hooks would have recorded: its board session naming
    ``transcript``, and its launch record's session (``launch_seal``), as handed."""
    record = launch_seal.read(agent.id)
    assert isinstance(record, launch_seal.LaunchRecord), record
    session_id = agent.session_id or f"{record.agent}-session"
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=session_id,
                project_id=agent.project_id,
                role=agent.role,
                started_at=now,
                last_seen_at=now,
                transcript_path=str(transcript),
                agent=record.agent,
            )
        )
        stored = store.get_fleet_agent(agent.id)
        assert stored is not None
        store.upsert_fleet_agent(stored.model_copy(update={"session_id": session_id}))
    facts = launch_seal.SessionFacts(
        id=session_id,
        agent=record.agent,
        native_id=native or session_id,
        transcript=str(transcript),
        started_at=now,
    )
    assert launch_seal.write(record.model_copy(update={"session": facts})) is None
    return session_id


def _conversation_lines(path: Path, agent: str) -> list[str]:
    """The source's conversation as its own reader pages it, without escapes: what a brief
    must carry, newest last."""
    page = transcript_service.read_page(path, limit=handoff.TURNS, width=handoff.WIDTH, agent=agent)
    lines = [re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", line).rstrip() for line in page.lines]
    return [line for line in lines if line.strip()]


def _typed(server: FakeTmux, pane: str) -> list[str]:
    return [text for at, kind, text in server.typed if at == pane and kind == "paste"]


def _first_message(server: FakeTmux, receipt: fleet_service.SwitchReceipt) -> str:
    """The replacement's first message: on its command line, through the prompt file
    (``--first-prompt-file``), and never typed into its pane."""
    assert receipt.prompt_delivery == "argv", receipt.prompt_delivery
    assert not _typed(server, receipt.started.pane_id)
    [prompt] = _first_lines(server, receipt.started.pane_id)
    return prompt


def _events(where: ProjectInfo) -> list[tuple[str, str]]:
    with store_session() as store:
        return [(e.kind, e.text) for e in store.recent_events(where.id, limit=30)]


# --- the brief ------------------------------------------------------------------------------


def _event(kind: str, text: str) -> TeamEvent:
    return TeamEvent(
        id=f"evt_{kind}", project_id="prj", kind=kind, text=text, created_at=datetime.now(tz=UTC)
    )


def test_the_brief_keeps_every_section_and_cuts_the_oldest_of_the_conversation_to_fit() -> None:
    """The first prompt is bounded (tmux takes a positional prompt in one message), every
    section is in it, and the conversation keeps its newest lines, saying the start was
    cut; the file's version keeps more."""
    now = datetime.now(tz=UTC)
    task = TeamTask(
        id="tsk_brief",
        project_id="prj",
        number=7,
        key="brief",
        title="Ship the router",
        detail="objective: route every request " * 200,
        status="doing",
        created_at=now,
        updated_at=now,
    )
    conversation = tuple(f"line {n} " + "word " * 40 for n in range(400))
    brief = handoff.HandoffBrief(
        label="coder-1",
        source="coder-1",
        source_agent="codex",
        target_agent="claude-code",
        reason="it stopped on 5h limit",
        task=task,
        claims=(task,),
        entries=(_event("note", "halfway through the router"),),
        git=handoff.GitState(
            tree=Path("/w"), branch="feat/x", changed=(" M a.py",), commits=("abc fix",)
        ),
        conversation=conversation,
        path=Path("/home/.aisquare/handoffs/brief.md"),
    )

    text = brief.text()

    assert len(text.encode("utf-8")) <= handoff.PROMPT_MAX
    assert text.startswith("You are coder-1, taking over from coder-1, which ran on Codex")
    assert "You run on Claude Code: a conversation does not move between agents" in text
    assert "Ship the router (tsk_brief)" in text and "Contract: objective" in text
    assert "Claims, now yours:\n- #7 Ship the router" in text
    assert "- note: halfway through the router" in text
    assert "on branch feat/x" in text and "uncommitted: M a.py" in text and "abc fix" in text
    assert "its start cut to fit" in text
    assert "line 399" in text and "line 0 " not in text  # the newest kept, the oldest cut
    assert text.endswith("The whole brief is kept at /home/.aisquare/handoffs/brief.md.")
    whole = brief.text(handoff.FILE_MAX)
    assert len(whole.encode("utf-8")) <= handoff.FILE_MAX and whole.count("line ") > text.count(
        "line "
    )


def test_a_fork_brief_names_the_source_s_work_as_its_own_and_claims_nothing() -> None:
    brief = handoff.HandoffBrief(
        label=None,
        source="coder-1",
        source_agent="claude-code",
        target_agent="codex",
        claims=(),
        conversation=("> you", "  do the thing"),
        fork=True,
    )
    text = brief.text()
    assert text.startswith("You are a fork of coder-1, which keeps running on Claude Code")
    assert "with no claims" in text and "never touch coder-1's tree" in text
    assert "Claims, now yours" not in text


def test_the_brief_file_is_kept_owner_only_under_the_aisquare_home(tmp_path: Path) -> None:
    brief = handoff.write(
        handoff.HandoffBrief(
            label="coder/../1",
            source="coder-1",
            source_agent="codex",
            target_agent="claude-code",
            conversation=("> you", "  hello"),
        )
    )
    assert brief.path is not None and brief.path.parent == handoff.brief_dir()
    assert ".." not in brief.path.name and "/" not in brief.path.name
    assert brief.path.read_text(encoding="utf-8").rstrip("\n") == brief.text(handoff.FILE_MAX)
    if sys.platform != "win32":
        assert stat.S_IMODE(brief.path.stat().st_mode) == 0o600
    assert handoff.prompt(brief).endswith(f"The whole brief is kept at {brief.path}.")


def test_the_brief_reads_the_tree_it_was_given(repo: Path) -> None:  # noqa: F811
    (repo / "wip.txt").write_text("half done\n", encoding="utf-8")
    state = handoff.git_state(repo)
    assert state is not None and state.branch == "main"
    assert state.changed == ("?? wip.txt",) and state.commits[0].endswith("init")
    assert handoff.git_state(repo / "nowhere") is None


# --- by hand: Codex -> Claude Code, Claude Code -> Codex -------------------------------------


def test_a_codex_agent_switched_onto_claude_code_starts_fresh_there_with_the_brief(
    tmux: FakeTmux,  # noqa: F811 - pytest resolves fixtures by NAME
    codex_on_path: Path,  # noqa: F811 - pytest resolves fixtures by NAME
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``fleet switch --agent claude-code`` of a Codex agent: the Codex rollout read by Codex's
    reader, delivered as Claude Code's first message; the claims moved onto the new
    session, the old one ended, and the new launch record names Claude Code, which its
    restart keeps."""
    own = _codex_slot(10)
    claude = _claude_slot(monkeypatch, 10)
    task = _task(project, "Wire the auth flow")
    agent = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, account=str(own.slot), task_id=task.id
    ).agent
    rollout = _written(_moments(get_adapter("codex"))["asked"], tmp_path / "rollout.jsonl")
    old = _ran(agent, rollout, native="0199a3b2-0000-7000-8000-000000000001")
    assert team_service.claim_task(task.id, session_ref=old).status == "doing"
    _ready_as(tmux, monkeypatch, "claude")

    receipt = fleet_service.switch(project, agent.label, to_agent="claude-code")

    assert receipt.crossed and (receipt.from_agent, receipt.to_agent) == ("codex", "claude-code")
    assert (receipt.from_slot, receipt.to_slot, receipt.resumed) == (own.slot, claude.slot, False)
    command = _command(tmux)
    assert _flag(command, "--account") == str(claude.slot)
    assert "resume" not in command and str(rollout) not in command
    new = receipt.started.session_id
    assert new is not None and new != old and _flag(command, "--session-id") == new
    assert receipt.started.agent == "claude-code" and receipt.started.task_id == task.id
    prompt = _first_message(tmux, receipt)
    assert prompt.startswith(f"You are {agent.label}, taking over from {agent.label}, which ran")
    assert "on Codex" in prompt and "You run on Claude Code" in prompt
    assert "Claims, now yours:" in prompt and "Wire the auth flow" in prompt
    for line in _conversation_lines(rollout, "codex")[-3:]:
        assert line.strip()[:60] in prompt  # Codex's conversation, as Codex's reader reads it
    assert len(prompt.encode("utf-8")) <= handoff.PROMPT_MAX
    kept = [note for note in receipt.notes if note.startswith("hand-off brief: ")]
    assert kept and Path(kept[0].removeprefix("hand-off brief: ")).is_file()
    assert any("no conversation moves from Codex to Claude Code" in n for n in receipt.notes)
    held = _task_now(task.id)
    assert held.status == "doing" and held.claimed_by == new  # moved, never in the pool
    with store_session() as store:
        retired = store.get_session(old)
    assert retired is not None and retired.ended_at is not None
    record = launch_seal.read(receipt.started.id)
    assert isinstance(record, launch_seal.LaunchRecord) and record.agent == "claude-code"
    events = _events(project)
    [switched] = [text for kind, text in events if kind == "switched"]
    assert "moved from Codex" in switched and "to Claude Code" in switched
    assert switched.endswith("started fresh with a hand-off brief")
    assert not any(kind in {"agent_exited", "task_released"} for kind, _ in events)

    restarted = fleet_service.restart(project, agent.label, fresh=True)
    assert restarted.started.agent == "claude-code" and not restarted.crossed
    forked = fleet_service.hand_off(project, agent.label, mode="fork", fresh=True)
    assert forked.started.agent.agent == "claude-code"  # a Fork of it stays there too


def test_a_claude_code_agent_switched_to_a_codex_account_takes_the_brief_as_its_argument(
    tmux: FakeTmux,  # noqa: F811 - pytest resolves fixtures by NAME
    codex_on_path: Path,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``fleet switch --to <a Codex slot>`` of a Claude Code agent: ``--to`` names an account
    of another agent, which brings its agent. Claude Code's transcript read by its reader,
    delivered as Codex's first message, which the window's command names and never holds."""
    claude = _claude_slot(monkeypatch, 95)
    codex = _codex_slot(10)
    task = _task(project, "Ship the router")
    agent = fleet_service.spawn(
        project, "coder", worktree=False, account=str(claude.slot), task_id=task.id
    ).agent
    transcript = _written(_moments(get_adapter("claude-code"))["asked"], tmp_path / "t.jsonl")
    old = _ran(agent, transcript)
    assert team_service.claim_task(task.id, session_ref=old).status == "doing"

    receipt = fleet_service.switch(project, agent.label, to=str(codex.slot), reason="5h limit")

    assert receipt.crossed and receipt.to_agent == "codex" and receipt.to_slot == codex.slot
    command = _command(tmux)
    assert _flag(command, "--account") == str(codex.slot)
    assert "--resume" not in command
    prompt = _first_message(tmux, receipt)
    assert prompt.startswith(f"You are {agent.label}, taking over from {agent.label}")
    assert "ran on Claude Code (it stopped on 5h limit)" in prompt and "You run on Codex" in prompt
    for line in _conversation_lines(transcript, "claude-code")[-3:]:
        assert line.strip()[:60] in prompt
    assert not any(prompt[:200] in part for part in command)  # the file holds it, not tmux
    assert receipt.started.agent == "codex" and _task_now(task.id).status == "doing"


def test_a_switch_onto_an_agent_that_is_not_installed_is_refused_before_the_stop(
    tmux: FakeTmux,  # noqa: F811 - pytest resolves fixtures by NAME
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _claude_slot(monkeypatch, 95)
    agent = fleet_service.spawn(project, "coder", worktree=False).agent
    _ran(agent, _written(_moments(get_adapter("claude-code"))["asked"], tmp_path / "t.jsonl"))

    with pytest.raises(fleet_service.FleetError, match="nothing was stopped"):
        fleet_service.switch(project, agent.label, to_agent="codex")

    assert agent.pane_id not in tmux.killed and len(tmux.spawned) == 1


# --- the automatic hand-over ----------------------------------------------------------------


def test_the_automatic_hand_over_resumes_on_its_own_agent_while_one_of_its_accounts_has_room(
    tmux: FakeTmux,  # noqa: F811 - pytest resolves fixtures by NAME
    codex_on_path: Path,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A Codex account with more room is no reason to cross: an own account under the line
    resumes natively, losing nothing."""
    full, roomy = _claude_slots(monkeypatch, 95, 40)
    _codex_slot(5)
    agent = fleet_service.spawn(project, "coder", worktree=False, account=str(full.slot)).agent
    transcript = _written(_moments(get_adapter("claude-code"))["asked"], tmp_path / "t.jsonl")
    _ran(agent, transcript)

    receipt = fleet_service.switch(project, agent.label, reason="5h limit", automatic=True)

    assert not receipt.crossed and receipt.to_agent == "claude-code"
    assert (receipt.to_slot, receipt.resumed) == (roomy.slot, True)
    assert _flag(_command(tmux), "--resume") == str(transcript)


def test_the_automatic_hand_over_crosses_agents_once_every_own_account_is_full(
    tmux: FakeTmux,  # noqa: F811 - pytest resolves fixtures by NAME
    codex_on_path: Path,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Both Codex accounts over the line, read: the hand-over crosses to the Claude Code
    account under it, by the priority order, fresh with the brief."""
    full = _codex_slot(100, "full@example.com")
    _codex_slot(97, "also-full@example.com")
    claude = _claude_slot(monkeypatch, 20)
    agent = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, account=str(full.slot)
    ).agent
    rollout = _written(_moments(get_adapter("codex"))["asked"], tmp_path / "rollout.jsonl")
    _ran(agent, rollout, native="0199a3b2-0000-7000-8000-000000000002")
    _ready_as(tmux, monkeypatch, "claude")

    receipt = fleet_service.switch(project, agent.label, reason="5h limit", automatic=True)

    assert receipt.crossed and receipt.to_agent == "claude-code" and receipt.to_slot == claude.slot
    assert receipt.resumed is False and receipt.started.spawned_by == "user"
    prompt = _first_message(tmux, receipt)
    assert "which ran on Codex (it stopped on 5h limit)" in prompt


def test_an_own_account_with_a_limit_mark_in_force_counts_as_full(
    tmux: FakeTmux,  # noqa: F811 - pytest resolves fixtures by NAME
    codex_on_path: Path,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """#295's refusal memory: a Codex account that refused with a weekly limit still in force
    is full however its rollout reads (its pick skips it), so the hand-over crosses."""
    from aisquare.services import claude_accounts as accounts_service

    full = _codex_slot(100, "full@example.com")
    marked = _codex_slot(10, "marked@example.com")
    claude = _claude_slot(monkeypatch, 20)
    accounts_service.remember_limit(marked.slot, "weekly", datetime.now(tz=UTC) + timedelta(days=2))
    agent = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, account=str(full.slot)
    ).agent
    _ran(agent, _written(_moments(get_adapter("codex"))["asked"], tmp_path / "r.jsonl"))
    _ready_as(tmux, monkeypatch, "claude")

    assert handoff.own_accounts_full("codex", full.slot)
    receipt = fleet_service.switch(project, agent.label, reason="5h limit", automatic=True)

    assert receipt.crossed and receipt.to_agent == "claude-code" and receipt.to_slot == claude.slot


def test_a_family_limit_mark_fills_an_own_account_for_that_family_alone(
    claude_on_path: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An Opus week marked on the other Claude Code account: full to an Opus session, which
    may cross, and not to any other, whose reading (20 %) still has room (#277)."""
    from aisquare.services import claude_accounts as accounts_service

    leaving, other = _claude_slots(monkeypatch, 95, 20)
    accounts_service.remember_limit(other.slot, "opus", datetime.now(tz=UTC) + timedelta(days=2))

    assert handoff.own_accounts_full("claude-code", leaving.slot, family="opus")
    assert not handoff.own_accounts_full("claude-code", leaving.slot, family="sonnet")
    assert not handoff.own_accounts_full("claude-code", leaving.slot)


def test_a_replacement_the_hand_over_started_on_another_agent_moves_again_only_with_force(
    tmux: FakeTmux,  # noqa: F811 - pytest resolves fixtures by NAME
    codex_on_path: Path,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """One switch per limit holds across agents: a manager woken by the same limit cannot move
    the Claude Code replacement the hand-over just started, back to Codex or anywhere, without
    ``--force`` (``hooks.fresh_replacement_refusal``)."""
    from aisquare.services import hooks

    full = _codex_slot(100, "full@example.com")
    _codex_slot(97, "also-full@example.com")
    _claude_slot(monkeypatch, 20)
    agent = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, account=str(full.slot)
    ).agent
    _ran(agent, _written(_moments(get_adapter("codex"))["asked"], tmp_path / "r.jsonl"))
    _ready_as(tmux, monkeypatch, "claude")
    crossed = fleet_service.switch(
        project,
        agent.label,
        reason="5h limit",
        automatic=True,
        spawned_by=hooks.HANDOVER_SPAWNER,
    )
    assert crossed.crossed and crossed.started.spawned_by == hooks.HANDOVER_SPAWNER

    with pytest.raises(fleet_service.FleetError, match="--force"):
        fleet_service.switch(project, agent.label, to_agent="codex")
    assert len(tmux.spawned) == 2 and crossed.started.pane_id not in tmux.killed

    again = fleet_service.switch(project, agent.label, to_agent="codex", force=True)
    assert again.crossed and (again.from_agent, again.to_agent) == ("claude-code", "codex")


def test_an_own_account_that_cannot_be_read_keeps_the_hand_over_on_its_agent(
    tmux: FakeTmux,  # noqa: F811 - pytest resolves fixtures by NAME
    codex_on_path: Path,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A Codex slot with no rollout yet reads nothing: not full, so no fresh start elsewhere
    (it may have all its room); the automatic hand-over refuses as before."""
    full = _codex_slot(100, "full@example.com")
    unread = core.create_account("codex")
    _sign_in(unread.config_dir, "fresh@example.com")  # signed in, never used: no reading
    _claude_slot(monkeypatch, 20)
    agent = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, account=str(full.slot)
    ).agent
    _ran(agent, _written(_moments(get_adapter("codex"))["asked"], tmp_path / "r.jsonl"))

    assert not handoff.own_accounts_full("codex", full.slot)
    with pytest.raises(fleet_service.FleetError, match="no account under the line"):
        fleet_service.switch(project, agent.label, reason="5h limit", automatic=True)
    assert len(tmux.spawned) == 1


def test_with_the_switch_off_a_full_agent_stays_on_its_own_accounts_as_before(
    tmux: FakeTmux,  # noqa: F811 - pytest resolves fixtures by NAME
    codex_on_path: Path,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``[accounts] cross_agent_handover = false``: today's behaviour, exactly. The automatic
    hand-over refuses, the hook's gate starts no worker for another agent's account, and a
    manual switch takes its own agent's least-bad account."""
    config = load_config()
    config.accounts.cross_agent_handover = False
    save_config(config)
    full = _codex_slot(100, "full@example.com")
    other = _codex_slot(97, "also-full@example.com")
    _claude_slot(monkeypatch, 20)
    agent = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, account=str(full.slot)
    ).agent
    _ran(agent, _written(_moments(get_adapter("codex"))["asked"], tmp_path / "r.jsonl"))

    assert not handoff.somewhere_across(full.slot, "codex")
    with pytest.raises(fleet_service.FleetError, match="no account under the line"):
        fleet_service.switch(project, agent.label, reason="5h limit", automatic=True)
    receipt = fleet_service.switch(project, agent.label)
    assert not receipt.crossed and receipt.to_slot == other.slot

    config = load_config()
    config.accounts.cross_agent_handover = True
    save_config(config)
    assert handoff.somewhere_across(other.slot, "codex")


def test_the_limit_hook_starts_the_worker_when_only_another_agent_has_an_account(
    project: ProjectInfo,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One Codex account, limited, and a Claude Code one: today's gate said nowhere to go;
    with the switch on, the plan made before the board write goes, and the worker is
    started (it crosses once it reads the Codex one full)."""
    from aisquare.services import hooks

    codex = core.create_account("codex")
    _sign_in(codex.config_dir, "only@example.com")
    _claude_slot(monkeypatch, 20)
    now = datetime.now(tz=UTC)
    with store_session() as store:
        session = TeamSession(
            id="codex-limited",
            project_id=project.id,
            role="coder",
            started_at=now,
            last_seen_at=now,
            agent="codex",
            account=str(codex.config_dir),
        )
        store.upsert_session(session)
        store.upsert_fleet_agent(
            FleetAgent(
                id="agt_codex",
                project_id=project.id,
                label="coder-1",
                role="coder",
                agent="codex",
                binary="codex",
                pane_id="%9",
                session_id=session.id,
                cwd=project.root,
                account_slot=codex.slot,
                created_at=now - timedelta(hours=1),
            )
        )
    started: list[list[str]] = []
    monkeypatch.setattr(hooks, "_detach", started.append)
    failure = team_service.TurnFailure(session=session, error="rate_limit", notice=None)

    plan = hooks._plan_hand_over(session.id, error="rate_limit", message=None)
    assert plan is not None and plan.go
    hooks._hand_over_if_configured(failure, plan)
    assert len(started) == 1 and "hand-over" in started[0]

    config = load_config()
    config.accounts.cross_agent_handover = False
    save_config(config)
    plan = hooks._plan_hand_over(session.id, error="rate_limit", message=None)
    assert plan is not None and not plan.go
    hooks._hand_over_if_configured(failure, plan)
    assert len(started) == 1, "switched off, a lone limited account waits as before"


def test_the_brief_dir_is_no_path_an_agent_s_sandbox_may_write() -> None:
    """Under the aisquare home, which every sandboxed agent's profile holds read-only
    (``types.store_read_only``)."""
    from aisquare.core.agent_adapters.types import store_dir, store_read_only

    home = Path(os.environ["AISQUARE_HOME"]).resolve()
    assert handoff.brief_dir().resolve().is_relative_to(home)
    assert any(
        handoff.brief_dir().resolve().is_relative_to(root.resolve())
        for root in store_read_only(store_dir())
    )
