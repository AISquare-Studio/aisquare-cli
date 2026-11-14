"""Codex parity for main's 0.9.2 features (SCOPE #113 092): the rows that say PARITY and run
through the adapter, each pinned on a Codex fleet agent over the fleet's fake tmux.

- ``fleet pause`` exits a Codex agent through its own exit command and keeps its claims parked
  on its thread; a resume brings the thread back (``codex … resume <id>``) with the carry-on
  line, and a paused Codex with no thread bound starts fresh and takes its claims over.
- ``fleet relabel`` of a Codex agent moves the label its launch record holds too: the label is
  resolved by the record (#113, r8 finding 5), so the new label finds it and the old one names
  nothing, and its window is renamed where the record places it.
- Who started a turn (``metric.origin``): a ``tell`` typed into a waiting Codex leaves its marker
  through the one door, and Codex's ``UserPromptSubmit`` takes it as Claude Code's does; a
  command run in a Codex tool shell is an agent's, by Codex's own mark of those shells.
- ``fleet top`` reads a Codex agent's spend off its rollout by Codex's own reader
  (``AgentTranscript.usage``), where Claude Code's scanner reads nothing of it.
- Auto admission holds a Codex coder on a busy machine and never a Codex manager: the exemption
  is the role's, whatever agent runs it.
- Usage over SSH from what the Mac recorded is Claude Code's Keychain alone: a Codex slot's login
  is its own file, never locked, never said in a ``usage-login:`` row.

The rows that say GAP are in the SCOPE note, each with the lane that closes it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aisquare.core import claude_accounts as accounts_core
from aisquare.core import launch_seal, prompt_origin
from aisquare.core.agent_adapters import claude_usage
from aisquare.core.store import store_session
from aisquare.models import ClaudeAccount, ProjectInfo, TeamSession, TeamTask
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from aisquare.services import fleet_top
from aisquare.services import team as team_service
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_codex_fleet_parity import (  # noqa: F401 - fixtures, by name
    CODEX,
    IDLE_PROMPT,
    THREAD_ONE,
    Codex,
    _claimed_by,
    _native,
    _rollout,
    _row,
    codex_agent,
)
from tests.test_codex_usage import _record, _turn
from tests.test_fleet_auto_admission import Machine, machine, now  # noqa: F401 - by name
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeClock,
    FakeTmux,
    _command,
    _events,
    _first_lines,
    _timed_input,
    claude_on_path,
    clock,
    project,
    repo,
    tmux,
)

# --- fleet pause / resume (SCOPE row: pause and resume) ----------------------------------------


def _the_owners_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """Out of the agent's window: only the owner ends a pause (main's rule, 0.9.2)."""
    for name in ("AISQUARE_FLEET_AGENT", "AISQUARE_LAUNCH_ID", "AISQUARE_ROLE"):
        monkeypatch.delenv(name, raising=False)


def test_a_paused_codex_agent_keeps_its_claims_and_resumes_its_thread_with_the_carry_on_line(
    codex_agent: tuple[Codex, TeamTask],  # noqa: F811
    tmux: FakeTmux,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex, task = codex_agent
    _rollout(codex, THREAD_ONE)
    codex.start(THREAD_ONE)
    thread = codex.board_id(THREAD_ONE)
    team_service.claim_task(task.id, session_ref=thread)

    paused = fleet_service.pause_agent(codex.project, codex.agent.label)

    assert tmux.typed[:2] == [
        (codex.agent.pane_id, "literal", CODEX.exit_command),
        (codex.agent.pane_id, "key", "Enter"),
    ], "exited through the adapter's own exit command"
    row = _row(codex.agent.id)
    assert row.paused_at is not None and row.ended_by == "stopped"
    assert paused.agent.paused_at is not None
    assert _claimed_by(task.id) == thread, "its claim is parked on its thread, not released"
    assert _events(codex.project, "agent_exited") == [], "a pause wakes nobody"

    _the_owners_shell(monkeypatch)
    resumed = fleet_service.restart(codex.project, codex.agent.label)

    assert resumed.resumed is True and resumed.started.agent == "codex"
    native = _native(_command(tmux))
    at = native.index("resume")
    assert native[at : at + 2] == ["resume", THREAD_ONE], "the thread by its native id"
    [line] = _first_lines(tmux, resumed.started.pane_id)
    assert line == fleet_service._restart_prompt(paused.agent), "the carry-on line, a pause's"
    assert line.startswith(f"You are {codex.agent.label}, resumed after `fleet pause`")
    assert _claimed_by(task.id) == thread, "the same thread keeps the claim"
    [said] = _events(codex.project, "restarted")
    assert said.startswith(f"{codex.agent.label} resumed after a pause"), said


def test_a_paused_codex_agent_with_no_thread_starts_fresh_and_takes_its_claims_over(
    codex_agent: tuple[Codex, TeamTask],  # noqa: F811
    tmux: FakeTmux,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex, _task = codex_agent  # spawned, and no hook has bound a thread yet

    fleet_service.pause_agent(codex.project, codex.agent.label)
    _the_owners_shell(monkeypatch)
    resumed = fleet_service.restart(codex.project, codex.agent.label)

    assert resumed.resumed is False
    assert "resume" not in _native(_command(tmux)), "no thread to resume: started fresh"
    old = _row(codex.agent.id)
    assert (old.paused_at, old.ended_by) == (None, "stopped"), "the paused row is history, a stop"


# --- Stop's one-click path (SCOPE row: Don't ask again) ----------------------------------------


def test_the_one_click_stop_says_a_stop_that_typed_no_exit_command_then_its_claims(
    codex_agent: tuple[Codex, TeamTask],  # noqa: F811
) -> None:
    """The PR's warning for a window killed with no exit command typed (a row no launch
    record places) rides main's helper, which the dialog and the one-click stop both call."""
    from aisquare.cli.ui.stop import warn_release_failed

    codex, _task = codex_agent
    said: list[tuple[str, str]] = []

    class App:
        def notify(self, message: str, *, severity: str, **_: object) -> None:
            said.append((message, severity))

    receipt = fleet_service.StopReceipt(
        codex.agent, [], "the board would not take them", killed="killed: no record places it"
    )
    warn_release_failed(App(), receipt)  # type: ignore[arg-type]

    assert said == [
        ("killed: no record places it", "warning"),
        ("claims: the board would not take them", "warning"),
    ]


