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
from typing import Literal

import pytest
from typer.testing import CliRunner

from aisquare.core import claude_accounts as accounts_core
from aisquare.models import ProjectInfo, TeamTask
from aisquare.services import claude_accounts as service
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
    assert f"forked as Codex forks a session: `fork {THREAD_ONE} -C .`" in receipt.notes
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


def test_a_fork_of_a_codex_row_on_no_account_forks_its_thread_past_a_managed_default(
    codex_agent: tuple[Codex, TeamTask], tmux: FakeTmux
) -> None:
    """r20 of #113, F1's sibling: a source whose record has no slot ran under the plain home.
    Forked with no account asked, it asked the ladder, whose managed Codex default moved the
    fork off the home its thread is in, and it started fresh ("cannot fork a thread under
    another account's home"). It asks for the plain slot by name now, as its restart does."""
    codex, _ = codex_agent
    _started(codex)
    assert codex.agent.account_slot is None
    managed = accounts_core.create_account("codex")
    service.set_default(str(managed.slot))

    receipt = fleet_service.hand_off(codex.project, codex.agent.label)

    assert (receipt.mode, receipt.resumed) == ("fork", True), receipt.notes
    assert _at(_native(_command(tmux)), "fork", THREAD_ONE)
    plain = accounts_core.default_account(agent="codex").slot
    assert receipt.started.agent.account_slot == plain


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


def test_a_take_over_of_a_slot_run_codex_row_onto_this_shells_home_starts_fresh(
    tmux: FakeTmux,
    codex_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """*(this shell's)* with no account on the ladder runs under the plain ``CODEX_HOME``,
    where a thread its slot ran has no rollout: ``codex resume <id>`` there exited the
    window (review r17 of #113, finding 2). It starts fresh with a hand-off prompt, as
    ``_forkable`` already does."""
    slot = accounts_core.create_account("codex")
    agent = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, account=str(slot.slot)
    ).agent
    plain = tmp_path / "plain-codex-home"
    plain.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(plain))
    codex = Codex(agent, project, slot.config_dir, tmux, monkeypatch)
    _started(codex)

    receipt = fleet_service.hand_off(
        project, agent.label, mode="take_over", account=fleet_service.THIS_SHELL_ACCOUNT
    )

    assert receipt.started.agent.account_slot is None, "never the row's own slot"
    assert receipt.resumed is False
    assert any(
        "cannot resume a thread under another account's home" in note for note in receipt.notes
    ), receipt.notes
    assert "resume" not in _native(_command(tmux))


@pytest.mark.parametrize("mode", ["take_over", "fork"])
def test_a_hand_off_of_a_plain_codex_row_onto_this_shells_home_keeps_its_thread(
    mode: Literal["fork", "take_over"],
    tmux: FakeTmux,
    codex_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row spawned on the plain codex (slot 0, ``~/.codex``) handed off onto *(this
    shell's)* runs under that same home, so its thread is there: a take-over resumes it and
    a fork forks it. Whether the home moves is asked of the homes, as their config dirs,
    never of whether the row had a slot (review r18 of #113, finding 7)."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    plain = accounts_core.default_account(agent="codex")
    assert plain.slot == 0
    plain.config_dir.mkdir(parents=True, exist_ok=True)
    agent = fleet_service.spawn(project, "coder", agent="codex", worktree=False, account="0").agent
    assert agent.account_slot == 0
    codex = Codex(agent, project, plain.config_dir, tmux, monkeypatch)
    _started(codex)

    receipt = fleet_service.hand_off(
        project, agent.label, mode=mode, account=fleet_service.THIS_SHELL_ACCOUNT
    )

    assert receipt.resumed is True, receipt.notes
    assert not any("another account's home" in note for note in receipt.notes), receipt.notes
    native = _native(_command(tmux))
    assert _at(native, "resume" if mode == "take_over" else "fork", THREAD_ONE), native
