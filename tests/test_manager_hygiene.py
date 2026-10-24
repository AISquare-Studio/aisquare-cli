"""P1 WS-4 (#281): a manager starts from a bounded start pack, and writes briefs by path.

A manager is the longest-lived session in a fleet, and every request re-sends its whole
context. After a compaction or a restart it had only the plain board, which shows eight
tasks, so it rebuilt its picture by re-reading the board, task details and files. The start
pack replaces the board for a manager at every SessionStart: its state file, every open item,
and the rules by path, within 20,000 characters. And a manager pasted scripts and rule text
into ``--detail``, where they rode every later request: ``task add --detail-file`` and
``--rules`` cite them by path, and ``[team] brief_blocks`` asks for the contract's blocks.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import harness, paths
from aisquare.core.config import load_config, save_config
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import TeamSession
from aisquare.services import team as team_service

pytestmark = pytest.mark.usefixtures("project_capture")

MANAGER = "aaaa1111-0000-0000-0000-000000000000"
CODER = "bbbb2222-0000-0000-0000-000000000000"


@pytest.fixture(autouse=True)
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def _start(monkeypatch: pytest.MonkeyPatch, work: Path, sid: str, role: str, source: str) -> str:
    with monkeypatch.context() as patch:
        patch.setenv("AISQUARE_ROLE", role)
        return team_service.hook_session_start(sid, work, source)


def _state(work: Path, text: str) -> Path:
    path = paths.manager_state_file(team_project(work).id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _coder(work: Path) -> None:
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=CODER,
                project_id=team_project(work).id,
                role="coder",
                started_at=now,
                last_seen_at=now,
            )
        )


# --- the start pack ------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["startup", "resume", "clear", "compact"])
def test_a_manager_starts_from_its_pack_at_every_source(
    work: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    """Every open task, not the board's first eight; its state file; the rules index; then the
    protocol and its cycle. A coder's start (the control) is the board as before."""
    _start(monkeypatch, work, MANAGER, "manager", "startup")
    for n in range(12):
        team_service.add_task(f"task number {n}", session_ref=MANAGER)
    state = _state(work, "goal: ship WS-4\ndecisions: off by default\nnext: review #16\n")
    rules = work / ".aisquare" / "rules"
    rules.mkdir(parents=True)
    (rules / "testing.md").write_text("# Testing: -n 2 at most\n\nnever -n auto\n")

    pack = _start(monkeypatch, work, MANAGER, "manager", source)

    assert all(f"task number {n}" in pack for n in range(12)), pack
    assert f"Resume: your state file {state}" in pack and "next: review #16" in pack
    assert "  - .aisquare/rules/testing.md — Testing: -n 2 at most" in pack
    assert "never -n auto" not in pack, "an index, never the rule's content"
    assert "Your standing cycle (manager)" in pack and f"Keep your state in {state}" in pack
    assert "wake events pending past your cursor: 0" in pack
    assert len(pack) <= 20_000

    _start(monkeypatch, work, CODER, "coder", "startup")
    board = _start(monkeypatch, work, CODER, "coder", source)
    assert "[todo] task number 11" not in board and "… 4 more — `aisquare task list`" in board
    assert "Resume:" not in board


