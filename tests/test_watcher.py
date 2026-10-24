"""P2-W2 (#284): ``aisquare watch``, the fleet's one non-AI watcher.

A manager watched panes, CI and pull requests with its own turns, each one re-sending its
whole context. The watcher looks instead, and wakes a manager only for something new, with
a count and a path and never an excerpt.

The needs are the phone's own scan over fake sources (``tests/test_remote_needs.py``'s
``Fleet``), an agent at Claude Code's folder-trust question being one ``permission`` item.
The fleet is ``tests/test_fleet_service.py``'s, on its in-memory tmux. Never a real
``claude`` and never GitHub.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import fleet as fleet_cli
from aisquare.cli.app import app
from aisquare.core import harness, locking
from aisquare.core.config import WatchSettings, WatchSource, load_config, save_config
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo, TeamSession
from aisquare.services import diagnostics, remote_needs
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from aisquare.services import watch as watch_service
from tests import test_fleet_service as fleet_suite
from tests.test_fleet_service import FakeTmux
from tests.test_remote_needs import Fleet, _sources

tmux = fleet_suite.tmux
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def nudges(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    """Every manager nudge the watcher asks for, recorded instead of typed."""
    calls: list[tuple[str, str | None]] = []

    def record(project_id: str, *, reason: str, board: str | None = None) -> bool:
        calls.append((project_id, board))
        return True

    monkeypatch.setattr(fleet_service, "nudge_manager", record)
    return calls


def _trust_prompt(project: ProjectInfo, label: str) -> FleetAgentStatus:
    """An agent at Claude Code's folder-trust question: one ``permission`` item."""
    row = FleetAgent(
        id=f"agt_{label}",
        project_id=project.id,
        label=label,
        role="coder",
        pane_id=f"%{len(label)}",
        session_id=f"ses_{label}",
        cwd=project.root,
        created_at=NOW - timedelta(minutes=5),
    )
    detail = fleet_service.trust_prompt_detail(project.root)
    return FleetAgentStatus.model_validate({"agent": row, "state": "attention", "detail": detail})


def _needs(project: ProjectInfo, fleet: Fleet) -> Any:
    return replace(_sources(fleet), list_projects=lambda: [project])


def _watcher(project: ProjectInfo, fleet: Fleet, **settings: Any) -> watch_service.Watcher:
    return watch_service.Watcher(
        WatchSettings(auto_stop=False, **settings),
        needs_sources=lambda: _needs(project, fleet),
        stale=False,
    )


def _watch_events(project_id: str) -> list[Any]:
    with store_session() as store:
        return [e for e in store.recent_events(project_id, limit=50) if e.kind == "watch"]


# --- what is new, once ---------------------------------------------------------------------


