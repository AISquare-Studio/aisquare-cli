"""A wrong board and an empty one are indistinguishable, and so is a plausible one.

Board reads resolve from cwd. A session working in a git worktree, or anywhere
outside a repo, silently reads a DIFFERENT board and nothing says so. Measured
on this machine while the team was live:

    /home/work/work/aisquare-cli   events=200  <- the team board
    a linked worktree              events=0    <- reads as "nothing happened"
    $HOME                          events=12   <- A POPULATED, WRONG BOARD

The third is the dangerous one. Empty invites suspicion; twelve plausible
events invite the conclusion "I read the board and it is not there", which is a
false negative wearing the clothes of a successful read. It cost two sessions
an hour tonight — once as a note delivered to the wrong project, once as a
documented recovery command that returned nothing for the person checking it.

WHAT THIS PINS AND WHY NOT MORE. The note fires only when the board a read
answers for is not the one the caller's own repository would give:

  * inside a repo whose team project matches the workspace project -> SILENT.
    This is every ordinary invocation, and a banner on those is how people
    learn to ignore banners.
  * in a linked worktree whose team project differs -> named.
  * outside any git repository at all -> named, because "the board follows
    your directory" is surprising precisely where there is no repository to
    anchor it.

It does NOT re-home anything and does not change which board is read. The
routing fix already exists for two commands (`log_events` threads a session
ref "exactly like attributed writes (#20)"); this is the part that tells you
which board you got when you have not used it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aisquare.services import team as team_service


def _in_repo(tmp_path: Path) -> Path:
    """A REAL git repository, because the resolver shells out to git.

    A hand-made `.git` directory is not a repository: `git_common_root` runs
    `git rev-parse --git-common-dir`, which fails on the fake and made the
    quiet case look like the outside-a-repo case. The fixture was wrong, not
    the helper — and a fixture that cannot produce the state under test is the
    premise failing silently.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    return repo


def test_an_ordinary_read_inside_its_own_repo_says_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The quiet case, asserted first — it is the one a banner would ruin."""
    monkeypatch.delenv("AISQUARE_TEAM_HUB", raising=False)
    repo = _in_repo(tmp_path)

    assert team_service.board_scope_note(repo) is None


def test_a_read_outside_any_repository_names_the_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The $HOME case: twelve plausible events from a board you did not mean."""
    monkeypatch.delenv("AISQUARE_TEAM_HUB", raising=False)
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    note = team_service.board_scope_note(plain)

    assert note is not None
    assert "not a git repository" in note, note


def test_a_hub_that_points_elsewhere_names_the_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worktree case, reproduced through the mechanism that causes it.

    `AISQUARE_TEAM_HUB` overrides board resolution, and set to a RELATIVE path
    it resolves against the process cwd — which is how every session on this
    team was running, and why a worktree read came back empty.
    """
    repo = _in_repo(tmp_path)
    elsewhere = tmp_path / "hub"
    elsewhere.mkdir()
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(elsewhere))

    note = team_service.board_scope_note(repo)

    assert note is not None
    assert "hub" in note, note


def test_the_note_names_the_board_it_answered_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "You are on another board" without saying which is a dead end.

    The whole failure is that a reader cannot tell which board they got; a
    warning that repeats that is decoration.
    """
    monkeypatch.delenv("AISQUARE_TEAM_HUB", raising=False)
    plain = tmp_path / "somewhere-else"
    plain.mkdir()

    note = team_service.board_scope_note(plain)

    assert note is not None
    assert "somewhere-else" in note or str(plain) in note, note


def test_it_never_raises_on_a_directory_that_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail open: a board read must never exit non-zero because of a diagnostic.

    The doctrine is that an observer may cost its own output and never the
    command. A note that raises would turn "which board am I on" into "your
    board read failed".
    """
    monkeypatch.delenv("AISQUARE_TEAM_HUB", raising=False)

    # `or True` was here on the first draft, which is an assertion that cannot
    # fail — the sixth face, in the file I wrote to demonstrate rigour. The
    # property is "returns a str or None without raising", so assert THAT.
    note = team_service.board_scope_note(tmp_path / "gone")

    assert note is None or isinstance(note, str)


@pytest.mark.parametrize(
    "argv",
    [
        ["team", "log", "--limit", "3"],
        ["board"],
        ["team", "status"],
        ["task", "list"],
    ],
)
def test_every_board_read_names_the_board_when_it_is_not_yours(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: object
) -> None:
    """The note has to reach EVERY read, not the one its author tested.

    I wired `team log` and `board`, checked those two end to end, and reported
    the task done — `task list` emitted nothing and the acceptance had named
    it. A parametrised list is the difference between covering a command and
    covering the one you remembered.

    (`note list` is in the acceptance too and is NOT here: `note` is a WRITE.
    `aisquare note list` posts a note whose text is "list", which is how a
    teammate accidentally filed one tonight. There is no note read to cover.)
    """
    from typer.testing import CliRunner

    from aisquare.cli.app import app

    monkeypatch.delenv("AISQUARE_TEAM_HUB", raising=False)
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    monkeypatch.chdir(outside)

    result = CliRunner().invoke(app, ["--json", *argv])

    assert "reading board" in result.stderr, f"{' '.join(argv)} said nothing: {result.stderr!r}"
