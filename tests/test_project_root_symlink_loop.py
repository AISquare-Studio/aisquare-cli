"""A stored project root that became a symbolic link loop is an ``OSError`` to every reader.

``Path.resolve`` raises ``RuntimeError`` ("Symlink loop from ...") for a loop on Python 3.11
and 3.12, and returns the path from 3.13. Round 39 caught it around ``restart_board``'s
checkout alone, and the other readers of a stored root, which catch ``OSError`` for one that
cannot be asked, let it through: a phone write that reaches a board, a stale Reply, a prune
(review of #259, round 40, 3/6). ``find_project_root`` and ``worktree_principal`` raise it as
``OSError`` (``ELOOP``) where they resolve, on every Python: a real loop, ``resolve`` as the
running interpreter has it. Made to raise as 3.12's from 3.13, these tests never saw 3.13's
``resolve`` hand the loop back, and ``find_project_root`` walk from it to an ancestor's board
(round 41, 1/6). Each looped root is below a directory with a marker, the board that walk
lands on.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import orchestrator
from aisquare.core.store import store_session
from aisquare.core.workspace import find_project_root, project_id_for, worktree_principal
from aisquare.models import ProjectInfo
from aisquare.services import project as project_service
from aisquare.services import remote_server


def _loop(at: Path) -> Path:
    """``at`` made a link to its sibling, which links back: ``a -> b -> a``."""
    other = at.with_name(at.name + "-other")
    try:
        at.symlink_to(other)
        other.symlink_to(at)
    except OSError:
        pytest.skip("this platform cannot make the symlink")
    return at


def _marked(at: Path) -> Path:
    """A directory with a ``.git`` marker, where a walk up from a loop below it lands."""
    (at / ".git").mkdir(parents=True)
    return at


def _registered(root: Path) -> ProjectInfo:
    """A project onboarded at ``root`` as it was before its root became a loop."""
    root.mkdir()
    with store_session() as store:
        return store.onboard_project(ProjectInfo(id=project_id_for(root), root=root))


def _became_a_loop(project: ProjectInfo) -> None:
    project.root.rmdir()
    _loop(project.root)


def test_a_root_through_a_symlink_loop_is_an_oserror_where_it_is_resolved(
    tmp_path: Path,
) -> None:
    repo = _marked(tmp_path / "repo")
    loop = _loop(repo / "loop")

    for resolving in (find_project_root, orchestrator.directory_project):
        with pytest.raises(OSError) as raised:
            resolving(loop)
        assert raised.value.errno == errno.ELOOP, resolving
        with pytest.raises(OSError) as raised:
            resolving(loop / "below")
        assert raised.value.errno == errno.ELOOP, resolving


def test_a_missing_root_is_no_loop(tmp_path: Path) -> None:
    """A root that is gone still resolves, to itself: ``directory_project`` is asked of
    deleted roots. Below a marked directory, as the loops here are: with none put there, it
    held only where no ancestor of the temp dir had one, and failed under a dotfiles
    ``~/.git`` (review of #259, round 42, 10/11)."""
    gone = _marked(tmp_path / "repo") / "gone"

    assert find_project_root(gone) == gone.resolve()
    assert orchestrator.directory_project(gone).root == gone.resolve()


def test_a_hub_that_became_a_symlink_loop_names_its_board_as_spelled_on_every_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 42, 6/11: the hub was resolved raw, the one root that was. A hub
    that became a loop raised ``RuntimeError`` on 3.11 and 3.12, which the readers that skip
    a root they cannot ask do not catch: a phone board write works out its reach through
    the hub (``_remote_boards``), and each was a 500. From 3.13 ``resolve`` handed the loop
    back. It names its board as it is spelled, made absolute, on every Python, as
    ``project.resolve`` reads a root that became a loop, and the window a spawn sets it on
    reads the same board."""
    hub = _loop(tmp_path / "hub")
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub))
    as_spelled = Path(os.path.abspath(hub))

    board = orchestrator.team_project(tmp_path)
    reach = remote_server._remote_boards()

    assert (board.root, board.id) == (as_spelled, project_id_for(as_spelled))
    assert orchestrator.window_team_hub() == str(as_spelled)
    assert board.id in reach


def test_a_project_root_that_became_a_symlink_loop_adds_no_board_and_fails_no_reach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A board write that names a task or an ``as`` works out the boards a phone may reach,
    and with them the one ``asq board`` shows in each project's root, under
    ``suppress(OSError)``: one root that became a loop turned every such write into a 500.
    Walked from to its ancestor, it added that board, a project nobody added."""
    monkeypatch.delenv("AISQUARE_TEAM_HUB", raising=False)
    repo = _marked(tmp_path / "repo")
    looped = _registered(repo / "looped")
    fine = _registered(tmp_path / "fine")
    _became_a_loop(looped)

    reach = remote_server._remote_boards()

    assert {looped.id, fine.id} <= reach, reach
    assert project_id_for(repo.resolve()) not in reach, reach


def test_a_stale_reply_to_a_project_whose_root_became_a_symlink_loop_names_no_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 409 of a Reply to a card gone from the feed names the board a note goes to; with
    no agent running, the one ``asq note`` in the project's root resolves. A root that
    became a loop is one that cannot be looked at, and names none, as ``EACCES`` does: it
    turned the 409 into a 500, and walked from, it named its ancestor's board."""
    monkeypatch.delenv("AISQUARE_TEAM_HUB", raising=False)
    looped = _registered(_marked(tmp_path / "repo") / "looped")
    _became_a_loop(looped)

    assert remote_server._remote_note_board_name(looped, "coder-1") is None


def test_forget_of_a_root_that_became_a_symlink_loop_forgets_it(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Doctor's capture-off line and ``ProjectBusyError`` print ``aisquare project forget
    <root>`` for a row. For a root that became a loop, ``project.resolve`` resolved the path
    raw, which raised ``RuntimeError`` on 3.11 and 3.12, and the forget ended in a traceback
    (review of #259, round 41, 2/6). It is looked up as it is spelled."""
    looped = _registered(tmp_path / "looped")
    _became_a_loop(looped)

    forgot = runner.invoke(app, ["--json", "project", "forget", str(looped.root)])

    assert forgot.exit_code == 0, forgot.output
    assert json.loads(forgot.stdout)["project"]["id"] == looped.id
    with store_session() as store:
        assert store.get_project(looped.id) is None


def test_a_path_through_a_symlink_loop_that_names_no_project_is_not_found(
    runner: CliRunner, tmp_path: Path
) -> None:
    """A loop no registration has for a root falls through to the name lookup, which finds
    nothing: the refusal of a ref that names no project, not a traceback."""
    loop = _loop(tmp_path / "loop")

    for ref in (loop, loop / "below"):
        refused = runner.invoke(app, ["--json", "project", "forget", str(ref)])

        assert refused.exit_code == 1, refused.output
        assert json.loads(refused.stdout) == {"error": "not_found", "ref": str(ref)}


def test_a_worktree_whose_way_to_its_principal_loops_fails_no_prune(tmp_path: Path) -> None:
    """``prune`` asks each root whether it is a worktree of a registered project, and its
    ``.git`` file names the principal: one whose way there loops ended the plan in a
    traceback. It names no principal, as a ``.git`` file that cannot be read does."""
    worktree = _registered(tmp_path / "wt")
    loop = _loop(tmp_path / "repo")
    (worktree.root / ".git").write_text(f"gitdir: {loop / '.git' / 'worktrees' / 'wt'}\n")

    assert worktree_principal(worktree.root) is None
    plan = project_service.prune_candidates(missing=True, worktrees=True)

    assert worktree.id not in {candidate.project.id for candidate in plan}
