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
from types import SimpleNamespace
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
from tests import captain_screens as shots
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
    "said",
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
    assert audit["utterance"] == "aisquare --json captain attention", "as typed, --json included"
    assert audit["args"]["via"] == "cli"
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
    code, data = run_json(runner, "resolve", item["id"][:6], "told the manager: yes")
    assert code == 0 and set(data) == {"item", "action_seq"}
    assert set(data["item"]) == ITEM_KEYS and data["item"]["status"] == "resolved"
    assert last_audit()["utterance"] == (
        f"aisquare --json captain resolve {item['id'][:6]} 'told the manager: yes'"
    ), "the argv as typed, quoting kept"
    assert run_json(runner, "attention")[1]["items"] == []
    # A second resolve is refused in the queue's words (#218 gate 1, blocker 3): no
    # silent second entry, and the refusal is audited like any other call.
    code, data = run_json(runner, "resolve", item["id"], "again")
    assert code == 1 and data["error"] == "refused"
    code, out = run(runner, "resolve", item["id"], "again")
    assert code == 1 and "already resolved" in out
    code, out = run(runner, "resolve", item["id"][:6], "second look")
    assert code == 1 and "already resolved" in out
    run(runner, "resolve", item["id"], "again")
    audit = last_audit()
    assert audit["tool"] == "resolve" and audit["ok"] is False
    assert audit["args"] == {"item": item["id"], "how": "again", "via": "cli"}
    assert audit["utterance"] == f"aisquare captain resolve {item['id']} again"


def test_snooze_hides_an_item_for_the_given_minutes(runner: CliRunner, alpha: ProjectInfo) -> None:
    (item,) = run_json(runner, "attention")[1]["items"]
    code, out = run(runner, "snooze", item["id"], "--for", "5")
    assert code == 0 and "snoozed" in out and "5 min" in out
    assert run_json(runner, "attention")[1]["items"] == []
    code, data = run_json(runner, "snooze", item["id"], "-m", "7")
    assert code == 0 and data["item"]["status"] == "snoozed"
    assert data["item"]["snoozed_until"] is not None
    assert last_audit()["utterance"] == f"aisquare --json captain snooze {item['id']} -m 7"
    assert set(data) == {"item", "action_seq"} and set(data["item"]) == ITEM_KEYS


def test_snooze_defaults_to_fifteen_minutes(runner: CliRunner, alpha: ProjectInfo) -> None:
    (item,) = run_json(runner, "attention")[1]["items"]
    assert run(runner, "snooze", item["id"])[1].count("15 min") == 1
    audit = last_audit()
    assert audit["args"]["minutes"] == captain_verbs.DEFAULT_SNOOZE_MINUTES == 15
    assert audit["utterance"] == f"aisquare captain snooze {item['id']}", "no flag it was not given"


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
    assert last_audit()["utterance"] == "aisquare captain uav"


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
    assert code == 0 and "1 event for alpha" in out and "approve the deploy" in out
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
    # the two reads above are audited too (13081: reads included), newest last
    code, data = run_json(runner, "log", "-n", "4")
    assert code == 0 and [row["tool"] for row in data["events"]] == ["since", "next", "log", "log"]
    code, out = run(runner, "log", "-n", "4")
    assert code == 0 and "next" in out and "attention" not in out
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

    class IdlePane:
        """T1c: wololo reads the agent's pane before any claim moves and refuses one it
        cannot read, so this agent sits at Claude Code's real idle box."""

        def capture(self, pane_id: str, **_: object) -> SimpleNamespace:
            assert pane_id == "%7"
            return SimpleNamespace(lines=list(shots.REAL_IDLE_AFTER_STOP))

    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: IdlePane())

    def tell(project: ProjectInfo, label: str, text: str, *, sender: str | None = None) -> Any:
        told.append(f"{label}: {text}")
        return fleet_service.TellResult(True, "typed into its pane")

    monkeypatch.setattr(fleet_service, "tell", tell)
    code, data = run_json(runner, "wololo", "alpha", "coder-1", tid)
    assert code == 0, data
    assert set(data) == {"label", "released", "claimed", "told", "said", "action_seq"}
    assert data["claimed"] == tid and data["said"] == f"Wololo! coder-1 converts to {tid}"
    assert told and tid in told[0]
    with store_session() as store:
        card = store.get_task(tid)
    assert card is not None and card.status == "doing" and card.claimed_by == agent.session_id
    audit = last_audit("alpha")
    assert audit["tool"] == "wololo" and audit["receipt"] is not None
    assert audit["utterance"] == f"aisquare --json captain wololo alpha coder-1 {tid}"
    code, data = run_json(runner, "wololo", "alpha", "nobody", tid)
    assert code == 1 and data["error"] == "refused"
    assert "no live agent" in data["detail"] and "nobody" in data["detail"]


