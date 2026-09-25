"""``aisquare captain`` verbs (card T5): the owner's own hands on the captain's tools.

Every test runs a verb through the Typer app on a fixture home — a real store in
the isolated ``AISQUARE_HOME`` with one onboarded project, a live manager session
and its question — and reads the result back the two ways the owner will: the
human lines, and ``--json`` (the tool's own shape, pinned). The fleet is never
reached: ``list_agents`` answers an empty fleet, and ``wololo``'s two fleet calls
are recorders. The acceptance lines are the test names: a test per verb, the
``--json`` shapes pinned, and (in ``test_documented_commands``, which sweeps the
whole tree) the documented-commands guard.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import captain_verbs
from aisquare.cli.app import app
from aisquare.core.ids import new_agent_id, new_event_id, new_task_id
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import (
    FleetAgent,
    FleetAgentStatus,
    ProjectInfo,
    TeamEvent,
    TeamSession,
    TeamTask,
)
from aisquare.services import fleet as fleet_service
from aisquare.services.captain import actions
from aisquare.services.captain import state as captain_state
from aisquare.services.captain.errors import Refused
from tests.test_stubs import IMPLEMENTED

VERBS = ("attention", "next", "resolve", "snooze", "since", "log", "uav", "wololo", "bt", "actions")

ITEM_KEYS = {
    "id",
    "project",
    "project_name",
    "agent",
    "kind",
    "text",
    "first_seen",
    "last_seen",
    "count",
    "source_seq",
    "source_ref",
    "card",
    "status",
    "snoozed_until",
    "history",
}
SINCE_KEYS = {
    "project",
    "agent",
    "from_seq",
    "to_seq",
    "events",
    "truncated",
    "pane",
    "pane_error",
    "advanced",
    "action_seq",
}


@pytest.fixture(autouse=True)
def no_fleet(monkeypatch: pytest.MonkeyPatch) -> None:
    """The queue's refresh asks the fleet for every project's agents; here there are none."""
    monkeypatch.setattr(fleet_service, "list_agents", lambda project, *, live_only=True: [])


@pytest.fixture
def alpha(tmp_path: Path) -> ProjectInfo:
    """One onboarded project with a live manager that asked the owner a question."""
    root = tmp_path / "alpha"
    root.mkdir()
    info = team_project(root)
    now = datetime.now(UTC)
    with store_session() as store:
        store.onboard_project(info)
        store.upsert_session(
            TeamSession(
                id="mgr-alpha", project_id=info.id, role="manager", started_at=now, last_seen_at=now
            )
        )
        store.add_team_event(
            TeamEvent(
                id=new_event_id(),
                project_id=info.id,
                session_id="mgr-alpha",
                kind="question",
                text="Owner, approve the deploy of the release train?",
                created_at=now,
            )
        )
    return info


def run(runner: CliRunner, *argv: str) -> tuple[int, str]:
    result = runner.invoke(app, ["captain", *argv], catch_exceptions=False)
    return result.exit_code, result.output


def run_json(runner: CliRunner, *argv: str) -> tuple[int, Any]:
    result = runner.invoke(app, ["--json", "captain", *argv], catch_exceptions=False)
    return result.exit_code, json.loads(result.output)


def last_audit(project: str | None = None) -> dict[str, Any]:
    rows = captain_verbs.audit_log(project)
    assert rows, "no captain_action yet"
    return rows[-1]


# --- the queue verbs ---------------------------------------------------------------------------


def test_attention_lists_what_needs_you_and_audits_the_call(
    runner: CliRunner, alpha: ProjectInfo
) -> None:
    code, out = run(runner, "attention")
    assert code == 0, out
    assert "approve the deploy" in out and "question" in out and "alpha" in out
    code, data = run_json(runner, "attention")
    assert code == 0
    assert set(data) == {"items", "action_seq"}
    (item,) = data["items"]
    assert set(item) == ITEM_KEYS
    assert item["kind"] == "question" and item["project_name"] == "alpha"
    audit = last_audit()
    assert audit["tool"] == "attention" and audit["ok"] is True
    assert audit["utterance"] == "aisquare captain attention --limit 10"
    assert audit["board"] == captain_state.home_project().root.name


def test_attention_says_when_nothing_needs_you(runner: CliRunner) -> None:
    code, out = run(runner, "attention")
    assert code == 0 and "nothing needs you" in out
    assert run_json(runner, "attention")[1]["items"] == []


def test_next_is_item_one_and_nothing_when_the_queue_is_empty(
    runner: CliRunner, alpha: ProjectInfo
) -> None:
    code, data = run_json(runner, "next")
    assert code == 0 and set(data) == {"item", "action_seq"}
    assert data["item"]["kind"] == "question"
    code, out = run(runner, "next")
    assert code == 0 and "approve the deploy" in out and data["item"]["id"] in out
    run(runner, "resolve", data["item"]["id"], "spoken: approved")
    code, data = run_json(runner, "next")
    assert code == 0 and data["item"] is None
    assert "nothing needs you" in run(runner, "next")[1]


def test_resolve_records_how_and_the_item_leaves_the_list(
    runner: CliRunner, alpha: ProjectInfo
) -> None:
    (item,) = run_json(runner, "attention")[1]["items"]
    code, out = run(runner, "resolve", item["id"][:6], "told the manager: yes")
    assert code == 0 and f"resolved {item['id']}" in out and "action seq" in out
    assert run_json(runner, "attention")[1]["items"] == []
    # A second resolve is refused in the queue's words (#218 gate 1, blocker 3): no
    # silent second entry, and the refusal is audited like any other call.
    code, data = run_json(runner, "resolve", item["id"], "again")
    assert code == 1 and data["error"] == "refused"
    code, out = run(runner, "resolve", item["id"], "again")
    assert code == 1 and "already resolved" in out
    audit = last_audit()
    assert audit["tool"] == "resolve" and audit["ok"] is False
    assert audit["args"] == {"item": item["id"], "how": "again"}
    assert audit["utterance"] == f"aisquare captain resolve {item['id']} again"


def test_snooze_hides_an_item_for_the_given_minutes(runner: CliRunner, alpha: ProjectInfo) -> None:
    (item,) = run_json(runner, "attention")[1]["items"]
    code, out = run(runner, "snooze", item["id"], "--for", "5")
    assert code == 0 and "snoozed" in out and "5 min" in out
    assert run_json(runner, "attention")[1]["items"] == []
    code, data = run_json(runner, "snooze", item["id"], "-m", "7")
    assert code == 0 and data["item"]["status"] == "snoozed"
    assert data["item"]["snoozed_until"] is not None
    assert last_audit()["utterance"] == f"aisquare captain snooze {item['id']} --for 7"


def test_snooze_defaults_to_fifteen_minutes(runner: CliRunner, alpha: ProjectInfo) -> None:
    (item,) = run_json(runner, "attention")[1]["items"]
    assert run(runner, "snooze", item["id"])[1].count("15 min") == 1
    assert last_audit()["args"]["minutes"] == captain_verbs.DEFAULT_SNOOZE_MINUTES == 15


def test_uav_prints_the_sitrep_header_first_and_carries_the_busy_flag(
    runner: CliRunner, alpha: ProjectInfo
) -> None:
    code, out = run(runner, "uav")
    assert code == 0
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[0].strip() == captain_verbs.UAV_LINE == "UAV online"
    assert lines[1].strip() == "captain idle"
    assert "1 item(s) need you" in out and "approve the deploy" in out
    captain_state.set_busy(True)
    code, data = run_json(runner, "uav")
    assert code == 0
    assert set(data) == {"uav", "busy_since", "items", "action_seq"}
    assert data["uav"] == "online" and data["busy_since"] is not None
    assert len(data["items"]) == 1
    assert "captain thinking since" in run(runner, "uav")[1]
    assert last_audit()["utterance"] == "aisquare captain uav --limit 10"


# --- since and log ------------------------------------------------------------------------------


def test_since_reads_a_board_and_advance_moves_the_watermark(
    runner: CliRunner, alpha: ProjectInfo
) -> None:
    code, data = run_json(runner, "since", "alpha")
    assert code == 0 and set(data) == SINCE_KEYS
    assert data["from_seq"] is None and data["advanced"] is False
    assert [e["kind"] for e in data["events"]] == ["question"]
    first_to = data["to_seq"]
    code, out = run(runner, "since", "alpha")
    assert code == 0 and "1 event(s)" in out and "approve the deploy" in out
    code, data = run_json(runner, "since", "alpha", "--advance")
    assert code == 0 and data["advanced"] is True
    assert captain_state.watermark(alpha.id, None) == first_to
    code, data = run_json(runner, "since", "alpha")
    assert code == 0 and data["from_seq"] == first_to and data["events"] == []
    assert "watermark advanced" not in run(runner, "since", "alpha")[1]
    assert last_audit("alpha")["utterance"] == "aisquare captain since alpha"


def test_since_for_an_unknown_project_or_agent_is_a_refusal(
    runner: CliRunner, alpha: ProjectInfo
) -> None:
    code, out = run(runner, "since", "nowhere")
    assert code == 1 and "refused" in out
    code, data = run_json(runner, "since", "alpha", "--agent", "ghost")
    assert code == 1 and data["error"] == "refused"


def test_log_reads_the_audit_newest_last_and_narrows_to_one_board(
    runner: CliRunner, alpha: ProjectInfo
) -> None:
    run(runner, "attention")
    run(runner, "since", "alpha")
    run(runner, "next")
    code, data = run_json(runner, "log")
    assert code == 0 and set(data) == {"events"}
    tools = [row["tool"] for row in data["events"]]
    assert tools[-3:] == ["attention", "since", "next"]
    assert set(data["events"][-1]) >= {
        "seq",
        "at",
        "board",
        "board_id",
        "tool",
        "project",
        "args",
        "utterance",
        "ok",
        "said",
        "receipt",
    }
    code, data = run_json(runner, "log", "alpha")
    assert code == 0 and [row["tool"] for row in data["events"]] == ["since"]
    code, out = run(runner, "log", "-n", "2")
    assert code == 0 and "since" in out and "next" in out and "attention" not in out
    code, out = run(runner, "log", "nowhere")
    assert code == 1 and "refused" in out


def test_log_says_when_nothing_has_been_done(runner: CliRunner) -> None:
    code, out = run(runner, "log")
    assert code == 0 and "no captain actions yet" in out


# --- wololo, bt, actions ------------------------------------------------------------------------


def test_wololo_converts_an_idle_agent_and_refuses_a_missing_one(
    runner: CliRunner, alpha: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id="sess-coder-1",
                project_id=alpha.id,
                role="coder",
                started_at=now,
                last_seen_at=now,
            )
        )
        agent = store.upsert_fleet_agent(
            FleetAgent(
                id=new_agent_id(),
                project_id=alpha.id,
                label="coder-1",
                role="coder",
                pane_id="%7",
                session_id="sess-coder-1",
                cwd=alpha.root,
                created_at=now,
            )
        )
        tid = new_task_id()
        store.upsert_task(
            TeamTask(
                id=tid,
                project_id=alpha.id,
                key=tid,
                title="Rotate the token",
                created_at=now,
                updated_at=now,
            )
        )
    told: list[str] = []
    monkeypatch.setattr(
        fleet_service, "status_of", lambda row: FleetAgentStatus(agent=row, state="waiting")
    )

    def tell(project: ProjectInfo, label: str, text: str, *, sender: str | None = None) -> Any:
        told.append(f"{label}: {text}")
        return fleet_service.TellResult(True, "typed into its pane")

    monkeypatch.setattr(fleet_service, "tell", tell)
    code, out = run(runner, "wololo", "alpha", "coder-1", tid)
    assert code == 0, out
    assert f"Wololo! coder-1 converts to {tid}" in out and "action seq" in out
    assert told and tid in told[0]
    with store_session() as store:
        card = store.get_task(tid)
    assert card is not None and card.status == "doing" and card.claimed_by == agent.session_id
    audit = last_audit("alpha")
    assert audit["tool"] == "wololo" and audit["receipt"] is not None
    assert audit["utterance"] == f"aisquare captain wololo alpha coder-1 {tid}"
    code, data = run_json(runner, "wololo", "alpha", "nobody", tid)
    assert code == 1 and data["error"] == "refused"
    assert "no live agent nobody" in data["message"] if "message" in data else True


def test_bt_pulls_the_brake_and_says_what_it_did(runner: CliRunner) -> None:
    captain_state.enqueue_speech("on it")
    code, data = run_json(runner, "bt")
    assert code == 0
    assert set(data) == {"cancelled_wait", "speech_cleared", "undid", "said", "action_seq"}
    assert data["speech_cleared"] == 1 and data["undid"] is None
    assert captain_state.pending_speech() == []
    code, out = run(runner, "bt")
    assert code == 0 and "brake:" in out and "nothing to undo" in out
    assert last_audit()["tool"] == "bt"


def test_actions_lists_the_owner_action_list(runner: CliRunner) -> None:
    code, data = run_json(runner, "actions")
    assert code == 0 and set(data) == {"actions"}
    assert set(data["actions"]) >= {"approve_prompt", "unblock", "open_spawn"}
    assert data["actions"]["unblock"] == {
        "steps": ["press y", "read_pane 20"],
        "description": "bundled",
        "problem": None,
    }
    code, out = run(runner, "actions")
    assert code == 0 and "approve_prompt" in out and "press y" in out


# --- the frame around every verb -------------------------------------------------------------


def test_a_refusal_is_one_line_exit_1_and_an_error_object_under_json(runner: CliRunner) -> None:
    code, out = run(runner, "resolve", "nothing", "x")
    assert code == 1
    assert "✗ refused: no queue item matches 'nothing'" in out and "action seq" in out
    assert "Traceback" not in out
    code, data = run_json(runner, "resolve", "nothing", "x")
    assert code == 1 and data["error"] == "refused"
    audit = last_audit()
    assert audit["tool"] == "resolve" and audit["ok"] is False


def test_the_verbs_need_no_mcp_sdk(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """A base install (no [serve] extra) drives the captain from the terminal all the same."""
    for name in list(sys.modules):
        if name == "mcp" or name.startswith("mcp."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "mcp", None)  # an import now raises ImportError
    code, out = run(runner, "resolve", "nothing", "x")
    assert code == 1 and "refused: no queue item matches" in out
    code, data = run_json(runner, "attention")
    assert code == 0 and data["items"] == []


def test_every_verb_is_on_the_captain_group_and_implemented() -> None:
    from tests.cli_tree import all_command_paths

    paths = set(all_command_paths())
    for verb in VERBS:
        assert ("captain", verb) in paths, verb
        assert ("captain", verb) in IMPLEMENTED, verb


def test_perform_is_the_one_door_and_names_an_unknown_tool() -> None:
    with pytest.raises(Refused) as caught:
        actions.perform("teleport", {}, "aisquare captain teleport")
    assert "no tool named 'teleport'" in str(caught.value)
