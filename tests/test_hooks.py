"""The Claude Code hook handlers: prompt capture + session-start injection."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import selfcli
from aisquare.core.version import __version__
from aisquare.services import install_route

_PACK = '<files>\n<file path="a.py">\nprint("hi")\n</file>\n</files>\n'


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def _json(output: str) -> Any:
    return json.loads(output)


def test_user_prompt_submit_captures(runner: CliRunner, work_dir: Path) -> None:
    payload = json.dumps({"prompt": "add a test for X", "cwd": str(work_dir)})
    result = runner.invoke(app, ["hook", "user-prompt-submit"], input=payload)
    assert result.exit_code == 0, result.output
    logged = runner.invoke(app, ["--json", "log"])
    assert "add a test for X" in [prompt["text"] for prompt in _json(logged.stdout)]


def test_user_prompt_submit_ignores_blank(runner: CliRunner) -> None:
    result = runner.invoke(app, ["hook", "user-prompt-submit"], input=json.dumps({"prompt": "  "}))
    assert result.exit_code == 0
    logged = runner.invoke(app, ["--json", "log"])
    assert _json(logged.stdout) == []


def test_user_prompt_submit_survives_garbage_input(runner: CliRunner) -> None:
    result = runner.invoke(app, ["hook", "user-prompt-submit"], input="not json at all")
    assert result.exit_code == 0  # a hook must never break the agent


def test_session_start_injects_curated_context(runner: CliRunner, work_dir: Path) -> None:
    runner.invoke(app, ["context", "add", "prefer tabs", "--user"])
    result = runner.invoke(app, ["hook", "session-start"], input=json.dumps({"cwd": str(work_dir)}))
    assert result.exit_code == 0, result.output
    assert "## Your preferences" in result.stdout
    assert "prefer tabs" in result.stdout


def test_session_start_is_empty_for_an_unknown_repo(runner: CliRunner, work_dir: Path) -> None:
    result = runner.invoke(app, ["hook", "session-start"], input=json.dumps({"cwd": str(work_dir)}))
    assert result.exit_code == 0
    assert result.stdout.strip() == ""


def test_session_start_directive_points_at_the_snapshot(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.core import snapshot
    from aisquare.core.workspace import current_project

    monkeypatch.setattr(
        snapshot,
        "_run_repomix",
        lambda _root, *, compress, ignore=(): (_PACK, "Total Tokens: 9"),
    )
    monkeypatch.setattr(snapshot, "_total_tokens", lambda _text, _out: 100)
    snapshot.generate(current_project(work_dir).id, work_dir)

    result = runner.invoke(app, ["hook", "session-start"], input=json.dumps({"cwd": str(work_dir)}))
    assert result.exit_code == 0, result.output
    assert "packed snapshot" in result.stdout
    assert "pack.repomix.xml" in result.stdout


def test_session_start_directive_points_at_the_skeleton_when_the_full_pack_was_skipped(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over budget even compressed: the agent gets the skeleton and its index, and no full pack.

    This is the case the cap used to leave with NOTHING — and the directive is
    the only reader of the snapshot, so it is where "usable" has to be true.
    """
    from aisquare.core import snapshot
    from aisquare.core.workspace import current_project

    monkeypatch.setattr(
        snapshot,
        "_run_repomix",
        lambda _root, *, compress, ignore=(): (_PACK, "Total Tokens: 9"),
    )
    monkeypatch.setattr(snapshot, "_total_tokens", lambda _text, _out: 100)
    meta = snapshot.generate(current_project(work_dir).id, work_dir, max_tokens=10)
    assert meta.status == "skeleton_only"

    result = runner.invoke(app, ["hook", "session-start"], input=json.dumps({"cwd": str(work_dir)}))
    assert result.exit_code == 0, result.output
    assert "packed skeleton" in result.stdout
    assert str(meta.skeleton_path) in result.stdout
    assert str(meta.index_path) in result.stdout
    assert "pack.repomix.xml" not in result.stdout


_REAL_WHICH = shutil.which


def _aisquare_on_path(found: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    """``shutil.which("aisquare")`` answers ``found``; every other name as it would."""

    def which(cmd: str, mode: int = os.F_OK | os.X_OK, path: str | None = None) -> str | None:
        return found if cmd == "aisquare" else _REAL_WHICH(cmd, mode, path)

    monkeypatch.setattr(shutil, "which", which)


def _session_start_after_a_prompt(runner: CliRunner, work_dir: Path) -> str:
    payload = json.dumps({"prompt": "add a test for X", "cwd": str(work_dir)})
    assert runner.invoke(app, ["hook", "user-prompt-submit"], input=payload).exit_code == 0
    started = runner.invoke(
        app, ["hook", "session-start"], input=json.dumps({"cwd": str(work_dir)})
    )
    assert started.exit_code == 0, started.output
    return started.stdout


def test_session_start_names_a_log_command_the_uvx_route_can_run(
    runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plugin's uvx route leaves no ``aisquare`` on the agent's PATH (review of #257).

    uvx runs the hook from an environment in uv's cache and puts that environment's
    ``bin`` on the hook's PATH only; the launcher takes the route only where no
    aisquare is installed. "run `aisquare log`" was command not found for the agent,
    and it was often all that session start said there.
    """
    cached = tmp_path / ".cache" / "uv" / "archive-v0" / "NPat_ypOvcy3YMzz"
    monkeypatch.setattr(sys, "prefix", str(cached))
    _aisquare_on_path(str(cached / "bin" / "aisquare"), monkeypatch)

    said = _session_start_after_a_prompt(runner, work_dir)

    assert f"run `uvx --from aisquare-cli=={__version__} aisquare log` to see" in said, said
    assert "run `aisquare log`" not in said


def test_session_start_names_plain_aisquare_log_where_path_has_it(
    runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: an installed CLI on PATH is named as people type it."""
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "tools" / "aisquare-cli"))
    _aisquare_on_path("/home/me/.local/bin/aisquare", monkeypatch)

    said = _session_start_after_a_prompt(runner, work_dir)

    assert "run `aisquare log` to see how the user tends to ask" in said, said


def test_session_start_names_this_interpreter_where_path_has_no_aisquare(
    runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hooks that name a virtualenv not on PATH: the bare name would not run either."""
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "project" / ".venv"))
    _aisquare_on_path(None, monkeypatch)

    said = _session_start_after_a_prompt(runner, work_dir)

    assert f"run `{install_route.command_line(selfcli.argv_for(['log']))}` to see" in said, said
    assert "run `aisquare log`" not in said


def test_the_plugin_page_names_the_log_command_the_uvx_route_prints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """docs/claude-code-plugin.md said a uv-only machine "gets memory with nothing else
    installed", where session start's one line named a command that is not there
    (review of #257). It now says what that route gives, with the command it prints."""
    from aisquare.services import hooks as hooks_service

    monkeypatch.setattr(sys, "prefix", str(tmp_path / ".cache" / "uv" / "archive-v0" / "x"))
    printed = hooks_service._log_command()
    page = Path(__file__).resolve().parents[1] / "docs" / "claude-code-plugin.md"
    text = " ".join(page.read_text(encoding="utf-8").split())

    assert printed.startswith("uvx --from aisquare-cli=="), printed
    assert f"`{printed.replace(__version__, '<version>')}`" in text
    assert "gets memory with nothing else installed" not in text
