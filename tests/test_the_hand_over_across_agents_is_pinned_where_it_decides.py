"""The decisions of a hand-over across agents, each pinned (review r22 of #113, #15's f5).

The reviewer mutated ``services.handoff`` and the fleet's two seams to it and 11 mutations
lived: nothing said that a fresh start which loses the conversation is never made for a
least-bad account, that the pick reads usage live, that an agent with no binary here is no
place to go, that a transcript which is not a regular file is not read, or that the captain
keeps its agent. Each test below states one of those and fails when it is removed.
"""

from __future__ import annotations

import os
import stat
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import claude_accounts as core
from aisquare.core import launch_seal
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.config import load_config, save_config
from aisquare.models import ProjectInfo, RedactionLevel
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from aisquare.services import handoff
from tests.test_adapter_contract import _moments, _written
from tests.test_claude_accounts import NOW
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    claude_on_path,
    clock,
    project,
    repo,
    tmux,
)
from tests.test_hand_off_across_agents import _claude_slot, _claude_slots, _codex_slot, _ran
from tests.test_usage_aware_accounts import _payload, _slot, _Usage


def _codex_agent(where: ProjectInfo, leaving: int, tmp_path: Path) -> Any:
    agent = fleet_service.spawn(
        where, "coder", agent="codex", worktree=False, account=str(leaving)
    ).agent
    _ran(
        agent,
        _written(_moments(get_adapter("codex"))["asked"], tmp_path / "rollout.jsonl"),
        native="0199a3b2-0000-7000-8000-0000000000bb",
    )
    return agent


# --- pick_across: under the line, and read now -----------------------------------------------


def test_the_pick_across_never_takes_an_account_over_the_line_for_want_of_a_better(
    monkeypatch: pytest.MonkeyPatch,
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
) -> None:
    """Every other agent's account is over ``switch_at``: the least used of them is no
    target, because a fresh start that loses the conversation is never made for a least-bad
    account (the docstring's rule). The same accounts, picked as the ladder picks any, would
    give one."""
    full = _codex_slot(100)
    (claude,) = _claude_slots(monkeypatch, 96)

    pick = handoff.pick_across("codex", full.slot)

    assert pick.account is None and pick.agent is None
    least_bad = accounts_service.choose_any(exclude=(full.slot,), live=True, least_bad=True)
    assert least_bad.account is not None and least_bad.account.slot == claude.slot


