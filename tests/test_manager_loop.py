"""The manager loop's wake-ups (docs/plans/fleet-tui.md §7.3) and the fleet's briefings (§7.1).

There is no daemon. Two moments that already exist carry the wake-up:

1. THE MANAGER'S OWN ``Stop`` HOOK. For every role it marks the session waiting
   and prints nothing. For ``manager`` it first asks the board for events since
   the session cursor authored by OTHERS of the kinds in
   ``team.MANAGER_WAKE_KINDS``; if there are any, it prints the Stop block
   decision with the rendered delta as the reason, so Claude Code continues the
   turn with that context, and the cursor advances. Three loop guards:
   ``stop_hook_active``, the hourly cap in ``[fleet] max_continuations_per_hour``,
   and the cursor itself (the same events can never wake it twice).
2. A SUB-AGENT'S BOARD WRITE — ``task review|done|block|reopen``, a ``result`` or
   ``question`` note — nudges the project's waiting manager through
   ``services.fleet.nudge_manager`` after the write commits. The nudge carries no
   content; the delta the manager's next prompt injects does.

Every claim below has its control (CONTRIBUTING, "Writing a guard that still
guards"). A manager woken by a coder's review is evidence only because the same
events leave a coder silent, a Stop with nothing new waits, and the cap blocks at
N where N-1 did not. The Stop hook's stdout is asserted as bytes through the CLI,
not through the service, because ``hook stop``'s contract is "valid JSON or
empty" and only the CLI can break it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.hook import _COST
from aisquare.core import harness, paths
from aisquare.core.config import AppConfig, ExplainabilitySettings, load_config, save_config
from aisquare.core.ids import new_event_id
from aisquare.core.store import store_session
from aisquare.models import TeamEvent, TeamSession, TeamTask
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from aisquare.services.fleet import FLEET_ROLES

MANAGER = "aaaa1111-0000-0000-0000-000000000000"
CODER = "bbbb2222-0000-0000-0000-000000000000"

BLOCK = "block"

#: The scaffold's real ``nudge_manager``, captured at import time — BEFORE the
#: autouse recorder below replaces it — so one test can put it back and prove the
#: service reaches the real seam. Never ``monkeypatch.undo()`` for that: the same
#: monkeypatch carries ``isolated_home``, and undoing it points the test at the
#: developer's real ``~/.aisquare``.
_REAL_NUDGE = fleet_service.nudge_manager


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture(autouse=True)
def no_nudge(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Path 2 is stubbed here so path 1's tests are about path 1.

    The scaffold's ``nudge_manager`` is a no-op today; when the fleet WP lands it
    talks to tmux, and a Stop-hook test must not depend on a tmux server. The
    recorder doubles as the spy the nudge tests below read.
    """
    calls: list[tuple[str, str]] = []

    def record(project_id: str, *, reason: str) -> bool:
        calls.append((project_id, reason))
        return False

    monkeypatch.setattr(fleet_service, "nudge_manager", record)
    return calls


# --- helpers ------------------------------------------------------------------------


def _start(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, session_id: str, work: Path, role: str
) -> Any:
    """Register a session with a role, the way ``aisquare launch <role>`` does."""
    monkeypatch.setenv("AISQUARE_ROLE", role)
    payload = json.dumps({"cwd": str(work), "session_id": session_id, "source": "startup"})
    result = runner.invoke(app, ["hook", "session-start"], input=payload)
    monkeypatch.delenv("AISQUARE_ROLE")
    assert result.exit_code == 0, result.output
    return result


def _stop(runner: CliRunner, session_id: str, work: Path, **fields: Any) -> Any:
    payload: dict[str, Any] = {"cwd": str(work), "session_id": session_id, **fields}
    return runner.invoke(app, ["hook", "stop"], input=json.dumps(payload))


def _prompt(runner: CliRunner, session_id: str, work: Path) -> Any:
    payload = json.dumps({"cwd": str(work), "session_id": session_id, "prompt": "go"})
    return runner.invoke(app, ["hook", "user-prompt-submit"], input=payload)


def _session(ref: str) -> TeamSession:
    with store_session() as store:
        session = store.get_session(ref)
    assert session is not None, ref
    return session


def _meta(key: str) -> str | None:
    with store_session() as store:
        return store.get_meta(key)


def _set_meta(key: str, value: str) -> None:
    with store_session() as store:
        store.set_meta(key, value)


def _latest_seq(project_id: str) -> int:
    with store_session() as store:
        return store.latest_seq(project_id)


def _counter_key(session_id: str) -> str:
    return team_service.continuation_key(session_id, datetime.now(tz=UTC))


