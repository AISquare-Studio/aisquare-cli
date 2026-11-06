"""A Claude Code ``fleet spawn --worktree`` runs the repository's ``post-checkout``, as 0.9.2's
plain ``git -C`` did (review r40 of #113, finding 6).

Every git aisquare runs points ``core.hooksPath`` at nothing (``core.workspace.GIT_OWN``): a
hook that lives in the tree (``.husky/``, ``.githooks/``) may be a sandboxed agent's to
write, and aisquare runs git outside its sandbox (review r11 of #113, finding 4). That guard
is the sandbox's: Claude Code sandboxes nothing, so for its spawn in a project where no agent
that sandboxes its tools has run, the worktree is made with the repository's hooks again
(husky, lefthook). A Codex spawn, or a Claude Code one in a project a Codex agent has run in,
keeps the guard.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aisquare.core import claude_accounts as core
from aisquare.core.store import store_session
from aisquare.models import ProjectInfo, TeamSession
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


def _post_checkout(work: ProjectInfo, tmp_path: Path) -> Path:
    """husky's layout: ``core.hooksPath`` into the tree, a ``post-checkout`` that leaves a mark."""
    hooks = work.root / ".husky"
    hooks.mkdir()
    marker = tmp_path / "post-checkout-ran"
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(["git", "-C", str(work.root), "config", "core.hooksPath", ".husky"], check=True)
    return marker


def test_a_claude_code_spawns_worktree_runs_the_post_checkout(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    marker = _post_checkout(project, tmp_path)

    spawned = fleet_service.spawn(project, "coder", worktree=True).agent

    assert spawned.worktree is not None and spawned.cwd != project.root
    assert marker.exists(), "0.9.2 ran the repository's post-checkout for the worktree"


def test_a_codex_spawns_worktree_runs_none(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    marker = _post_checkout(project, tmp_path)
    slot = core.create_account("codex")
    _sign_in(slot.config_dir, "x@example.com")

    spawned = fleet_service.spawn(
        project, "coder", agent="codex", worktree=True, account=str(slot.slot)
    ).agent

    assert spawned.worktree is not None
    assert not marker.exists(), "a sandboxed agent may have written the tree's hooks"


def test_a_claude_code_spawn_where_a_codex_agent_ran_runs_none(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    marker = _post_checkout(project, tmp_path)
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id="codex-ran-here",
                project_id=project.id,
                role="coder",
                started_at=now,
                last_seen_at=now,
                agent="codex",
            )
        )

    spawned = fleet_service.spawn(project, "coder", worktree=True).agent

    assert spawned.worktree is not None
    assert not marker.exists(), "the tree may hold a hook the Codex agent wrote"
