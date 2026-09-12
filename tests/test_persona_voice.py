"""The opt-in response STYLE: the one persona setting allowed to reach an agent.

Everything else about personas is display-only. The style is a per-project opt-in
communication contract (Answer-First, Careful Reviewer, …) injected by the
PER-TURN hook — not the launch system prompt — so it changes mid-session with no
restart. It carries a hard facts guard and never enters board records, evidence,
task notes or --json.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.personas import parse_pack, voice_text
from aisquare.services import personas, team


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    team.activate()
    return root


def test_style_packs_carry_a_contract_and_decoration_packs_do_not() -> None:
    # The helpful STYLE packs are what reach the agent: each carries a default
    # communication contract that shapes replies.
    for name in ("answer-first", "teacher", "board-brief", "careful-reviewer", "proactive"):
        pack = personas.load_pack(name)
        assert voice_text(pack, "coder"), name
        assert voice_text(pack, "bot7") == pack.voice["default"], "any role gets the default style"
    # Studio and Mission Control are side-panel DECORATION only: no injected voice.
    for name in ("studio", "mission-control"):
        assert personas.load_pack(name).voice == {}, name


def test_voice_entries_are_validated_like_every_other_pack_text() -> None:
    base: dict[str, Any] = {
        "schema_version": 1,
        "id": "v",
        "version": "1.0.0",
        "name": "V",
        "generic": {"default": ["x"]},
    }
    parse_pack(json.dumps({**base, "voice": {"default": "Speak plainly."}}).encode())
    for bad in (
        {"coder2": "seat"},
        {"default": "a\x1bb"},
        {"default": " "},
        {"default": "x" * 801},
    ):
        with pytest.raises(ValueError):
            parse_pack(json.dumps({**base, "voice": bad}).encode())


def test_a_plain_description_becomes_the_voice_not_a_generated_phrase_set() -> None:
    draft = personas.author_draft("calm-dev", "Calm, friendly developer; brief updates.")
    assert draft.voice == {"default": "Calm, friendly developer; brief updates."}
    # The panel phrases are still the starter's: the author edits them by hand.
    assert draft.generic["task_claimed"] == personas.load_pack("studio").generic["task_claimed"]


def test_style_is_off_by_default_and_on_only_for_the_project_that_asked(board: Path) -> None:
    project = team.board_data(cwd=board)[0]
    personas.select("use", project, reference="answer-first")
    assert personas.voice_instruction(project, "coder") is None
    assert personas.persona_status(project)["voice"] is False
    receipt = personas.set_voice(project, True)
    assert "on for new sessions" in receipt.message
    instruction = personas.voice_instruction(project, "coder")
    assert instruction is not None
    assert personas.load_pack("answer-first").voice["default"] in instruction
    # The facts guard, in the new communication-contract frame.
    assert "Never change facts" in instruction and "answer plainly" in instruction
    assert personas.persona_status(project)["voice_roles"]
    # A decoration pack injects nothing even with the style switch on.
    personas.select("use", project, reference="studio")
    assert personas.voice_instruction(project, "coder") is None, "decoration packs never inject"
    personas.select("use", project, reference="answer-first")
    personas.select("off", project)
    assert personas.voice_instruction(project, "coder") is None, "project Off wins"


def test_style_reaches_the_agent_per_turn_and_changes_mid_session(board: Path) -> None:
    """The reframed feature: the style is injected by the per-turn hook (so it can
    change live), NOT frozen onto the system prompt at launch."""
    project = team.board_data(cwd=board)[0]
    with pytest.MonkeyPatch.context() as env:
        env.setenv("AISQUARE_ROLE", "coder")
        team.hook_session_start("coder-1", board, "startup")

        def style() -> str | None:
            beat = team.hook_prompt_heartbeat("coder-1", board)
            if "<aisquare-style>" not in beat:
                return None
            return beat.split("<aisquare-style>")[1].split("</aisquare-style>")[0]

        assert style() is None, "off by default"
        personas.select("use", project, reference="answer-first")
        personas.set_voice(project, True)
        first = style()
        assert first is not None and "Answer-First" in first
        # Switch persona with NO restart — the next heartbeat carries the new style.
        personas.select("use", project, reference="careful-reviewer")
        second = style()
        assert second is not None and "Careful Reviewer" in second
        assert "Answer-First" not in second, "the change is live, not additive"
        # Off again silences it on the next turn.
        personas.set_voice(project, False)
        assert style() is None


def _strip_style(text: str) -> str:
    """Drop the <aisquare-style> fence the hook now carries, leaving the factual part."""
    # The trailer is appended last, as "\n<aisquare-style>...": cut it off there.
    return text.split("\n<aisquare-style>")[0]


def test_style_on_changes_no_record_only_the_per_turn_hook(board: Path, runner: CliRunner) -> None:
    project = team.board_data(cwd=board)[0]
    task, _ = team.add_task("Build login", role="coder", cwd=board)
    with pytest.MonkeyPatch.context() as env:
        env.setenv("AISQUARE_ROLE", "coder")
        env.setenv("AISQUARE_TASK_ID", task.id)
        before = team.hook_session_start("s-1", board, "startup")
    board_before = runner.invoke(app, ["--json", "task", "show", task.id]).stdout
    # A real STYLE pack, switched on.
    personas.select("use", project, reference="answer-first")
    personas.set_voice(project, True)
    with pytest.MonkeyPatch.context() as env:
        env.setenv("AISQUARE_ROLE", "coder")
        env.setenv("AISQUARE_TASK_ID", task.id)
        after = team.hook_session_start("s-1", board, "resume")
    # The style DOES now reach the agent (that is the point) — but only in the fence,
    # and the factual part of the context is byte-identical either way.
    assert "<aisquare-style>" in after and "Answer-First" in after
    assert _strip_style(after) == _strip_style(before)
    # The board record and its --json are untouched by the style.
    assert runner.invoke(app, ["--json", "task", "show", task.id]).stdout == board_before


def test_the_voice_command_family(board: Path, runner: CliRunner) -> None:
    project = team.board_data(cwd=board)[0]
    personas.select("use", project, reference="studio")
    result = runner.invoke(app, ["persona", "voice", "on"])
    assert result.exit_code == 0, result.output
    assert personas.persona_status(project)["voice"] is True
    result = runner.invoke(app, ["persona", "voice", "sideways"])
    assert result.exit_code == 2
    receipt = personas.run_persona_command("/persona voice off", project)
    assert receipt.action == "voice" and receipt.data["voice"] is False
