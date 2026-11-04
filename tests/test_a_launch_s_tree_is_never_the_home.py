"""A sandboxed launch's tree is never the home, a directory above it or a filesystem root,
unless the launch is in that very directory (review r39 of #113, finding 1).

With a dotfiles ``~/.git`` (or a ``~/.hg``), a launch in a folder of the home that is no
checkout (``~/scratch``) took the home for its tree: ``_tree_top`` and ``git_common_root``
both answered ``~``. So ``--detail-file ~/.codex/auth.json``, on the launch's own board or a
member's, and ``--artifact ~/.ssh/…`` counted as "in the tree", and its broker, which reads
as the user outside the sandbox, read them past a ``deny_read`` of the sandbox's.
``workspace.real_project_root`` refuses the home and ``/`` as a project; ``launch_scope``
never did. Through the real broker (``Broker.answer``): the tree is ``~/scratch`` alone, a
file of the home outside it is refused on both boards and as an artifact, and a file of
``~/scratch`` is read.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aisquare.core import launch_seal
from aisquare.core.store import store_session
from aisquare.models import LaunchSpec, TeamSession
from aisquare.services import store_broker
from aisquare.services import team as team_service
from tests.test_a_sandboxed_codex_records_evidence import _linked
from tests.test_store_broker import LAUNCH, MINE, THEIRS, _answer, _broker, _request, parented

if sys.platform == "win32":  # the broker is POSIX's (FIFOs, descriptors): nothing below runs
    pytest.skip("the broker is POSIX's", allow_module_level=True)

# Not named for what it stands for: CodeQL reads such a word as one and flags its writes.
FOREIGN = "what only the user outside the sandbox may read"
OUTSIDE = "is outside this agent's tree"


def _git_init(directory: Path) -> None:
    subprocess.run(["git", "init", "-q", str(directory)], check=True)


def _a_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, marker: str = ".git") -> Path:
    """A home holding ``marker`` (a dotfiles ``.git`` its ``git init`` makes, else a marker
    directory), with ``.codex/auth.json`` in it; ``HOME`` names it."""
    home = tmp_path / "home"
    home.mkdir()
    if marker == ".git":
        _git_init(home)
    else:
        (home / marker).mkdir()
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text(FOREIGN, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    assert Path.home() == home
    return home


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A launch in ``~/scratch``, no checkout, under a home with a dotfiles ``.git``: a board
    with two sessions, the first bound to this launch by its own hook (as
    ``test_store_broker.work`` binds one), and a member board ``api`` beside it."""
    home = _a_home(tmp_path, monkeypatch)
    scratch = home / "scratch"
    scratch.mkdir()
    monkeypatch.chdir(scratch)
    monkeypatch.setenv("AISQUARE_LAUNCH_ID", LAUNCH)
    monkeypatch.delenv("AISQUARE_FLEET_AGENT", raising=False)
    monkeypatch.delenv("AISQUARE_TEAM_HUB", raising=False)
    project = team_service.activate(scratch)
    now = datetime.now(UTC)
    with store_session() as store:
        for sid in (MINE, THEIRS):
            store.upsert_session(
                TeamSession(id=sid, project_id=project.id, started_at=now, last_seen_at=now)
            )
        store.set_meta(f"launch-session:{LAUNCH}", MINE)
    parented(scratch, "api")
    yield scratch


def _roots(cwd: Path, environ: dict[str, str] | None = None) -> tuple[Path, ...]:
    return store_broker.launch_scope(cwd, dict(os.environ) if environ is None else environ).roots


# --- the scope ----------------------------------------------------------------------------


@pytest.mark.parametrize("marker", [".git", ".hg"])
def test_a_launch_in_a_folder_of_the_home_takes_that_folder_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    """The finding's own run: ``[~/scratch, ~]`` before. A ``~/.hg`` is a project marker
    (``find_project_root``), and git ignores it."""
    home = _a_home(tmp_path, monkeypatch, marker=marker)
    (home / "scratch" / "sub").mkdir(parents=True)

    assert _roots(home / "scratch") == ((home / "scratch").resolve(),)
    assert _roots(home / "scratch" / "sub") == ((home / "scratch" / "sub").resolve(),)


