"""The Claude Code hook handlers: prompt capture + session-start injection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app

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
