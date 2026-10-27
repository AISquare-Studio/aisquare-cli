"""SCOPE #113 091, item 1: ``[accounts] before_limit`` moves a Codex agent before its limit.

A fleet agent's turn end is a safe point only while its turn left nothing running in the
agent's own process and nothing began since; both are read off its transcript by its own
agent's reader (``AgentTranscript.background_work``, ``activity_since``), and the move weighs
its own agent's accounts alone. Here a real Codex fleet row on the fleet's fake tmux, its
Stop through ``aisquare hook codex``, the worker the hook starts, and the real ``fleet
switch``. Codex's records are its own, measured on 0.164.0
(``tests/fixtures/agent_transcripts/codex-background-0.164.0.jsonl``: an exec answered
``Process running with session ID 48812`` and the turn over, then ``/stop``; another that
ends by itself, then a ``write_stdin`` poll answered ``Process exited``). Claude Code's gate
is ``tests/test_before_limit.py``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.core import paths, selfcli
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.agent_adapters.codex_transcript import CodexTranscript
from aisquare.core.config import AccountsSettings, AppConfig, load_config, save_config
from aisquare.core.store import store_session
from aisquare.models import ClaudeAccount, FleetAgent, ProjectInfo, TeamSession
from aisquare.services import agent_events
from aisquare.services import claude_accounts as service
from aisquare.services import fleet as fleet_service
from aisquare.services import hooks as hooks_service
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_codex_accounts import _sign_in, _token_count, _window
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    clock,
    project,
    repo,
    tmux,
)
from tests.test_usage_aware_accounts import _payload, _slot, _Usage

ROLLOUT = (
    Path(__file__).parent / "fixtures" / "agent_transcripts" / ("codex-background-0.164.0.jsonl")
)
LINES = ROLLOUT.read_text(encoding="utf-8").splitlines()
THREAD = "01a17f29-43c3-7d10-972f-ef89d6556e05"
"""The recorded session's thread id: its rollout's name and the ``session_id`` its hooks
carry."""
FIRST_TURN_OVER = 17
"""Lines written when its first turn's Stop hook ran: up to its ``task_complete``
(measured: only that follows the hook), the exec ``48812`` still running."""
STOPPED = 19
"""Lines once ``/stop`` ended it at the idle prompt (its ``CommandExecution`` completed)."""
NEXT_TURN = 21
"""Lines once the next prompt's turn started (``task_started``)."""


@pytest.fixture(autouse=True)
def unhurried(monkeypatch: pytest.MonkeyPatch) -> None:
    """The move's settle is not slept, and the pane reads idle at its prompt (what a capture
    reads is ``tests/test_before_limit.py``'s)."""
    monkeypatch.setattr(hooks_service, "_sleep", lambda seconds: None)
    monkeypatch.setattr(hooks_service, "_pane_shows", lambda agent: "idle")


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every worker a hook starts, recorded; the test runs it (``_Row.work``)."""
    workers: list[list[str]] = []
    monkeypatch.setattr(hooks_service, "_detach", lambda argv: workers.append(list(argv)))
    return workers


def _recorded_at(line: str) -> datetime:
    return datetime.fromisoformat(json.loads(line)["timestamp"].replace("Z", "+00:00"))


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _settings(**overrides: Any) -> None:
    config: AppConfig = load_config()
    config.accounts = AccountsSettings(**overrides)
    save_config(config)


def _codex_slot(email: str, percent: float) -> ClaudeAccount:
    """A signed-in Codex slot whose sessions last read its five-hour window at ``percent``."""
    now = datetime.now(tz=UTC)
    account = core.create_account("codex")
    _sign_in(account.config_dir, email)
    reading = account.config_dir / "sessions" / "2026" / "10" / "26"
    reading.mkdir(parents=True)
    (reading / "rollout-2026-10-26T09-00-00-reading.jsonl").write_text(
        json.dumps(_token_count(now, _window(percent, 300, now + timedelta(hours=3)))) + "\n",
        encoding="utf-8",
    )
    return account


def _on_record() -> None:
    """Every account's reading on record, as the Accounts page or a launch leaves them."""
    service.cached_usage(service.list_accounts())


