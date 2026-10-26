"""The Spawn dialog's *Hand off from* onto another agent, run end to end (review r22 of #113,
#15's f5), and the pins its mutation run asked for.

The dialog's *Take over* is ``fleet.hand_off(mode="take_over", to_agent=)`` over
``restart(to_agent=)``, and its *Fork* is ``hand_off(mode="fork", to_agent=)``. The dialog's
own tests record the keywords and nothing called either, so four mutations of them lived: the
fork replaying the source's launch onto the other agent, keeping the source's account slot,
keeping the source's agent as the launch's family, and the brief naming claims that are not the
source's. Each leg here runs on the fleet's fake tmux, in both directions between the two real
agents; The DONE note of the task lists the mutation each pin kills.
"""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import launch_seal
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo, TeamSession
from aisquare.services import fleet as fleet_service
from aisquare.services import handoff
from aisquare.services import team as team_service
from tests.test_adapter_contract import _moments, _written
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
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
from tests.test_hand_off_across_agents import (
    _claude_slot,
    _codex_slot,
    _ran,
    _ready_as,
)

NATIVE = "0199a3b2-0000-7000-8000-0000000000aa"


@dataclass
class Leg:
    """One direction: a source agent that has run, its claim, and the account it goes to."""

    source: str
    target: str
    agent: FleetAgent
    source_slot: int
    target_slot: int
    claimed: str
    """The task the source's session holds."""
    elsewhere: str
    """A task another session holds: no brief of the source's work names it."""
    session: str
    transcript: Path


def _spans(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    where: ProjectInfo,
    server: FakeTmux,
    source: str,
) -> Leg:
    """``source`` running with a claim and some conversation, another session holding another
    task, and the other agent's account ready under the line."""
    target = "claude-code" if source == "codex" else "codex"
    if source == "codex":
        own, onward = _codex_slot(10), _claude_slot(monkeypatch, 10)
    else:
        own, onward = _claude_slot(monkeypatch, 10), _codex_slot(10)
    mine = _task(where, "Wire the auth flow")
    theirs = _task(where, "Somebody else's work")
    agent = fleet_service.spawn(
        where,
        "coder",
        agent=source,
        worktree=False,
        account=str(own.slot),
        task_id=mine.id,
    ).agent
    transcript = _written(_moments(get_adapter(source))["asked"], tmp_path / f"{source}.jsonl")
    session = _ran(agent, transcript, native=NATIVE if source == "codex" else None)
    assert team_service.claim_task(mine.id, session_ref=session).status == "doing"
    other = TeamSession(
        id="somebody-else",
        project_id=where.id,
        role="coder",
        started_at=datetime.now(tz=UTC),
        last_seen_at=datetime.now(tz=UTC),
        agent=source,
    )
    with store_session() as store:
        store.upsert_session(other)
    assert team_service.claim_task(theirs.id, session_ref=other.id).status == "doing"
    _ready_as(server, monkeypatch, get_adapter(target).binary)
    return Leg(
        source=source,
        target=target,
        agent=agent,
        source_slot=own.slot,
        target_slot=onward.slot,
        claimed=mine.id,
        elsewhere=theirs.id,
        session=session,
        transcript=transcript,
    )


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """What ``fleet.spawn`` was asked, call by call: the dialog's paths start the agent with
    it, and the launch's replay (``spec``), family and account are its arguments."""
    asked: list[dict[str, Any]] = []
    real = fleet_service.spawn

    def spy(*args: Any, **kwargs: Any) -> Any:
        asked.append({"args": args, **kwargs})
        return real(*args, **kwargs)

    monkeypatch.setattr(fleet_service, "spawn", spy)
    return asked


def _record(agent: FleetAgent) -> launch_seal.LaunchRecord:
    record = launch_seal.read(agent.id)
    assert isinstance(record, launch_seal.LaunchRecord), record
    return record


LEGS = pytest.mark.parametrize("source", ["codex", "claude-code"])


# --- Take over: the dialog's restart onto another agent -------------------------------------