def _task(work: Path, title: str = "wire auth") -> TeamTask:
    """A coder task on the board (``add_task`` returns ``(task, created)``)."""
    task, _created = team_service.add_task(title, role="coder", cwd=work)
    return task


def _coder_reviews(work: Path, title: str = "wire auth") -> str:
    """A coder hands a task to review — the canonical event that wakes a manager."""
    task = _task(work, title)
    team_service.review_task(task.id, note="tests green", session_ref=CODER)
    return task.id


def _emit_from_coder(kind: str, text: str = "x") -> int:
    """An event of an arbitrary kind authored by the coder — for kinds only the
    fleet emits (``agent_exited``) and for the negative controls."""
    session = _session(CODER)
    with store_session() as store:
        stored = store.add_team_event(
            TeamEvent(
                id=new_event_id(),
                project_id=session.project_id,
                session_id=session.id,
                kind=kind,
                text=text,
                created_at=datetime.now(tz=UTC),
            )
        )
    return stored.seq


def _decision(result: Any) -> dict[str, Any]:
    """The hook's stdout as the object Claude Code will parse — one line, JSON."""
    assert result.exit_code == 0, result.output
    assert result.stdout.strip(), "the hook printed nothing"
    assert result.stdout.count("\n") == 1, "stdout must be exactly one JSON line"
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict)
    return parsed


def _fleet(runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work: Path) -> None:
    _start(runner, monkeypatch, MANAGER, work, "manager")
    _start(runner, monkeypatch, CODER, work, "coder")


# --- path 1: the manager's Stop hook ------------------------------------------------


