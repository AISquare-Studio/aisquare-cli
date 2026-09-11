"""The response-only boundary, replayed: personas change nothing an agent receives.

Required verification items 1 and 7 of the build plan. The same board activity is
read back under persona Off, Studio, Mission Control and a per-role override, and
every surface that reaches an agent or a machine caller must be byte-identical.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.personas import parse_pack
from aisquare.services import personas, team


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    team.activate()
    return root


_VOLATILE = re.compile(r"\d{4}-\d{2}-\d{2}T[\d:.]+Z|\"cursor\": \d+|\d+m ago")


def _stable(text: str) -> str:
    """Timestamps, cursors and ages move between reads; nothing else may."""
    return _VOLATILE.sub("<volatile>", text)


def _agent_surfaces(board: Path, runner: CliRunner, task_id: str) -> dict[str, str]:
    """Everything an agent or a --json caller sees for this board."""
    with pytest.MonkeyPatch.context() as env:
        env.setenv("AISQUARE_ROLE", "coder")
        env.setenv("AISQUARE_TASK_ID", task_id)
        start = team.hook_session_start("coder-one", board, "startup")
        beat = team.hook_prompt_heartbeat("coder-one", board)
    surfaces = {"session_start": _stable(start), "heartbeat": _stable(beat)}
    for name, argv in (
        ("board", ["--json", "board"]),
        ("task", ["--json", "task", "show", task_id]),
        ("log", ["--json", "team", "log"]),
    ):
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, result.output
        surfaces[name] = _stable(result.stdout)
    return surfaces


def _pack_phrases() -> set[str]:
    phrases: set[str] = set()
    for name in ("studio", "mission-control"):
        pack = parse_pack(
            (Path(personas.__file__).parents[1] / "personas" / f"{name}.json").read_bytes()
        )
        for patterns in (pack.generic, *pack.roles.values()):
            for alternatives in patterns.values():
                phrases.update(alternatives)
    return phrases


def test_every_agent_facing_surface_is_identical_under_every_persona_state(
    board: Path, runner: CliRunner
) -> None:
    task, _ = team.add_task("Build login", role="coder", cwd=board, detail="the contract")
    with pytest.MonkeyPatch.context() as env:
        env.setenv("AISQUARE_ROLE", "coder")
        team.hook_session_start("coder-one", board, "startup")  # the session must exist
    team.claim_task(task.id, session_ref="coder-one")
    team.add_note("half way", session_ref="coder-one")
    project = team.board_data(cwd=board)[0]

    baseline = _agent_surfaces(board, runner, task.id)
    personas.select("use", project, reference="studio")
    studio = _agent_surfaces(board, runner, task.id)
    personas.select("use", project, reference="mission-control")
    personas.select("use", project, reference="studio", role="coder")
    mixed = _agent_surfaces(board, runner, task.id)
    personas.select("off", project)
    off = _agent_surfaces(board, runner, task.id)

    for name in baseline:
        assert studio[name] == baseline[name], name
        assert mixed[name] == baseline[name], name
        assert off[name] == baseline[name], name
    everything = "\n".join(baseline.values())
    for phrase in _pack_phrases():
        assert phrase not in everything, phrase
    assert "Role narration" not in everything
    # The narration DOES exist for a human, so the comparison above is not vacuous.
    personas.select("use", project, reference="mission-control")
    events = team.board_data(cwd=board)[3]
    captions = [personas.render_caption(event, project) for event in events]
    assert any("Mission Control" in caption for caption in captions)
    assert json.loads(baseline["task"])["id"] == task.id


def test_agent_context_modules_never_import_persona_code() -> None:
    """Static isolation: nothing that builds a prompt can even reach the packs."""
    script = (
        "import sys, aisquare.services.team, aisquare.services.fleet, "
        "aisquare.services.hooks, aisquare.core.harness, aisquare.core.spawn, "
        "aisquare.services.work_briefs; "
        "print(sorted(m for m in sys.modules if 'persona' in m))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]", result.stdout
