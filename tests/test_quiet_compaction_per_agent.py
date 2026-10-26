"""P1 WS-4's quiet-point compaction, per agent (#113's #17): the command, the acknowledgement
and the start pack that comes back.

Measured on Codex 0.164.0 (a scratch install, a loopback provider, temp homes; the live run
is ``tests/test_codex_native.py::test_real_codex_manager_is_compacted_at_a_quiet_point``):

- a bare ``/compact`` typed at its idle prompt compacts the session and writes a ``compacted``
  record to its rollout; ``/compact <text>`` is run as a prompt, a turn and no compaction;
- no hook fires at the compaction. ``SessionStart`` with source ``compact`` fires with the NEXT
  turn, ahead of its ``UserPromptSubmit``, and what a hook hands back rides that turn's
  request: the start pack comes back there (the request after the compaction holds it, and the
  compacted record's own history does not).

So the worker types Codex's bare command (``capabilities.compact``), weighs the context off
its rollout (``AgentTranscript.context_tokens``) and takes the rollout's ``compacted`` record
for the acknowledgement Claude Code's ``SessionStart`` gives (``AgentTranscript.
compacted_since``), and ``RESUME_LINE`` is true for both: the pack is in front of it. The
synthetic agent that declares a command is compacted through its own reader; the one that
declares none is refused (``tests/test_third_agent_seams.py``).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import paths
from aisquare.core.agent_adapters import register, unregister
from aisquare.core.config import load_config, save_config
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo, TeamSession
from aisquare.services import delivery, hooks, quiet_compact
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from tests import test_fleet_service as fleet_suite
from tests.fake_agent import FakeAgentAdapter
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_codex_fleet_parity import THREAD_M, Codex
from tests.test_fleet_service import FakeClock, FakeTmux

repo = fleet_suite.repo
project = fleet_suite.project
claude_on_path = fleet_suite.claude_on_path
clock = fleet_suite.clock

AT = 400_000


def _usage(input_tokens: int) -> str:
    usage = {"input_tokens": input_tokens, "cached_input_tokens": input_tokens - 10}
    stamp = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    record = {"timestamp": stamp, "type": "token_usage_record", "payload": {"usage": usage}}
    return json.dumps(record) + "\n"


def _compacted(after: timedelta = timedelta(seconds=1)) -> str:
    stamp = (datetime.now(tz=UTC) + after).isoformat().replace("+00:00", "Z")
    return json.dumps({"timestamp": stamp, "type": "compacted", "payload": {"message": "s"}}) + "\n"


class QuietManager:
    """A fleet manager's row (Codex's, unless ``agent`` says otherwise), its waiting session
    and its transcript, its state file fresh, its context over the line, and the worker
    armed."""

    def __init__(
        self, project: ProjectInfo, tmp_path: Path, agent: str = "codex", binary: str = "codex"
    ) -> None:
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
                    agent=agent,
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
                    binary=binary,
                    agent=agent,
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
        config.fleet.quiet_compact_at = AT
        save_config(config)


@pytest.fixture
def manager(project: ProjectInfo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(quiet_compact, "_sleep", lambda seconds: None)
    monkeypatch.setattr(fleet_service, "quiet_prompt_refusal", lambda agent: None)
    return QuietManager(project, tmp_path)


def _typing(monkeypatch: pytest.MonkeyPatch, manager: QuietManager, *, compacts: bool) -> list[str]:
    """``type_line`` as the pane takes it: the line recorded, and Codex's ``compacted``
    record appended to its rollout when it compacts."""
    typed: list[str] = []

    def type_line(agent: FleetAgent, text: str, *, acked: Any) -> delivery.Delivered:
        typed.append(text)
        if compacts:
            with manager.rollout.open("a", encoding="utf-8") as handle:
                handle.write(_compacted())
        return delivery.Delivered(delivery.TellResult("yes", "typed"), None, 1)

    monkeypatch.setattr(fleet_service, "type_line", type_line)
    return typed


def test_a_codex_manager_is_sent_the_bare_command_and_its_rollout_acknowledges_it(
    manager: QuietManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    typed = _typing(monkeypatch, manager, compacts=True)

    assert quiet_compact.check(manager.sid, manager.cursor, manager.token) == "compacted"

    assert typed == ["/compact"], "never `/compact <text>`, which Codex runs as a prompt"
    with store_session() as store:
        mark = team_service.read_quiet_mark(store, manager.sid)
    assert mark is not None and mark.acked_at is not None, "acknowledged off the rollout"


def test_no_compacted_record_is_no_compaction_and_the_board_says_so(
    manager: QuietManager, monkeypatch: pytest.MonkeyPatch, project: ProjectInfo
) -> None:
    typed = _typing(monkeypatch, manager, compacts=False)

    said = quiet_compact.check(manager.sid, manager.cursor, manager.token)

    assert typed == ["/compact"] and "no compaction followed" in said
    with store_session() as store:
        texts = [event.text for event in store.recent_events(project.id, limit=5)]
    assert any("no compaction started" in text for text in texts), texts


def test_a_compaction_older_than_the_line_is_no_acknowledgement(
    manager: QuietManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rollout's tail holds an earlier compaction (the manager's own auto one): only a
    record written since the line was typed answers it."""
    with manager.rollout.open("a", encoding="utf-8") as handle:
        handle.write(_compacted(timedelta(minutes=-30)))
    typed = _typing(monkeypatch, manager, compacts=False)

    said = quiet_compact.check(manager.sid, manager.cursor, manager.token)

    assert typed == ["/compact"] and "no compaction followed" in said


