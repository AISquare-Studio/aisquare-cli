"""A Claude Code ``fleet spawn --worktree`` runs the repository's ``post-checkout``, as 0.9.2's
plain ``git -C`` did (review r40 of #113, finding 6; the owner's ruling in #96).

Every other git aisquare runs points ``core.hooksPath`` at nothing (``core.workspace.GIT_OWN``):
a hook that lives in the tree may be a sandboxed agent's to write, and aisquare runs git outside
its sandbox (review r11 of #113, finding 4). For a spawn's worktree that guard is only where it
is needed: a hooks folder inside the work tree (husky's, a ``.githooks``:
``core.workspace.hooks_in_tree``) in a project where an agent that sandboxes its tools spawns or
has run (a session, or a fleet row with none yet). ``.git/hooks`` runs always, as no sandbox
writes ``.git``. Where the guard skips the hooks, the spawn says so in one line, naming why.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aisquare.core import claude_accounts as core
from aisquare.core import workspace
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo, TeamSession
from aisquare.services import fleet as fleet_service
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_codex_accounts import _sign_in
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    claude_on_path,
    clock,
    project,
    repo,
    tmux,
)

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    # Git for Windows' sh reads the marker's backslashed path as escapes, as
    # test_fleet_service's own hook test skips it.
    reason="a POSIX hook script",
)


def _post_checkout(work: ProjectInfo, tmp_path: Path, *, in_tree: bool = True) -> Path:
    """A ``post-checkout`` that leaves a mark: in husky's layout (``core.hooksPath`` into the
    tree), or git's own ``.git/hooks`` (``in_tree=False``)."""
    hooks = work.root / ".husky" if in_tree else work.root / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    marker = tmp_path / "post-checkout-ran"
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    if in_tree:
        subprocess.run(
            ["git", "-C", str(work.root), "config", "core.hooksPath", ".husky"], check=True
        )
    return marker


def _codex_ran_here(work: ProjectInfo, *, session: bool = True) -> None:
    """A Codex agent that ran in ``work``: its session, or (``session=False``) only its fleet
    row, spawned and never registered."""
    now = datetime.now(tz=UTC)
    with store_session() as store:
        if session:
            store.upsert_session(
                TeamSession(
                    id="codex-ran-here",
                    project_id=work.id,
                    role="coder",
                    started_at=now,
                    last_seen_at=now,
                    agent="codex",
                )
            )
        else:
            store.upsert_fleet_agent(
                FleetAgent(
                    id="agt_codex_ran_here",
                    project_id=work.id,
                    label="coder-codex",
                    role="coder",
                    agent="codex",
                    binary="codex",
                    pane_id="%77",
                    cwd=work.root,
                    created_at=now,
                )
            )


SKIPPED = "made its worktree without the repository's hooks: core.hooksPath (.husky) is inside"


def test_a_claude_code_spawns_worktree_runs_the_post_checkout(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    marker = _post_checkout(project, tmp_path)

    receipt = fleet_service.spawn(project, "coder", worktree=True)
    spawned = receipt.agent

    assert spawned.worktree is not None and spawned.cwd != project.root
    assert marker.exists(), "0.9.2 ran the repository's post-checkout for the worktree"
    assert not [note for note in receipt.notes if "without the repository's hooks" in note]


def test_a_codex_spawns_worktree_runs_none(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    marker = _post_checkout(project, tmp_path)
    slot = core.create_account("codex")
    _sign_in(slot.config_dir, "x@example.com")

    receipt = fleet_service.spawn(
        project, "coder", agent="codex", worktree=True, account=str(slot.slot)
    )

    assert receipt.agent.worktree is not None
    assert not marker.exists(), "a sandboxed agent may have written the tree's hooks"
    [said] = [note for note in receipt.notes if note.startswith(SKIPPED)]
    assert "Codex sandboxes its tools and could have written them" in said, said


@pytest.mark.parametrize("session", [True, False], ids=["its-session", "its-row-alone"])
def test_a_claude_code_spawn_where_a_codex_agent_ran_runs_none_and_says_why(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    session: bool,
) -> None:
    """A Codex agent ran here, registered or only spawned (11240's surviving mutant counted
    sessions alone): the tree may hold a hook it wrote."""
    marker = _post_checkout(project, tmp_path)
    _codex_ran_here(project, session=session)

    receipt = fleet_service.spawn(project, "coder", worktree=True)

    assert receipt.agent.worktree is not None
    assert not marker.exists(), "the tree may hold a hook the Codex agent wrote"
    [said] = [note for note in receipt.notes if note.startswith(SKIPPED)]
    assert "a Codex agent has run in this project and could have written them" in said, said


@pytest.mark.parametrize("agent", ["claude-code", "codex"])
def test_git_s_own_hooks_folder_runs_wherever_codex_ran_or_spawns(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    agent: str,
) -> None:
    """``.git/hooks`` is no sandbox's to write (Codex's grants ``.git`` read-only): it runs for
    a Claude Code spawn where a Codex agent ran, and for a Codex spawn, and nothing is said."""
    marker = _post_checkout(project, tmp_path, in_tree=False)
    _codex_ran_here(project)
    account: str | None = None
    if agent == "codex":
        slot = core.create_account("codex")
        _sign_in(slot.config_dir, "x@example.com")
        account = str(slot.slot)

    receipt = fleet_service.spawn(project, "coder", agent=agent, worktree=True, account=account)

    assert receipt.agent.worktree is not None
    assert marker.exists(), "git's own .git/hooks ran"
    assert not [note for note in receipt.notes if "without the repository's hooks" in note]


def test_hooks_in_tree_names_a_folder_in_the_tree_alone(tmp_path: Path) -> None:
    tree = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(tree)], check=True)

    def hooks_path(value: str | None) -> str | None:
        if value is None:
            subprocess.run(["git", "-C", str(tree), "config", "--unset-all", "core.hooksPath"])
        else:
            subprocess.run(["git", "-C", str(tree), "config", "core.hooksPath", value], check=True)
        return workspace.hooks_in_tree(tree)

    assert hooks_path(None) is None, "none set: .git/hooks"
    assert hooks_path(".husky/_") == ".husky/_"
    assert hooks_path(".githooks") == ".githooks"
    assert hooks_path(str(tree / "tools" / "hooks")) == str(tree / "tools" / "hooks")
    assert hooks_path(".git/hooks") is None
    assert hooks_path(str(tmp_path / "elsewhere")) is None
