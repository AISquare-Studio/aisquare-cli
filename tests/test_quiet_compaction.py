"""P1 WS-4 (#281): a quiet manager is compacted at a quiet point, and only there.

Every request re-sends a manager's whole context, and nothing in aisquare compacted it: it
grew until Claude Code's own auto or idle compaction fired, mid-plan or not. With ``[fleet]
quiet_compact_at`` set, a manager's Stop arms ``aisquare hook compact-check``, which waits,
asks every condition again, and types ``/compact`` only into an idle prompt with an empty
input box. The compaction's own SessionStart acknowledges it and puts the row back to
``waiting``, where a coder's ``task review`` still nudges it.

Driven as ``tests/test_first_prompt_delivery.py`` drives a typed delivery: the real store in
an isolated home, real git under ``tmp_path``, and a fake tmux that draws Claude Code's input
box (:class:`ClaudeCode`). Never a real ``claude``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths, selfcli
from aisquare.core import tmux as tmux_core
from aisquare.core.config import load_config, save_config
from aisquare.core.store import store_session
from aisquare.core.tmux import TmuxServer
from aisquare.models import FleetAgent, ProjectInfo, TeamSession
from aisquare.services import fleet as fleet_service
from aisquare.services import hooks as hooks_service
from aisquare.services import quiet_compact
from aisquare.services import team as team_service
from tests import test_fleet_service as fleet_suite
from tests.test_first_prompt_delivery import ClaudeCode, _box
from tests.test_fleet_service import FakeClock

clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project

AT = 400_000

#: The real nudge, captured before :func:`no_nudge` replaces it: one test puts it back to show
#: the compacted manager is still one a review wakes.
_REAL_NUDGE = fleet_service.nudge_manager


@pytest.fixture(autouse=True)
def no_nudge(monkeypatch: pytest.MonkeyPatch) -> None:
    """A board write's nudge would type into the manager's pane, which these tests watch."""
    monkeypatch.setattr(fleet_service, "nudge_manager", lambda *a, **k: False)


@pytest.fixture
def tmux(monkeypatch: pytest.MonkeyPatch) -> ClaudeCode:
    """``tests/test_fleet_service.py``'s ``tmux``, drawing Claude Code's input box."""
    fake = ClaudeCode()

    def factory(config: object = None) -> TmuxServer:
        return fake

    monkeypatch.setattr(fleet_service, "server", factory)
    monkeypatch.setattr(tmux_core, "desktop_environment", lambda environ=None: {})
    return fake


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The worker's waits, recorded instead of slept."""
    waits: list[float] = []
    monkeypatch.setattr(quiet_compact, "_sleep", waits.append)
    return waits


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """What the Stop hook detached: the worker's argv, recorded instead of started."""
    started: list[list[str]] = []
    monkeypatch.setattr(hooks_service, "_detach", started.append)
    return started


def _quiet_compact_at(tokens: int) -> None:
    config = load_config()
    config.fleet.quiet_compact_at = tokens
    save_config(config)


def _transcript(path: Path, tokens: int) -> None:
    """A transcript whose last request was ``tokens`` in: the manager's context now."""
    usage = {"input_tokens": 10, "cache_read_input_tokens": tokens - 10}
    entry = {"type": "assistant", "message": {"model": "claude-opus-5-5", "usage": usage}}
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")


