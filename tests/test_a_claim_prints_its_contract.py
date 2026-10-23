"""#74 — a coder that claims a task sees its contract.

The repro: a planner adds a task with ``--detail`` (objective, why, acceptance),
a coder claims it, and the claim printed ``✓ claimed: #1 tsk_… [doing] <title>``
and nothing else. The contract was a separate ``task show`` that nothing named,
so humans pasted it into the coder's pane. And a task reopened with
``--reason`` lost that reason the moment it was claimed again:
``stopped_because`` reads the note of the CURRENT status only, and a claimed
task is ``doing``.

Now ``task claim`` and ``task next --claim`` print the title line, the contract,
the newest reopen feedback and the command for the full task; ``task next``
without ``--claim`` says a contract exists. ``--json`` is unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from tests.projects import add_project

PLANNER = "aaaa1111-0000-0000-0000-000000000000"
CODER = "bbbb2222-0000-0000-0000-000000000000"
CONTRACT = "OBJECTIVE: wire the auth flow\nACCEPTANCE: a signed-out user is sent to /login"


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    add_project(work)
    return work


def _start(
    runner: CliRunner, session_id: str, role: str, work: Path, mp: pytest.MonkeyPatch
) -> None:
    mp.setenv("AISQUARE_ROLE", role)
    payload = json.dumps({"cwd": str(work), "session_id": session_id, "source": "startup"})
    assert runner.invoke(app, ["hook", "session-start"], input=payload).exit_code == 0
    mp.delenv("AISQUARE_ROLE")


def _ok(runner: CliRunner, *argv: str) -> str:
    result = runner.invoke(app, list(argv))
    assert result.exit_code == 0, result.output
    return result.stdout


@pytest.fixture
def board(runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    _start(runner, PLANNER, "planner", work_dir, monkeypatch)
    _start(runner, CODER, "coder", work_dir, monkeypatch)
    _ok(
        runner,
        "task", "add", "wire auth", "--detail", CONTRACT, "--role", "coder", "--as", PLANNER[:8],
    )  # fmt: skip
    return runner


def test_a_claim_prints_the_contract(board: CliRunner) -> None:
    out = _ok(board, "task", "claim", "1", "--as", CODER[:8])

    assert "✓ claimed: #1 tsk_" in out and "[doing] wire auth" in out
    assert CONTRACT in out
    assert "aisquare task show #1" in out


def test_next_claim_prints_the_contract_and_next_alone_says_there_is_one(
    board: CliRunner,
) -> None:
    peek = _ok(board, "task", "next", "--role", "coder", "--as", CODER[:8])
    assert "has a contract" in peek and "OBJECTIVE" not in peek  # a read: the title only

    claimed = _ok(board, "task", "next", "--role", "coder", "--claim", "--as", CODER[:8])
    assert CONTRACT in claimed


def test_a_reclaimed_task_shows_why_it_was_reopened(board: CliRunner) -> None:
    _ok(board, "task", "claim", "1", "--as", CODER[:8])
    _ok(board, "task", "review", "1", "--note", "done, see PR", "--as", CODER[:8])
    _ok(board, "task", "reopen", "1", "--reason", "the 401 path is untested", "--as", PLANNER[:8])

    again = _ok(board, "task", "claim", "1", "--as", CODER[:8])

    assert CONTRACT in again
    assert "the 401 path is untested" in again
    assert again.index(CONTRACT) < again.index("the 401 path is untested")


def test_json_and_a_task_without_a_contract_are_unchanged(board: CliRunner) -> None:
    """The control: ``--json`` already carried ``detail``; a bare task prints its one line."""
    _ok(board, "task", "add", "bare one", "--as", PLANNER[:8])
    bare = _ok(board, "task", "claim", "2", "--as", CODER[:8])
    assert bare.strip().splitlines()[0].startswith("✓ claimed: #2 tsk_")
    assert "contract" not in bare.lower()

    machine = json.loads(_ok(board, "--json", "task", "claim", "1", "--as", CODER[:8]))
    assert machine["detail"] == CONTRACT and "feedback" not in machine