@LEGS
def test_the_dialogs_take_over_onto_another_agent_starts_it_fresh_with_the_brief(
    source: str,
    tmux: FakeTmux,  # noqa: F811 - pytest resolves fixtures by NAME
    codex_on_path: Path,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    started: list[dict[str, Any]],
) -> None:
    leg = _spans(monkeypatch, tmp_path, project, tmux, source)
    spawns = len(started)

    receipt = fleet_service.hand_off(
        project, leg.agent.label, mode="take_over", fresh=True, to_agent=leg.target
    )

    new = receipt.started.agent
    assert receipt.mode == "take_over" and new.label == leg.agent.label
    assert new.agent == leg.target and new.account_slot == leg.target_slot
    assert _record(new).agent == leg.target
    assert [pane for pane in tmux.killed] == [leg.agent.pane_id]  # the source's window, stopped
    # started as today's config launches the role on the other agent, never a replay of the
    # source's launch, and on the other agent's account the ladder chose
    [ask] = started[spawns:]
    assert ask["spec"] is None and ask["family"] is None
    assert ask["agent"] == leg.target and ask["resume"] is None
    assert ask["takes_over"] == leg.session
    command = _command(tmux)
    assert _flag(command, "--account") == str(leg.target_slot)
    assert "--resume" not in command and "resume" not in command
    # the brief: the source's work, and only its claims
    [prompt] = _first_lines(tmux, new.pane_id)
    assert prompt.startswith("You are " + new.label + ", taking over from the previous session")
    assert f"which ran on {get_adapter(leg.source).label}" in prompt
    assert f"You run on {get_adapter(leg.target).label}" in prompt
    assert "Claims, now yours:" in prompt and "Wire the auth flow" in prompt
    assert "Somebody else's work" not in prompt
    kept = [note for note in receipt.notes if note.startswith("hand-off brief: ")]
    [brief] = [Path(note.removeprefix("hand-off brief: ")) for note in kept]
    assert brief.parent == handoff.brief_dir() and brief.is_file()
    assert stat.S_IMODE(brief.stat().st_mode) == 0o600
    # the claim moved with the row; the other session's did not
    assert _task_now(leg.claimed).claimed_by == new.session_id
    assert _task_now(leg.elsewhere).claimed_by == "somebody-else"
    with store_session() as store:
        retired = store.get_session(leg.session)
    assert retired is not None
    if get_adapter(leg.target).capabilities.assigns_session_id:
        # the launch names the new session, so the old one ends and the claims move at once
        assert new.session_id != leg.session and retired.ended_at is not None
    else:
        # the target names its session at its first hook: the row holds the claims, under the
        # old id, until that hook adopts them (`fleet._take_over`), as any fresh start does
        assert new.session_id == leg.session and _task_now(leg.claimed).status == "doing"


