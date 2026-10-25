"""P1 WS-4's quiet-point compaction for a Codex manager (#113's #17).

Measured on Codex 0.164.0 (a scratch install, a loopback provider, temp homes): a bare
``/compact`` typed at its idle prompt compacts the session, writing a ``compacted`` record
to its rollout and firing NO hook; ``/compact <text>`` is run as a prompt, a turn and no
compaction. So the worker types Codex's bare command (``capabilities.compact``), weighs the
context off its rollout (``AgentTranscript.context_tokens``) and takes the rollout's
``compacted`` record for the acknowledgement Claude Code's ``SessionStart`` gives
(``AgentTranscript.compacted_since``). The live run is
``tests/test_codex_native.py::test_real_codex_manager_is_compacted_at_a_quiet_point``.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import paths
from aisquare.core.config import load_config, save_config
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo, TeamSession
from aisquare.services import delivery, quiet_compact
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from tests import test_fleet_service as fleet_suite

repo = fleet_suite.repo
project = fleet_suite.project
claude_on_path = fleet_suite.claude_on_path


def _usage(input_tokens: int) -> str:
    usage = {"input_tokens": input_tokens, "cached_input_tokens": input_tokens - 10}
    stamp = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    record = {"timestamp": stamp, "type": "token_usage_record", "payload": {"usage": usage}}
    return json.dumps(record) + "\n"


class CodexManager:
    """A Codex fleet manager's row, its waiting session and its rollout, its state file
    fresh, its context over the line, and the worker armed."""

    def __init__(self, project: ProjectInfo, tmp_path: Path) -> None:
        self.rollout = tmp_path / "rollout-manager.jsonl"
        self.rollout.write_text(_usage(450_000), encoding="utf-8")
        now = datetime.now(tz=UTC)
        self.sid = "codex-manager-session"
        with store_session() as store:
            store.upsert_session(
                TeamSession(
                    id=self.sid,
                    project_id=project.id,
                    role="manager",
                    agent="codex",
                    started_at=now,
                    last_seen_at=now,
                    cursor=store.latest_seq(project.id),
                    state="waiting",
                    transcript_path=str(self.rollout),
                )
            )
            store.upsert_fleet_agent(
                FleetAgent(
                    id="agt_codexmanager",
                    project_id=project.id,
                    label="manager",
                    role="manager",
                    binary="codex",
                    agent="codex",
                    pane_id="%9",
                    session_id=self.sid,
                    cwd=project.root,
                    created_at=now,
                )
            )
            session = store.get_session(self.sid)
            assert session is not None
            self.cursor = session.cursor
            self.token = "tok"
            store.set_meta(team_service.compact_arm_key(self.sid), self.token)
        state = paths.manager_state_file(project.id)
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text("goal: ship\n", encoding="utf-8")
        when = datetime.now(tz=UTC).timestamp()
        os.utime(state, (when, when))
        config = load_config()
        config.fleet.quiet_compact_at = 400_000
        save_config(config)


@pytest.fixture
def manager(project: ProjectInfo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(quiet_compact, "_sleep", lambda seconds: None)
    monkeypatch.setattr(fleet_service, "quiet_prompt_refusal", lambda agent: None)
    return CodexManager(project, tmp_path)


def _typing(monkeypatch: pytest.MonkeyPatch, manager: CodexManager, *, compacts: bool) -> list:
    """``type_line`` as the pane takes it: the line recorded, and Codex's ``compacted``
    record appended to its rollout when it compacts."""
    typed: list[str] = []

    def type_line(agent: FleetAgent, text: str, *, acked: Any) -> delivery.Delivered:
        typed.append(text)
        if compacts:
            stamp = (datetime.now(tz=UTC) + timedelta(seconds=1)).isoformat()
            record = {"timestamp": stamp.replace("+00:00", "Z"), "type": "compacted",
                      "payload": {"message": "summary"}}  # fmt: skip
            with manager.rollout.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        return delivery.Delivered(delivery.TellResult("yes", "typed"), None, 1)

    monkeypatch.setattr(fleet_service, "type_line", type_line)
    return typed


def test_a_codex_manager_is_sent_the_bare_command_and_its_rollout_acknowledges_it(
    manager: CodexManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    typed = _typing(monkeypatch, manager, compacts=True)

    assert quiet_compact.check(manager.sid, manager.cursor, manager.token) == "compacted"

    assert typed == ["/compact"], "never `/compact <text>`, which Codex runs as a prompt"
    with store_session() as store:
        mark = team_service.read_quiet_mark(store, manager.sid)
    assert mark is not None and mark.acked_at is not None, "acknowledged off the rollout"


def test_no_compacted_record_is_no_compaction_and_the_board_says_so(
    manager: CodexManager, monkeypatch: pytest.MonkeyPatch, project: ProjectInfo
) -> None:
    typed = _typing(monkeypatch, manager, compacts=False)

    said = quiet_compact.check(manager.sid, manager.cursor, manager.token)

    assert typed == ["/compact"] and "no compaction followed" in said
    with store_session() as store:
        texts = [event.text for event in store.recent_events(project.id, limit=5)]
    assert any("no compaction started" in text for text in texts), texts


def test_a_codex_context_under_the_line_is_not_compacted(
    manager: CodexManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager.rollout.write_text(_usage(450_000) + _usage(120_000), encoding="utf-8")
    typed = _typing(monkeypatch, manager, compacts=True)

    said = quiet_compact.check(manager.sid, manager.cursor, manager.token)

    assert typed == [] and "120,000 tokens" in said, "its rollout's newest request decides"
