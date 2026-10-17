"""A stored project root that became a symbolic link loop is an ``OSError`` to every reader.

``Path.resolve`` raises ``RuntimeError`` ("Symlink loop from ...") for a loop on Python 3.11
and 3.12, and returns the path from 3.13. Round 39 caught it around ``restart_board``'s
checkout alone, and the other readers of a stored root, which catch ``OSError`` for one that
cannot be asked, let it through: a phone write that reaches a board, a stale Reply, a prune
(review of #259, round 40, 3/6). ``find_project_root`` and ``worktree_principal`` raise it as
``OSError`` (``ELOOP``) where they resolve. From 3.13 these tests make ``resolve`` raise for
the loop as 3.12's does.
"""

from __future__ import annotations

import errno
import sys
from pathlib import Path

import pytest

from aisquare.core import orchestrator
from aisquare.core.store import store_session
from aisquare.core.workspace import find_project_root, project_id_for, worktree_principal
from aisquare.models import ProjectInfo
from aisquare.services import project as project_service
from aisquare.services import remote_server


def _loop(at: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``at`` made a link to its sibling, which links back: ``a -> b -> a``. From 3.13
    ``resolve`` raises for a path through it, as 3.11's and 3.12's do."""
    other = at.with_name(at.name + "-other")
    try:
        at.symlink_to(other)
        other.symlink_to(at)
    except OSError:
        pytest.skip("this platform cannot make the symlink")
    if sys.version_info >= (3, 13):
        resolve = Path.resolve
        looped = (str(at), str(other))

        def resolve_as_3_12(self: Path, strict: bool = False) -> Path:
            if str(self).startswith(looped):
                raise RuntimeError(f"Symlink loop from {str(self)!r}")
            return resolve(self, strict)

        monkeypatch.setattr(Path, "resolve", resolve_as_3_12)
    return at


def _registered(root: Path) -> ProjectInfo:
    """A project onboarded at ``root`` as it was before its root became a loop."""
    root.mkdir()
    with store_session() as store:
        return store.onboard_project(ProjectInfo(id=project_id_for(root), root=root))


def _became_a_loop(project: ProjectInfo, monkeypatch: pytest.MonkeyPatch) -> None:
    project.root.rmdir()
    _loop(project.root, monkeypatch)


def test_a_root_through_a_symlink_loop_is_an_oserror_where_it_is_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = _loop(tmp_path / "loop", monkeypatch)

    for resolving in (find_project_root, orchestrator.directory_project):
        with pytest.raises(OSError) as raised:
            resolving(loop)
        assert raised.value.errno == errno.ELOOP, resolving


def test_a_project_root_that_became_a_symlink_loop_adds_no_board_and_fails_no_reach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A board write that names a task or an ``as`` works out the boards a phone may reach,
    and with them the one ``asq board`` shows in each project's root, under
    ``suppress(OSError)``: one root that became a loop turned every such write into a 500."""
    monkeypatch.delenv("AISQUARE_TEAM_HUB", raising=False)
    looped = _registered(tmp_path / "looped")
    fine = _registered(tmp_path / "fine")
    _became_a_loop(looped, monkeypatch)

    reach = remote_server._remote_boards()

    assert {looped.id, fine.id} <= reach, reach


def test_a_stale_reply_to_a_project_whose_root_became_a_symlink_loop_names_no_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 409 of a Reply to a card gone from the feed names the board a note goes to; with
    no agent running, the one ``asq note`` in the project's root resolves. A root that
    became a loop is one that cannot be looked at, and names none, as ``EACCES`` does: it
    turned the 409 into a 500."""
    monkeypatch.delenv("AISQUARE_TEAM_HUB", raising=False)
    looped = _registered(tmp_path / "looped")
    _became_a_loop(looped, monkeypatch)

    assert remote_server._remote_note_board_name(looped, "coder-1") is None


def test_a_worktree_whose_way_to_its_principal_loops_fails_no_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``prune`` asks each root whether it is a worktree of a registered project, and its
    ``.git`` file names the principal: one whose way there loops ended the plan in a
    traceback. It names no principal, as a ``.git`` file that cannot be read does."""
    worktree = _registered(tmp_path / "wt")
    loop = _loop(tmp_path / "repo", monkeypatch)
    (worktree.root / ".git").write_text(f"gitdir: {loop / '.git' / 'worktrees' / 'wt'}\n")

    assert worktree_principal(worktree.root) is None
    plan = project_service.prune_candidates(missing=True, worktrees=True)

    assert worktree.id not in {candidate.project.id for candidate in plan}