@LEGS
def test_the_take_over_onto_another_agent_through_restart_says_it_started_there(
    source: str,
    tmux: FakeTmux,  # noqa: F811 - pytest resolves fixtures by NAME
    codex_on_path: Path,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    leg = _spans(monkeypatch, tmp_path, project, tmux, source)

    receipt = fleet_service.restart(project, leg.agent.label, fresh=True, to_agent=leg.target)

    assert receipt.crossed and (receipt.from_agent, receipt.to_agent) == (leg.source, leg.target)
    assert receipt.started.agent == leg.target and not receipt.resumed
    assert any("no conversation moves from" in note for note in receipt.notes)
    restarted = fleet_service.restart(project, leg.agent.label, fresh=True)
    assert restarted.started.agent == leg.target and not restarted.crossed  # it stays there


# --- Fork: the dialog's hand-off onto another agent -----------------------------------------


@LEGS
def test_the_dialogs_fork_onto_another_agent_touches_nothing_of_its_source(
    source: str,
    tmux: FakeTmux,  # noqa: F811 - pytest resolves fixtures by NAME
    codex_on_path: Path,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    started: list[dict[str, Any]],
) -> None:
    leg = _spans(monkeypatch, tmp_path, project, tmux, source)
    spawns = len(started)

    receipt = fleet_service.hand_off(
        project, leg.agent.label, mode="fork", fresh=True, to_agent=leg.target
    )

    fork = receipt.started.agent
    assert receipt.mode == "fork" and fork.label != leg.agent.label
    assert fork.agent == leg.target and fork.account_slot == leg.target_slot
    assert _record(fork).agent == leg.target
    # the source keeps running with its claim; the fork has none
    assert not tmux.killed
    assert _task_now(leg.claimed).claimed_by == leg.session and fork.task_id is None
    assert _task_now(leg.elsewhere).claimed_by == "somebody-else"
    with store_session() as store:
        kept = store.get_session(leg.session)
    assert kept is not None and kept.ended_at is None
    # started as today's config launches the role there, on the other agent's account
    [ask] = started[spawns:]
    assert ask["spec"] is None and ask["family"] is None
    assert ask["agent"] == leg.target and ask["resume"] is None and ask["fork"] is True
    assert ask["account"] is None and ask["task_id"] is None  # the other agent's ladder chooses
    assert _flag(_command(tmux), "--account") == str(leg.target_slot)
    [prompt] = _first_lines(tmux, fork.pane_id)
    assert prompt.startswith(
        f"You are a fork of {leg.agent.label}, which keeps running on "
        f"{get_adapter(leg.source).label}"
    )
    assert "with no claims" in prompt and "Claims, now yours" not in prompt
    assert "Wire the auth flow" in prompt and "Somebody else's work" not in prompt
    assert any(note.startswith("hand-off brief: ") for note in receipt.notes)
    assert any("no conversation moves from" in note for note in receipt.notes)


@LEGS
def test_a_fork_onto_another_agent_keeps_the_label_it_asked_for_and_the_words_it_was_given(
    source: str,
    tmux: FakeTmux,  # noqa: F811 - pytest resolves fixtures by NAME
    codex_on_path: Path,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    leg = _spans(monkeypatch, tmp_path, project, tmux, source)

    receipt = fleet_service.hand_off(
        project,
        leg.agent.label,
        mode="fork",
        fresh=True,
        to_agent=leg.target,
        label="second-opinion",
        prompt="review the router",
    )

    assert receipt.started.agent.label == "second-opinion"
    [prompt] = _first_lines(tmux, receipt.started.agent.pane_id)
    assert prompt.startswith("You are second-opinion, a fork of " + leg.agent.label)
    assert prompt.endswith("review the router")


def test_a_fork_onto_the_agent_it_already_runs_on_is_a_fork_of_its_own_session(
    tmux: FakeTmux,  # noqa: F811 - pytest resolves fixtures by NAME
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    started: list[dict[str, Any]],
) -> None:
    """``to_agent`` naming the source's own agent is no crossing: no brief, and the fork
    replays the source's launch on the source's account, as it always did."""
    claude = _claude_slot(monkeypatch, 10)
    agent = fleet_service.spawn(project, "coder", worktree=False, account=str(claude.slot)).agent
    _ran(agent, _written(_moments(get_adapter("claude-code"))["asked"], tmp_path / "t.jsonl"))
    spawns = len(started)

    receipt = fleet_service.hand_off(
        project, agent.label, mode="fork", fresh=True, to_agent="claude-code"
    )

    [ask] = started[spawns:]
    assert ask["spec"] is not None and ask["agent"] == "claude-code"
    assert not any(note.startswith("hand-off brief: ") for note in receipt.notes)
    assert receipt.started.agent.agent == "claude-code"


def test_a_key_the_human_pasted_reaches_neither_the_first_message_nor_the_brief_file(
    tmux: FakeTmux,  # noqa: F811 - pytest resolves fixtures by NAME
    codex_on_path: Path,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The conversation goes to another agent's vendor, so what is typed into it is scrubbed
    first (the owner's decision on r22 f4): a key the human pasted into the source's session is
    in the transcript, and in neither the new agent's first message nor the file."""
    key = "sk-ant-api03-" + "Zq9xW3mK" * 6
    leg = _spans(monkeypatch, tmp_path, project, tmux, "claude-code")
    leg.transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "u9",
                "timestamp": "2026-10-19T18:41:00.000Z",
                "message": {"role": "user", "content": f"it fails: ANTHROPIC_API_KEY={key}"},
            }
        )
        + "\n"
        + leg.transcript.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    receipt = fleet_service.hand_off(
        project, leg.agent.label, mode="take_over", fresh=True, to_agent="codex"
    )

    [prompt] = _first_lines(tmux, receipt.started.agent.pane_id)
    [note] = [n for n in receipt.notes if n.startswith("hand-off brief: ")]
    file_text = Path(note.removeprefix("hand-off brief: ")).read_text(encoding="utf-8")
    assert "it fails: ANTHROPIC_API_KEY=[redacted]" in prompt
    assert "it fails: ANTHROPIC_API_KEY=[redacted]" in file_text
    assert key not in prompt and key not in file_text


def _talk(role: str, n: int, text: str) -> str:
    """One Claude Code transcript record, as a line."""
    stamp = f"2026-10-19T18:{n // 60:02d}:{n % 60:02d}.000Z"
    if role == "user":
        message: dict[str, object] = {"role": "user", "content": text}
    else:
        message = {"id": f"m{n}", "role": "assistant", "content": [{"type": "text", "text": text}]}
    return json.dumps(
        {"type": role, "uuid": f"{role[0]}{n}", "timestamp": stamp, "message": message}
    )


def test_credentials_the_reader_wraps_or_the_window_cuts_reach_neither_the_message_nor_the_file(
    tmux: FakeTmux,  # noqa: F811 - pytest resolves fixtures by NAME
    codex_on_path: Path,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The tester's three (review of #15): a real-length Anthropic key in an assignment, a
    JWT of 283 characters, and an RSA key pasted before so much was said that the first
    prompt's window begins inside it. The reader wraps a line at 110 columns, where no pattern
    matches half a key, and the window cuts the start of a block off its end: each is scrubbed
    in the record, whole, before either happens."""
    key = "sk-ant-api03-" + "Qw3rTy9uIo" * 9 + "Zx7cV"  # 108 characters, a real key's length
    jwt = ".".join(
        [
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
            "eyJzdWIiOiI" + "MTIzNDU2Nzg5MA" * 8,
            "SflKxwRJSMeKKF2" + "QT4fwpMeJf36POk6yJV" * 5,
        ]
    )
    assert len(key) == 108 and len(jwt) > 250
    pem = [
        "-----BEGIN RSA PRIVATE KEY-----",
        *(f"MIIEowIBAAKCAQEA{n:02d}" + "q9ZxK" * 12 for n in range(26)),
        "-----END RSA PRIVATE KEY-----",
    ]
    leg = _spans(monkeypatch, tmp_path, project, tmux, "claude-code")
    records = [
        _talk("user", 1, f"it fails: ANTHROPIC_API_KEY={key}"),
        _talk("assistant", 2, "rotate that key"),
        _talk("user", 3, f"this token keeps failing: {jwt}"),
        _talk("assistant", 4, "that one is expired"),
        _talk("user", 5, "and my deploy key:\n" + "\n".join(pem)),
        *(
            _talk("user" if n % 2 else "assistant", 10 + n, f"exchange {n}: " + "word " * 70)
            for n in range(40)
        ),
    ]
    leg.transcript.write_text("\n".join(records) + "\n", encoding="utf-8")

    receipt = fleet_service.hand_off(
        project, leg.agent.label, mode="take_over", fresh=True, to_agent="codex"
    )

    [prompt] = _first_lines(tmux, receipt.started.agent.pane_id)
    [note] = [n for n in receipt.notes if n.startswith("hand-off brief: ")]
    file_text = Path(note.removeprefix("hand-off brief: ")).read_text(encoding="utf-8")
    assert "exchange 39" in prompt and "exchange 39" in file_text, "the newest is carried"
    fragments = [key[-24:], key[20:60], jwt.split(".")[1][20:60], jwt.split(".")[2][-30:],
                 "q9ZxK", "MIIEowIBAAKCAQEA", "BEGIN RSA", "END RSA"]  # fmt: skip
    for where, text in (("the first message", prompt), ("the brief file", file_text)):
        for fragment in fragments:
            assert fragment not in text, (where, fragment)
    assert "ANTHROPIC_API_KEY=[redacted]" in file_text


@pytest.mark.parametrize(("level", "kept"), [("off", True), ("standard", False), ("strict", False)])
def test_the_redaction_level_the_config_holds_is_the_one_a_hand_off_sends_at(
    level: str,
    kept: bool,
    tmux: FakeTmux,  # noqa: F811 - pytest resolves fixtures by NAME
    codex_on_path: Path,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``off`` is the owner's word that the conversation goes as typed; the rest scrub it, and
    ``strict`` takes who and where out of it too."""
    from aisquare.core.config import load_config, save_config
    from aisquare.models import RedactionLevel

    config = load_config()
    config.redaction.level = RedactionLevel(level)
    save_config(config)
    leg = _spans(monkeypatch, tmp_path, project, tmux, "claude-code")
    secret = "API_KEY=abcdef0123456789abcdef"
    leg.transcript.write_text(
        _talk("user", 1, f"mail me@example.com from /Users/anmol/Work/x {secret}") + "\n",
        encoding="utf-8",
    )

    receipt = fleet_service.hand_off(
        project, leg.agent.label, mode="take_over", fresh=True, to_agent="codex"
    )

    [prompt] = _first_lines(tmux, receipt.started.agent.pane_id)
    assert (secret in prompt) is kept
    assert ("me@example.com" in prompt) is (level != "strict")
    assert ("/Users/anmol/Work/x" in prompt) is (level != "strict")
