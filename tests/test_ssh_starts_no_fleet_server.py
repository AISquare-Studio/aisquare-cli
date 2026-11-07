"""#20: an SSH login on macOS never starts the fleet's tmux server.

A tmux server runs in the macOS security session of the process that started it, and so does
every pane it opens. An SSH asq that started the fleet's server opened every agent in a session
that cannot read the Keychain ("login keychain is locked"). Spawning into a server the Mac
started is unchanged.

The premise was probed on the owner's Mac (macOS 27.2) with throwaway servers on their own
``-L`` sockets, quoted in PR #464: from an SSH login, a pane of a server it started could not
read the Claude Code login (rc=36, session ``0x5020 [tty REMOTE authenticated]``), and a new
window of a server started at the Mac could (rc=0, ``0x6030 [graphic tty console
authenticated]``).

SCOPE #113 092 row 6: the refusal is the agent's, by its accounts slot
(``AgentAccounts.keychain_login``). A Codex agent, whose login is ``auth.json`` in its home,
starts the server from SSH, which is marked so (``fleet_service.SSH_STARTED_OPTION``). A Claude
Code agent is still refused there, and in that server from SSH or at the Mac, since every pane
of it is in the SSH login's security session.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import claude_accounts as accounts_core
from aisquare.core import login_session
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from tests import test_codex_account_switch as codex_suite
from tests import test_fleet_service as fleet_suite
from tests import test_hand_off_across_agents as across_suite

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project
codex_on_path = codex_suite.codex_on_path

_REAL_REMOTE_SESSION = login_session.remote_session
"""The real one: ``conftest.at_the_machine`` replaces it in every test."""


class _Server:
    """The fleet's server as ``_before_a_server_starts`` and ``open_window`` ask it: what its
    socket answers, the windows opened on it, and its global user options (the mark of a
    server an SSH login started), which tmux reads and sets by ``run``."""

    socket = "asq-test"
    path = None

    def __init__(self, answer: str | None) -> None:
        self.answer = answer
        self.opened: list[str] = []
        self.options: dict[str, str] = {}

    def run(self, *args: str, stdin: bytes | None = None) -> str:
        match list(args):
            case ["show-options", "-gqv", option]:
                return self.options.get(option, "")
            case ["set-option", "-g", option, value]:
                self.options[option] = value
                return ""
        raise AssertionError(f"tmux asked to run {list(args)}")

    def socket_path(self) -> Path:
        return Path("/tmp/tmux-test/asq")

    def listener(self, recorded: Path | None = None) -> str | None:
        return self.answer

    def spawn_window(self, session: str, **_: Any) -> str:
        self.opened.append(session)
        return "window"


def _open(srv: _Server, agent: str | None = None) -> object:
    return fleet_service.open_window(
        srv,  # type: ignore[arg-type]
        "asq-test",
        name="coder",
        cwd=Path.cwd(),
        command=["true"],
        config=fleet_service.settings(),
        agent=agent,
    )


@pytest.mark.parametrize("answer", [None, "gone"], ids=["stale-file", "no-file"])
def test_an_ssh_login_does_not_start_the_fleets_server(
    monkeypatch: pytest.MonkeyPatch, answer: str | None
) -> None:
    monkeypatch.setattr(login_session, "remote_session", lambda: True)
    srv = _Server(answer)

    with pytest.raises(fleet_service.FleetError) as refused:
        _open(srv)

    assert fleet_service.SSH_START_REFUSAL in str(refused.value)
    assert str(refused.value).startswith(
        "not started: start asq on the Mac first: agents started from an SSH login can't "
        "read the Keychain"
    )
    assert srv.opened == [], "a window opened, and with it a server"


def test_a_remote_session_joins_a_running_server_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe's step 5, end to end through the session read: an SSH login's session (its
    measured bits, REMOTE among them) opens a window in a server the Mac started, and is refused
    only where it would start one."""
    monkeypatch.setattr(login_session, "remote_session", _REAL_REMOTE_SESSION)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", lambda path: _Security(0, 0x5020))
    running = _Server("answers")
    assert _open(running) == "window"
    assert running.opened == ["asq-test"]

    none_yet = _Server(None)
    with pytest.raises(fleet_service.FleetError, match="start asq on the Mac first"):
        _open(none_yet)
    assert none_yet.opened == []