def test_the_since_watermark_never_moves_backwards(alpha: ProjectInfo) -> None:
    """The CLI and the captain both advance it now; a slower writer must not rewind it."""
    captain_state.set_watermark(alpha.id, None, 10)
    captain_state.set_watermark(alpha.id, None, 7)
    assert captain_state.watermark(alpha.id, None) == 10
    captain_state.set_watermark(alpha.id, None, 12)
    assert captain_state.watermark(alpha.id, None) == 12


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
        "steps": ["press yes", "read_pane 20"],  # T1b: yes is read off the pane (13265)
        "description": "bundled",
        "problem": None,
    }
    code, out = run(runner, "actions")
    assert code == 0 and "approve_prompt" in out and "press yes" in out


# --- the frame around every verb -------------------------------------------------------------


def test_a_refusal_is_one_line_exit_1_and_an_error_object_under_json(runner: CliRunner) -> None:
    code, out = run(runner, "resolve", "nothing", "x")
    assert code == 1
    assert "✗ refused: no queue item matches 'nothing'" in out and "action seq" in out
    assert "Traceback" not in out
    code, data = run_json(runner, "resolve", "nothing", "x")
    assert code == 1 and data["error"] == "refused"
    assert "no queue item matches 'nothing'" in data["detail"], "the queue's words, kept"
    assert "action seq" in data["detail"]
    audit = last_audit()
    assert audit["tool"] == "resolve" and audit["ok"] is False


def test_a_failure_is_error_failed_and_a_refusal_error_refused(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The frame's split (Failed: said as error:, Refused: said as refused:) reaches --json."""
    from aisquare.services.captain import queue as captain_queue

    def locked(limit: int) -> list[dict[str, object]]:
        raise RuntimeError("queue.json is locked by another process")

    monkeypatch.setattr(captain_queue, "ranked", locked)
    code, data = run_json(runner, "attention")
    assert code == 1 and data["error"] == "failed"
    assert data["detail"].startswith("error: the attention queue failed")
    code, data = run_json(runner, "since", "nowhere")
    assert code == 1 and data["error"] == "refused"


def test_log_and_actions_are_audited_like_every_verb(runner: CliRunner) -> None:
    """13081: one captain_action per call, reads included — the audit of the audit too."""
    run(runner, "log")
    audit = last_audit()
    assert (audit["tool"], audit["ok"], audit["utterance"]) == ("log", True, "aisquare captain log")
    assert audit["args"]["via"] == "cli"
    run(runner, "actions")
    audit = last_audit()
    assert (audit["tool"], audit["utterance"]) == ("actions", "aisquare captain actions")


def test_the_everyday_cli_does_not_load_the_captain(tmp_path: Path) -> None:
    """Every aisquare command — a hook included — imports the CLI; the captain's actions,
    queue and state load only when a captain verb runs."""
    import os
    import subprocess

    probe = (
        "import sys, aisquare.cli.app\n"
        "heavy = ('aisquare.services.captain.actions', 'aisquare.services.captain.queue',"
        " 'aisquare.services.captain.state')\n"
        "print([m for m in heavy if m in sys.modules])"
    )
    env = {**os.environ, "AISQUARE_HOME": str(tmp_path / "home")}
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, env=env, check=True
    )
    assert out.stdout.strip() == "[]"


