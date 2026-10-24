"""A hand-off Fork of a Codex teammate comes up in its own worktree, and a Codex at a dialog
of its own before its first hook reads as one in ``fleet ls``, never ``waiting``.

SMOKE8 of #113, check 12: the Fork ran ``codex … fork <id> -- "You are a fork …"`` in the
fork's worktree, and Codex, started outside the source thread's last cwd, opened ``Working
directory · fork``, whose highlight and Esc both run the fork in the SOURCE's tree. No hook
fires there: ``fleet ls`` read it ``waiting … no hooks`` and the fork line was never sent.
Measured on 0.162.1 and 0.164.0, ``-C`` opens no chooser (``CodexAdapter.fork_args``). The
same listing read a Codex at its folder-trust question ``waiting`` too, where a Claude Code
at its own reads ``attention`` with how to unblock it (observed in SMOKE8): the two read
the same now, through the adapter's ``dialog_on_screen``.

On the fleet's fake tmux, with Codex's hooks as 0.162.1 sends them
(``tests/test_codex_fleet_parity.py``), over the screens measured
(``tests/fixtures/agent_screens/codex.json``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.models import ProjectInfo, TeamTask
from aisquare.services import fleet as fleet_service
from tests import test_codex_account_switch as switch_suite
from tests import test_codex_fleet_parity as codex_suite
from tests import test_fleet_service as fleet_suite
from tests.test_codex_fleet_parity import IDLE_PROMPT, THREAD_ONE, Codex, _launched_argv, _rollout
from tests.test_fleet_service import TRUST_SCREEN, FakeTmux, _coder
from tests.test_remote_needs_codex import SCREENS

# The suites' fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project
codex_on_path = switch_suite.codex_on_path
codex_agent = codex_suite.codex_agent

CHOOSER = SCREENS["Working directory · fork, a bare codex fork run outside the source thread's cwd"]
CODEX_TRUST = SCREENS["folder trust, asked in a git checkout no trust covers"]
REVIEW = SCREENS["the hook review"]


def test_a_fork_into_its_own_worktree_starts_codex_in_that_tree_not_at_its_chooser(
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Fork's window opens in the fork's own worktree, and its Codex is told that tree is
    its directory (``-C .``, the window's own), so it opens no ``Working directory · fork``
    and the fork line reaches its first turn. A bare ``fork <id>`` there waited at the
    chooser, whose default ran it in the source's checkout."""
    codex, _ = codex_agent
    _rollout(codex, THREAD_ONE)
    codex.start(THREAD_ONE)

    receipt = fleet_service.hand_off(codex.project, codex.agent.label)

    assert (receipt.mode, receipt.resumed) == ("fork", True)
    window = tmux.spawned[-1]
    tree = Path(str(window["cwd"]))
    assert os.path.realpath(tree) != os.path.realpath(codex.agent.cwd), "its own worktree"
    assert tree == receipt.started.agent.cwd
    argv = _launched_argv(list(window["command"]), runner, monkeypatch)  # type: ignore[call-overload]
    at = argv.index("fork")
    assert argv[at : at + 4] == ["fork", THREAD_ONE, "-C", "."], argv
    assert (tree / argv[at + 3]).resolve() == tree.resolve(), "the window's own directory"
    assert argv[-2] == "--" and argv[-1].startswith(f"You are a fork of {codex.agent.label}")
    assert f"forked as Codex forks a session: `fork {THREAD_ONE} -C .`" in receipt.notes


def test_a_codex_at_its_folder_trust_reads_as_a_claude_code_at_its_own(
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
) -> None:
    """Both before any hook, in the same folder: the Claude Code at its question read
    ``attention``, ``waiting for folder trust — approve it once, or `aisquare accounts trust
    <dir>``` (a command that trusts it on Codex's accounts too); the Codex read ``waiting``
    with ``no hooks``, and nothing said it needed the human. They read the same now."""
    codex, _ = codex_agent
    claude = _coder(project)
    tmux.set_command(claude.pane_id, "claude")
    tmux.screens[claude.pane_id] = TRUST_SCREEN
    tmux.screens[codex.agent.pane_id] = CODEX_TRUST

    states = {s.agent.id: (s.state, s.detail) for s in fleet_service.list_agents(project)}

    assert claude.cwd == codex.agent.cwd, "the premise: one folder"
    assert states[codex.agent.id] == states[claude.id]
    assert states[claude.id] == ("attention", fleet_service.trust_prompt_detail(claude.cwd))
    assert fleet_service.status_of(codex.agent).detail == states[claude.id][1]


@pytest.mark.parametrize(
    ("screen", "words"),
    [
        (
            CHOOSER,
            "Working directory · fork Session = latest cwd recorded in the forked session "
            "Current = your current working directory",
        ),
        (
            REVIEW,
            "Hooks need review 8 hooks are new or changed. "
            "Hooks can run outside the sandbox after you trust them.",
        ),
    ],
    ids=["fork-chooser", "hook-review"],
)
def test_a_codex_at_another_dialog_before_its_first_hook_reads_attention_naming_it(
    codex_agent: tuple[Codex, TeamTask], tmux: FakeTmux, screen: list[str], words: str
) -> None:
    """Its fork chooser, should one ever show, or its hook review: ``attention`` with the
    dialog's words, never a healthy ``waiting`` that ``fleet tell`` and the manager read as
    a prompt to type at. Its empty prompt is ``waiting``, as before."""
    codex, _ = codex_agent
    tmux.screens[codex.agent.pane_id] = screen

    (status,) = fleet_service.list_agents(codex.project)

    assert (status.state, status.detail) == ("attention", f"{fleet_service.AT_A_DIALOG}: {words}")
    tmux.screens[codex.agent.pane_id] = IDLE_PROMPT
    assert fleet_service.list_agents(codex.project)[0].state == "waiting"


def test_a_codex_whose_hooks_have_run_has_its_screen_read_for_no_such_dialog(
    codex_agent: tuple[Codex, TeamTask], tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its hooks run once it is past its start dialogs: a row whose session has spoken since
    it began is not captured on every listing, as a Claude Code's is not."""
    codex, _ = codex_agent
    _rollout(codex, THREAD_ONE)
    codex.start(THREAD_ONE)
    tmux.screens[codex.agent.pane_id] = CHOOSER  # what a stale screen could still hold
    read: list[str] = []
    screen_text = tmux.screen_text

    def counted(pane_id: str) -> list[str]:
        read.append(pane_id)
        return screen_text(pane_id)

    monkeypatch.setattr(tmux, "screen_text", counted)

    (status,) = fleet_service.list_agents(codex.project)

    assert status.state != "attention" and read == []