def test_once_json_prints_a_permission_item_once_and_a_new_prompt_is_new(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``watch --once --json``: an agent at a permission question is one item; a second run
    prints none; another agent's question is a new item."""
    fleet = Fleet(agents=[_trust_prompt(project, "coder-1")])
    monkeypatch.setattr(remote_needs, "live_needs_sources", lambda: _needs(project, fleet))
    config = load_config()
    config.watch.auto_stop = False
    save_config(config)

    def once() -> list[dict[str, Any]]:
        result = CliRunner().invoke(app, ["--json", "watch", "--once"])
        assert result.exit_code == 0, result.output
        return list(json.loads(result.stdout)["new"])

    first = once()
    assert [(item["kind"], item["detail"]["agent"]) for item in first] == [
        ("permission", "coder-1")
    ]
    assert once() == [], "the same prompt is not new twice"
    fleet.agents.append(_trust_prompt(project, "coder-2"))
    assert [item["detail"]["agent"] for item in once()] == ["coder-2"]


# --- delivery ------------------------------------------------------------------------------


def test_a_waiting_manager_gets_one_watch_line_with_counts_and_a_path_and_one_nudge(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    nudges: list[tuple[str, str | None]],
) -> None:
    """One ``watch`` event addressed to the manager on the board its row reads, the counts
    and the detail file's path and never an excerpt, and one nudge; a tick with nothing new
    writes and nudges nothing."""
    manager = fleet_service.spawn(project, "manager").agent
    fleet = Fleet(agents=[_trust_prompt(project, "coder-1"), _trust_prompt(project, "coder-2")])
    watcher = _watcher(project, fleet)

    report = watcher.tick(NOW)

    [event] = _watch_events(project.id)
    path = report.files[project.id]
    assert event.to_role == "manager"
    assert event.text == f"watch: 2 new (2 permission): aisquare watch show {path}"
    excerpt = fleet_service.trust_prompt_detail(project.root)
    assert excerpt not in event.text and "coder-1" not in event.text, "counts, never text"
    assert nudges == [(project.id, fleet_service.board_of_row(manager))]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 or sys.platform == "win32"
    shown = watch_service.show(None)
    assert [item["kind"] for item in shown["items"]] == ["permission", "permission"]

    watcher.tick(NOW + timedelta(minutes=1))
    assert len(_watch_events(project.id)) == 1 and len(nudges) == 1, "nothing new, nothing sent"


def test_a_working_manager_is_handed_the_watch_line_at_its_turns_end(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """An event addressed to the manager is a wake (P1 WS-2): the Stop that ends its turn
    keeps it going with the line."""
    manager = fleet_service.spawn(project, "manager").agent
    assert manager.session_id is not None
    with store_session() as store:
        now = datetime.now(tz=UTC)
        store.upsert_session(
            TeamSession(
                id=manager.session_id,
                project_id=project.id,
                role="manager",
                started_at=now,
                last_seen_at=now,
                cursor=store.latest_seq(project.id),
            )
        )
    _watcher(project, Fleet(agents=[_trust_prompt(project, "coder-1")])).tick(NOW)

    decision = team_service.hook_stop(manager.session_id, project.root)

    assert decision is not None and "watch: 1 new (1 permission)" in decision.reason


def test_the_manager_cycle_says_to_run_the_watcher_not_poll() -> None:
    cycle = " ".join(harness.role_cycle("manager", "abcd1234", fleet=lambda verb: verb))
    assert "Never watch panes, CI or pull requests with Monitor or sleep loops" in cycle
    assert "`watch:` lines wake you" in cycle


# --- the operator's sources ----------------------------------------------------------------


def test_a_failing_source_is_said_once_per_streak_while_the_others_run(
    project: ProjectInfo, tmp_path: Path
) -> None:
    good = tmp_path / "good.py"
    good.write_text(
        "import json\n"
        f"print(json.dumps({{'id': 'mail-1', 'project': {project.id!r}, 'kind': 'inbox', "
        "'summary': '1 unread'}))\n"
    )
    sources = {
        "bad": WatchSource(command=[sys.executable, "-c", "raise SystemExit(3)"], every_seconds=1),
        "good": WatchSource(command=[sys.executable, str(good)], every_seconds=1),
    }
    watcher = _watcher(project, Fleet(), sources=sources)

    first = watcher.tick(NOW)
    second = watcher.tick(NOW + timedelta(seconds=2))

    assert [note for note in first.notes if note.startswith("source bad")] == [
        "source bad is failing: exit 3"
    ]
    assert not any("bad" in note for note in second.notes), "once per streak"
    assert [item.kind for item in first.new] == ["inbox"] and second.new == []
    assert [item.summary for item in second.items] == ["1 unread"], "the good one still runs"


# --- one watcher per home, and the doctor ---------------------------------------------------


def test_a_second_watcher_is_refused_naming_the_first() -> None:
    watch_service.watch_dir().mkdir(parents=True, exist_ok=True)
    fd = locking.open_lock_file(watch_service.lock_path())
    try:
        locking.acquire(fd, wait_s=0)
        watch_service.pid_path().write_text("4242", encoding="utf-8")
        assert watch_service.running_pid() == 4242
        with pytest.raises(watch_service.AlreadyWatching, match="pid 4242"):
            watch_service.detach()
        with pytest.raises(watch_service.AlreadyWatching, match="pid 4242"):
            watch_service.run(on_tick=lambda report: None)
    finally:
        os.close(fd)
    assert watch_service.running_pid() is None, "the lock goes with its holder"


def test_a_shutdown_that_leaves_no_agent_stops_the_watcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped: list[bool] = []
    monkeypatch.setattr(watch_service, "running_pid", lambda: 4242)

    def stop() -> int:
        stopped.append(True)
        return 4242

    monkeypatch.setattr(watch_service, "stop_detached", stop)
    fleet_cli._stop_the_watcher_with_the_fleet()
    assert stopped == [True]


def test_a_shutdown_that_leaves_agents_keeps_the_watcher(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet_service.spawn(project, "coder", worktree=False)
    stopped: list[bool] = []
    monkeypatch.setattr(watch_service, "running_pid", lambda: 4242)

    def stop() -> int:
        stopped.append(True)
        return 4242

    monkeypatch.setattr(watch_service, "stop_detached", stop)
    fleet_cli._stop_the_watcher_with_the_fleet()
    assert stopped == [], "another project's agents still need it"


def test_the_doctor_row_shows_the_last_looks_age(monkeypatch: pytest.MonkeyPatch) -> None:
    watch_service.watch_dir().mkdir(parents=True, exist_ok=True)
    watch_service._write_state(
        {"last_scan_at": (datetime.now(tz=UTC) - timedelta(minutes=10)).isoformat()}
    )
    row = diagnostics._check_watcher()
    assert row.status.value == "ok" and "not running" in row.detail and "s ago" in row.detail
    monkeypatch.setattr(watch_service, "running_pid", lambda: 4242)
    stale = diagnostics._check_watcher()
    assert stale.status.value == "warn" and "running (pid 4242)" in stale.detail