def test_uav_says_an_unreadable_state_file_rather_than_crashing(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable() -> None:
        raise PermissionError(13, "Permission denied", "state.json")

    monkeypatch.setattr(captain_state, "busy_since", unreadable)
    code, out = run(runner, "uav")
    assert code == 0 and "captain state unreadable" in out and "Traceback" not in out
    code, data = run_json(runner, "uav")
    assert code == 0 and data["busy_since"] is None and "Permission denied" in data["busy_error"]


def test_since_for_an_agent_that_never_joined_says_so_in_its_own_words(
    runner: CliRunner, alpha: ProjectInfo
) -> None:
    now = datetime.now(UTC)
    with store_session() as store:
        store.upsert_fleet_agent(
            FleetAgent(
                id=new_agent_id(), project_id=alpha.id, label="coder-9", role="coder",
                pane_id="%9", cwd=alpha.root, created_at=now,
            )
        )  # fmt: skip
    code, out = run(runner, "since", "alpha", "--agent", "coder-9")
    assert code == 0, out
    assert "coder-9 has not joined the board" in out and "None" not in out


def test_since_says_an_agent_never_joined_even_over_an_old_watermark(
    runner: CliRunner, alpha: ProjectInfo
) -> None:
    """A label reused by a new agent that has not joined: the tool's own line, never a
    bare "0 event(s)" span read off the old agent's watermark."""
    now = datetime.now(UTC)
    with store_session() as store:
        store.upsert_fleet_agent(
            FleetAgent(
                id=new_agent_id(), project_id=alpha.id, label="coder-9", role="coder",
                pane_id="%9", cwd=alpha.root, created_at=now,
            )
        )  # fmt: skip
    captain_state.set_watermark(alpha.id, "coder-9", 1)
    code, out = run(runner, "since", "alpha", "--agent", "coder-9")
    assert code == 0, out
    assert "coder-9 has not joined the board" in out and "None" not in out


def test_log_of_one_board_is_audited_on_that_board(runner: CliRunner, alpha: ProjectInfo) -> None:
    run(runner, "log", "alpha")
    code, data = run_json(runner, "log", "alpha")
    assert code == 0 and [row["tool"] for row in data["events"]] == ["log"]
    assert data["events"][-1]["project"] == alpha.id


def test_the_utterance_is_the_argv_as_typed(
    runner: CliRunner, alpha: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """13081: what the owner typed — the process's own argv when it carries this call, the
    root's flags included; a --json typed after the verb is recorded once, where it was."""
    typed = ["--no-color", "captain", "attention", "--limit", "3"]
    monkeypatch.setattr(sys, "argv", ["/usr/local/bin/aisquare", *typed])
    assert runner.invoke(app, typed, catch_exceptions=False).exit_code == 0
    assert last_audit()["utterance"] == "aisquare --no-color captain attention --limit 3"
    monkeypatch.setattr(sys, "argv", ["pytest"])
    # (--json right after `captain` is a message to the captain: T2's say-by-default.)
    assert runner.invoke(app, ["captain", "next", "--json"], catch_exceptions=False).exit_code == 0
    assert last_audit()["utterance"] == "aisquare captain next --json"


def test_the_one_captain_group_records_the_words_before_it_routes_a_message_to_say(
    runner: CliRunner, alpha: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """13350: one group for T2's say-by-default and T5's audit. The words are kept as typed
    BEFORE the rewrite to ``say`` (a later ``--voice`` rewrite goes after it too), a verb's
    name is never taken for a message, and ``say`` and ``chat`` still route."""
    from aisquare.services.captain import brain

    heard: list[tuple[str, tuple[str, ...]]] = []

    def say(text: str, *, timeout: float = 180.0) -> brain.Reply:
        heard.append((text, captain_verbs.TYPED.get()))
        return brain.Reply(text="all quiet")

    monkeypatch.setattr(brain, "say", say)
    for argv in (["what is up"], ["say", "what", "is", "up"], ["--timeout", "30", "hi"]):
        result = runner.invoke(app, ["captain", *argv], catch_exceptions=False)
        assert result.exit_code == 0 and "all quiet" in result.output, result.output
    result = runner.invoke(app, ["captain", "chat"], input="one\n\ntwo\n", catch_exceptions=False)
    assert result.exit_code == 0
    assert heard[:3] == [
        ("what is up", ("what is up",)),
        ("what is up", ("say", "what", "is", "up")),
        ("hi", ("--timeout", "30", "hi")),
    ]
    assert [text for text, _ in heard[3:]] == ["one", "two"]
    code, _ = run(runner, "attention")
    assert code == 0 and last_audit()["tool"] == "attention", "a verb is never a message"
    assert last_audit()["utterance"] == "aisquare captain attention"


def test_a_leading_option_looks_past_itself_and_the_first_word_decides(
    runner: CliRunner, alpha: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T5b (13437): where --json sits never changes which command runs. Typing into the
    captain is an outward effect, so it happens only when the first WORD is not a verb:
    options before it are skipped, each with its value. The audit keeps the argv as typed."""
    from aisquare.services.captain import brain

    heard: list[str] = []

    def say(text: str, *, timeout: float = 180.0) -> brain.Reply:
        heard.append(text)
        return brain.Reply(text="all quiet")

    monkeypatch.setattr(brain, "say", say)
    result = runner.invoke(app, ["captain", "--json", "next"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert heard == [], "the verb ran; nothing was typed into the captain"
    assert last_audit()["tool"] == "next"
    assert last_audit()["utterance"] == "aisquare captain --json next"
    result = runner.invoke(app, ["captain", "--json", "what", "is", "up"], catch_exceptions=False)
    assert result.exit_code == 0 and json.loads(result.output)["reply"] == "all quiet"
    result = runner.invoke(app, ["captain", "--timeout", "30", "next"], catch_exceptions=False)
    assert heard == ["what is up"], "an option's VALUE is skipped too: 30 is not the word"
    assert last_audit()["tool"] == "next"


def test_voice_among_the_leading_options_opens_the_page_wherever_it_sits(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """13445: the words are recorded, then T3's --voice rewrite, then say-by-default. --voice
    is the page wherever it sits among the options before the first word, the leaf's own
    options honoured; after ``--`` it is a word of a message."""
    from aisquare.cli import captain_voice
    from aisquare.services.captain import brain

    served: list[dict[str, Any]] = []
    monkeypatch.setattr("aisquare.services.captain.voice.serve", lambda **kw: served.append(kw))
    monkeypatch.setattr(captain_voice, "voice_dependency_error", lambda: None)
    heard: list[str] = []

    def say(text: str, *, timeout: float = 180.0) -> brain.Reply:
        heard.append(text)
        return brain.Reply(text="all quiet")

    monkeypatch.setattr(brain, "say", say)
    for argv in (
        ["--voice", "--mode", "listen"],
        ["--no-color", "--voice", "--mode", "listen"],
        ["--mode", "listen", "--voice"],  # the leaf's option's value is no word either
    ):
        result = runner.invoke(app, ["captain", *argv], catch_exceptions=False)
        assert result.exit_code == 0, result.output
    assert [call["mode"] for call in served] == ["listen"] * 3 and heard == []
    result = runner.invoke(app, ["captain", "--", "--voice", "is", "loud"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert heard == ["--voice is loud"] and len(served) == 3


def test_a_leading_option_alone_is_the_bare_captain_never_an_empty_say(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`captain --json` is `--json captain`: no word, so nothing is said (T2's bare rule)."""
    from aisquare.cli import fleet as fleet_cli
    from aisquare.services import fleet
    from aisquare.services.captain import brain

    row = FleetAgent(
        id="agt_c", project_id="prj_home", label="captain", role="captain",
        pane_id="%1", cwd=Path("/tmp"), created_at=datetime.now(tz=UTC),
    )  # fmt: skip
    monkeypatch.setattr(brain, "find", lambda: row)
    monkeypatch.setattr(brain, "say", lambda *a, **k: pytest.fail("said with no words"))
    monkeypatch.setattr(fleet_cli, "interactive_terminal", lambda: True)
    monkeypatch.setattr(fleet_cli, "_exec_attach", lambda argv: pytest.fail("exec'd under --json"))
    monkeypatch.setattr(fleet, "attach_argv", lambda project: ["tmux", "attach", "-t", "asq-h"])
    result = runner.invoke(app, ["captain", "--json"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["agent"]["id"] == "agt_c"


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
