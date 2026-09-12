"""Pre-hook MCP work joins a native session without losing ownership or history."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aisquare.core.store import store_session
from aisquare.models import TeamEvent
from aisquare.services import hooks, mcp_server, team


@pytest.mark.parametrize("token_key", ["AISQUARE_LAUNCH_ID", "AISQUARE_FLEET_AGENT"])
def test_unjoined_local_mcp_sessions_are_distinct_per_launch(
    token_key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    project = team.activate()
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", "shared-parent-fleet")
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    monkeypatch.setenv(token_key, "first")
    first = mcp_server._ensure_virtual_session()
    monkeypatch.setenv(token_key, "second")
    monkeypatch.setenv("AISQUARE_ROLE", "reviewer")
    second = mcp_server._ensure_virtual_session()
    assert first != second
    with store_session() as store:
        roles = {row.id: row.role for row in store.team_sessions(project.id)}
    assert roles == {first: "coder", second: "reviewer"}


@pytest.mark.parametrize("join", ["session-start", "prompt"])
@pytest.mark.parametrize("family", ["claude-code", "codex"])
def test_native_join_adopts_claims_history_focus_and_late_writes(
    join: str, family: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    project = team.activate()
    monkeypatch.setenv("AISQUARE_LAUNCH_ID", "pending-native-launch")
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    monkeypatch.setenv("AISQUARE_LAUNCH_AGENT", family)
    provisional = mcp_server._ensure_virtual_session()
    task, _ = team.add_task("pre-trust work", session_ref=provisional)
    team.claim_task(task.id, session_ref=provisional)
    team.set_focus("preserve my work", session_ref=provisional)
    team.set_signal("build", "running", session_ref=provisional)
    with store_session() as store:
        old_row = store.get_session(provisional)
        old_events = store.recent_events(project.id)
        assert old_row is not None
        store.renew_leases(provisional, datetime.now(UTC) + timedelta(seconds=10))

    native = f"{family}:native-session"
    if join == "session-start":
        hooks.session_start_context(
            tmp_path, session_id=native, agent=family, native_session_id="native-session"
        )
    else:
        team.hook_prompt_heartbeat(
            native, tmp_path, agent=family, native_session_id="native-session"
        )
    assert mcp_server._ensure_virtual_session() == native
    with store_session() as store:
        current = store.get_session(native)
        adopted_task = store.get_task(task.id)
        assert current is not None and current.focus == "preserve my work"
        assert current.agent == family and current.native_session_id == "native-session"
        assert adopted_task is not None and adopted_task.status == "doing"
        assert adopted_task.claimed_by == adopted_task.created_by == native
        assert adopted_task.claim_expires_at is not None
        assert adopted_task.claim_expires_at > datetime.now(UTC) + timedelta(minutes=1)
        assert not store.claim_task(task.id, "other-seat", datetime.now(UTC) + timedelta(minutes=5))
        assert {event.id for event in store.recent_events(project.id)} == {
            event.id for event in old_events
        }
        assert {event.id: event.session_id for event in store.recent_events(project.id)} == {
            event.id: native if event.session_id == provisional else event.session_id
            for event in old_events
        }
        signal = store.get_meta(f"signal/{project.id}/build")
        assert signal is not None and json.loads(signal)["session_id"] == native
        # A tool may have resolved its provisional identity before the native join.
        # Replaying that pending upsert/write must still hit the native row.
        assert store.upsert_session(old_row).id == native
        store.touch_session(provisional)
        event = store.add_team_event(
            TeamEvent(
                id="evt_late",
                project_id=project.id,
                session_id=provisional,
                kind="note",
                text="late tool reply",
                created_at=datetime.now(UTC),
            )
        )
        assert event.session_id == native
        late_task, _ = store.upsert_task(task.model_copy(update={"id": "tsk_late", "key": "late"}))
        assert late_task.created_by == native
        assert store.claim_task(late_task.id, provisional, datetime.now(UTC) + timedelta(minutes=5))
        assert store.get_task(late_task.id).claimed_by == native  # type: ignore[union-attr]
        rows = store.team_sessions(project.id)
        assert [row.id for row in rows if row.ended_at is None] == [native]
        assert next(row for row in rows if row.id == provisional).ended_at is not None


def test_unjoined_child_does_not_use_its_parents_fleet_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    team.activate()
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", "parent-fleet")
    hooks.session_start_context(tmp_path, session_id="parent-native")
    monkeypatch.setenv("AISQUARE_LAUNCH_ID", "new-child")
    assert mcp_server._ensure_virtual_session() != "parent-native"
