"""The Claude Code hook handlers: prompt capture + session-start injection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.store import store_session
from aisquare.core.workspace import current_project


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def _json(output: str) -> Any:
    return json.loads(output)


def _add(work_dir: Path) -> None:
    """Add ``work_dir`` on purpose, as ``project onboard`` does, without its facts."""
    with store_session() as store:
        store.onboard_project(current_project(work_dir))


def _prompt(runner: CliRunner, work_dir: Path) -> None:
    payload = json.dumps({"prompt": "add a test for X", "cwd": str(work_dir)})
    result = runner.invoke(app, ["hook", "user-prompt-submit"], input=payload)
    assert result.exit_code == 0, result.output
    assert result.stdout == ""


def _logged(runner: CliRunner) -> list[str]:
    return [prompt["text"] for prompt in _json(runner.invoke(app, ["--json", "log"]).stdout)]


def test_user_prompt_submit_captures_in_a_project_added_on_purpose(
    runner: CliRunner, work_dir: Path
) -> None:
    _add(work_dir)
    _prompt(runner, work_dir)
    assert "add a test for X" in _logged(runner)
    with store_session() as store:
        assert len(store.turn_metrics(project_id=current_project(work_dir).id)) == 1


def test_a_prompt_where_nothing_was_added_records_nothing_and_makes_no_project(
    runner: CliRunner, work_dir: Path
) -> None:
    """``[projects] capture`` is off by default: no row, no prompt, no turn (#139)."""
    _prompt(runner, work_dir)
    assert _logged(runner) == []
    with store_session() as store:
        assert store.list_projects(all=True) == []
        assert store.turn_metrics() == []


def test_with_capture_on_a_prompt_captures_the_directory_unlisted(
    runner: CliRunner, work_dir: Path, project_capture: None
) -> None:
    _prompt(runner, work_dir)
    assert "add a test for X" in _logged(runner)
    with store_session() as store:
        assert [p.root for p in store.captured_projects()] == [work_dir.resolve()]
        assert store.list_projects() == []
        assert len(store.turn_metrics()) == 1


def test_capture_is_a_setting_off_by_default_and_on_captures_as_0_8_0_did(
    runner: CliRunner, work_dir: Path, isolated_home: Path
) -> None:
    """Through config.toml, not the tests' patch: the default is in the system layer,
    so only a change is written, and setting it back writes nothing."""
    got = runner.invoke(app, ["--json", "config", "get", "projects.capture"])
    assert _json(got.stdout) == {"projects.capture": "false"}
    assert runner.invoke(app, ["config", "set", "projects.capture", "true"]).exit_code == 0
    assert "[projects]\ncapture = true" in (isolated_home / "config.toml").read_text()
    _prompt(runner, work_dir)
    assert "add a test for X" in _logged(runner)
    assert runner.invoke(app, ["config", "set", "projects.capture", "false"]).exit_code == 0
    assert "[projects]" not in (isolated_home / "config.toml").read_text()


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


def test_session_start_names_no_snapshot_and_no_log_command(
    runner: CliRunner, work_dir: Path
) -> None:
    """An old pack on disk and prompts in the log, and session start says neither: the
    pack line told agents to read ~110k tokens FIRST (the skeleton and the full pack were
    often the same file), and both rode every request of the session. The pack is
    written after the store's first open, so its one-time cleanup has run and left it."""
    from aisquare.core import paths

    _add(work_dir)
    runner.invoke(app, ["context", "add", "prefer tabs", "--user"])
    _prompt(runner, work_dir)
    pack = paths.project_data_dir(current_project(work_dir).id) / "snapshot"
    pack.mkdir(parents=True)
    (pack / "pack.xml").write_text("<files/>", encoding="utf-8")
    (pack / "snapshot.json").write_text("{}", encoding="utf-8")

    result = runner.invoke(app, ["hook", "session-start"], input=json.dumps({"cwd": str(work_dir)}))

    assert result.exit_code == 0, result.output
    assert "prefer tabs" in result.stdout
    assert pack.is_dir(), "the pack was there to be named"
    assert "snapshot" not in result.stdout
    assert "pack.xml" not in result.stdout
    assert "aisquare log" not in result.stdout
    assert "Past user prompts" not in result.stdout


def test_session_start_leaves_out_a_preference_a_claude_md_already_loads(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``agents connect`` imported ~/.claude/CLAUDE.md into the user pool, so its sections
    reached every session twice. Dropped while a CLAUDE.md Claude Code loads still holds
    them (whitespace aside); a preference from anywhere else stays."""
    claude_home = tmp_path / "claude-home"
    claude_home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    (claude_home / "CLAUDE.md").write_text("# graphify\n- run   graphify on /graphify\n")
    (work_dir / "CLAUDE.md").write_text("# repo\nuse make check\n")
    for text in ("# graphify\n- run graphify on /graphify", "use make check", "prefer tabs"):
        runner.invoke(app, ["context", "add", text, "--user"])

    result = runner.invoke(app, ["hook", "session-start"], input=json.dumps({"cwd": str(work_dir)}))

    assert result.exit_code == 0, result.output
    assert "prefer tabs" in result.stdout
    assert "graphify" not in result.stdout
    assert "use make check" not in result.stdout


def test_the_claude_md_chain_is_the_hook_inputs_cwd_not_the_hook_process_cwd(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The plugin launcher or a worktree runs the hook elsewhere than the session: the
    files Claude Code loads are the session's (the payload's ``cwd``), so a CLAUDE.md
    where the hook process happens to run neither drops nor keeps anything."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    session_dir = tmp_path / "session-checkout"
    session_dir.mkdir()
    (session_dir / "CLAUDE.md").write_text("# session\nuse the session's make check\n")
    (work_dir / "CLAUDE.md").write_text("# hook process\nuse the hook's tox\n")
    for text in ("use the session's make check", "use the hook's tox"):
        runner.invoke(app, ["context", "add", text, "--user"])
    assert Path.cwd() == work_dir  # the hook process runs here

    payload = json.dumps({"cwd": str(session_dir)})
    result = runner.invoke(app, ["hook", "session-start"], input=payload)

    assert result.exit_code == 0, result.output
    assert "use the hook's tox" in result.stdout
    assert "use the session's make check" not in result.stdout


def test_a_preference_is_left_out_only_when_it_is_whole_lines_of_a_claude_md(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review of #346: "Use uv" was dropped beside a CLAUDE.md line that only starts
    with it. A shorter rule the user asked for is not the longer one Claude Code loads."""
    claude_home = tmp_path / "claude-home"
    claude_home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    (claude_home / "CLAUDE.md").write_text(
        "# tools\n- Use uv run for every Python command, and never pip install into the system.\n"
        "- Never push to main without review.\n"
    )
    for text in (
        "Use uv",
        "Never push",
        "- Never push to main   without review.",
    ):
        runner.invoke(app, ["context", "add", text, "--user"])

    result = runner.invoke(app, ["hook", "session-start"], input=json.dumps({"cwd": str(work_dir)}))

    assert result.exit_code == 0, result.output
    assert "- Use uv\n" in result.stdout
    assert "- Never push\n" in result.stdout
    assert "without review" not in result.stdout


def _spooled_prompts() -> list[str]:
    from aisquare.core import outbox

    return [path.read_text(encoding="utf-8") for path in outbox.pending()]


def test_capture_off_stores_no_prompt_and_spools_none(
    runner: CliRunner, work_dir: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#268: ``capture.enabled = false`` stored every prompt anyway, and ``log`` listed it.
    Off, no prompt row and nothing in the Explainability spool; the turn row is still
    opened, without text."""
    from aisquare.core import insights

    monkeypatch.setattr(insights, "shipping_enabled", lambda: True)
    _add(work_dir)
    set_ = runner.invoke(app, ["config", "set", "capture.enabled", "false"])
    assert set_.exit_code == 0, set_.output
    assert "no longer stored" in set_.stdout
    _prompt(runner, work_dir)
    assert _logged(runner) == []
    assert not any("add a test for X" in record for record in _spooled_prompts())
    with store_session() as store:
        assert len(store.turn_metrics(project_id=current_project(work_dir).id)) == 1


def test_capture_on_still_stores_and_spools_the_prompt(
    runner: CliRunner, work_dir: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.core import insights

    monkeypatch.setattr(insights, "shipping_enabled", lambda: True)
    _add(work_dir)
    _prompt(runner, work_dir)
    assert _logged(runner) == ["add a test for X"]
    assert any("add a test for X" in record for record in _spooled_prompts())


def test_capture_off_still_delivers_the_teammate_delta(
    runner: CliRunner, work_dir: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delta is coordination, not capture: it reaches the session with capture off."""
    from aisquare.services import team as team_service

    monkeypatch.setattr(team_service, "hook_prompt_heartbeat", lambda *a, **k: "TEAM DELTA")
    _add(work_dir)
    assert runner.invoke(app, ["config", "set", "capture.enabled", "false"]).exit_code == 0
    payload = json.dumps({"prompt": "add a test for X", "cwd": str(work_dir), "session_id": "s1"})
    result = runner.invoke(app, ["hook", "user-prompt-submit"], input=payload)
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "TEAM DELTA"
    assert _logged(runner) == []


def test_an_unreadable_config_records_no_prompt(
    runner: CliRunner, work_dir: Path, isolated_home: Path
) -> None:
    """Privacy fails closed: a config that cannot be read may have said off, so nothing is
    recorded, and one line on stderr says so."""
    _add(work_dir)
    isolated_home.mkdir(parents=True, exist_ok=True)
    (isolated_home / "config.toml").write_text("[capture\nenabled = ", encoding="utf-8")
    payload = json.dumps({"prompt": "add a test for X", "cwd": str(work_dir)})
    result = runner.invoke(app, ["hook", "user-prompt-submit"], input=payload)
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert result.stderr.count("\n") == 1 and "capture" in result.stderr
    with store_session() as store:
        assert store.recent_prompts(current_project(work_dir).id) == []


def test_capture_set_under_json_prints_only_the_value(runner: CliRunner) -> None:
    got = runner.invoke(app, ["--json", "config", "set", "capture.enabled", "false"])
    assert _json(got.stdout) == {"capture.enabled": "false"}