class Manager:
    """A fleet manager in its pane: its row, its session, its transcript and state file."""

    def __init__(self, project: ProjectInfo, tmux: ClaudeCode, tmp_path: Path) -> None:
        self.project = project
        self.tmux = tmux
        self.agent: FleetAgent = fleet_service.spawn(project, "manager").agent
        assert self.agent.session_id is not None
        self.sid: str = self.agent.session_id
        tmux.set_command(self.agent.pane_id, "claude")
        tmux.screens[self.agent.pane_id] = _box()
        self.transcript = tmp_path / "manager.jsonl"
        _transcript(self.transcript, 450_000)
        self.state = paths.manager_state_file(project.id)
        self.write_state()
        self.start("startup")

    def env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AISQUARE_FLEET_AGENT", self.agent.id)
        monkeypatch.setenv("AISQUARE_ROLE", "manager")

    def start(self, source: str) -> str:
        with pytest.MonkeyPatch.context() as patch:
            self.env(patch)
            return team_service.hook_session_start(
                self.sid, self.project.root, source, transcript_path=str(self.transcript)
            )

    def write_state(self, *, ago: timedelta = timedelta(0)) -> None:
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text("goal: ship WS-4\nnext: review #16\n", encoding="utf-8")
        when = (datetime.now(tz=UTC) - ago).timestamp()
        os.utime(self.state, (when, when))

    def stop(self, armed: list[list[str]], **fields: str) -> int:
        """Its Stop: the row waiting, the worker armed; returns the cursor it was armed at."""
        with pytest.MonkeyPatch.context() as patch:
            self.env(patch)
            decision = hooks_service.turn_stopped(
                self.project.root,
                session_id=self.sid,
                last_message=fields.get("last_message", "Spawned coder-auth for #12."),
                permission_mode=fields.get("permission_mode", "auto"),
            )
        assert decision is None
        return self.session().cursor

    def session(self) -> TeamSession:
        with store_session() as store:
            session = store.get_session(self.sid)
        assert session is not None
        return session

    def refusal(self) -> str | None:
        with store_session() as store:
            raw = store.get_meta(team_service.compact_refusal_key(self.sid))
        return None if raw is None else str(json.loads(raw)["reason"])

    def compacts_on_submit(self) -> None:
        """Claude Code's ``/compact``: no turn, its SessionStart of source ``compact``."""

        def submitted(pane_id: str) -> None:
            if self.tmux.submitted and self.tmux.submitted[-1].startswith("/compact"):
                self.start("compact")

        self.tmux.on_submit = submitted


@pytest.fixture
def manager(
    project: ProjectInfo, tmux: ClaudeCode, claude_on_path: Path, tmp_path: Path
) -> Manager:
    _quiet_compact_at(AT)
    return Manager(project, tmux, tmp_path)


def _token(manager: Manager) -> str:
    """The arm token of the manager's latest Stop."""
    with store_session() as store:
        token = store.get_meta(team_service.compact_arm_key(manager.sid))
    assert token is not None
    return token


def _check(manager: Manager, cursor: int, token: str | None = None) -> str:
    """The worker its latest Stop armed (``token``: an earlier Stop's, when given)."""
    return quiet_compact.check(manager.sid, cursor, token or _token(manager))


# --- the quiet point ---------------------------------------------------------------------


def test_a_quiet_manager_is_compacted_once(
    manager: Manager,
    armed: list[list[str]],
    slept: list[float],
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At 450k with the threshold at 400k, no wake event pending and a fresh state file, the
    Stop arms the worker, which types ``/compact`` once after the delay. The compaction's
    SessionStart leaves the row ``waiting``, and a coder's ``task review`` nudges it."""
    manager.compacts_on_submit()
    cursor = manager.stop(armed)

    assert armed == [
        selfcli.argv_for(
            [
                "--quiet",
                "hook",
                "compact-check",
                manager.sid,
                "--cursor",
                str(cursor),
                "--token",
                _token(manager),
            ]
        )
    ]
    assert _check(manager, cursor) == "compacted"

    assert slept[0] == load_config().fleet.quiet_compact_delay
    assert manager.tmux.submitted == [quiet_compact.COMPACT_LINE]
    assert manager.session().state == "waiting", "a quiet compaction leaves it waiting"
    with store_session() as store:
        mark = team_service.read_quiet_mark(store, manager.sid)
    assert mark is not None and mark.acked_at is not None

    # Once: the same quiet point again is inside the cooldown.
    assert _check(manager, cursor).startswith("it was compacted 0 min ago")
    assert manager.tmux.submitted == [quiet_compact.COMPACT_LINE]

    # And it is still a manager a review wakes: the real nudge types into its pane.
    monkeypatch.setattr(fleet_service, "nudge_manager", _REAL_NUDGE)
    assert fleet_service.nudge_manager(manager.project.id, reason="task_review")
    assert manager.tmux.typed[-1][2] == "Enter" and fleet_service.NUDGE_TEXT in str(
        manager.tmux.typed[-2]
    )


def test_a_compaction_claude_code_started_itself_keeps_the_turns_state(
    manager: Manager, armed: list[list[str]]
) -> None:
    """Control: a ``compact`` SessionStart with no quiet mark (auto-compaction, mid-turn)
    keeps what the turn said, as on P0."""
    with store_session() as store:
        store.touch_session(manager.sid, state="working")
    manager.start("compact")
    assert manager.session().state == "working"


def test_a_stale_mark_is_not_taken_for_a_later_compaction(manager: Manager) -> None:
    """A mark older than :data:`team.QUIET_COMPACT_ACK` that no compaction took is no claim on
    an auto-compaction an hour later."""
    with store_session() as store:
        store.touch_session(manager.sid, state="working")
        team_service.write_quiet_mark(
            store,
            manager.sid,
            team_service.QuietMark(
                datetime.now(tz=UTC) - team_service.QUIET_COMPACT_ACK - timedelta(seconds=1), None
            ),
        )
    manager.start("compact")
    assert manager.session().state == "working"


# --- what refuses it ---------------------------------------------------------------------


def test_off_by_default_nothing_is_armed(
    project: ProjectInfo,
    tmux: ClaudeCode,
    claude_on_path: Path,
    tmp_path: Path,
    armed: list[list[str]],
) -> None:
    assert load_config().fleet.quiet_compact_at == 0
    manager = Manager(project, tmux, tmp_path)
    manager.stop(armed)
    assert armed == [] and manager.refusal() is None


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"last_message": "Merge #351 now, or wait for the full matrix?"}, "on a question"),
        ({"permission_mode": "plan"}, "plan mode"),
    ],
)
def test_a_turn_that_asks_or_plans_is_not_armed(
    manager: Manager, armed: list[list[str]], fields: dict[str, str], reason: str
) -> None:
    manager.stop(armed, **fields)
    assert armed == []
    refused = manager.refusal()
    assert refused is not None and reason in refused