# --- fleet relabel (SCOPE row: rename a running agent) -----------------------------------------


def test_a_relabelled_codex_agent_answers_to_its_new_label_by_its_launch_record(
    codex_agent: tuple[Codex, TeamTask],  # noqa: F811
    tmux: FakeTmux,  # noqa: F811
) -> None:
    codex, _task = codex_agent
    old = codex.agent.label

    receipt = fleet_service.relabel(codex.project, old, "Login Fix")

    assert receipt.said == f"{old} is now login-fix"
    record = launch_seal.read(codex.agent.id)
    assert isinstance(record, launch_seal.LaunchRecord) and record.label == "login-fix"
    with store_session() as store:
        found = fleet_service._by_label(store, codex.project.id, "login-fix", live_only=True)
        gone = fleet_service._by_label(store, codex.project.id, old, live_only=True)
    assert found is not None and found[0].id == codex.agent.id
    assert gone is None, "the old label names nothing, by the store or by a record"
    assert (codex.agent.pane_id, "login-fix") in tmux.renamed_windows
    with pytest.raises(fleet_service.NoSuchAgent, match="renamed: it is login-fix now"):
        fleet_service.stop(codex.project, old)


# --- who started each turn (SCOPE row: metric.origin) ------------------------------------------


def test_a_tell_typed_into_a_waiting_codex_is_its_turns_origin(
    codex_agent: tuple[Codex, TeamTask],  # noqa: F811
    tmux: FakeTmux,  # noqa: F811
    clock: FakeClock,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex, _task = codex_agent
    pane = codex.agent.pane_id
    tmux.set_command(pane, "codex")
    tmux.screens[pane] = IDLE_PROMPT
    _timed_input(tmux, clock, monkeypatch)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("AISQUARE_FLEET_AGENT", raising=False)  # told from a person's shell

    told = fleet_service.tell(codex.project, codex.agent.label, "reply ok")
    assert told.delivered is True, told
    codex.start(THREAD_ONE)
    codex.hook("UserPromptSubmit", THREAD_ONE, prompt="reply ok", turn_id="turn-1")

    with store_session() as store:
        turns = store.turn_metrics(session_id=codex.board_id(THREAD_ONE))
    assert [turn.origin for turn in turns] == ["human:tell"]


def test_a_command_run_in_a_codex_tool_shell_is_an_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("CLAUDECODE", "AISQUARE_FLEET_AGENT"):
        monkeypatch.delenv(name, raising=False)
    assert prompt_origin.origin_sender() == prompt_origin.HUMAN, "a person's shell"

    tool_shell = CODEX.capabilities.tool_shell_env
    assert tool_shell is not None
    monkeypatch.setenv(tool_shell, THREAD_ONE)

    assert prompt_origin.origin_sender() == prompt_origin.AGENT
    prompt_origin.mark_origin("go on", prompt_origin.TELL)
    assert prompt_origin.take_origin("go on") == "agent:tell"


# --- fleet top's spend (SCOPE row: fleet top and the Agent resources page) ---------------------


def test_fleet_top_reads_a_codex_agents_spend_off_its_rollout_by_its_own_reader(
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    rollout = tmp_path / "rollout-codex.jsonl"
    rollout.write_text(
        _turn(0, "t1", "gpt-5.4") + _record(1, "t1", "resp_a", input=1000, output=10),
        encoding="utf-8",
    )
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id="ses-codex",
                project_id=project.id,
                role="coder",
                started_at=fleet_service._now(),
                last_seen_at=fleet_service._now(),
                transcript_path=str(rollout),
                agent="codex",
            )
        )
    reader = fleet_top.SpendReader()

    with store_session() as store:
        first = reader.spend(store, "ses-codex", "codex")
    with rollout.open("a", encoding="utf-8") as more:
        more.write(_record(2, "t1", "resp_b", input=100, output=5))
    with store_session() as store:
        second = reader.spend(store, "ses-codex", "codex")

    assert first is not None and first.tokens == 1010 and first.priced
    assert second is not None and second.tokens == 1010 + 105, "only what the rollout gained"
    nothing, _state = claude_usage.scan(rollout)
    assert nothing.totals().total == 0, "Claude Code's scanner reads nothing of a rollout"


