"""A Claude Code ``fleet spawn --worktree`` runs the repository's hooks, as 0.9.2's plain
``git -C`` did, in a project where no agent that sandboxes its tools has run (review r40 of
#113, finding 6); where one has, none run, and the spawn says why (the owner's ruling, #97).

Every other git aisquare runs points ``core.hooksPath`` at nothing (``core.workspace.GIT_OWN``):
any hook may be one a sandboxed agent changed, and aisquare runs git outside its sandbox (review
r11 of #113, finding 4). #96 guarded only a hooks folder inside the tree; its verification found
``.git/hooks`` entries that lead into the tree (a linked script, a linked folder) and stubs that
run a config in it (husky v4, lefthook, pre-commit), which no path check catches (seq 11250, F1).
So where such an agent spawns or has run (a session, or a fleet row with none yet), no hook of
the repository's runs, and the spawn says one line naming why, unless the repository has none.
Both questions fail closed: a store that cannot say is a yes, a git that cannot say has hooks.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aisquare.core import claude_accounts as core
from aisquare.core import workspace
from aisquare.core.agent_adapters import get_adapter
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

posix = pytest.mark.skipif(
    os.name == "nt",
    # Git for Windows' sh reads the marker's backslashed path as escapes, as
    # test_fleet_service's own hook test skips it.
    reason="a POSIX hook script",
)

KINDS = ["git-hooks", "linked-script", "linked-folder", "husky"]
"""The repository's ``post-checkout``: git's own ``.git/hooks``; a link from there to a script in
the tree, and the whole folder linked into the tree (seq 11250's F1); husky's in-tree
``core.hooksPath``."""

SKIPPED = "made its worktree without the repository's hooks: "


def _post_checkout(work: ProjectInfo, tmp_path: Path, kind: str) -> Path:
    """A ``post-checkout`` of ``kind`` (:data:`KINDS`) that leaves a mark when it runs."""
    root = work.root
    marker = tmp_path / "post-checkout-ran"
    if kind == "git-hooks":
        script = root / ".git" / "hooks" / "post-checkout"
    elif kind == "linked-script":
        script = root / "scripts" / "post-checkout"
        (root / ".git" / "hooks").mkdir(exist_ok=True)
        (root / ".git" / "hooks" / "post-checkout").symlink_to("../../scripts/post-checkout")
    elif kind == "linked-folder":
        script = root / ".githooks" / "post-checkout"
        hooks = root / ".git" / "hooks"
        if hooks.exists():
            for sample in hooks.iterdir():
                sample.unlink()
            hooks.rmdir()
        hooks.symlink_to("../.githooks")
    else:
        script = root / ".husky" / "post-checkout"
        subprocess.run(["git", "-C", str(root), "config", "core.hooksPath", ".husky"], check=True)
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    script.chmod(0o755)
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


def _said(receipt: fleet_service.SpawnReceipt) -> list[str]:
    return [note for note in receipt.notes if note.startswith(SKIPPED)]


@posix
@pytest.mark.parametrize("kind", KINDS)
def test_a_claude_only_project_runs_every_hook_as_0_9_2_did(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    kind: str,
) -> None:
    marker = _post_checkout(project, tmp_path, kind)

    receipt = fleet_service.spawn(project, "coder", worktree=True)

    assert receipt.agent.worktree is not None and receipt.agent.cwd != project.root
    assert marker.exists(), "0.9.2 ran the repository's post-checkout for the worktree"
    assert _said(receipt) == []


@posix
@pytest.mark.parametrize("session", [True, False], ids=["its-session", "its-row-alone"])
@pytest.mark.parametrize("kind", KINDS)
def test_where_a_codex_agent_ran_no_hook_runs_and_the_spawn_says_why(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    kind: str,
    session: bool,
) -> None:
    """Any hook may be one the Codex agent changed: a link from ``.git/hooks`` into the tree
    too (seq 11250's F1, which #96 ran). Registered or only spawned (#96's pin gap 6)."""
    marker = _post_checkout(project, tmp_path, kind)
    _codex_ran_here(project, session=session)

    receipt = fleet_service.spawn(project, "coder", worktree=True)

    assert receipt.agent.worktree is not None
    assert not marker.exists(), "the hook may be one the Codex agent changed"
    [said] = _said(receipt)
    assert said == (
        SKIPPED + "a Codex agent has run in this project, and a hook such an agent could have "
        "changed would run outside its sandbox (a project where none has run runs them)"
    )


@posix
@pytest.mark.parametrize("kind", ["git-hooks", "linked-script"])
def test_a_codex_spawn_runs_no_hook_and_says_why(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    kind: str,
) -> None:
    marker = _post_checkout(project, tmp_path, kind)
    slot = core.create_account("codex")
    _sign_in(slot.config_dir, "x@example.com")

    receipt = fleet_service.spawn(
        project, "coder", agent="codex", worktree=True, account=str(slot.slot)
    )

    assert receipt.agent.worktree is not None
    assert not marker.exists(), "a sandboxed agent may have changed the hook"
    [said] = _said(receipt)
    assert said.startswith(SKIPPED + "Codex sandboxes its tools, "), said


def test_a_repository_with_no_hooks_is_told_nothing(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """Guarded all the same, but with no hook to skip there is nothing to say: git's own
    ``*.sample`` files are no hooks."""
    _codex_ran_here(project)
    assert not workspace.repository_has_hooks(project.root)

    receipt = fleet_service.spawn(project, "coder", worktree=True)

    assert receipt.agent.worktree is not None and _said(receipt) == []


# --- failing closed (#97, seq 11250's F2) ------------------------------------------------------


def test_a_store_that_cannot_say_whether_a_sandboxed_agent_ran_is_a_yes(
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreadable() -> Iterator[None]:
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(fleet_service, "store_session", unreadable)

    said = fleet_service._repository_hooks_run(project, get_adapter("claude-code"))

    assert said is not None and "the store could not say whether" in said


def test_a_git_that_cannot_say_has_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tree = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    assert not workspace.repository_has_hooks(tree), "control: git's samples alone"

    (tree / ".git" / "config").write_text("[core\n\tbroken = \n", encoding="utf-8")
    assert workspace.repository_has_hooks(tree), "a config git cannot read"

    def no_git(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("git is gone")

    monkeypatch.setattr("aisquare.core.workspace.subprocess.run", no_git)
    assert workspace.repository_has_hooks(tree), "no git to ask"


@posix
def test_repository_has_hooks_reads_every_place_a_hook_can_be(tmp_path: Path) -> None:
    tree = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    hooks = tree / ".git" / "hooks"
    assert not workspace.repository_has_hooks(tree), "git's own *.sample files only"
    (hooks / "post-checkout").write_text("#!/bin/sh\n", encoding="utf-8")
    assert workspace.repository_has_hooks(tree)
    (hooks / "post-checkout").unlink()
    for sample in hooks.iterdir():
        sample.unlink()
    hooks.rmdir()
    (tree / ".githooks").mkdir()
    (tree / ".githooks" / "post-merge").write_text("#!/bin/sh\n", encoding="utf-8")
    hooks.symlink_to("../.githooks")
    assert workspace.repository_has_hooks(tree), "a linked folder's"
    hooks.unlink()
    assert not workspace.repository_has_hooks(tree), "no folder at all"
    subprocess.run(["git", "-C", str(tree), "config", "core.hooksPath", ".husky/_"], check=True)
    assert workspace.repository_has_hooks(tree), "a hooks folder of the repository's"