class _Row:
    """A Codex coder of project ``of`` on slot ``on``, bound to the recorded session, its rollout
    written as it stood at its first turn's Stop hook. Spawned while ``on`` is its agent's one
    account, so the spawn's own pick put it there: a row put on its account by name
    (``--account``) is one ``before_limit`` leaves alone."""

    def __init__(self, of: ProjectInfo, on: ClaudeAccount) -> None:
        spawned = fleet_service.spawn(of, "coder", agent="codex", worktree=False).agent
        assert spawned.agent == "codex" and spawned.account_slot == on.slot
        assert spawned.launch_spec is not None and not spawned.launch_spec.account_pinned
        self.project, self.home, self.label = of, on.config_dir, spawned.label
        self.turns = 0
        self.rollout = (
            on.config_dir
            / "sessions"
            / "2026"
            / "10"
            / "27"
            / (f"rollout-2026-10-27T16-59-33-{THREAD}.jsonl")
        )
        self.rollout.parent.mkdir(parents=True, exist_ok=True)
        self.session = agent_events.session_key(get_adapter("codex"), on.config_dir, THREAD)
        now = datetime.now(tz=UTC)
        with store_session() as store:
            store.upsert_session(
                TeamSession(
                    id=self.session,
                    project_id=of.id,
                    role="coder",
                    started_at=now,
                    last_seen_at=now,
                    transcript_path=str(self.rollout),
                    agent="codex",
                    native_session_id=THREAD,
                    account=str(on.config_dir),
                )
            )
            stored = store.get_fleet_agent(spawned.id)
            assert stored is not None
            store.upsert_fleet_agent(stored.model_copy(update={"session_id": self.session}))
        # Started an hour ago, past the cool-down a spawn starts (``HANDOVER_COOLDOWN``),
        # and its session a second after that: the row's process, whose work is its own.
        self.began = datetime.now(tz=UTC) - timedelta(hours=1) + timedelta(seconds=1)
        connection = sqlite3.connect(str(paths.db_path()))
        try:
            connection.execute(
                "UPDATE fleet_agent SET created_at = ? WHERE id = ?",
                ((self.began - timedelta(seconds=1)).isoformat(), spawned.id),
            )
            connection.commit()
        finally:
            connection.close()
        self.write(FIRST_TURN_OVER)
        self.id = spawned.id

    def write(self, lines: int) -> None:
        """The rollout as its first ``lines`` recorded lines stood, its times moved to a
        session begun a second after the spawn (the row's process: what it started since is
        its own)."""
        shift = self.began - _recorded_at(LINES[0])
        moved = []
        for line in LINES[:lines]:
            record = json.loads(line)
            record["timestamp"] = _stamp(_recorded_at(line) + shift)
            moved.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        self.rollout.write_text("".join(line + "\n" for line in moved), encoding="utf-8")

    def stop(self, runner: CliRunner) -> None:
        """A turn's Stop, as Codex's hook hands it over: each its own turn's (one redelivered
        is answered as the first was, and not handled again)."""
        self.turns += 1
        payload = {
            "session_id": THREAD,
            "cwd": str(self.project.root),
            "transcript_path": str(self.rollout),
            "hook_event_name": "Stop",
            "turn_id": f"turn-{self.turns}",
            "stop_hook_active": False,
            "last_assistant_message": "turn over",
        }
        done = runner.invoke(
            app, ["hook", "codex", "--config-dir", str(self.home)], input=json.dumps(payload)
        )
        assert done.exit_code == 0, done.output

    def work(self, runner: CliRunner) -> None:
        """The worker its Stop started (``aisquare hook before-limit``)."""
        done = runner.invoke(app, ["hook", "before-limit", self.session])
        assert done.exit_code == 0 and done.stdout == "", done.output

    def live(self) -> FleetAgent:
        """The live fleet row under its label: the replacement, once it is moved."""
        with store_session() as store:
            live = store.fleet_agents(self.project.id, live_only=True)
        [row] = [agent for agent in live if agent.label == self.label]
        return row

    def events(self) -> list[tuple[str, str]]:
        with store_session() as store:
            return [(e.kind, e.text) for e in store.recent_events(self.project.id, limit=50)]


def _worker(row: _Row) -> list[str]:
    return [*selfcli.argv_for([]), "--quiet", "hook", "before-limit", row.session]


# --- the move -------------------------------------------------------------------------------