# --- auto admission (SCOPE row: fleet admission follows the machine's load) ---------------------


def test_auto_admission_holds_a_codex_coder_and_never_a_codex_manager(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    machine: Machine,  # noqa: F811
    now: list[object],  # noqa: F811
) -> None:
    for _ in range(3):
        fleet_service.spawn(project, "coder", agent="codex", worktree=False)
    machine.busy()

    manager = fleet_service.spawn(project, "manager", agent="codex")

    assert manager.agent.agent == "codex" and manager.agent.ended_at is None
    assert not any("busy" in note for note in manager.notes), manager.notes
    with pytest.raises(fleet_service.CapacityRefused) as held:
        fleet_service.spawn(project, "coder", agent="codex", worktree=False)
    assert held.value.code == "capacity_busy", "a coder is auto's, whatever its agent"


# --- usage over SSH (SCOPE row: what the Mac recorded) -----------------------------------------


def test_a_codex_slots_login_is_no_keychain_item_and_no_row_is_said_for_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(accounts_core, "keychain_platform", lambda: True)
    codex = ClaudeAccount(slot=3, config_dir=tmp_path / "c3", agent="codex", managed=True)

    logins = accounts_service._logins([codex], fleet_service._now(), say=True)
    read = accounts_service.read_usage([codex], now=fleet_service._now())
    accounts_service.forget_arrangement(codex.slot, codex)  # its removal, as `accounts remove`

    assert not accounts_core.has_stored_login(codex)
    assert (logins.tokens, logins.seen, logins.locked) == ({}, [], []), "read off its home"
    assert set(read) == {codex.slot}, "read where the Keychain is, with no item to name for it"
