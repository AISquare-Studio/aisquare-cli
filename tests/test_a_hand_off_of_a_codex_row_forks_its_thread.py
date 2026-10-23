"""A hand-off from a Codex teammate forks its thread, and a take-over resumes it.

SCOPE #113 0.8.2, A7. Main's hand-off forked by Claude Code's ``--resume <transcript>
--fork-session`` under a ``--session-id`` it minted, and the merge started every agent
without ``assigns_session_id`` fresh. The fork is the agent's own (``fork_args``): Codex's
``codex fork <id>``, measured on 0.162.1 (a new rollout, a new thread id, carrying the
source's turns), whose new id its first hook brings. A Codex thread is a rollout under its
``CODEX_HOME``, so a fork onto another account's home starts fresh with a hand-off prompt,
and so does a take-over there; on its own home a take-over resumes ``codex resume <id>``.

On the fleet's fake tmux, with Codex's hooks as 0.162.1 sends them
(``tests/test_codex_fleet_parity.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.core import claude_accounts as accounts_core
from aisquare.models import ProjectInfo, TeamTask
from aisquare.services import fleet as fleet_service
from tests import test_codex_account_switch as switch_suite
from tests import test_codex_fleet_parity as codex_suite
from tests import test_fleet_service as fleet_suite
from tests.test_codex_fleet_parity import (
    THREAD_ONE,
    Codex,
    _launched_argv,
    _native,
    _rollout,
)
from tests.test_fleet_service import FakeTmux, _command, _events

# The suites' fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project
codex_on_path = switch_suite.codex_on_path
codex_agent = codex_suite.codex_agent


def _at(native: list[str], *words: str) -> bool:
    return any(native[at : at + len(words)] == list(words) for at in range(len(native)))


def _started(codex: Codex) -> None:
    """Its thread on disk and bound by its first hook, as a running Codex has it."""
    _rollout(codex, THREAD_ONE)
    codex.start(THREAD_ONE)


def test_a_fork_of_a_codex_row_is_codexs_own_fork_of_its_thread(
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``codex … fork <id>``: no Claude Code flag, no id minted (its first hook names the new
    thread), the receipt saying how it forked, and the window's launch putting every option
    before ``fork <id>`` and the fork's one line after Codex's own ``--``."""
    codex, _ = codex_agent
    _started(codex)

    receipt = fleet_service.hand_off(codex.project, codex.agent.label)

    assert (receipt.mode, receipt.resumed) == ("fork", True)
    fork = receipt.started.agent
    assert fork.agent == "codex" and fork.session_id is None, "its id binds at its first hook"
    native = _native(_command(tmux))
    assert _at(native, "fork", THREAD_ONE) and "resume" not in native
    assert not {"--resume", "--fork-session", "--session-id"} & set(native)
    assert f"forked as Codex forks a session: `fork {THREAD_ONE}`" in receipt.notes
    assert _events(codex.project, "forked") == [
        f"{fork.label} forked from {codex.agent.label} — resumed its session as a fork"
    ]
    argv = _launched_argv(_command(tmux), runner, monkeypatch)
    at = argv.index("fork")
    assert argv[at : at + 2] == ["fork", THREAD_ONE] and argv[-2] == "--"
    assert "--approve-for-me" in argv[:at], "its options before the subcommand"


def test_a_fork_of_a_codex_row_onto_another_account_starts_fresh_and_says_so(
    codex_agent: tuple[Codex, TeamTask], tmux: FakeTmux
) -> None:
    """Its thread lives under its own ``CODEX_HOME``: a fork onto another account's home
    could not open it, so the new agent starts fresh with a hand-off prompt there."""
    codex, _ = codex_agent
    _started(codex)
    other = accounts_core.create_account("codex")

    receipt = fleet_service.hand_off(codex.project, codex.agent.label, account=str(other.slot))

    assert receipt.resumed is False
    assert any(
        "cannot fork a thread under another account's home" in note
        and "starts fresh with a hand-off prompt" in note
        for note in receipt.notes
    ), receipt.notes
    native = _native(_command(tmux))
    assert "fork" not in native and "resume" not in native
    assert receipt.started.agent.account_slot == other.slot


def test_a_take_over_of_a_codex_row_resumes_its_thread_through_its_resume(
    codex_agent: tuple[Codex, TeamTask], tmux: FakeTmux
) -> None:
    codex, _ = codex_agent
    _started(codex)

    receipt = fleet_service.hand_off(codex.project, codex.agent.label, mode="take_over")

    assert (receipt.mode, receipt.resumed) == ("take_over", True)
    native = _native(_command(tmux))
    assert _at(native, "resume", THREAD_ONE) and "fork" not in native
    assert receipt.started.agent.session_id == codex.board_id(THREAD_ONE), "the same thread"


def test_a_take_over_of_a_codex_row_onto_another_account_starts_fresh_and_says_so(
    codex_agent: tuple[Codex, TeamTask], tmux: FakeTmux
) -> None:
    codex, _ = codex_agent
    _started(codex)
    other = accounts_core.create_account("codex")

    receipt = fleet_service.hand_off(
        codex.project, codex.agent.label, mode="take_over", account=str(other.slot)
    )

    assert receipt.resumed is False
    assert any(
        "cannot resume a thread under another account's home" in note for note in receipt.notes
    ), receipt.notes
    assert "resume" not in _native(_command(tmux))


def test_a_fork_of_a_codex_row_with_no_thread_recorded_starts_fresh_and_says_why(
    codex_agent: tuple[Codex, TeamTask], tmux: FakeTmux, project: ProjectInfo, tmp_path: Path
) -> None:
    """Before its first prompt a Codex row has no thread to fork (its id binds at its start)."""
    codex, _ = codex_agent

    receipt = fleet_service.hand_off(codex.project, codex.agent.label)

    assert receipt.resumed is False
    assert any("no session is recorded for it" in note for note in receipt.notes), receipt.notes
    assert "fork" not in _native(_command(tmux))
