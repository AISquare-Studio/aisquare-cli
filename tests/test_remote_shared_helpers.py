"""The rules two remote lanes share live in one place, so a fix to one reaches both.

Review of #243, round 3 (13/13) found three kept twice: the audit trail's scrub
(``action_audit_excerpt`` re-did ``_audit_clean``), the API's ISO stamp
(``remote_push._push_iso`` was ``_iso_seconds`` line for line), and the stale-pane
check (``send-keys`` put ``fleet._pane_is_the_agent`` and the server's start together
by hand, as needs-you's own check did). They agreed on the day they were found; the
next character class, or the next restart signal, would have reached one copy only.
Each test teaches the shared rule something and checks that every user of it learned it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app as cli
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import remote_needs, remote_push, remote_server
from aisquare.services.remote_actions import (
    ACTION_AUDIT_EXCERPT,
    ActionLedger,
    action_audit_excerpt,
)
from aisquare.services.remote_server import RequestError, live_writes

BORN = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)


def test_a_tells_excerpt_is_scrubbed_by_the_trails_one_scrub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(remote_server, "_audit_clean", lambda text, limit: f"<{limit}:{text}>")
    assert action_audit_excerpt("yes,\ncommit it") == f"<{ACTION_AUDIT_EXCERPT}:yes,\ncommit it>"


def test_a_push_stamps_its_records_as_the_api_does() -> None:
    assert remote_push._push_iso is remote_server._iso_seconds
    at = datetime(2026, 10, 7, 12, 30, 15, 999_999, tzinfo=UTC) + timedelta(hours=2)
    assert remote_push._push_iso(at) == "2026-10-07T14:30:15+00:00"


def test_the_ledger_and_the_servers_own_stamps_are_the_apis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ledger's ``at`` and the stamp on every frame and audit line were the API's stamp
    kept twice more, by hand and without its turn to UTC: a clock in another zone put its
    own offset in them."""
    east = datetime(2026, 10, 7, 14, 30, 15, 999_999, tzinfo=timezone(timedelta(hours=2)))
    ledger = ActionLedger(clock=lambda: east)
    ledger.ledger_finish("dev_00000001", "rq-1", 200, {})
    monkeypatch.setattr(remote_server, "_remote_now", lambda: east)
    stamps = [ledger.ledger_recent("dev_00000001")[0]["at"], remote_server._stamp()]
    assert stamps == ["2026-10-07T12:30:15+00:00"] * 2
    monkeypatch.setattr(remote_server, "_iso_seconds", lambda at: "the rule, learned")
    ledger.ledger_finish("dev_00000001", "rq-2", 200, {})
    stamps = [ledger.ledger_recent("dev_00000001")[0]["at"], remote_server._stamp()]
    assert stamps == ["the rule, learned"] * 2


class Tmux:
    """The row's own server, its pane running the agent: nothing here refuses it."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, ...]] = []

    def started_at(self) -> datetime:
        return BORN - timedelta(hours=1)

    def pane_facts(self, pane_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            dead=False,
            dead_status=None,
            current_command="claude",
            server_started=self.started_at(),
            last_output=BORN,
        )

    def send_literal(self, pane_id: str, text: str) -> None:
        self.sent.append(("literal", pane_id, text))

    def send_keys(self, pane_id: str, *keys: str) -> None:
        self.sent.append(("keys", pane_id, *keys))


@pytest.fixture
def row(isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FleetAgent:
    """``coder-1``, live in the current project's pane ``%2``."""
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    assert CliRunner().invoke(cli, ["init", "--local", "--no-onboard", "--yes"]).exit_code == 0
    project: ProjectInfo = fleet_service.resolve_project(None)
    agent = FleetAgent(
        id="agt_coder-1",
        project_id=project.id,
        label="coder-1",
        role="coder",
        pane_id="%2",
        cwd=project.root,
        created_at=BORN,
    )
    with store_session() as store:
        store.upsert_fleet_agent(agent)
    return agent


def test_send_keys_and_needs_you_judge_the_rows_pane_by_one_rule(
    row: FleetAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The quick answers and the agent actions type on the strength of needs-you's
    ``pane_is_agent``; send-keys asks before it types. A rule the shared check learns,
    here a refusal the real checks would not make, is one both apply."""
    tmux = Tmux()
    server: Any = tmux
    now = BORN + timedelta(minutes=5)
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    send = live_writes().handlers["send-keys"]
    assert send({"agent": "coder-1", "keys": ["1"]})[0]["sent"] is True
    assert remote_needs._needs_pane_now(server, row, now) == (True, True)
    asked: list[str] = []

    def learned(agent: FleetAgent, facts: object) -> str:
        asked.append(agent.id)
        return "{label}'s pane answers for a server that restarted in a new way"

    monkeypatch.setattr(remote_server, "_remote_facts_refusal", learned)
    with pytest.raises(RequestError) as refused:
        send({"agent": "coder-1", "keys": ["1"]})
    assert (refused.value.status, refused.value.error) == (409, "not_agent")
    assert refused.value.message == (
        "coder-1's pane answers for a server that restarted in a new way — nothing was sent"
    )
    assert remote_needs._needs_pane_now(server, row, now) == (False, None)
    assert asked == [row.id, row.id]
    assert tmux.sent == [("keys", "%2", "1")], "only the key sent before the rule changed"
