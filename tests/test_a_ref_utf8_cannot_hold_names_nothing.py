"""A ref UTF-8 cannot hold names nothing on the board, said in words (review r39 of #113,
finding 6).

A shell passes a byte that is not UTF-8 (``tsk_$'\\xff'``) as a lone surrogate, and the
store, which encodes every ref it is asked in UTF-8, raised ``UnicodeEncodeError``:
``task claim tsk_\\udcff`` and ``task claim 1 --as <ref>\\udcff`` printed a Rich traceback, as
did every task verb, ``team show`` and ``board --project``, and ``task add --project`` printed
the codec's error for its reason. Each is now the not-found its ref gets when it names
nothing, as ``team._brokered_member`` refuses such a ``--project`` from a sandbox (#74).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.services import team as team_service
from tests.test_a_claim_prints_its_contract import CODER, PLANNER, _ok, _start

#: How a shell passes the byte 0xff, which is not UTF-8.
NOT_UTF8 = chr(0xDCFF)
#: What the store's encode said, raised or printed.
CODEC = ("Traceback", "UnicodeEncodeError", "codec", "surrogates not allowed")


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from tests.projects import add_project

    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    add_project(work)
    return work


@pytest.fixture
def board(runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    _start(runner, PLANNER, "planner", work_dir, monkeypatch)
    _start(runner, CODER, "coder", work_dir, monkeypatch)
    _ok(runner, "task", "add", "wire auth", "--as", PLANNER[:8])
    return runner


def _refused(runner: CliRunner, *argv: str) -> str:
    """What ``aisquare <argv>`` said, once it is checked as a refusal in words: exit 1, with
    no traceback and nothing of the codec's."""
    result = runner.invoke(app, list(argv))
    said = result.output
    assert result.exit_code == 1, (argv, said, result.exception)
    assert isinstance(result.exception, SystemExit), (argv, result.exception)
    assert not any(word in said for word in CODEC), (argv, said)
    return " ".join(said.split())


AS = ("--as", CODER[:8])


@pytest.mark.parametrize(
    "verb",
    [
        ("claim", *AS),
        ("show",),
        ("done", *AS),
        ("review", *AS),
        ("release", *AS),
        ("drop", *AS),
        ("block", "--reason", "stuck", *AS),
    ],
    ids=lambda verb: verb[0],
)
def test_a_task_ref_utf8_cannot_hold_matches_nothing(
    board: CliRunner, verb: tuple[str, ...]
) -> None:
    """The finding's ``task claim tsk_\\udcff``, and every other verb that names a task."""
    said = _refused(board, "task", verb[0], f"tsk_{NOT_UTF8}", *verb[1:])
    assert "nothing matches 'tsk_" in said, said


def test_a_session_ref_utf8_cannot_hold_matches_no_session(board: CliRunner) -> None:
    """The finding's ``claim --as <ref>\\udcff``, and a write's ``--as`` the same."""
    for argv in (
        ("task", "claim", "1", "--as", f"{CODER[:8]}{NOT_UTF8}"),
        ("note", "hi", "--as", f"{CODER[:8]}{NOT_UTF8}"),
    ):
        said = _refused(board, *argv)
        assert f"no session matches '{CODER[:8]}" in said, said
    with pytest.raises(team_service.NoSuchSession):
        team_service.claim_task("1", session_ref=f"{CODER[:8]}{NOT_UTF8}")
    assert "todo" in board.invoke(app, ["task", "show", "1"]).output  # not claimed


def test_with_json_each_is_the_not_found_its_ref_gets(board: CliRunner) -> None:
    for argv in (
        ("--json", "task", "claim", f"tsk_{NOT_UTF8}", "--as", CODER[:8]),
        ("--json", "task", "claim", "1", "--as", f"{CODER[:8]}{NOT_UTF8}"),
    ):
        result = board.invoke(app, list(argv))
        assert result.exit_code == 1, result.output
        assert json.loads(result.stdout)["error"] == "not_found", result.stdout


def test_an_event_or_a_project_ref_utf8_cannot_hold_matches_nothing(board: CliRunner) -> None:
    """``team show``/``team verify`` by an event id, ``board --project`` and a delegation's
    ``task add --project``, run here (a sandbox's broker refuses the last two as #74 made
    it)."""
    bad = f"evt_{NOT_UTF8}"
    assert "no event matches" in _refused(board, "team", "show", bad, "--as", CODER[:8])
    assert "no event matches" in _refused(board, "team", "verify", bad, "--as", CODER[:8])
    said = _refused(board, "board", "--project", f"api{NOT_UTF8}")
    assert "no project matches 'api" in said, said
    said = _refused(board, "task", "add", "x", "--project", f"api{NOT_UTF8}", "--as", PLANNER[:8])
    assert "no such project" in said, said
