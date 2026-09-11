"""The opt-in voice: the ONE persona setting allowed to reach an agent, and only how.

Everything else about personas is display-only and stays that way. Voice is a
deliberate, per-project opt-in that appends the selected pack's speaking
instruction to a NEW agent's system prompt through Claude Code's own flag. It
never touches board records, evidence, working rules or a running session.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
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


def test_both_bundled_packs_carry_a_voice_for_every_role() -> None:
    for name in ("studio", "mission-control"):
        pack = personas.load_pack(name)
        assert pack.voice["default"]
        for role in ("manager", "coder", "runner", "tester", "reviewer", "validator", "ui-tester"):
            assert voice_text(pack, role), (name, role)
        assert voice_text(pack, "coder2") == pack.voice["coder"], "seats inherit"
        assert voice_text(pack, "bot7") == pack.voice["default"], "unknown roles get the default"


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


def test_voice_is_off_by_default_and_on_only_for_the_project_that_asked(board: Path) -> None:
    project = team.board_data(cwd=board)[0]
    personas.select("use", project, reference="studio")
    assert personas.voice_instruction(project, "coder") is None
    assert personas.persona_status(project)["voice"] is False
    receipt = personas.set_voice(project, True)
    assert "on for new sessions" in receipt.message
    instruction = personas.voice_instruction(project, "coder")
    assert instruction is not None
    assert personas.load_pack("studio").voice["coder"] in instruction
    assert "never changes code" in instruction and "stay exact" in instruction
    assert personas.persona_status(project)["voice_roles"]
    personas.select("off", project)
    assert personas.voice_instruction(project, "coder") is None, "project Off wins"
    personas.select("use", project, reference="studio")
    personas.set_voice(project, False)
    assert personas.voice_instruction(project, "coder") is None


def test_launch_appends_the_flag_only_when_asked_and_only_for_claude(
    board: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    project = team.board_data(cwd=board)[0]
    personas.select("use", project, reference="mission-control")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/true")
    monkeypatch.setattr(
        launch_cli, "_exec", lambda binary, argv, env: captured.update(env=env, argv=argv)
    )
    result = runner.invoke(app, ["launch", "coder"])
    assert result.exit_code == 0, result.output
    assert "--append-system-prompt" not in captured["argv"], "off by default"

    personas.set_voice(project, True)
    result = runner.invoke(app, ["launch", "coder"])
    assert result.exit_code == 0, result.output
    argv = captured["argv"]
    index = argv.index("--append-system-prompt")
    assert personas.load_pack("mission-control").voice["coder"] in argv[index + 1]
    assert "persona voice: appended" in result.output

    # A caller's own flag is kept; ours is not added beside it.
    result = runner.invoke(app, ["launch", "coder", "--append-system-prompt", "mine"])
    assert result.exit_code == 0, result.output
    assert captured["argv"].count("--append-system-prompt") == 1
    assert "mine" in captured["argv"]

    # A role bound to another binary never receives a Claude-only flag.
    result = runner.invoke(app, ["launch", "coder", "--command", "codex"])
    assert result.exit_code == 0, result.output
    assert "--append-system-prompt" not in captured["argv"]


def test_voice_on_changes_no_record_and_no_briefing(board: Path, runner: CliRunner) -> None:
    project = team.board_data(cwd=board)[0]
    task, _ = team.add_task("Build login", role="coder", cwd=board)
    with pytest.MonkeyPatch.context() as env:
        env.setenv("AISQUARE_ROLE", "coder")
        env.setenv("AISQUARE_TASK_ID", task.id)
        before = team.hook_session_start("s-1", board, "startup")
    board_before = runner.invoke(app, ["--json", "task", "show", task.id]).stdout
    personas.select("use", project, reference="studio")
    personas.set_voice(project, True)
    with pytest.MonkeyPatch.context() as env:
        env.setenv("AISQUARE_ROLE", "coder")
        env.setenv("AISQUARE_TASK_ID", task.id)
        after = team.hook_session_start("s-1", board, "resume")
    assert after == before
    assert runner.invoke(app, ["--json", "task", "show", task.id]).stdout == board_before
    assert "persona" not in after.lower()


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