def test_a_root_above_the_home_is_no_tree_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``.git`` in the directory that holds the home (``/Users``) holds every home in it."""
    above = tmp_path / "users"
    home = above / "me"
    (home / "scratch").mkdir(parents=True)
    _git_init(above)
    monkeypatch.setenv("HOME", str(home))

    assert _roots(home / "scratch") == ((home / "scratch").resolve(),)


def test_a_filesystem_root_is_no_tree_even_with_no_home_to_be_told(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/`` is above every home, so it is refused as one is; with no home to be told
    (``Path.home`` raising), still as a filesystem root, and only it."""

    def no_home() -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", staticmethod(no_home))
    assert not store_broker._project_root(Path(tmp_path.anchor))
    assert store_broker._project_root(tmp_path)


def test_a_launch_in_the_home_itself_or_in_a_checkout_of_it_keeps_its_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The home is the tree of a launch in the home, whose sandbox writes it anyway; a real
    checkout under it is the tree of a launch in a folder of that checkout."""
    home = _a_home(tmp_path, monkeypatch)
    app = home / "code" / "app"
    (app / "src").mkdir(parents=True)
    _git_init(app)

    assert _roots(home) == (home.resolve(),)
    assert _roots(app / "src") == ((app / "src").resolve(), app.resolve())


def test_a_launch_record_s_project_at_the_home_is_no_tree_of_a_worktree_in_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fleet agent spawned from ``~/scratch`` under the dotfiles ``~/.git`` has the home
    for its project's root: its launch record names it, and its worktree for its cwd. The
    worktree is its tree, the home is not; a record whose own directory is the home keeps
    it, as its own directory. Mutant: the record's project root taken unasked."""
    home = _a_home(tmp_path, monkeypatch)
    worktree = home / ".aisquare-worktrees" / "coder"
    worktree.mkdir(parents=True)
    agent = "agt-0000"
    for cwd, kept in ((worktree, False), (home, True)):
        record = launch_seal.LaunchRecord(
            id=agent,
            project_id="prj_home",
            project_root=str(home),
            role="coder",
            agent="codex",
            binary="codex",
            cwd=str(cwd),
            spec=LaunchSpec(binary="codex"),
        )
        assert launch_seal.write(record) is None
        roots = _roots(worktree, {**os.environ, "AISQUARE_FLEET_AGENT": agent})
        assert (home.resolve() in roots) is kept, roots
        assert worktree.resolve() in roots, roots


# --- through the broker ---------------------------------------------------------------------


@pytest.mark.parametrize("member", [False, True], ids=["own board", "member's board"])
def test_a_detail_file_of_the_home_outside_the_launch_s_folder_is_refused(
    scratch: Path, tmp_path: Path, member: bool
) -> None:
    broker = _broker(tmp_path)
    broker.settle()
    (scratch / "brief.md").write_text("objective: ship it", encoding="utf-8")
    try:
        assert broker.scope().roots == (scratch.resolve(),)
        board = ["--project", "api"] if member else []
        named = scratch.parent / ".codex" / "auth.json"
        argv = ["task", "add", "x", "--detail-file", str(named), *board]
        refused = _answer(broker, _request(argv, scratch, rid=f"{1:032x}"))
        said = f"{refused['stdout']}{refused['stderr']}"
        assert refused["code"] == 1 and OUTSIDE in said and FOREIGN not in said, refused

        argv = ["--json", "task", "add", "y", "--detail-file", "brief.md", *board]
        read = _answer(broker, _request(argv, scratch, rid=f"{2:032x}"))
        assert read["code"] == 0, read
        assert json.loads(str(read["stdout"]))["detail"] == "objective: ship it", read
    finally:
        broker.close()


@pytest.mark.parametrize("verb", ["evidence", "finding"])
def test_an_artifact_of_the_home_outside_the_launch_s_folder_is_refused(
    scratch: Path, tmp_path: Path, verb: str
) -> None:
    brief, task = _linked()
    broker = _broker(tmp_path)
    broker.settle()
    named = scratch.parent / ".codex" / "auth.json"
    verdict = ["--verdict", "pass"] if verb == "evidence" else []
    argv = [
        "brief", verb, brief, "R1", "--task", task, *verdict, "--summary", "looked",
        "--artifact", str(named), "--as", MINE[:8],
    ]  # fmt: skip
    try:
        refused = _answer(broker, _request(argv, scratch))
    finally:
        broker.close()
    said = f"{refused['stdout']}{refused['stderr']}"
    assert refused["code"] not in (0, None) and OUTSIDE in said, refused
    assert FOREIGN not in said, refused