def test_a_codex_row_over_the_line_at_its_turn_end_moves_to_a_codex_slot_under_it(
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    started: list[list[str]],
) -> None:
    """The SCOPE's acceptance: a Codex coder at its turn end, its account over ``switch_at``
    and its exec ended (``/stop``), moves to the Codex slot under the line — never the Claude
    slot between them on the ladder — and the board's ``switched`` line says ``(before
    limit)``. Before 091's lane the gate read Claude Code's transcript alone, and a Codex row
    was never moved early."""
    _settings(before_limit="switch")
    full = _codex_slot("full@example.com", 92)
    row = _Row(project, full)
    core.create_account()  # a Claude slot on the ladder: never a Codex agent's target
    roomy = _codex_slot("roomy@example.com", 15)
    _on_record()
    row.write(STOPPED)
    runner = CliRunner()

    row.stop(runner)
    assert started == [_worker(row)]
    row.work(runner)

    moved = row.live()
    assert moved.id != row.id and moved.agent == "codex" and moved.account_slot == roomy.slot
    switched = [text for kind, text in row.events() if kind == "switched"]
    assert switched and "(before limit)" in switched[0], row.events()


def test_an_exec_still_open_in_the_rollout_is_no_safe_point(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    started: list[list[str]],
) -> None:
    """The other half: its turn ended with an exec still running (``Process running with
    session ID 48812``, no ``CommandExecution`` completed yet). The ``/exit`` of a move kills
    it, so the turn end looks no further — no worker, no cool-down spent — and the row stays
    on its account; once ``/stop`` ended it, the next turn end looks."""
    _settings(before_limit="switch")
    full = _codex_slot("full@example.com", 92)
    row = _Row(project, full)
    _codex_slot("roomy@example.com", 15)
    _on_record()
    runner = CliRunner()

    row.stop(runner)
    assert started == []
    assert row.live().id == row.id and row.live().account_slot == full.slot

    row.write(STOPPED)
    row.stop(runner)
    assert started == [_worker(row)]


def test_a_turn_begun_since_the_turn_end_is_left_alone(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    started: list[list[str]],
) -> None:
    """The move's own checks, around the stop, read Codex's rollout: a prompt that started a
    turn (``task_started``) after the turn end the hook looked at leaves the row where it is,
    and the next turn end asks again."""
    _settings(before_limit="switch")
    full = _codex_slot("full@example.com", 92)
    row = _Row(project, full)
    _codex_slot("roomy@example.com", 15)
    _on_record()
    row.write(STOPPED)
    runner = CliRunner()
    row.stop(runner)
    assert started == [_worker(row)]

    row.write(NEXT_TURN)  # the next prompt, between the hook's look and the worker's move
    row.work(runner)

    assert row.live().id == row.id and row.live().account_slot == full.slot
    assert "switched" not in [kind for kind, _ in row.events()]


def test_a_role_whose_binding_sets_codex_home_by_hand_stays_on_it(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    started: list[list[str]],
) -> None:
    """A binding that writes the role's ``CODEX_HOME`` itself is an account put by hand
    (``account_pin``, asked of the row's own agent's home variable): ``before_limit`` leaves
    the row there, as it leaves a hand-written ``CLAUDE_CONFIG_DIR``'s."""
    from aisquare.services import settings as settings_service

    _settings(before_limit="switch")
    full = _codex_slot("full@example.com", 92)
    row = _Row(project, full)
    _codex_slot("roomy@example.com", 15)
    settings_service.bind_role("coder", env={"CODEX_HOME": str(full.config_dir)})
    row.write(STOPPED)

    row.stop(CliRunner())

    assert started == []


def test_a_codex_row_under_the_line_is_looked_at_once_a_cool_down(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    started: list[list[str]],
) -> None:
    """A Codex account's reading is read off its home by the worker, never kept in the cache
    the hook decides from (``AgentAccounts.network_usage`` off): the hook cannot tell, and
    starts the worker; the worker finds it under the line and keeps the cool-down, so the
    next turn ends start none, where giving it back started a worker at every one."""
    _settings(before_limit="switch")
    home = _codex_slot("home@example.com", 40)
    row = _Row(project, home)
    _codex_slot("roomy@example.com", 15)
    row.write(STOPPED)
    runner = CliRunner()

    row.stop(runner)
    assert started == [_worker(row)]
    row.work(runner)
    row.stop(runner)
    row.stop(runner)

    assert started == [_worker(row)]
    assert row.live().id == row.id and row.live().account_slot == home.slot