def test_an_ssh_login_spawns_into_a_running_server_as_ever(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(login_session, "remote_session", lambda: True)
    srv = _Server("answers")
    assert _open(srv) == "window"
    assert srv.opened == ["asq-test"]


def test_a_socket_that_could_not_be_asked_is_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A denied socket or a connect that timed out says nothing of whether a server runs:
    nothing is refused on a guess."""
    monkeypatch.setattr(login_session, "remote_session", lambda: True)
    srv = _Server("unasked")
    assert _open(srv) == "window"


@pytest.mark.parametrize("remote", [False, None], ids=["at-the-mac", "not-macos"])
def test_a_login_at_the_machine_starts_the_server_as_ever(
    monkeypatch: pytest.MonkeyPatch, remote: bool | None
) -> None:
    monkeypatch.setattr(login_session, "remote_session", lambda: remote)
    calls: list[object] = []

    def forget(*args: object) -> list[object]:
        calls.append(args)
        return []

    monkeypatch.setattr(fleet_service, "_forget_an_exited_server", forget)
    srv = _Server(None)
    assert _open(srv) == "window"
    assert srv.opened == ["asq-test"] and len(calls) == 1


def test_a_spawn_from_ssh_is_refused_before_anything_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spawn's own early check (before the project is captured or a worktree made) is the
    same one, so the refusal comes first."""
    monkeypatch.setattr(login_session, "remote_session", lambda: True)
    srv = _Server(None)
    forgot: list[object] = []
    monkeypatch.setattr(fleet_service, "_forget_an_exited_server", lambda *a: forgot.append(a))
    with pytest.raises(fleet_service.FleetError, match="start asq on the Mac first"):
        fleet_service._before_a_server_starts(srv, fleet_service.settings())  # type: ignore[arg-type]
    assert forgot == [], "a row's server start was forgotten for a server never started"


class _Security:
    """``Security.framework`` as ``SessionGetInfo`` answers: ``status`` and the attribute bits."""

    def __init__(self, status: int, bits: int) -> None:
        self.status = status
        self.bits = bits

    def SessionGetInfo(self, caller: Any, session: Any, attributes: Any) -> int:
        assert caller.value == login_session.CALLER_SECURITY_SESSION
        attributes._obj.value = self.bits
        return self.status


@pytest.mark.parametrize(
    ("status", "bits", "expected"),
    [
        # Both measured by the probe on the owner's Mac (macOS 27.2), quoted in PR #464.
        (0, 0x6030, False),  # graphic tty console authenticated: at the Mac, Keychain rc=0
        (0, 0x5020, True),  # tty REMOTE authenticated: an SSH login, Keychain rc=36
        # The REMOTE bit alone decides: the SSH login without it would not be refused, and
        # a session without graphic access is not refused for that.
        (0, 0x5020 & ~login_session.SESSION_IS_REMOTE, False),
        (-60500, 0x5020, None),  # SessionGetInfo failed: no answer, no guess
    ],
    ids=["at-the-mac", "ssh", "ssh-without-the-remote-bit", "unanswered"],
)
def test_the_session_says_whether_it_was_reached_over_the_network(
    monkeypatch: pytest.MonkeyPatch, status: int, bits: int, expected: bool | None
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", lambda path: _Security(status, bits))
    assert _REAL_REMOTE_SESSION() is expected


def test_no_security_framework_is_no_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(path: str) -> object:
        raise OSError(f"dlopen({path}) failed")

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", missing)
    assert _REAL_REMOTE_SESSION() is None


def test_off_macos_there_is_no_session_to_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert _REAL_REMOTE_SESSION() is None


@pytest.mark.skipif(sys.platform != "darwin", reason="the real Security.framework")
def test_the_real_session_answers_on_macos() -> None:
    assert _REAL_REMOTE_SESSION() in (True, False)


def test_a_restart_from_ssh_over_a_stale_socket_refuses_before_it_records_the_dead_row(
    tmux: Any, claude_on_path: Path, project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``restart`` refuses all that does not depend on the stop BEFORE anything is stopped or
    recorded (its contract). From SSH over a stale socket it recorded the dead row, and only
    its replacement's spawn refused to start the server (review of #464, finding 1)."""
    from aisquare.core.store import store_session

    agent = fleet_service.spawn(project, "coder").agent
    tmux.kill_server()  # the Mac's server exited: its socket file is stale, no server listens
    monkeypatch.setattr(login_session, "remote_session", lambda: True)  # and this is SSH

    def row() -> tuple[object, object]:
        with store_session() as store:
            [now] = [r for r in store.fleet_agents(project.id) if r.id == agent.id]
        return now.ended_at, now.ended_by

    before = (row(), list(tmux.typed), len(tmux.spawned))

    with pytest.raises(fleet_service.FleetError, match="start asq on the Mac first") as refused:
        fleet_service.restart(project, agent.label)

    assert row() == (None, None), "the dead row is left for a listing, as a refusal leaves it"
    assert (row(), list(tmux.typed), len(tmux.spawned)) == before, "nothing recorded or started"
    assert fleet_suite._events(project, "agent_exited") == []
    assert f"cannot restart {agent.label!r}" in str(refused.value), "restart's own refusal"


def test_a_resume_from_ssh_with_no_server_says_resume(
    tmux: Any, claude_on_path: Path, project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REVIEW-RC092-MI, nit 2: ``fleet resume`` from SSH with no server refused each paused
    agent as "cannot restart": the restart it runs on is not the command the owner gave."""
    agent = fleet_service.spawn(project, "coder").agent
    fleet_service.bulk("pause", projects=[project])
    assert not tmux.running, "the pause closed the server's last window, and the server went"
    monkeypatch.setattr(login_session, "remote_session", lambda: True)

    report = fleet_service.bulk("resume", projects=[project])

    [why] = [r.why for r in report.receipts if r.agent.id == agent.id and r.outcome == "failed"]
    assert why is not None and why.startswith(f"cannot resume {agent.label!r}: not started: ")
    assert fleet_service.SSH_START_REFUSAL in why and "cannot restart" not in why
    assert fleet_service.is_paused(project), "still paused, as a refused resume leaves it"


@pytest.mark.parametrize("answer", [None, "gone", "answers", "unasked"])  # listener()'s answers
def test_the_start_and_restarts_early_check_keep_one_rule_for_no_server_listens(
    answer: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#23 (review 5388): `restart`'s pre-stop refusal and the start's own check asked the same
    question in two copies. One rule now (`_no_server_listens`): over SSH both refuse exactly
    where a window would start a server, a stale file or none."""
    monkeypatch.setattr(login_session, "remote_session", lambda: True)
    monkeypatch.setattr(fleet_service, "_forget_an_exited_server", lambda *a: [])
    monkeypatch.setattr(fleet_service, "_refuse_a_freed_socket", lambda *a: None)
    starts = answer in (None, "gone")

    def refused(check: Any) -> bool:
        try:
            check()
        except fleet_service.FleetError as exc:
            assert "start asq on the Mac first" in str(exc)
            return True
        return False

    srv = _Server(answer)
    config = fleet_service.settings()
    assert fleet_service._no_server_listens(answer) is starts  # type: ignore[arg-type]
    assert refused(lambda: fleet_service._refuse_a_server_start_from_ssh(srv)) is starts  # type: ignore[arg-type]
    assert refused(lambda: fleet_service._before_a_server_starts(srv, config)) is starts  # type: ignore[arg-type]


# --- 092 row 6: the agent's accounts slot says whether SSH may start the server for it ---------


def test_claude_codes_login_is_in_the_keychain_and_refreshes_quietly_codexs_neither() -> None:
    """The two accounts facts, as each agent declares them: Claude Code's login is a Keychain
    item and its stale token is refreshed by ``claude mcp get``; Codex's is ``auth.json``,
    which Codex renews itself."""
    from aisquare.core.agent_adapters import get_adapter

    claude, codex = get_adapter("claude-code").accounts, get_adapter("codex").accounts
    assert claude is not None and codex is not None
    assert (claude.keychain_login, claude.refresh_args()) == (
        True,
        accounts_core.TOKEN_REFRESH_ARGS,
    )
    assert (codex.keychain_login, codex.refresh_args()) == (False, ())
    assert accounts_core.has_stored_login(accounts_core.default_account())
    assert not accounts_core.has_stored_login(accounts_core.create_account("codex"))


def _no_server(tmux: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """No fleet server runs (the last one exited, leaving its socket file, which the next one
    takes); this process is an SSH login."""
    tmux.running = False
    monkeypatch.setattr(login_session, "remote_session", lambda: True)


def test_from_ssh_a_codex_spawn_starts_the_server_and_its_agent(
    tmux: Any, codex_on_path: Path, project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Codex agent's login is ``auth.json`` in its home, which an SSH login's session reads
    as the Mac's does (``CodexAccounts.keychain_login`` is False): ``fleet spawn --agent
    codex`` from SSH starts the fleet's server and the agent in it, and marks the server as an
    SSH login's."""
    _no_server(tmux, monkeypatch)

    agent = fleet_service.spawn(project, "coder", agent="codex", worktree=False).agent

    assert agent.agent == "codex" and tmux.running, "the server started"
    assert agent.pane_id in tmux.facts, "and the agent in it"
    assert tmux.options == {fleet_service.SSH_STARTED_OPTION: "1"}, "marked as SSH's"


def test_from_ssh_a_claude_code_spawn_with_no_server_is_still_refused(
    tmux: Any, claude_on_path: Path, project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code's login is in the Keychain (``keychain_login`` True): from SSH its spawn
    starts no server, before anything is written, and the refusal names the agent that can."""
    _no_server(tmux, monkeypatch)

    with pytest.raises(fleet_service.FleetError) as refused:
        fleet_service.spawn(project, "coder", worktree=False)

    said = str(refused.value)
    assert said.startswith(f"not started: {fleet_service.SSH_START_REFUSAL}"), said
    assert said.endswith("; a Codex agent can start it from here"), said
    assert not tmux.running and tmux.spawned == [], "no server, no window"


@pytest.mark.parametrize("at", ["ssh", "the-mac"])
def test_a_claude_code_spawn_into_the_server_ssh_started_is_refused_from_anywhere(
    tmux: Any,
    claude_on_path: Path,
    codex_on_path: Path,
    project: Any,
    monkeypatch: pytest.MonkeyPatch,
    at: str,
) -> None:
    """Every pane of a server an SSH login started is in that login's security session,
    whichever process asks for it: a Claude Code spawn into it is refused from SSH and from
    the Mac alike, and a Codex one joins it. Once that server is gone, the Mac starts the
    next one, unmarked, and Claude Code runs in it."""
    _no_server(tmux, monkeypatch)
    fleet_service.spawn(project, "coder", agent="codex", worktree=False)
    monkeypatch.setattr(login_session, "remote_session", lambda: at == "ssh")
    windows = len(tmux.spawned)

    with pytest.raises(fleet_service.FleetError) as refused:
        fleet_service.spawn(project, "coder", worktree=False)

    said = str(refused.value)
    assert said.startswith(f"not started: {fleet_service.SSH_STARTED_REFUSAL}"), said
    assert "`aisquare fleet shutdown --all` ends that server" in said, said
    assert len(tmux.spawned) == windows, "no window opened"
    joined = fleet_service.spawn(project, "coder", agent="codex", worktree=False).agent
    assert joined.pane_id in tmux.facts, "a Codex agent joins it"

    tmux.kill_server()
    monkeypatch.setattr(login_session, "remote_session", lambda: False)  # at the Mac
    claude = fleet_service.spawn(project, "coder", worktree=False).agent
    assert claude.pane_id in tmux.facts and tmux.options == {}, "the Mac's server: unmarked"


def _row_now(project: Any, agent_id: str) -> tuple[object, object]:
    """Whether the store has ended the row ``agent_id``, and by whom: what a refusal before the
    stop leaves as it was."""
    from aisquare.core.store import store_session

    with store_session() as store:
        [now] = [r for r in store.fleet_agents(project.id) if r.id == agent_id]
    return now.ended_at, now.ended_by


@pytest.mark.parametrize("at", ["ssh", "the-mac"])
def test_a_restart_into_the_server_ssh_started_refuses_before_it_records_the_dead_row(
    tmux: Any,
    claude_on_path: Path,
    codex_on_path: Path,
    project: Any,
    monkeypatch: pytest.MonkeyPatch,
    at: str,
) -> None:
    """``restart`` refuses all that does not depend on the stop before anything is stopped or
    recorded. A Claude Code row whose Mac server exited, restarted while a Codex spawn from
    SSH started the next server, would join a server it cannot read its login in, from SSH
    and from the Mac alike (every pane of it is in the SSH login's session): refused first,
    with the row as it was. At the Mac the check is still asked of the marked server; main's
    ``if not remote: return`` recorded the row ``died`` and only the replacement's spawn
    refused (tester of #56, seq 10312)."""
    agent = fleet_service.spawn(project, "coder").agent  # at the Mac
    tmux.kill_server()  # the Mac's server exited
    monkeypatch.setattr(login_session, "remote_session", lambda: True)
    fleet_service.spawn(project, "coder", agent="codex", worktree=False)  # SSH starts the next
    monkeypatch.setattr(login_session, "remote_session", lambda: at == "ssh")
    before = (_row_now(project, agent.id), list(tmux.typed), len(tmux.spawned))

    with pytest.raises(fleet_service.FleetError) as refused:
        fleet_service.restart(project, agent.label)

    assert (_row_now(project, agent.id), list(tmux.typed), len(tmux.spawned)) == before, (
        "nothing recorded or started"
    )
    assert str(refused.value).startswith(
        f"cannot restart {agent.label!r}: not started: {fleet_service.SSH_STARTED_REFUSAL}"
    ), str(refused.value)


@pytest.mark.parametrize("at", ["ssh", "the-mac"])
def test_a_take_over_onto_claude_code_in_the_server_ssh_started_refuses_before_the_stop(
    tmux: Any,
    claude_on_path: Path,
    codex_on_path: Path,
    project: Any,
    monkeypatch: pytest.MonkeyPatch,
    at: str,
) -> None:
    """A take-over is asked of the agent it starts on (``to_agent``), not the row's: a Codex
    row in the server an SSH login started, taken over by Claude Code, would put Claude Code
    in a pane that cannot read its Keychain login. Refused before the stop, from SSH and from
    the Mac, with the Codex row as it was (tester of #56, seq 10312: asked of the row's own
    agent, the stop ran and recorded it first)."""
    _no_server(tmux, monkeypatch)
    agent = fleet_service.spawn(project, "coder", agent="codex", worktree=False).agent
    monkeypatch.setattr(login_session, "remote_session", lambda: at == "ssh")
    before = (_row_now(project, agent.id), list(tmux.typed), len(tmux.spawned))

    with pytest.raises(fleet_service.FleetError) as refused:
        fleet_service.restart(project, agent.label, fresh=True, to_agent="claude-code")

    assert fleet_service.SSH_STARTED_REFUSAL in str(refused.value), str(refused.value)
    assert (_row_now(project, agent.id), list(tmux.typed), len(tmux.spawned)) == before, (
        "nothing stopped, recorded or started"
    )


def test_a_take_over_onto_codex_from_ssh_starts_the_server_and_marks_it(
    tmux: Any,
    claude_on_path: Path,
    codex_on_path: Path,
    project: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other way round: a Claude Code row whose Mac server exited, taken over by Codex from
    SSH, starts the server for Codex, whose login is a file, and marks it as an SSH login's.
    Asked of the row's own agent it was refused for Claude Code's Keychain (tester of #56)."""
    agent = fleet_service.spawn(project, "coder", worktree=False).agent  # at the Mac
    tmux.kill_server()
    monkeypatch.setattr(login_session, "remote_session", lambda: True)

    receipt = fleet_service.restart(project, agent.label, fresh=True, to_agent="codex")

    assert receipt.started.agent == "codex" and tmux.running, "the server started, for Codex"
    assert receipt.started.pane_id in tmux.facts
    assert tmux.options == {fleet_service.SSH_STARTED_OPTION: "1"}, "marked as SSH's"


def _codex_in_the_server_ssh_started(
    tmux: Any, project: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **spawn: Any
) -> tuple[Any, str, Any]:
    """A Codex agent that an SSH login started the fleet's server for (marked so), its session
    on the board holding its task's claim: what a stop before a refusal would lose. A second
    Codex agent keeps that server up past the first one's stop, as the reviewer's probe
    did: the switch's replacement opens in it, not in the next server."""
    from aisquare.core.agent_adapters import get_adapter
    from aisquare.services import team as team_service
    from tests.test_adapter_contract import _moments, _written

    _no_server(tmux, monkeypatch)
    task = fleet_suite._task(project, "Wire the auth flow")
    agent = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, task_id=task.id, **spawn
    ).agent
    rollout = _written(_moments(get_adapter("codex"))["asked"], tmp_path / "rollout.jsonl")
    session = across_suite._ran(agent, rollout, native="0199a3b2-0000-7000-8000-0000000000a5")
    assert team_service.claim_task(task.id, session_ref=session).status == "doing"
    fleet_service.spawn(project, "coder", agent="codex", worktree=False)  # keeps the server up
    assert tmux.options == {fleet_service.SSH_STARTED_OPTION: "1"}, "SSH started the server"
    return agent, session, task


def _as_it_was(project: Any, agent: Any, tmux: Any) -> tuple[object, ...]:
    """What a refusal before the stop leaves as it was: the row, every key typed, every window
    opened and killed."""
    return (_row_now(project, agent.id), list(tmux.typed), len(tmux.spawned), list(tmux.killed))


def _still_runs_with_its_claim(agent: Any, session: str, task: Any, tmux: Any) -> None:
    from aisquare.core.store import store_session
    from aisquare.services import team as team_service

    assert agent.pane_id in tmux.facts, "its window still runs"
    held = fleet_suite._task_now(task.id)
    assert (held.status, held.claimed_by) == ("doing", session), "its claim is still its own"
    with store_session() as store:
        now = store.get_session(session)
    assert now is not None and now.ended_at is None
    assert now.state != team_service.HANDOVER_STATE, "no hand-over mark left on it"


@pytest.mark.parametrize("at", ["ssh", "the-mac"])
def test_a_switch_onto_claude_code_in_the_server_ssh_started_refuses_before_the_stop(
    tmux: Any,
    claude_on_path: Path,
    codex_on_path: Path,
    project: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    at: str,
) -> None:
    """Review r36 of #113, finding 1: ``fleet switch --agent claude-code`` of a Codex row in
    the server an SSH login started (by hand, and Remote's Switch, which calls the same
    ``switch``) stopped the Codex agent first, and only the Claude Code spawn was refused:
    the agent lost, its task back in the pool. Asked of the agent it starts on, before the
    stop, as ``restart``'s take-over asks it, from SSH and at the Mac: refused, and the Codex
    agent runs on with its claim."""
    agent, session, task = _codex_in_the_server_ssh_started(tmux, project, monkeypatch, tmp_path)
    monkeypatch.setattr(login_session, "remote_session", lambda: at == "ssh")
    before = _as_it_was(project, agent, tmux)

    with pytest.raises(fleet_service.FleetError) as refused:
        fleet_service.switch(project, agent.label, to_agent="claude-code")

    assert str(refused.value).startswith(
        f"cannot switch {agent.label!r}: not started: {fleet_service.SSH_STARTED_REFUSAL}"
    ), str(refused.value)
    assert _as_it_was(project, agent, tmux) == before, "nothing stopped, recorded or started"
    _still_runs_with_its_claim(agent, session, task, tmux)


@pytest.mark.parametrize("at", ["ssh", "the-mac"])
def test_the_automatic_hand_over_onto_claude_code_there_refuses_before_the_stop_and_says_why(
    tmux: Any,
    claude_on_path: Path,
    codex_on_path: Path,
    project: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    at: str,
) -> None:
    """The same, for the automatic hand-over (``hooks.hand_over``, ``[accounts]
    cross_agent_handover`` on by default): every Codex account full and a Claude Code one
    with room, it crossed onto Claude Code, stopped the Codex agent and lost it. Refused
    before the stop, it stays where it is with its claim, and the board's
    ``handover_failed`` line, the one that wakes the manager, says why."""
    from aisquare.services import hooks as hooks_service

    full = across_suite._codex_slot(100, "full@example.com")
    across_suite._claude_slot(monkeypatch, 20)
    agent, session, task = _codex_in_the_server_ssh_started(
        tmux, project, monkeypatch, tmp_path, account=str(full.slot)
    )
    monkeypatch.setattr(login_session, "remote_session", lambda: at == "ssh")
    before = _as_it_was(project, agent, tmux)

    hooks_service.hand_over(session, reason="5h limit")

    events = across_suite._events(project)
    [said] = [text for kind, text in events if kind == "handover_failed"]
    assert said.startswith(
        f"{agent.label}: not switched — cannot switch {agent.label!r}: not started: "
        f"{fleet_service.SSH_STARTED_REFUSAL}"
    ), said
    assert "switched" not in {kind for kind, _text in events}
    assert _as_it_was(project, agent, tmux) == before, "nothing stopped, recorded or started"
    _still_runs_with_its_claim(agent, session, task, tmux)


@pytest.mark.parametrize("at", ["ssh", "the-mac"])
def test_a_codex_to_codex_switch_in_the_server_ssh_started_goes_ahead(
    tmux: Any,
    codex_on_path: Path,
    project: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    at: str,
) -> None:
    """The refusal is the agent the replacement runs, by its accounts slot: a Codex agent moved
    onto another Codex account there starts in that server as before, from SSH and at the
    Mac, which stays marked."""
    full = across_suite._codex_slot(100, "full@example.com")
    roomy = across_suite._codex_slot(10, "roomy@example.com")
    agent, _session, task = _codex_in_the_server_ssh_started(
        tmux, project, monkeypatch, tmp_path, account=str(full.slot)
    )
    monkeypatch.setattr(login_session, "remote_session", lambda: at == "ssh")

    receipt = fleet_service.switch(project, agent.label, to=str(roomy.slot))

    assert (receipt.from_agent, receipt.to_agent, receipt.to_slot) == ("codex", "codex", roomy.slot)
    assert receipt.started.pane_id in tmux.facts and receipt.started.task_id == task.id
    assert agent.pane_id not in tmux.facts, "the old window was stopped"
    assert tmux.options == {fleet_service.SSH_STARTED_OPTION: "1"}, "the same server, marked"


def test_a_sign_in_window_asks_the_agent_of_its_slot(
    tmux: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Accounts page's sign-in window runs the slot's agent: a Codex slot's starts the
    fleet's server from SSH, marked, and a Claude Code slot's is then refused in it."""
    _no_server(tmux, monkeypatch)
    config = fleet_service.settings()
    codex = accounts_core.create_account("codex")

    window = accounts_service.open_sign_in_window(codex, tmux, config=config)

    assert window.pane_id in tmux.facts
    assert tmux.options == {fleet_service.SSH_STARTED_OPTION: "1"}
    with pytest.raises(fleet_service.FleetError, match="was started from an SSH login"):
        accounts_service.open_sign_in_window(accounts_core.default_account(), tmux, config=config)


@pytest.mark.parametrize(
    ("agent", "starts"),
    [("claude-code", False), ("codex", True), (None, False)],
    ids=["keychain", "file", "no-agent"],
)
def test_the_start_from_ssh_is_the_agents_and_a_window_of_none_is_refused_as_before(
    monkeypatch: pytest.MonkeyPatch, agent: str | None, starts: bool
) -> None:
    """``open_window`` asks the agent it opens: one that declares its login no Keychain item
    starts the server from SSH and marks it; one whose login is, and a window that runs no
    agent (the watcher, an editor), are refused as every start was before."""
    monkeypatch.setattr(login_session, "remote_session", lambda: True)
    srv = _Server(None)
    if starts:
        assert _open(srv, agent) == "window"
        assert srv.options == {fleet_service.SSH_STARTED_OPTION: "1"}
    else:
        with pytest.raises(fleet_service.FleetError, match="start asq on the Mac first"):
            _open(srv, agent)
        assert srv.opened == [] and srv.options == {}


@pytest.mark.parametrize("remote", [True, False, None], ids=["ssh", "at-the-mac", "not-macos"])
def test_a_marked_server_refuses_the_keychain_agent_alone(
    monkeypatch: pytest.MonkeyPatch, remote: bool | None
) -> None:
    """In a server an SSH login started, Claude Code is refused and Codex or a window of no
    agent joins it. Off macOS no server is ever marked, so none is asked."""
    monkeypatch.setattr(login_session, "remote_session", lambda: remote)
    for agent, refused in (("claude-code", remote is not None), ("codex", False), (None, False)):
        srv = _Server("answers")
        srv.options[fleet_service.SSH_STARTED_OPTION] = "1"
        if refused:
            with pytest.raises(fleet_service.FleetError, match="was started from an SSH login"):
                _open(srv, agent)
            assert srv.opened == []
        else:
            assert _open(srv, agent) == "window", agent