def test_the_pack_says_where_to_write_a_missing_state_file(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack = _start(monkeypatch, work, MANAGER, "manager", "startup")
    where = paths.manager_state_file(team_project(work).id)
    assert f"Resume: no state file yet: write one at {where} before you stop." in pack


def test_the_pack_stays_within_its_budget_and_says_what_it_left_out(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 12,000-char state file is clipped at 4,000, the cycle's own limit, with its path;
    200 open tasks of 150 chars cannot all fit, and the line that ends them says how many did
    not and where they are. Every line it keeps is whole."""
    _start(monkeypatch, work, MANAGER, "manager", "startup")
    for n in range(200):
        team_service.add_task(f"t{n:03d} " + "x" * 145, session_ref=MANAGER)
    state = _state(work, "s" * 12_000)
    rules = work / ".aisquare" / "rules"
    rules.mkdir(parents=True)
    for n in range(60):
        (rules / f"rule-{n:02d}.md").write_text(f"rule {n} " + "r" * 80 + "\n")

    pack = _start(monkeypatch, work, MANAGER, "manager", "compact")

    assert len(pack) <= 20_000, len(pack)
    assert "s" * 4_000 in pack and "s" * 4_001 not in pack
    assert f"… clipped at 4,000 chars; the rest is in {state}" in pack
    shown = [n for n in range(200) if f"t{n:03d} " in pack]
    assert shown == list(range(len(shown))) and 0 < len(shown) < 200
    assert f"  … {200 - len(shown)} more open tasks — `aisquare task list`" in pack
    assert "wake events pending past your cursor: 0" in pack, "the short items are never cut"
    assert "more in" in pack, "the rules index is capped too"
    assert "Your standing cycle (manager)" in pack, "the cycle is never what is cut"


def test_the_pack_names_unanswered_questions_live_agents_and_pending_wakes(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A coder's question the manager answered (a note to ``coder``) is not open; one asked
    after it is. Wake events past the manager's cursor are counted."""
    _start(monkeypatch, work, MANAGER, "manager", "startup")
    _coder(work)
    team_service.add_note("which base, rc/p1 or rc/p2?", kind="question", session_ref=CODER)
    team_service.add_note("rc/p1", to_role="coder", session_ref=MANAGER)
    team_service.add_note("may I squash the sync?", kind="question", session_ref=CODER)

    pack = _start(monkeypatch, work, MANAGER, "manager", "resume")

    assert "unanswered questions:" in pack
    assert "may I squash the sync?" in pack
    assert "which base, rc/p1 or rc/p2?" not in pack
    # Both questions wake it: its own answer does not move its cursor.
    assert "wake events pending past your cursor: 2" in pack


# --- the manager's cycle -------------------------------------------------------------------


def test_the_manager_cycle_keeps_state_in_a_file_and_rules_by_path() -> None:
    cycle = "\n".join(
        harness.role_cycle(
            "manager", "aaaa1111", fleet=lambda verb: f"aisquare fleet {verb}", state_file="/s.md"
        )
    )
    assert "Keep your state in /s.md; rewrite it (≤ 4,000 chars: goal ·" in cycle
    assert "never\npaste them inline." in cycle
    coder = "\n".join(
        harness.role_cycle("coder", "bbbb2222", fleet=lambda verb: verb, state_file="/s.md")
    )
    assert "/s.md" not in coder, "a manager's rule"


# --- the brief builder ---------------------------------------------------------------------

CONTRACT = "objective: wire auth\nacceptance: login works\nboundaries: no schema change\n"


def _add(*args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(app, ["task", "add", *args])
    return result.exit_code, result.stdout, result.stderr


def test_rules_are_cited_by_path_and_a_missing_one_refuses(work: Path) -> None:
    rules = work / ".aisquare" / "rules"
    rules.mkdir(parents=True)
    (rules / "testing.md").write_text("# testing\n")
    (rules / "git.md").write_text("# git\n")

    code, _out, err = _add("wire auth", "--detail", CONTRACT, "--rules", "nosuch")
    assert code == 2 and f"no rules file at {rules / 'nosuch.md'}" in err
    assert team_service.list_tasks() == [], "refused: nothing added"

    code, _out, err = _add("wire auth", "--detail", CONTRACT, "--rules", "testing,git")
    assert code == 0, err
    [task] = team_service.list_tasks()
    assert task.detail is not None
    assert task.detail.endswith("\n\nRules: .aisquare/rules/testing.md, .aisquare/rules/git.md")


def test_a_detail_file_is_the_contract(work: Path, tmp_path: Path) -> None:
    brief = tmp_path / "brief.md"
    brief.write_text(CONTRACT)
    code, _out, err = _add("wire auth", "--detail-file", str(brief))
    assert code == 0 and err == ""
    [task] = team_service.list_tasks()
    assert task.detail == CONTRACT.rstrip()

    code, _out, err = _add("other", "--detail", "x", "--detail-file", str(brief))
    assert code == 2 and "not both" in err


def test_a_missing_block_warns_and_refuses_when_strict(work: Path) -> None:
    code, _out, err = _add("a", "--detail", "objective: x\nboundaries: y")
    assert code == 0 and "aisquare: the contract has no acceptance: block" in err
    result = CliRunner().invoke(
        app, ["--json", "task", "add", "b", "--detail", "objective: x\nboundaries: y"]
    )
    assert json.loads(result.stdout)["missing_blocks"] == ["acceptance"]
    assert "the contract has no acceptance: block" in result.stderr

    code, _out, err = _add("c", "--detail", CONTRACT)
    assert code == 0 and "no acceptance" not in err, "the control: every block present"

    config = load_config()
    config.team.brief_strict = True
    save_config(config)
    code, _out, err = _add("d", "--detail", "objective: x")
    assert code == 2 and "acceptance:" in err and "boundaries:" in err
    assert [t.title for t in team_service.list_tasks()] == ["a", "b", "c"]