def test_a_manager_that_needs_the_human_is_not_armed(
    manager: Manager, armed: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``attention`` comes from a Notification after the Stop; the Stop leaves it as it is
    only for a ``limited`` row, so the arm is asked with the row read in that state."""
    original = team_service.hook_stop

    def stop_then_attention(*args: object, **kwargs: object) -> object:
        decision = original(*args, **kwargs)  # type: ignore[arg-type]
        with store_session() as store:
            store.touch_session(manager.sid, state="attention")
        return decision

    monkeypatch.setattr(team_service, "hook_stop", stop_then_attention)
    manager.stop(armed)
    assert armed == [] and manager.refusal() == "it needs the human"


def _pending_task_done(manager: Manager) -> None:
    task, _ = team_service.add_task("wire auth", cwd=manager.project.root)
    coder = "cccc3333-0000-0000-0000-000000000000"
    with store_session() as store:
        now = datetime.now(tz=UTC)
        store.upsert_session(
            TeamSession(
                id=coder,
                project_id=manager.project.id,
                role="coder",
                started_at=now,
                last_seen_at=now,
            )
        )
    team_service.claim_task(task.id, session_ref=coder)
    team_service.finish_task(task.id, session_ref=coder, cwd=manager.project.root)


Setup = Callable[[Manager], None]


def _working(m: Manager) -> None:
    with store_session() as store:
        store.touch_session(m.sid, state="working")


def _small(m: Manager) -> None:
    _transcript(m.transcript, 120_000)


def _stale_state(m: Manager) -> None:
    m.write_state(ago=timedelta(minutes=25))


def _no_state(m: Manager) -> None:
    m.state.unlink()


def _draft(m: Manager) -> None:
    m.tmux.screens[m.agent.pane_id] = _box("also check the CI on #3")


def _busy(m: Manager) -> None:
    m.tmux.screens[m.agent.pane_id] = ["✻ Thinking… (esc to interrupt)", *_box()]


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (_pending_task_done, "a task_done is pending"),
        (_working, "it reads working, not waiting"),
        (_small, "its context is 120,000 tokens, under 400,000"),
        (_stale_state, "its state file is 25 min old"),
        (_no_state, "it has no state file at"),
        (_draft, "a draft is in its input box"),
        (_busy, "it is not idle at its prompt"),
    ],
    ids=["pending-wake", "working", "small-context", "stale-state", "no-state", "draft", "busy"],
)
def test_each_condition_refuses_and_says_why(
    manager: Manager,
    armed: list[list[str]],
    slept: list[float],
    clock: FakeClock,
    setup: Setup,
    reason: str,
) -> None:
    """The refusal matrix. Each condition alone keeps ``/compact`` out of the pane, and its
    reason is kept in ``team_meta``; the control (nothing set up) is the test above."""
    cursor = manager.stop(armed)
    setup(manager)

    outcome = _check(manager, cursor)

    assert manager.tmux.submitted == [] and manager.tmux.typed == []
    assert outcome.startswith(reason), outcome
    assert manager.refusal() == outcome


def test_a_cursor_that_moved_since_the_stop_refuses(
    manager: Manager, armed: list[list[str]], slept: list[float], clock: FakeClock
) -> None:
    cursor = manager.stop(armed)
    assert _check(manager, cursor - 1) == "it has read the board since its turn ended"


def test_a_state_file_older_than_the_last_wake_refuses(
    manager: Manager, armed: list[list[str]], slept: list[float], clock: FakeClock
) -> None:
    """The manager was handed a decision (its cursor past it) and did not write its state
    since: the summary would lose what it decided."""
    manager.write_state(ago=timedelta(minutes=5))
    _pending_task_done(manager)
    with store_session() as store:
        latest = store.latest_seq(manager.project.id)
        store.touch_session(manager.sid, cursor=latest)
    cursor = manager.stop(armed)
    assert cursor == latest
    assert _check(manager, cursor) == "its state file predates the last decision it was woken for"


def test_no_compaction_after_the_line_is_one_human_note_and_no_retry(
    manager: Manager, armed: list[list[str]], slept: list[float], clock: FakeClock
) -> None:
    """``/compact`` was typed and nothing compacted: one ``notice`` (human-only), the cooldown
    holds the next try."""
    cursor = manager.stop(armed)
    assert _check(manager, cursor) == "no compaction followed /compact"
    with store_session() as store:
        notices = [
            e for e in store.recent_events(manager.project.id, limit=20) if e.kind == "notice"
        ]
    assert len(notices) == 1 and "no compaction started" in notices[0].text
    assert _check(manager, cursor).startswith("it was compacted")
    assert manager.tmux.submitted == [quiet_compact.COMPACT_LINE]


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"last_assistant_message": "Shall I merge #351?"}, "its turn ended on a question"),
        ({"permission_mode": "plan"}, "it is in plan mode"),
    ],
)
def test_the_stop_hook_hands_the_arm_its_last_message_and_mode(
    manager: Manager,
    armed: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, str],
    reason: str,
) -> None:
    """Through ``aisquare hook stop`` as Claude Code calls it: the payload's
    ``last_assistant_message`` and ``permission_mode`` reach the arm, which refuses."""
    manager.env(monkeypatch)
    stdin = json.dumps({"cwd": str(manager.project.root), "session_id": manager.sid, **payload})
    result = CliRunner().invoke(app, ["hook", "stop"], input=stdin)
    assert result.exit_code == 0 and result.stdout == ""
    assert armed == [] and manager.refusal() == reason

    control = json.dumps({"cwd": str(manager.project.root), "session_id": manager.sid})
    assert CliRunner().invoke(app, ["hook", "stop"], input=control).exit_code == 0
    assert len(armed) == 1, "the same Stop without them arms the worker"


def test_a_later_turn_that_asks_supersedes_the_worker_an_earlier_one_armed(
    manager: Manager, armed: list[list[str]], slept: list[float], clock: FakeClock
) -> None:
    """Review of #361, major 1: turn 1 ends quietly and arms; within the delay the human asks
    a follow-up, and turn 2 ends on a question, with nothing new on the board (the cursor
    stays). Turn 2's own arm refuses, and turn 1's worker, woken later, must not type
    ``/compact`` after the question: the latest Stop decides."""
    manager.compacts_on_submit()
    cursor = manager.stop(armed)
    first = _token(manager)
    second = manager.stop(armed, last_message="Shall I merge #12 now?")
    assert second == cursor and manager.refusal() == "its turn ended on a question"

    assert _check(manager, cursor, first) == "a later turn ended since it was armed"
    assert manager.tmux.submitted == [] and manager.tmux.typed == []
    assert manager.refusal() == "its turn ended on a question", "the latest turn's reason stays"

    # The control: a third, quiet turn arms again, and its worker compacts.
    third = manager.stop(armed)
    assert _check(manager, third) == "compacted"