def test_only_its_own_agents_accounts_are_room_on_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Claude account under the line on record is no room for a Codex agent, which a
    hand-over moves among its own agent's accounts (``room_on_record(agent=)``): a Codex
    agent with no other Codex account has none, where a Claude Code one has that one."""
    full = _codex_slot("full@example.com", 92)
    _slot("claude@example.com", "tok-claude")
    monkeypatch.setattr(service, "_http_get", _Usage({"tok-claude": _payload(5)}))
    _on_record()

    assert not service.room_on_record(full.slot, switch_at=85, agent="codex")
    assert service.room_on_record(full.slot, switch_at=85, agent="claude-code")


def test_warn_names_the_codex_slot_with_room_and_moves_nothing(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    started: list[list[str]],
) -> None:
    """``before_limit = "warn"``: one ``before_limit`` line, naming the Codex account with
    room and the ``fleet switch`` that moves it; the row stays where it is."""
    _settings(before_limit="warn")
    full = _codex_slot("full@example.com", 92)
    row = _Row(project, full)
    roomy = _codex_slot("roomy@example.com", 15)
    _on_record()
    row.write(STOPPED)
    runner = CliRunner()

    row.stop(runner)
    row.work(runner)

    [line] = [text for kind, text in row.events() if kind == "before_limit"]
    assert core.label(roomy) in line and "fleet switch" in line, line
    assert row.live().id == row.id and row.live().account_slot == full.slot


# --- the reader: what a rollout says is still running ---------------------------------------


def _scan(tmp_path: Path, lines: list[str]) -> list[str]:
    file = tmp_path / "rollout.jsonl"
    file.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return CodexTranscript().background_work(file, None)


def test_a_write_stdin_answered_process_exited_ends_its_session(tmp_path: Path) -> None:
    """The second exec (session 51145) with its ``CommandExecution`` left out: it runs on
    through the poll's call, and the poll's ``Process exited with code 0`` ends it."""
    without_its_end = [*LINES[:33], *LINES[34:]]
    poll_called = next(i for i, line in enumerate(without_its_end) if '"write_stdin"' in line)
    assert _scan(tmp_path, without_its_end[: poll_called + 1]) == ["51145"]
    assert _scan(tmp_path, without_its_end) == []


def test_only_a_results_header_says_a_process_runs(tmp_path: Path) -> None:
    """The words in a command's own output (below ``Output:``) are not its process running:
    a ``cat`` of a rollout is no background terminal."""
    record = json.loads(LINES[11])
    record["payload"]["call_id"] = "call_cat"
    record["payload"]["output"] = (
        "Chunk ID: 1\nWall time: 0.1 seconds\nProcess exited with code 0\nOutput:\n"
        "Process running with session ID 7\n"
    )
    assert _scan(tmp_path, [*LINES[:11], json.dumps(record)]) == []
    assert _scan(tmp_path, LINES[:12]) == ["48812"]


def test_an_end_too_long_to_parse_is_read_off_its_head(tmp_path: Path) -> None:
    """A ``CommandExecution`` whose output makes its line too long to parse still ends its
    session: its ``process_id`` comes before the output."""
    record = json.loads(LINES[18])
    assert record["payload"]["item"]["type"] == "CommandExecution"
    record["payload"]["item"]["aggregated_output"] = "x" * 400_000
    assert _scan(tmp_path, [*LINES[:18], json.dumps(record, separators=(",", ":"))]) == []


def test_a_record_begun_before_the_turn_end_is_not_a_turn_begun_since(tmp_path: Path) -> None:
    """The turn end's length can fall inside a line Codex was still writing (a record of the
    turn that ended): that line began before it and is no turn begun since, though the rest
    of it, read from there, names a ``response_item``; a turn opened after it is one."""
    file = tmp_path / "rollout.jsonl"
    file.write_text("".join(line + "\n" for line in LINES[:STOPPED]), encoding="utf-8")
    last_response = max(i for i, line in enumerate(LINES[:STOPPED]) if '"response_item"' in line)
    begun = len("".join(line + "\n" for line in LINES[:last_response]).encode())
    inside = begun + 2  # in its first bytes: what follows still names it a response_item
    assert '"response_item"' in LINES[last_response][2:]
    reader = CodexTranscript()
    assert not reader.activity_since(file, inside)
    assert reader.activity_since(file, begun)
    file.write_text("".join(line + "\n" for line in LINES[:NEXT_TURN]), encoding="utf-8")
    assert reader.activity_since(file, inside)