def test_a_codex_context_under_the_line_is_not_compacted(
    manager: QuietManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager.rollout.write_text(_usage(450_000) + _usage(120_000), encoding="utf-8")
    typed = _typing(monkeypatch, manager, compacts=True)

    said = quiet_compact.check(manager.sid, manager.cursor, manager.token)

    assert typed == [] and "120,000 tokens" in said, "its rollout's newest request decides"


# --- the start pack comes back with the next turn -----------------------------------------------


class CodexPane(FakeTmux):
    """A FakeTmux whose pane runs Codex: a pasted line and its Enter are a prompt, which
    ``on_prompt`` answers as the real one does (``/compact`` writes its record; the next turn
    fires ``SessionStart`` with source ``compact``, then ``UserPromptSubmit``)."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []
        self.on_prompt: Callable[[str], None] | None = None
        self._draft: dict[str, str] = {}

    def paste(self, pane_id: str, text: str) -> None:
        super().paste(pane_id, text)
        self._draft[pane_id] = text

    def send_keys(self, pane_id: str, *keys: str) -> None:
        super().send_keys(pane_id, *keys)
        text = self._draft.pop(pane_id, None) if keys == ("Enter",) else None
        if text is not None:
            self.prompts.append(text)
            if self.on_prompt is not None:
                self.on_prompt(text)


class LiveQuietManager:
    """A Codex manager in its pane, its hooks fired through Codex's own decoder: it
    compacts when ``/compact`` is typed, and the start pack its next turn's ``SessionStart``
    hands back is kept in :attr:`packs`."""

    def __init__(
        self,
        project: ProjectInfo,
        pane: CodexPane,
        home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.project, self.pane = project, pane
        self.agent: FleetAgent = fleet_service.spawn(
            project, "manager", agent="codex", worktree=False
        ).agent
        self.codex = Codex(self.agent, project, home, pane, monkeypatch)
        monkeypatch.setenv("AISQUARE_ROLE", "manager")
        pane.set_command(self.agent.pane_id, "codex")
        self.sid = self.codex.board_id(THREAD_M)
        self.rollout = home / "sessions" / f"rollout-{THREAD_M}.jsonl"
        self.rollout.parent.mkdir(parents=True, exist_ok=True)
        self.rollout.write_text(_usage(450_000), encoding="utf-8")
        self.packs: list[str] = []
        self.compacted = False
        pane.on_prompt = self.takes
        state = paths.manager_state_file(project.id)
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text("goal: ship WS-4\nnext: review #16\n", encoding="utf-8")
        self.opening = self.codex.start(THREAD_M)

    def takes(self, line: str) -> None:
        """What Codex does with a typed line: ``/compact`` compacts (a record, no hook);
        anything else is a turn, which first fires the start the compaction owes."""
        if line == "/compact":
            with self.rollout.open("a", encoding="utf-8") as handle:
                handle.write(_compacted(timedelta(milliseconds=1)))
            self.compacted = True
            return
        if self.compacted:
            self.packs.append(self.codex.start(THREAD_M, source="compact"))
            self.compacted = False
        self.codex.hook("UserPromptSubmit", THREAD_M, turn_id="turn-2", prompt=line)

    def stop(self) -> None:
        self.codex.hook("Stop", THREAD_M, turn_id="turn-1", last_assistant_message="Done.")

    def session(self) -> TeamSession:
        with store_session() as store:
            found = store.get_session(self.sid)
        assert found is not None
        return found


@pytest.fixture
def codex_pane(monkeypatch: pytest.MonkeyPatch) -> CodexPane:
    pane = CodexPane()
    monkeypatch.setattr(fleet_service, "server", lambda config=None: pane)
    return pane


@pytest.fixture
def live(
    project: ProjectInfo,
    codex_pane: CodexPane,
    codex_on_path: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> LiveQuietManager:
    config = load_config()
    config.fleet.quiet_compact_at = AT
    config.fleet.quiet_compact_resume = True
    save_config(config)
    monkeypatch.setattr(quiet_compact, "_sleep", lambda seconds: None)
    monkeypatch.setattr(hooks, "_detach", lambda argv: None)
    monkeypatch.setattr(fleet_service, "nudge_manager", lambda *a, **k: False)
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    return LiveQuietManager(project, codex_pane, home, monkeypatch)


def _check(live: LiveQuietManager, clock: FakeClock) -> str:
    live.stop()
    session = live.session()
    with store_session() as store:
        token = store.get_meta(team_service.compact_arm_key(live.sid))
    assert token is not None
    return quiet_compact.check(live.sid, session.cursor, token)


def test_a_compacted_codex_manager_gets_its_start_pack_with_the_line_typed_after(
    live: LiveQuietManager, clock: FakeClock
) -> None:
    """Codex fires no hook at ``/compact``, so the pack does not come then: it comes with the
    next turn, and the next turn is the resume line, so "the start pack above" is true."""
    assert "<aisquare-team>" in live.opening, "the start pack at its first start"

    assert _check(live, clock) == "compacted"

    assert live.pane.prompts == ["/compact", quiet_compact.RESUME_LINE]
    [pack] = live.packs
    assert "<aisquare-team>" in pack and "goal: ship WS-4" in pack, "its state file, in its pack"
    assert live.session().prompted_at is not None


def test_codexs_compact_start_comes_with_a_turn_so_it_neither_acknowledges_nor_rests_the_row(
    live: LiveQuietManager, clock: FakeClock
) -> None:
    """Claude Code's ``compact`` start comes at rest and puts the row back to ``waiting``;
    Codex's comes with a turn, which is working, and the mark is the rollout's to answer."""
    live.stop()
    with store_session() as store:
        store.touch_session(live.sid, state="working")
        team_service.write_quiet_mark(
            store, live.sid, team_service.QuietMark(datetime.now(tz=UTC), None)
        )

    live.codex.start(THREAD_M, source="compact")

    assert live.session().state == "working"
    with store_session() as store:
        mark = team_service.read_quiet_mark(store, live.sid)
    assert mark is not None and mark.acked_at is None


def test_a_codex_manager_is_still_given_its_pack_when_the_resume_line_is_off(
    live: LiveQuietManager, clock: FakeClock
) -> None:
    """Under the default (no resume line) the next prompt of any kind carries the pack."""
    config = load_config()
    config.fleet.quiet_compact_resume = False
    save_config(config)

    assert _check(live, clock) == "compacted"
    assert live.packs == [] and live.pane.prompts == ["/compact"]

    live.pane.paste(live.agent.pane_id, "the board has news")
    live.pane.send_keys(live.agent.pane_id, "Enter")

    assert len(live.packs) == 1 and "<aisquare-team>" in live.packs[0]


# --- the synthetic agent that declares a compaction command --------------------------------------


@pytest.fixture
def fake_registered() -> Iterator[FakeAgentAdapter]:
    adapter = FakeAgentAdapter()
    register(adapter)
    try:
        yield adapter
    finally:
        unregister(adapter.id)


def test_a_synthetic_agent_is_compacted_through_its_own_command_and_reader(
    fake_registered: FakeAgentAdapter,
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fake declares ``compact`` (``/shrink``, no instructions) and records a compaction
    as ``{"kind": "compacted"}``: its reader weighs the context (``billed`` records) and
    answers the mark, and nothing here names it."""
    manager = QuietManager(project, tmp_path, fake_registered.id, fake_registered.binary)
    manager.rollout.write_text(
        json.dumps({"kind": "billed", "model": "fake-1", "fresh": 5, "reused": 450_000, "out": 1})
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(quiet_compact, "_sleep", lambda seconds: None)
    monkeypatch.setattr(fleet_service, "quiet_prompt_refusal", lambda agent: None)
    typed: list[str] = []

    def type_line(agent: FleetAgent, text: str, *, acked: Any) -> delivery.Delivered:
        typed.append(text)
        stamp = (datetime.now(tz=UTC) + timedelta(seconds=1)).isoformat()
        record = {"kind": "compacted", "timestamp": stamp.replace("+00:00", "Z")}
        with manager.rollout.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        return delivery.Delivered(delivery.TellResult("yes", "typed"), None, 1)

    monkeypatch.setattr(fleet_service, "type_line", type_line)

    assert quiet_compact.check(manager.sid, manager.cursor, manager.token) == "compacted"

    assert typed == ["/shrink"]
