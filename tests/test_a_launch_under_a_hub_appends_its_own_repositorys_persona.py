"""A launch under a hub appends the persona of its own repository to the system prompt.

Review of #240, F1. ``launch --persona`` reaches the agent on two channels: the session-start
hook's briefing, and for Claude Code a system-prompt file (``--append-system-prompt-file``,
b39f6fa9). #201's F1 made the check (``_check_persona``) and the hook resolve the persona in
the working directory's own project, because under ``AISQUARE_TEAM_HUB`` the board is the
hub's. The file still resolved at the board's root:

- a persona only the repository has passed the check, then ended the launch in a traceback
  (``PersonaError: no persona named …``) after the spawn had started the window;
- a persona of the same name in the hub's layer too went into the system prompt, while the
  hook briefed the repository's: two personas, one in each channel.

Both now resolve where the check does (``personas.project_root``). A persona that cannot be
resolved by then costs the file, never the launch.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.core import orchestrator, personas
from aisquare.core.agent_adapters import claude_code


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository, the working directory, under a hub that is another directory."""
    work = tmp_path / "repo"
    (work / ".git").mkdir(parents=True)
    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv(orchestrator.TEAM_HUB_ENV_VAR, str(hub))
    assert orchestrator.team_project(work).root == hub.resolve(), "the board is the hub's"
    return work


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Intercept the exec so the agent is never really launched (``tests/test_launch.py``)."""
    captured: dict[str, Any] = {}

    def fake_exec(binary: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(binary=binary, argv=argv, env=env)

    monkeypatch.setattr(launch_cli, "_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/local/bin/{cmd}")
    return captured


def _persona(root: Path, name: str, body: str) -> None:
    skill = root / ".aisquare" / "personas" / name / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        f"---\nname: {name}\ndescription: For this test.\n---\n{body}\n", encoding="utf-8"
    )


def _system_prompt(spy: dict[str, Any]) -> str:
    assert spy["argv"][:2] == ["claude", claude_code.SYSTEM_PROMPT_FLAG], spy["argv"]
    return Path(spy["argv"][2]).read_text(encoding="utf-8")


def test_a_persona_only_the_repository_has_is_appended_from_its_layer(
    runner: CliRunner, repo: Path, spy: dict[str, Any]
) -> None:
    _persona(repo, "tempo", "Work at the repository's tempo.")

    result = runner.invoke(app, ["launch", "coder", "--persona", "tempo"])

    assert result.exit_code == 0, result.output
    assert spy["env"]["AISQUARE_PERSONA"] == "tempo"
    assert _system_prompt(spy) == (
        "\n".join(personas.briefing(personas.resolve("tempo", repo))) + "\n"
    )


def test_the_hubs_persona_of_the_same_name_is_not_the_one_appended(
    runner: CliRunner, repo: Path, spy: dict[str, Any], tmp_path: Path
) -> None:
    """The hook briefs the repository's persona; the system prompt must hold the same one."""
    _persona(repo, "tempo", "REPO BODY: work at the repository's tempo.")
    _persona(tmp_path / "hub", "tempo", "HUB BODY: rush everything.")

    result = runner.invoke(app, ["launch", "coder", "--persona", "tempo"])

    assert result.exit_code == 0, result.output
    prompt = _system_prompt(spy)
    assert "REPO BODY" in prompt and "HUB BODY" not in prompt


def test_a_persona_gone_after_the_check_costs_the_file_never_the_launch(
    runner: CliRunner, repo: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removed between the check and the file (or broken since): one dim line, the hook alone."""
    monkeypatch.setattr(launch_cli, "_check_persona", lambda name, root: None)

    result = runner.invoke(app, ["launch", "coder", "--persona", "tempo"])

    assert result.exit_code == 0, result.output
    assert spy["argv"] == ["claude"]
    assert spy["env"]["AISQUARE_PERSONA"] == "tempo", "the hook channel still asks for it"
    said = " ".join((result.output + (result.stderr if hasattr(result, "stderr") else "")).split())
    assert "persona tempo:" in said and "the session-start hook alone" in said