def test_a_coders_review_keeps_the_manager_going(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """The whole claim: block decision, the delta as the reason, cursor and counter moved."""
    _fleet(runner, monkeypatch, work_dir)
    before = _session(MANAGER)
    task_id = _coder_reviews(work_dir)

    result = _stop(runner, MANAGER, work_dir, stop_hook_active=False)

    decision = _decision(result)
    assert decision["decision"] == BLOCK
    assert set(decision) == {"decision", "reason"}, "the documented Stop shape, nothing extra"
    reason = decision["reason"]
    assert reason, "an empty reason continues the turn with no instruction"
    assert "bbbb2222 (coder) review:" in reason and task_id in reason and "tests green" in reason
    assert reason.endswith(team_service.WAKEUP_CLOSE)
    assert result.stderr.strip() == "", "a healthy wake-up says nothing on stderr"
    after = _session(MANAGER)
    assert after.state == "working"
    assert after.cursor == _latest_seq(after.project_id) > before.cursor
    assert _meta(_counter_key(MANAGER)) == "1"


def test_the_same_events_can_never_wake_it_twice(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """The cursor is the third loop guard: a second Stop with nothing new waits."""
    _fleet(runner, monkeypatch, work_dir)
    _coder_reviews(work_dir)
    assert _decision(_stop(runner, MANAGER, work_dir))["decision"] == BLOCK

    again = _stop(runner, MANAGER, work_dir)

    assert again.exit_code == 0 and again.stdout == "", again.output
    assert _session(MANAGER).state == "waiting"
    assert _meta(_counter_key(MANAGER)) == "1", "no continuation, no count"


def test_nothing_new_means_waiting_exactly_as_before(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    _fleet(runner, monkeypatch, work_dir)
    before = _session(MANAGER)

    result = _stop(runner, MANAGER, work_dir)

    assert result.exit_code == 0
    assert result.stdout == "" and result.stderr == ""
    after = _session(MANAGER)
    assert after.state == "waiting" and after.cursor == before.cursor
    assert _meta(_counter_key(MANAGER)) is None


def test_stop_hook_active_defers_the_wakeup_to_the_next_prompt(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    work_dir: Path,
    no_nudge: list[tuple[str, str]],
) -> None:
    """Claude Code is already continuing on a stop hook: wait, and lose nothing.

    The second half is the point of "defers": the events stay past the cursor,
    so the next prompt's delta — the path that already existed — delivers them.
    And a next prompt is SCHEDULED rather than hoped for: the deferral issues
    path 2's nudge itself, because nothing else will (see
    ``test_the_stop_that_ends_a_continuation_schedules_the_rest`` below).
    """
    _fleet(runner, monkeypatch, work_dir)
    before = _session(MANAGER)
    project_id = before.project_id
    task_id = _coder_reviews(work_dir)
    no_nudge.clear()  # the coder's own write-time nudge is not what is under test

    result = _stop(runner, MANAGER, work_dir, stop_hook_active=True)

    assert result.exit_code == 0 and result.stdout == "", result.output
    after = _session(MANAGER)
    assert after.state == "waiting" and after.cursor == before.cursor
    assert _meta(_counter_key(MANAGER)) is None
    assert no_nudge == [(project_id, "deferred:task_review")]
    delta = _prompt(runner, MANAGER, work_dir).stdout
    assert task_id in delta and "review" in delta, "the deferred events reached the next prompt"


def test_a_deferring_stop_with_nothing_pending_nudges_nobody(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    work_dir: Path,
    no_nudge: list[tuple[str, str]],
) -> None:
    """The negative control for the deferral's nudge: an empty board schedules
    nothing, so a manager that simply finished a continuation is left alone."""
    _fleet(runner, monkeypatch, work_dir)
    _emit_from_coder("note", "just saying")

    result = _stop(runner, MANAGER, work_dir, stop_hook_active=True)

    assert result.exit_code == 0 and result.stdout == "", result.output
    assert _session(MANAGER).state == "waiting"
    assert no_nudge == [], "a plain note is news; nothing is waiting for the manager"


def test_muted_deltas_mute_the_deferred_nudge_too(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    work_dir: Path,
    no_nudge: list[tuple[str, str]],
) -> None:
    """The nudge's whole payload is the delta the next prompt injects, so with
    that door shut (``AISQUARE_TEAM_DELTA=0``) a nudge would be pure noise."""
    _fleet(runner, monkeypatch, work_dir)
    _coder_reviews(work_dir)
    no_nudge.clear()
    monkeypatch.setenv("AISQUARE_TEAM_DELTA", "0")

    result = _stop(runner, MANAGER, work_dir, stop_hook_active=True)

    assert result.exit_code == 0 and result.stdout == "", result.output
    assert _session(MANAGER).state == "waiting"
    assert no_nudge == []


@pytest.mark.parametrize("value", [True, "true", "false", 1])
def test_any_present_value_but_false_counts_as_active(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path, value: object
) -> None:
    """A loop guard misread as "not continuing" opens a loop; misread the other
    way it costs one deferred wake-up. So a stray string is read as active."""
    _fleet(runner, monkeypatch, work_dir)
    _coder_reviews(work_dir)

    result = _stop(runner, MANAGER, work_dir, stop_hook_active=value)

    assert result.stdout == "", (value, result.output)


@pytest.mark.parametrize("value", [False, None, 0])
def test_false_null_and_zero_are_not_active(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path, value: object
) -> None:
    """The control for the rule above, plus an absent field: an older payload
    without ``stop_hook_active`` must still let the manager be woken."""
    _fleet(runner, monkeypatch, work_dir)
    _coder_reviews(work_dir)

    result = _stop(runner, MANAGER, work_dir, stop_hook_active=value)

    assert _decision(result)["decision"] == BLOCK


def test_an_absent_field_is_not_active(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    _fleet(runner, monkeypatch, work_dir)
    _coder_reviews(work_dir)

    assert _decision(_stop(runner, MANAGER, work_dir))["decision"] == BLOCK


def test_the_hourly_cap_blocks_at_n_and_not_at_n_minus_one(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """Both sides of the comparison, or a ``<=`` would pass as a ``<``."""
    _fleet(runner, monkeypatch, work_dir)
    cap = team_service.continuation_cap()
    assert cap == load_config().fleet.max_continuations_per_hour == 30, "the plan's default"
    key = _counter_key(MANAGER)
    _coder_reviews(work_dir, "first")
    _set_meta(key, str(cap))

    capped = _stop(runner, MANAGER, work_dir)

    assert capped.exit_code == 0 and capped.stdout == "", capped.output
    assert _session(MANAGER).state == "waiting"
    assert _meta(key) == str(cap), "a refused continuation is not counted"

    _set_meta(key, str(cap - 1))
    allowed = _stop(runner, MANAGER, work_dir)

    assert _decision(allowed)["decision"] == BLOCK
    assert _meta(key) == str(cap)


def test_the_counter_is_per_hour_and_per_session(work_dir: Path) -> None:
    """The key the cap hangs on: UTC hour, converted; one manager's count is its own."""
    noon_utc = datetime(2026, 8, 28, 12, 30, tzinfo=UTC)

    key = team_service.continuation_key(MANAGER, noon_utc)

    assert key == f"continuations:{MANAGER}:2026-08-28T12"
    assert team_service.continuation_key(CODER, noon_utc) != key
    assert team_service.continuation_key(MANAGER, noon_utc.replace(minute=59)) == key
    assert team_service.continuation_key(MANAGER, noon_utc.replace(hour=13)) != key
    from datetime import timedelta, timezone

    plus_two = noon_utc.astimezone(timezone(timedelta(hours=2)))  # 14:30+02:00 is 12:30Z
    assert team_service.continuation_key(MANAGER, plus_two) == key


def test_the_cap_is_a_default_the_config_changes(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """§3.10: ``[fleet] max_continuations_per_hour = 1`` allows one, then waits;
    ``0`` switches the wake-up off without touching the rest of the hook."""
    config = AppConfig()
    config.fleet.max_continuations_per_hour = 1
    save_config(config)
    _fleet(runner, monkeypatch, work_dir)
    _coder_reviews(work_dir, "first")
    assert _decision(_stop(runner, MANAGER, work_dir))["decision"] == BLOCK

    _coder_reviews(work_dir, "second")
    second = _stop(runner, MANAGER, work_dir)

    assert second.stdout == "", second.output
    assert _session(MANAGER).state == "waiting"

    config.fleet.max_continuations_per_hour = 0
    save_config(config)
    _set_meta(_counter_key(MANAGER), "0")
    off = _stop(runner, MANAGER, work_dir)

    assert off.stdout == "" and off.exit_code == 0, off.output
    assert _session(MANAGER).state == "waiting"


def test_a_garbage_counter_costs_the_count_not_the_wakeup(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    _fleet(runner, monkeypatch, work_dir)
    _coder_reviews(work_dir)
    _set_meta(_counter_key(MANAGER), "not a number")

    result = _stop(runner, MANAGER, work_dir)

    assert _decision(result)["decision"] == BLOCK
    assert _meta(_counter_key(MANAGER)) == "1"


@pytest.mark.parametrize("role", ["coder", "planner", "runner", "tester", "reviewer", "validator"])
def test_every_other_role_stays_silent_on_the_same_events(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path, role: str
) -> None:
    """THE CONTROL. Fresh wake-kind events from a teammate; a non-manager's Stop is
    byte-identical to what it always was: no stdout, waiting, cursor untouched."""
    _start(runner, monkeypatch, MANAGER, work_dir, role)
    _start(runner, monkeypatch, CODER, work_dir, "coder")
    before = _session(MANAGER)
    _coder_reviews(work_dir)
    _emit_from_coder("question", "which auth?")

    result = _stop(runner, MANAGER, work_dir, stop_hook_active=False)

    assert result.exit_code == 0
    assert result.stdout == "" and result.stderr == ""
    after = _session(MANAGER)
    assert after.state == "waiting" and after.cursor == before.cursor
    assert _meta(_counter_key(MANAGER)) is None


def test_the_managers_own_writes_do_not_wake_it(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """Authored by OTHERS: a manager's own READY note is not news to it."""
    _fleet(runner, monkeypatch, work_dir)
    team_service.add_note("READY: PR #1", kind="result", session_ref=MANAGER, cwd=work_dir)

    result = _stop(runner, MANAGER, work_dir)

    assert result.stdout == "", result.output
    assert _session(MANAGER).state == "waiting"


def test_the_wake_kinds_are_the_plans() -> None:
    """A literal pin of §7.3's list, so a kind cannot quietly join or leave it."""
    assert {
        "task_review",
        "task_done",
        "task_blocked",
        "task_reopened",
        "result",
        "question",
        "agent_exited",
    } == team_service.MANAGER_WAKE_KINDS


@pytest.mark.parametrize("kind", sorted(team_service.MANAGER_WAKE_KINDS))
def test_each_wake_kind_wakes(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path, kind: str
) -> None:
    """Including ``agent_exited``, which only the fleet's ``reap`` emits."""
    _fleet(runner, monkeypatch, work_dir)
    _emit_from_coder(kind, "coder-auth exited (1)")

    decision = _decision(_stop(runner, MANAGER, work_dir))

    assert decision["decision"] == BLOCK
    assert kind.removeprefix("task_") in decision["reason"]


@pytest.mark.parametrize("kind", ["note", "decision", "task_claimed", "task_added", "attention"])
def test_news_that_is_not_a_decision_does_not_wake(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path, kind: str
) -> None:
    """The negative half of the kind filter: without it the cheapest rule wakes on everything."""
    _fleet(runner, monkeypatch, work_dir)
    _emit_from_coder(kind, "just saying")

    result = _stop(runner, MANAGER, work_dir)

    assert result.stdout == "", (kind, result.output)
    assert _session(MANAGER).state == "waiting"


def test_the_reason_is_the_whole_delta_not_only_the_trigger(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """A plain note that arrived before the review rides along and is consumed:
    the manager continues its turn and gets no prompt delta for it otherwise."""
    _fleet(runner, monkeypatch, work_dir)
    team_service.add_note("heads-up: touching the router", session_ref=CODER, cwd=work_dir)
    _coder_reviews(work_dir)

    decision = _decision(_stop(runner, MANAGER, work_dir))

    assert "touching the router" in decision["reason"]
    assert _session(MANAGER).cursor == _latest_seq(_session(MANAGER).project_id)


def test_the_reason_is_bounded_and_the_rest_waits_its_turn(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """Claude Code caps hook output at 10,000 characters; the delta limit keeps
    the reason far under it, and the cursor stops at what was shown so the
    remainder is still pending afterwards rather than vanishing.

    The second Stop here is a Stop of a turn the manager began on its own — no
    ``stop_hook_active`` — which is why it wakes again. The Stop that ends the
    CONTINUATION is a different payload and has its own test below.
    """
    _fleet(runner, monkeypatch, work_dir)
    seqs = [_emit_from_coder("question", f"question number {i}") for i in range(15)]

    decision = _decision(_stop(runner, MANAGER, work_dir))

    reason = decision["reason"]
    assert reason.count("\n- ") == team_service._DELTA_LIMIT
    assert "question number 9" in reason and "question number 10" not in reason
    assert "more waiting" in reason and "aisquare board" in reason
    assert reason.endswith(team_service.WAKEUP_CLOSE)
    assert len(reason) < 10_000
    assert _session(MANAGER).cursor == seqs[team_service._DELTA_LIMIT - 1]

    leftover = _decision(_stop(runner, MANAGER, work_dir))

    assert "question number 14" in leftover["reason"]
    assert _session(MANAGER).cursor == seqs[-1]
    assert _meta(_counter_key(MANAGER)) == "2"


def test_the_stop_that_ends_a_continuation_schedules_the_rest(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    work_dir: Path,
    no_nudge: list[tuple[str, str]],
) -> None:
    """The payload sequence Claude Code really produces after a block — and the
    one the truncation depends on.

    A block continues the turn; the Stop that ends THAT turn is exactly the Stop
    Claude Code marks ``stop_hook_active`` (``cli/hook.py``: "Claude Code saying
    it is already continuing on a stop hook"). So "the remainder wakes it on its
    next Stop" is not true of the next Stop, and the remainder's own write-time
    nudges were already refused while the manager was working. At the end of a
    burst — every coder finished — no further board write is coming, and the
    manager would park as waiting with five unseen questions past its cursor and
    nothing at all scheduled to deliver them. What is scheduled instead is path
    2's nudge, which produces the ``UserPromptSubmit`` the delta rides on.
    """
    _fleet(runner, monkeypatch, work_dir)
    project_id = _session(MANAGER).project_id
    seqs = [_emit_from_coder("question", f"question number {i}") for i in range(15)]
    assert _decision(_stop(runner, MANAGER, work_dir))["decision"] == BLOCK
    no_nudge.clear()

    parked = _stop(runner, MANAGER, work_dir, stop_hook_active=True)

    assert parked.exit_code == 0 and parked.stdout == "", parked.output
    assert _session(MANAGER).state == "waiting"
    assert _session(MANAGER).cursor == seqs[team_service._DELTA_LIMIT - 1], "nothing consumed"
    assert no_nudge == [(project_id, "deferred:question")]
    delta = _prompt(runner, MANAGER, work_dir).stdout
    assert "question number 10" in delta and "question number 14" in delta
    assert _session(MANAGER).cursor == seqs[-1], "the nudge's prompt consumed the remainder"


def test_muted_deltas_mute_the_wakeup_too(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """It is the same injection through a different door; ``AISQUARE_TEAM_DELTA=0``
    means the operator asked for no teammate context in this session."""
    _fleet(runner, monkeypatch, work_dir)
    _coder_reviews(work_dir)
    monkeypatch.setenv("AISQUARE_TEAM_DELTA", "0")

    result = _stop(runner, MANAGER, work_dir)

    assert result.stdout == "", result.output
    assert _session(MANAGER).state == "waiting"


def test_stdout_is_json_or_empty_on_both_branches(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """The contract ``test_json_stdout_is_machine_readable`` holds for every command;
    this is the one command where the JSON is read by Claude Code itself."""
    _fleet(runner, monkeypatch, work_dir)
    quiet = _stop(runner, MANAGER, work_dir)
    assert quiet.stdout == ""

    _coder_reviews(work_dir)
    loud = _stop(runner, MANAGER, work_dir)

    parsed = json.loads(loud.stdout)  # raises on anything that is not JSON
    assert parsed["decision"] == BLOCK


# --- path 1 fails open on its own -------------------------------------------------------


def test_a_wakeup_failure_costs_the_wakeup_and_says_so(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """§5.5: the row still says waiting; stderr names THIS cost, not the generic one
    (which would claim the board does not show it waiting — a wrong sentence)."""
    _fleet(runner, monkeypatch, work_dir)
    _coder_reviews(work_dir)

    def boom(*_a: object, **_kw: object) -> None:
        raise ValueError("the wake-up broke in a new way")

    monkeypatch.setattr(team_service, "_manager_wakeup", boom)

    result = _stop(runner, MANAGER, work_dir)

    assert result.exit_code == 0
    assert result.stdout == "", "a diagnostic on stdout would be parsed as the decision"
    assert _COST["manager-wakeup"] in result.stderr
    assert "the wake-up broke in a new way" in result.stderr
    assert "doctor" in result.stderr
    assert _COST["stop"] not in result.stderr
    assert len(result.stderr.strip().splitlines()) == 1, result.stderr
    assert _session(MANAGER).state == "waiting", "the contract held while the extra failed"


def test_a_store_shaped_wakeup_failure_names_the_file(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    _fleet(runner, monkeypatch, work_dir)
    _coder_reviews(work_dir)

    def locked(*_a: object, **_kw: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(team_service, "_manager_wakeup", locked)

    result = _stop(runner, MANAGER, work_dir)

    assert str(paths.db_path()) in result.stderr
    assert _COST["manager-wakeup"] in result.stderr


def test_the_wakeup_branch_is_not_reached_for_other_roles(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """The control for the two above: the same sabotage, a coder's Stop, silence."""
    _start(runner, monkeypatch, MANAGER, work_dir, "coder")
    _start(runner, monkeypatch, CODER, work_dir, "coder")
    _coder_reviews(work_dir)

    def boom(*_a: object, **_kw: object) -> None:
        raise ValueError("must not be reached")

    monkeypatch.setattr(team_service, "_manager_wakeup", boom)

    result = _stop(runner, MANAGER, work_dir)

    assert result.exit_code == 0 and result.stdout == "" and result.stderr == ""
    assert _session(MANAGER).state == "waiting"


def test_the_cost_line_is_registered_with_the_others() -> None:
    """``_COST`` is what the hook prints; a key that drifted from the plan's sentence
    would print a different cost than the one this file promises."""
    assert _COST["manager-wakeup"] == "the manager will not be woken by this turn's board updates"
    assert _COST["stop"] == "the board will not show this session as waiting for input"


# --- path 2: a sub-agent's board write nudges the manager -------------------------------


def test_review_done_block_and_reopen_each_nudge_once(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    work_dir: Path,
    no_nudge: list[tuple[str, str]],
) -> None:
    _fleet(runner, monkeypatch, work_dir)
    project_id = _session(MANAGER).project_id
    task = _task(work_dir)
    assert no_nudge == [], "adding a task is not a decision for the manager"

    team_service.review_task(task.id, note="ready", session_ref=CODER)
    team_service.reopen_task(task.id, reason="flaky test", session_ref=CODER)
    team_service.block_task(task.id, reason="needs spec", session_ref=CODER)
    team_service.finish_task(task.id, note="verified", session_ref=CODER)

    assert no_nudge == [
        (project_id, "task_review"),
        (project_id, "task_reopened"),
        (project_id, "task_blocked"),
        (project_id, "task_done"),
    ]


def test_result_and_question_notes_nudge_and_other_notes_do_not(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    work_dir: Path,
    no_nudge: list[tuple[str, str]],
) -> None:
    _fleet(runner, monkeypatch, work_dir)
    project_id = _session(MANAGER).project_id

    team_service.add_note("fyi", session_ref=CODER, cwd=work_dir)
    team_service.add_note("JWT it is", kind="decision", session_ref=CODER, cwd=work_dir)
    assert no_nudge == [], "a plain note or a decision is news, not a wake-up"

    team_service.add_note("which auth?", kind="question", session_ref=CODER, cwd=work_dir)
    team_service.add_note("GATE: PASS", kind="result", session_ref=CODER, cwd=work_dir)

    assert no_nudge == [(project_id, "question"), (project_id, "result")]


@pytest.mark.parametrize("addressed", ["manager", "Manager"])
def test_a_note_to_the_manager_nudges_it_whatever_its_kind(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    work_dir: Path,
    no_nudge: list[tuple[str, str]],
    addressed: str,
) -> None:
    """§7.3 path 2 lists ``note --to manager`` among the writes that nudge a
    waiting manager, and only the KIND was consulted here.

    So a note addressed to the manager reached a waiting one on no path at all:
    'note' is not a ``MANAGER_WAKE_KIND`` either, so its Stop hook did not
    deliver it and it sat past the cursor until some unrelated event arrived.
    ``fleet tell manager`` files exactly this shape."""
    _fleet(runner, monkeypatch, work_dir)
    project_id = _session(MANAGER).project_id

    team_service.add_note("look at the auth contract", session_ref=CODER, to_role=addressed)

    assert no_nudge == [(project_id, "note_to_manager")]


def test_a_note_to_someone_else_does_not_nudge_the_manager(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    work_dir: Path,
    no_nudge: list[tuple[str, str]],
) -> None:
    """The negative half: addressed mail wakes the addressee, and a rule that
    nudged on any ``--to`` would wake the manager for a coder's mail."""
    _fleet(runner, monkeypatch, work_dir)

    team_service.add_note("please rebase", session_ref=CODER, to_role="coder-1")
    team_service.add_note("no address at all", session_ref=CODER)

    assert no_nudge == []


def test_the_note_command_itself_nudges_the_manager(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    work_dir: Path,
    no_nudge: list[tuple[str, str]],
) -> None:
    """Through the CLI, because the plan names the COMMAND: ``note --to manager``.
    A service that nudges from a call the command never makes would pass the
    tests above and leave the plan's sentence false."""
    _fleet(runner, monkeypatch, work_dir)
    project_id = _session(MANAGER).project_id

    result = runner.invoke(
        app, ["note", "the auth contract changed", "--as", CODER, "--to", "manager"]
    )

    assert result.exit_code == 0, result.output
    assert no_nudge == [(project_id, "note_to_manager")]


def test_a_note_to_the_manager_also_wakes_it_at_its_next_stop(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """The other half of the same mailbox, and the reason the nudge alone is not
    enough: ``nudge_manager`` refuses a WORKING manager by design, which is
    precisely the state ``fleet tell`` files a note in. With 'note' outside
    MANAGER_WAKE_KINDS and the address ignored at Stop too, a message to a
    working manager had no delivery path in either direction."""
    _fleet(runner, monkeypatch, work_dir)

    team_service.add_note("the auth contract changed", session_ref=CODER, to_role="manager")

    decision = _decision(_stop(runner, MANAGER, work_dir))

    assert decision["decision"] == BLOCK
    assert "the auth contract changed" in decision["reason"]
    assert _session(MANAGER).state == "working"


def test_a_note_to_someone_else_still_leaves_the_manager_waiting(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """The negative control for the Stop half: unaddressed news and another
    agent's mail still arrive with the next prompt's delta, like everyone
    else's (``test_news_that_is_not_a_decision_does_not_wake`` covers the
    kinds; this one covers the address)."""
    _fleet(runner, monkeypatch, work_dir)

    team_service.add_note("please rebase", session_ref=CODER, to_role="coder-1")

    result = _stop(runner, MANAGER, work_dir)

    assert result.exit_code == 0 and result.stdout == "", result.output
    assert _session(MANAGER).state == "waiting"


def test_claims_releases_and_drops_do_not_nudge(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    work_dir: Path,
    no_nudge: list[tuple[str, str]],
) -> None:
    _fleet(runner, monkeypatch, work_dir)
    task = _task(work_dir)

    team_service.claim_task(task.id, session_ref=CODER)
    team_service.release_task(task.id, session_ref=CODER)
    team_service.drop_task(task.id, session_ref=CODER)

    assert no_nudge == []


def test_the_nudge_comes_after_the_write_is_durable(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """§7.3: "after the write commits". The spy reads the event back through a
    FRESH connection at the moment it is called."""
    _fleet(runner, monkeypatch, work_dir)
    task = _task(work_dir)
    seen: list[str] = []

    def read_back(project_id: str, *, reason: str) -> bool:
        with store_session() as store:
            kinds = [e.kind for e in store.recent_events(project_id, limit=1)]
        seen.extend(kinds)
        return True

    monkeypatch.setattr(fleet_service, "nudge_manager", read_back)

    team_service.review_task(task.id, session_ref=CODER)

    assert seen == ["task_review"]


def test_a_fleet_that_raises_costs_only_the_nudge(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """The nudge runs inside the CODER's process; its failure must not fail the write."""
    _fleet(runner, monkeypatch, work_dir)
    task = _task(work_dir)

    def broken(project_id: str, *, reason: str) -> bool:
        raise RuntimeError("tmux server gone")

    monkeypatch.setattr(fleet_service, "nudge_manager", broken)

    reviewed = team_service.review_task(task.id, note="ready", session_ref=CODER)

    assert reviewed.status == "review"
    delivery = team_service.last_delivery()
    assert delivery is not None and delivery.seq is not None, "the write still has its receipt"


def test_a_fleet_without_the_verb_is_a_no_op(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """Until every WP is merged, ``services.fleet`` may predate ``nudge_manager``."""
    _fleet(runner, monkeypatch, work_dir)
    task = _task(work_dir)
    monkeypatch.delattr(fleet_service, "nudge_manager")

    assert team_service.finish_task(task.id, session_ref=CODER).status == "done"


def test_the_spy_is_pointed_at_the_real_seam(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """The nudge tests above hold only if the service resolves ``nudge_manager``
    through the module at call time — which is what lets ``monkeypatch`` see it.
    Without the autouse recorder the scaffold's real function runs and returns
    ``False``; with a recorder on the module, the recorder runs. Both halves."""
    _fleet(runner, monkeypatch, work_dir)
    task = _task(work_dir)
    monkeypatch.setattr(fleet_service, "nudge_manager", _REAL_NUDGE)
    assert fleet_service.nudge_manager("prj_x", reason="task_review") is False
    assert team_service.review_task(task.id, session_ref=CODER).status == "review"

    calls: list[str] = []

    def record(project_id: str, *, reason: str) -> bool:
        calls.append(reason)
        return True

    monkeypatch.setattr(fleet_service, "nudge_manager", record)
    team_service.reopen_task(task.id, reason="again", session_ref=CODER)

    assert calls == ["task_reopened"]


# --- the briefings (§7.1, §3.3) --------------------------------------------------------


def _cycle(role: str) -> str:
    return " ".join(harness.role_cycle(role, "abcd1234"))


def test_the_manager_cycle_names_fleet_spawn_and_the_two_prohibitions() -> None:
    text = _cycle("manager")

    assert text, "the manager has a briefing"
    assert "fleet spawn" in text
    assert "never write code" in text.lower()
    assert "never merge" in text.lower()
    assert "abcd1234" in text, "the commands are pre-filled with the session id"


def test_the_manager_cycle_carries_the_loop_protocol() -> None:
    """§7.1 end to end: intake, contracts, spawn per role, steer, gate, READY, ask the human."""
    text = _cycle("manager")

    for phrase in (
        "acceptance criteria",
        "task add",
        "--needs",
        "fleet spawn tester",
        "fleet spawn reviewer",
        "fleet spawn validator",
        "fleet tell",
        "fleet-paused",
        "READY:",
        "--kind question",
        "coder-auth, not coder-2",
    ):
        assert phrase in text, phrase


def test_the_tester_cycle_is_the_runners_shape() -> None:
    """`tester` is the fleet's name for `runner`: same lines, only the label differs."""
    tester = harness.role_cycle("tester", "abcd1234")
    runner_ = harness.role_cycle("runner", "abcd1234")

    assert tester and runner_
    assert tester[0].startswith("Your standing cycle (tester)")
    assert runner_[0].startswith("Your standing cycle (runner)")
    assert [line.replace("(tester)", "(runner)") for line in tester] == runner_
    assert tester != runner_, "the label does differ, so the comparison above did work"


def test_the_reviewer_is_read_only_and_reviews_on_the_pr() -> None:
    text = _cycle("reviewer")

    assert "READ-ONLY" in text
    assert "gh pr review" in text
    assert "never merge" in text.lower()
    assert "critical|major|minor|nit" in text


@pytest.mark.parametrize("role", ["planner", "coder", "runner", "validator"])
def test_the_existing_roles_did_not_inherit_the_fleet_verbs(role: str) -> None:
    """The control: the fleet vocabulary belongs to the manager's briefing alone."""
    text = _cycle(role)

    assert text, role
    assert "fleet spawn" not in text
    assert "never write code" not in text.lower()


def test_every_fleet_role_has_a_briefing() -> None:
    for role in FLEET_ROLES:
        lines = harness.role_cycle(role, "abcd1234")
        assert lines, role
        assert any("abcd1234" in line for line in lines), role


def test_the_manager_briefing_is_injected_at_session_start(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """§7.1's parenthetical: injected at SessionStart, through the hook, not pasted."""
    result = _start(runner, monkeypatch, MANAGER, work_dir, "manager")

    assert "Your standing cycle (manager)" in result.stdout
    assert "fleet spawn" in result.stdout
    assert "Never write code. Never merge." in result.stdout


# --- the roster registers every identity the fleet can emit (§3.3) ---------------------


def test_the_roster_default_registers_every_fleet_role() -> None:
    roles = ExplainabilitySettings().roles

    assert roles == ["planner", "coder", "runner", "manager", "tester", "reviewer", "validator"]
    assert roles[:3] == ["planner", "coder", "runner"], "the runbooks quote these three first"
    assert set(FLEET_ROLES) <= set(roles)


def test_every_harness_role_is_a_registered_identity() -> None:
    """Asked of the two modules, not of two literals: a role added to the harness
    without joining the roster would ship under a name the gateway 409s."""
    assert set(harness.ROLE_PROFILES) <= set(ExplainabilitySettings().roles)