def test_a_manual_switch_whose_other_agents_are_all_over_the_line_takes_its_own_least_bad(
    tmux: FakeTmux,  # noqa: F811 - pytest resolves fixtures by NAME
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The review's case: with every other agent over the line the hand-over would still
    cross. By hand it keeps today's answer, its own agent's least-bad account, a native
    resume; the automatic one refuses."""
    full = _codex_slot(100, "full@example.com")
    less = _codex_slot(97, "less@example.com")
    _claude_slots(monkeypatch, 96)
    agent = _codex_agent(project, full.slot, tmp_path)

    with pytest.raises(fleet_service.FleetError, match="no account under the line"):
        fleet_service.switch(project, agent.label, reason="5h limit", automatic=True)
    assert not tmux.killed

    receipt = fleet_service.switch(project, agent.label, reason="5h limit")

    assert not receipt.crossed and receipt.to_agent == "codex" and receipt.to_slot == less.slot


def test_the_pick_across_reads_usage_now_not_through_the_minute_cache(
    project: ProjectInfo,  # noqa: F811 - the store the readings are kept in
    monkeypatch: pytest.MonkeyPatch,
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
) -> None:
    """The leaving account refused seconds ago and the hand-over decides on what is true now:
    a reading a minute deep that said 20 % does not hide the 99 % the account reads at the
    hand-over."""
    full = _codex_slot(100)
    claude = _slot("c@example.com", "tok-c", expires_in=timedelta(days=3650))
    monkeypatch.setattr(accounts_service, "_now", lambda: NOW)  # every window still open
    usage = _Usage({"tok-c": _payload(20)})
    monkeypatch.setattr(accounts_service, "_http_get", usage)
    assert accounts_service.cached_usage([claude])[claude.slot].session_used_percent == 20.0
    primed = len(usage.calls)
    accounts_service.cached_usage([claude], max_age=accounts_service.PICK_MAX_AGE_SECONDS)
    assert len(usage.calls) == primed, "the minute-deep cache is what a live read bypasses"
    usage.by_token["tok-c"] = _payload(99)
    before = len(usage.calls)

    pick = handoff.pick_across("codex", full.slot)

    assert len(usage.calls) > before, "a hand-over's pick reads the endpoint again"
    assert pick.account is None


# --- the agents a hand-over may cross to are installed ---------------------------------------


def test_another_agent_is_somewhere_to_go_only_where_its_binary_is_installed() -> None:
    claude, codex = get_adapter("claude-code").binary, get_adapter("codex").binary

    assert handoff.other_agents("codex", which=lambda binary: None) == ()
    assert handoff.other_agents("codex", which=lambda binary: "/bin/x") == ("claude-code",)
    only_codex = handoff.other_agents("claude-code", which=lambda b: "/x" if b == codex else None)
    assert only_codex == ("codex",)
    assert handoff.other_agents("codex", which=lambda b: "/x" if b == claude else None) == (
        "claude-code",
    )
    assert handoff.other_agents("codex", which=lambda b: "/x" if b == codex else None) == ()


def test_the_limit_hook_starts_no_worker_for_an_agent_that_is_not_installed(
    codex_on_path: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signed-in Claude Code account and no ``claude`` on this machine: a Codex limit has
    nowhere to cross, and the hook's gate (``somewhere_across``) starts no worker that could
    only refuse. With ``claude`` installed it does."""
    leaving = _codex_slot(100)
    _claude_slots(monkeypatch, 10)
    monkeypatch.setenv("PATH", str(codex_on_path.parent))  # codex here, claude nowhere

    assert not handoff.somewhere_across(leaving.slot, "codex")
    pick = handoff.pick_across("codex", leaving.slot)
    assert pick.account is None
    assert pick.notes == ("across agents: no other agent with accounts is installed",)

    from tests import fakebin

    bin_dir = codex_on_path.parent.parent / "claude-bin"
    fakebin.executable_fake(
        bin_dir, "claude", windows="echo claude\nset /p line=", posix='echo "claude"\nread line'
    )
    monkeypatch.setenv("PATH", os.pathsep.join([str(codex_on_path.parent), str(bin_dir)]))

    assert handoff.somewhere_across(leaving.slot, "codex")


def test_the_gate_asks_no_agent_that_has_no_signed_in_account(
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
) -> None:
    leaving = _codex_slot(100)
    core.create_account()  # a Claude Code slot nobody signed in to

    assert not handoff.somewhere_across(leaving.slot, "codex")

    _slot("signed@example.com", "tok-s", expires_in=timedelta(days=3650))
    assert handoff.somewhere_across(leaving.slot, "codex")


# --- the brief reads a regular file only ------------------------------------------------------


def _record_naming(where: ProjectInfo, tmp_path: Path, transcript: str) -> launch_seal.LaunchRecord:
    agent = fleet_service.spawn(where, "coder", worktree=False).agent
    _ran(agent, tmp_path / "unused.jsonl")
    record = launch_seal.read(agent.id)
    assert isinstance(record, launch_seal.LaunchRecord) and record.session is not None
    facts = record.session.model_copy(update={"transcript": transcript})
    return record.model_copy(update={"session": facts})


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """The transcripts the brief started to read: a read of the wrong file is a hang or a
    leak, and the reader's own failure would be swallowed (``conversation`` fails open)."""
    opened: list[Path] = []

    def lines_backwards(path: Path, *args: Any, **kwargs: Any) -> Any:
        opened.append(path)
        return iter(())

    def lines_forward(path: Path, *args: Any, **kwargs: Any) -> Any:
        opened.append(path)
        return iter(())

    monkeypatch.setattr(handoff, "lines_backwards", lines_backwards)
    monkeypatch.setattr(handoff, "lines_forward", lines_forward)
    return opened


def test_a_regular_transcript_is_read(
    tmux: FakeTmux,  # noqa: F811 - pytest resolves fixtures by NAME
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    reads: list[Path],
) -> None:
    real = _written(_moments(get_adapter("claude-code"))["asked"], tmp_path / "t.jsonl")

    assert handoff.conversation(_record_naming(project, tmp_path, str(real))) == ()
    assert reads == [real, real]  # the walk back and the look ahead


@pytest.mark.skipif(sys.platform == "win32", reason="a named pipe is a POSIX file")
def test_a_transcript_that_is_a_pipe_is_not_opened_for_a_writer_that_may_never_come(
    tmux: FakeTmux,  # noqa: F811 - pytest resolves fixtures by NAME
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    reads: list[Path],
) -> None:
    if sys.platform == "win32":  # mypy narrows on this, not on the skipif that keeps it off
        pytest.skip("a named pipe is a POSIX file")
    pipe = tmp_path / "t.jsonl"
    os.mkfifo(pipe)
    assert stat.S_ISFIFO(pipe.stat().st_mode)

    assert handoff.conversation(_record_naming(project, tmp_path, str(pipe))) == ()
    assert reads == []


def test_a_transcript_that_is_a_folder_or_a_relative_path_is_not_read(
    tmux: FakeTmux,  # noqa: F811 - pytest resolves fixtures by NAME
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    reads: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)  # where "t.jsonl" is a file that exists: it is still no path
    folder = tmp_path / "a-folder"
    folder.mkdir()
    (tmp_path / "t.jsonl").write_text("{}\n", encoding="utf-8")

    assert handoff.conversation(_record_naming(project, tmp_path, str(folder))) == ()
    assert handoff.conversation(_record_naming(project, tmp_path, "t.jsonl")) == ()
    assert handoff.conversation(_record_naming(project, tmp_path, "")) == ()
    assert reads == []


# --- the captain keeps its agent --------------------------------------------------------------


def _record_of_role(where: ProjectInfo, tmp_path: Path, role: str) -> launch_seal.LaunchRecord:
    return _record_naming(where, tmp_path, str(tmp_path / "t.jsonl")).model_copy(
        update={"role": role}
    )


@pytest.mark.needs_captain
def test_a_take_over_or_a_fork_of_the_captain_onto_another_agent_is_refused(
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    captain = _record_of_role(project, tmp_path, "captain")
    coder = _record_of_role(project, tmp_path, "coder")

    with pytest.raises(fleet_service.FleetError, match="the captain keeps its agent"):
        fleet_service._crossing_to(captain, "codex", "captain")
    assert fleet_service._crossing_to(captain, "claude-code", "captain") is None
    assert fleet_service._crossing_to(captain, None, "captain") is None
    assert fleet_service._crossing_to(coder, "codex", "coder-1") == "codex"
    assert fleet_service._crossing_to(coder, "claude-code", "coder-1") is None


@pytest.mark.needs_captain
def test_the_automatic_hand_over_of_the_captain_never_crosses_agents(
    monkeypatch: pytest.MonkeyPatch,
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """Every Codex account full and a Claude Code one with room: a coder crosses to it, and
    the captain, which keeps its agent, is told its own agent has no account under the line."""
    full = _codex_slot(100, "full@example.com")
    _codex_slot(97, "also-full@example.com")
    claude = _claude_slot(monkeypatch, 10)

    def target(role: str) -> Any:
        return fleet_service._handover_target(
            None,
            None,
            kind="codex",
            role=role,
            project=project,
            current=full.slot,
            automatic=True,
        )

    crossed, agent = target("coder")
    assert agent == "claude-code" and crossed.account is not None
    assert crossed.account.slot == claude.slot

    kept, agent = target("captain")
    assert agent == "codex" and kept.account is None


# --- the level the brief is scrubbed at is the config's --------------------------------------


def test_a_brief_is_built_at_the_redaction_level_the_config_holds(
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    agent = fleet_service.spawn(project, "coder", worktree=False).agent
    record = launch_seal.read(agent.id)
    assert isinstance(record, launch_seal.LaunchRecord)

    def built() -> RedactionLevel:
        return handoff.build(
            label=agent.label, source=agent, record=record, target_agent="codex"
        ).redaction

    assert built() is RedactionLevel.standard
    config = load_config()
    config.redaction.level = RedactionLevel.strict
    save_config(config)
    assert built() is RedactionLevel.strict
    config.redaction.level = RedactionLevel.off
    save_config(config)
    assert built() is RedactionLevel.off
