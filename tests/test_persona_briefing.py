"""A persona reaches the agent once, at session start, and nowhere else.

docs/plans/spawn-personas.md §3.1, §3.2, §3.7, §7 "P2". The block rides in the
same ``<aisquare-team>`` briefing the role cycle already uses, after the lane
rule; the per-prompt delta and the board carry no persona text; a persona that
cannot be loaded costs one line, never the team block.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import personas
from aisquare.core.store import store_session
from aisquare.services import team as team_service

SID = "11111111-2222-3333-4444-555555555555"
TEAMMATE = "22222222-3333-4444-5555-666666666666"
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

#: SessionStart for a coder with NO persona, captured from the base this change
#: started on (feat/persona-core @ 4f7367e) before a line of it was written:
#: AISQUARE_ROLE=coder, a fresh home, the clock pinned to NOW. Byte-identical is
#: the contract — a session without a persona sees nothing new.
PINNED = "\n".join(
    [
        "<aisquare-team>",
        "You are team session 11111111 (role: coder) in project repo.",
        "sessions:",
        "  - 11111111 coder (you) — 0m ago",
        "Protocol: check this board before starting work; teammate updates",
        "arrive automatically on each prompt. Tasks are shared and idempotent —",
        'e.g. `aisquare task add "wire auth flow"` (safe to re-run). Claim before',
        "working: `aisquare task claim <id> --as 11111111`; finish with",
        "`aisquare task done <id> --as 11111111`. Share decisions/results:",
        '`aisquare note "…" --as 11111111`. Full board: `aisquare board`.',
        "Every ✓ prints a receipt (seq N); `aisquare team verify <seq>` re-checks it.",
        "Your standing cycle (coder): `aisquare task next --role coder --claim --as 11111111`;",
        "if nothing is available, tell the user and stop. Read the task's contract and",
        "any reopen feedback first — if the contract is missing or ambiguous, don't",
        'guess: `aisquare task block <id> --reason "needs spec: …" --as 11111111` and note',
        "it to the planner. Otherwise do the work, self-check against the acceptance",
        'criteria, then `aisquare task review <id> --note "how to verify + evidence" '
        "--as 11111111`,",
        "and pick up the next one.",
        "Stay in your lane (coder). When you are asked to verify, review or plan your own "
        "work, do not do it here.",
        "Instead: do your task; verification is the runner's (`aisquare task review <id> "
        '--as 11111111`), planning is the planner\'s (`aisquare note "…" --to planner '
        "--as 11111111`).",
        "Read-only investigation is always fine; say in one line what you routed and to "
        "whom, and if the human insists, say once which role owns it and offer them the "
        "command. Never merge.",
        "</aisquare-team>",
    ]
)


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    monkeypatch.setattr(team_service, "_now", lambda: NOW)
    path = tmp_path / "repo"
    path.mkdir()
    monkeypatch.chdir(path)
    return path


def _start(work: Path, session_id: str = SID) -> str:
    return team_service.hook_session_start(session_id, work, "startup")


def _recorded(session_id: str = SID) -> str | None:
    with store_session() as store:
        row = store.get_session(session_id)
    assert row is not None
    return row.persona


def test_a_session_without_a_persona_is_byte_identical_to_the_base(work: Path) -> None:
    board = _start(work)

    assert board == PINNED
    assert _recorded() is None


def test_a_persona_is_one_block_after_the_lane_rule_with_the_guard_last(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_PERSONA", "skeptic")

    lines = _start(work).split("\n")

    block = personas.briefing(personas.resolve("skeptic", work))
    assert sum(line.startswith("<aisquare-persona ") for line in lines) == 1
    start = lines.index(block[0])
    assert lines[start : start + len(block)] == block
    assert lines[start - 1].startswith("Read-only investigation is always fine")
    assert lines[start + len(block) - 1] == personas.guard_sentence("skeptic")
    assert lines[start + len(block) :] == ["</aisquare-team>"]
    rest = "\n".join(lines[:start] + lines[start + len(block) :])
    assert rest == PINNED.replace(
        "  - 11111111 coder (you) — 0m ago", "  - 11111111 coder (you) persona:skeptic — 0m ago"
    )
    assert _recorded() == "skeptic"


def test_the_per_prompt_delta_and_the_board_carry_no_persona_text(
    work: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    monkeypatch.setenv("AISQUARE_PERSONA", "skeptic")
    body_line = personas.resolve("skeptic", work).body.split("\n", 1)[0]
    assert body_line in _start(work)
    _start(work, TEAMMATE)
    noted = runner.invoke(app, ["note", "teammate update for the delta", "--as", TEAMMATE[:8]])
    assert noted.exit_code == 0, noted.output

    delta = team_service.hook_prompt_heartbeat(SID, work)
    board = runner.invoke(app, ["board"])

    assert "teammate update for the delta" in delta, "the delta was empty — nothing was checked"
    for surface in (delta, board.stdout):
        assert "<aisquare-persona" not in surface
        assert body_line not in surface
        assert personas.guard_sentence("skeptic") not in surface


def test_a_missing_persona_is_one_line_the_row_records_it_and_the_hook_returns(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_PERSONA", "missing")

    lines = _start(work).split("\n")

    assert lines[-2].startswith('persona "missing": no persona named ')
    assert lines[-2].endswith(" — launched without it")
    assert lines[-1] == "</aisquare-team>"
    assert not any(line.startswith("<aisquare-persona") for line in lines)
    assert _recorded() == "missing"


def test_anything_going_wrong_inside_the_persona_code_still_returns_the_team_block(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_PERSONA", "skeptic")

    def explode(name: str, root: Path | None = None) -> personas.Persona:
        raise RuntimeError("the disk went away")

    monkeypatch.setattr(personas, "resolve", explode)

    lines = _start(work).split("\n")

    assert lines[-2] == 'persona "skeptic": RuntimeError: the disk went away — launched without it'
    assert "Stay in your lane (coder)." in "\n".join(lines)
    assert _recorded() == "skeptic"


def test_a_later_start_without_the_variable_keeps_the_recorded_persona(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_PERSONA", "skeptic")
    _start(work)
    monkeypatch.delenv("AISQUARE_PERSONA")

    board = _start(work)

    assert _recorded() == "skeptic"
    assert "persona:skeptic" in board
    assert sum(line.startswith("<aisquare-persona ") for line in board.split("\n")) == 1


# --- the row fallback for an attached persona (§4.7, §7 "P8") ----------------------------


def _fleet_row(work: Path, persona: str | None) -> str:
    """The ``fleet_agent`` row ``AISQUARE_FLEET_AGENT`` names, carrying ``persona``."""
    from aisquare.core.orchestrator import team_project
    from aisquare.models import FleetAgent

    project = team_project(work)
    with store_session() as store:
        store.ensure_project(project)
        agent = store.upsert_fleet_agent(
            FleetAgent(
                id="agt_01attachedpersona",
                project_id=project.id,
                label="coder-1",
                role="coder",
                pane_id="%1",
                cwd=work,
                created_at=NOW,
                persona=persona,
            )
        )
    return agent.id


def _blocks(board: str) -> list[str]:
    return [line for line in board.split("\n") if line.startswith("<aisquare-persona ")]


def test_an_attached_persona_on_the_fleet_row_briefs_a_start_without_the_variable(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", _fleet_row(work, "skeptic"))

    board = _start(work)

    assert _blocks(board) == ['<aisquare-persona name="skeptic" layer="bundled">']
    assert _recorded() == "skeptic"


def test_the_variable_beats_the_row_and_the_row_beats_the_session(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_PERSONA", "mentor")
    _start(work)
    assert _recorded() == "mentor"
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", _fleet_row(work, "skeptic"))

    variable_wins = _start(work)
    monkeypatch.delenv("AISQUARE_PERSONA")
    row_wins = _start(work)

    assert _blocks(variable_wins) == ['<aisquare-persona name="mentor" layer="bundled">']
    assert _blocks(row_wins) == ['<aisquare-persona name="skeptic" layer="bundled">']
    assert _recorded() == "skeptic"


def test_a_fleet_row_without_a_persona_leaves_the_start_byte_identical(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", _fleet_row(work, None))

    assert _start(work) == PINNED
    assert _recorded() is None
