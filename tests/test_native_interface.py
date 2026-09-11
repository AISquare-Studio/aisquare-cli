"""Real local persona controls, source identity and launcher integration."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Select, Static, TextArea
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.cli.ui.persona_activity import PersonaActivity
from aisquare.cli.ui.personas import PersonaEditor, PersonaScreen
from aisquare.core import snapshot
from aisquare.core.source_revision import source_fingerprint, source_root_for
from aisquare.models import ProjectInfo, Snapshot
from aisquare.services import personas
from aisquare.services import team as team_service


class PersonaHost(App[None]):
    def __init__(self, project: ProjectInfo) -> None:
        super().__init__()
        self.project = project

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Input("My unfinished task request", id="agent-draft")
            yield PersonaActivity(self.project)


def test_local_picker_switches_and_edits_without_touching_agent_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)
    project = team_service.activate()

    async def run() -> None:
        host = PersonaHost(project)
        async with host.run_test(size=(120, 50)) as pilot:
            draft = host.query_one("#agent-draft", Input)
            draft.focus()
            host.push_screen(PersonaScreen(project))
            await pilot.pause()
            await host.workers.wait_for_complete()
            await pilot.pause()
            picker = host.screen.query_one("#persona-pack", Select)
            picker.value = "mission-control@1.0.0"
            await pilot.click("#persona-use")
            await host.workers.wait_for_complete()
            await pilot.pause()
            assert personas.persona_status(project)["default"] == "mission-control@1.0.0"
            assert draft.value == "My unfinished task request"
            command = host.screen.query_one("#persona-command", Input)
            command.value = '/persona add --name calm --text "Friendly and calm"'
            command.focus()
            await pilot.press("enter")
            await host.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(host.screen, PersonaEditor)
            editor = host.screen.query_one("#persona-json", TextArea)
            data = json.loads(editor.text)
            data["generic"]["default"] = ["A fresh update is ready."]
            editor.load_text(json.dumps(data))
            await pilot.click("#save-persona")
            await pilot.pause()
            assert personas.load_pack("calm").generic["default"] == ["A fresh update is ready."]
            await pilot.click("#persona-close")
            await pilot.pause()
            assert draft.value == "My unfinished task request"

    asyncio.run(run())


def test_global_picker_preview_and_bad_quoting_do_not_crash() -> None:
    async def run() -> None:
        host: App[None] = App()
        async with host.run_test(size=(100, 42)) as pilot:
            host.push_screen(PersonaScreen())
            await pilot.pause()
            await host.workers.wait_for_complete()
            await pilot.pause()
            await pilot.click("#persona-preview")
            await host.workers.wait_for_complete()
            await pilot.pause()
            assert "original_failure" in str(
                host.screen.query_one("#persona-output", Static).render()
            )
            box = host.screen.query_one("#persona-command", Input)
            box.value = '/persona add "unterminated'
            box.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert "quotation" in str(host.screen.query_one("#persona-output", Static).render())

    asyncio.run(run())


def test_source_fingerprint_sees_dirty_new_deleted_but_not_generated(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text("artifacts/\n")
    source = root / "app.py"
    source.write_text("answer = 1\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    first = source_fingerprint(root)
    (root / "artifacts").mkdir()
    (root / "artifacts" / "report.txt").write_text("40 tests passed")
    assert source_fingerprint(root) == first
    source.write_text("answer = 2\n")
    assert source_fingerprint(root) != first
    changed = source_fingerprint(root)
    (root / "new.py").write_text("new = True")
    assert source_fingerprint(root) != changed
    changed = source_fingerprint(root)
    source.unlink()
    assert source_fingerprint(root) != changed


def test_focus_uses_existing_index_but_never_returns_escaped_or_missing_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "login.py").write_text("current code")
    (tmp_path / "login-secret.txt").write_text("outside")
    project = ProjectInfo(id="focus-test", root=root)
    data_dir = snapshot.snapshot_dir(project.id)
    data_dir.mkdir(parents=True)
    meta = Snapshot(
        project_id=project.id,
        generated_at=datetime.now(UTC),
        head_sha="old",
        pack_path=data_dir / "pack.xml",
        skeleton_path=data_dir / "skeleton.xml",
        index_path=snapshot.index_path(project.id),
    )
    snapshot.meta_path(project.id).write_text(meta.model_dump_json())
    meta.index_path.write_text(
        json.dumps(
            [
                {"path": "login.py"},
                {"path": "missing-login.py"},
                {"path": "../login-secret.txt"},
            ]
        )
    )
    selected = snapshot.focus_files(project.id, root, "login")
    assert selected["files"] == ["login.py"]
    assert selected["index_commit_changed"]
    (root / "login.py").unlink()
    assert snapshot.focus_files(project.id, root, "login")["files"] == []


def test_source_fingerprint_tracks_directory_links_and_rejects_special_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plain-project"
    root.mkdir()
    (root / "one").mkdir()
    (root / "two").mkdir()
    link = root / "active"
    link.symlink_to("one", target_is_directory=True)
    first = source_fingerprint(root)
    link.unlink()
    link.symlink_to("two", target_is_directory=True)
    assert source_fingerprint(root) != first
    os.mkfifo(root / "app.pipe")
    with pytest.raises(ValueError, match="not a regular file"):
        source_fingerprint(root)


def test_source_root_keeps_worktree_and_shared_hub_worker_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    principal = tmp_path / "principal"
    principal.mkdir()
    subprocess.run(["git", "init", "-q", str(principal)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(principal),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    worker = tmp_path / "worker"
    subprocess.run(
        ["git", "-C", str(principal), "worktree", "add", "-qb", "worker", str(worker)],
        check=True,
    )
    (worker / "src").mkdir()
    assert source_root_for(principal, worker / "src") == worker.resolve()
    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub))
    assert source_root_for(hub, worker / "src") == worker.resolve()
    monkeypatch.delenv("AISQUARE_TEAM_HUB")
    assert source_root_for(hub, worker) == hub.resolve()


def test_source_fingerprint_rejects_tracked_directory_replaced_by_external_link(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    folder = root / "source"
    folder.mkdir()
    (folder / "app.py").write_text("original")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    outside = tmp_path / "outside"
    folder.rename(outside)
    folder.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes through a directory link"):
        source_fingerprint(root)


def test_launch_task_is_resolved_and_does_not_leak_to_unassigned_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)
    team_service.activate()
    task, _ = team_service.add_task("Intended task", detail="Only this task", role="coder")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/true")
    monkeypatch.setattr(
        launch_cli, "_exec", lambda binary, argv, env: captured.update(env=env, argv=argv)
    )
    result = runner.invoke(app, ["launch", "coder", "--task", task.id])
    assert result.exit_code == 0, result.output
    assert captured["env"]["AISQUARE_TASK_ID"] == task.id
    assert "--task" not in captured["argv"]
    monkeypatch.setenv("AISQUARE_TASK_ID", task.id)
    result = runner.invoke(app, ["launch", "reviewer"])
    assert result.exit_code == 0, result.output
    assert "AISQUARE_TASK_ID" not in captured["env"]
    captured.clear()
    result = runner.invoke(app, ["launch", "coder", "--task", "tsk_missing"])
    assert result.exit_code == 1
    assert not captured
    team_service.add_task("Another task", detail="Another assignment", role="coder")
    result = runner.invoke(app, ["--json", "launch", "coder", "--task", "tsk_"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "invalid_task"
    assert "longer ID" in result.stdout
    assert not captured
